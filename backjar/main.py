import os
import json
import asyncio
from datetime import datetime, timezone
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel
from pywebpush import webpush, WebPushException
import firebase_admin
from firebase_admin import credentials, firestore, messaging
from dotenv import load_dotenv

load_dotenv()

# ── Firebase ───────────────────────────────────────────────────────────────────
_firebase_initialized = False

def get_db():
    global _firebase_initialized
    if not _firebase_initialized:
        svc = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON")
        if not svc:
            raise RuntimeError("FIREBASE_SERVICE_ACCOUNT_JSON not set")
        cred = credentials.Certificate(json.loads(svc))
        firebase_admin.initialize_app(cred)
        _firebase_initialized = True
    return firestore.client()

# ── VAPID ──────────────────────────────────────────────────────────────────────
VAPID_PRIVATE_KEY = os.getenv("VAPID_PRIVATE_KEY", "")
VAPID_PUBLIC_KEY  = os.getenv("VAPID_PUBLIC_KEY", "")
VAPID_CLAIMS      = {"sub": f"mailto:{os.getenv('VAPID_EMAIL', 'admin@jarai.app')}"}

# ── In-memory scheduler ────────────────────────────────────────────────────────
# Maps reminder_id → asyncio.Task so we can cancel on delete
_scheduled: dict[str, asyncio.Task] = {}

async def _fire_at(reminder_id: str, remind_at: datetime, data: dict):
    """Sleep until remind_at, then fire the reminder — exact to the second."""
    now   = datetime.now(timezone.utc)
    delay = (remind_at - now).total_seconds()

    if delay > 0:
        print(f"[Scheduler] Waiting {delay:.0f}s for reminder {reminder_id}")
        await asyncio.sleep(delay)

    # Re-check Firestore — client timer may have already fired it
    try:
        doc = get_db().collection("reminders").document(reminder_id).get()
        if not doc.exists or doc.to_dict().get("sent"):
            print(f"[Scheduler] {reminder_id} already sent — skipping")
            _scheduled.pop(reminder_id, None)
            return
    except Exception as e:
        print(f"[Scheduler] Firestore check failed: {e}")

    print(f"[Scheduler] Firing {reminder_id}: {data.get('text')}")
    fire_reminder(reminder_id, data)
    _scheduled.pop(reminder_id, None)

def schedule_reminder(reminder_id: str, remind_at: datetime, data: dict):
    """Schedule a reminder in the asyncio event loop."""
    # Cancel any existing task for this id
    existing = _scheduled.get(reminder_id)
    if existing and not existing.done():
        existing.cancel()

    task = asyncio.ensure_future(_fire_at(reminder_id, remind_at, data))
    _scheduled[reminder_id] = task
    print(f"[Scheduler] Booked {reminder_id} for {remind_at.isoformat()}")

def cancel_scheduled(reminder_id: str):
    task = _scheduled.pop(reminder_id, None)
    if task and not task.done():
        task.cancel()
        print(f"[Scheduler] Cancelled {reminder_id}")

# ── Models ─────────────────────────────────────────────────────────────────────
class SubscriptionKeys(BaseModel):
    p256dh: str
    auth: str

class PushSubscription(BaseModel):
    endpoint: str
    keys: SubscriptionKeys

class ReminderCreate(BaseModel):
    text: str
    remind_at: str
    language: str
    subscription: PushSubscription
    fcm_token: str = ""

class ReminderOut(BaseModel):
    id: str
    text: str
    remind_at: str
    language: str
    sent: bool

# ── Push via FCM ───────────────────────────────────────────────────────────────
def send_fcm(fcm_token: str, title: str, body: str, reminder_id: str, lang: str):
    message = messaging.Message(
        notification=messaging.Notification(title=title, body=body),
        data={"id": reminder_id, "lang": lang, "body": body, "title": title},
        android=messaging.AndroidConfig(
            priority="high",
            notification=messaging.AndroidNotification(
                sound="default",
                priority="high",
                channel_id="reminders",
            ),
        ),
        apns=messaging.APNSConfig(
            payload=messaging.APNSPayload(
                aps=messaging.Aps(sound="default", badge=1),
            ),
        ),
        token=fcm_token,
    )
    resp = messaging.send(message)
    print(f"[FCM] ✅ sent: {resp}")

# ── Push via WebPush (desktop fallback) ────────────────────────────────────────
def send_webpush(endpoint: str, p256dh: str, auth: str, payload: dict):
    webpush(
        subscription_info={"endpoint": endpoint, "keys": {"p256dh": p256dh, "auth": auth}},
        data=json.dumps(payload),
        vapid_private_key=VAPID_PRIVATE_KEY,
        vapid_claims=VAPID_CLAIMS,
    )

# ── Core: fire one reminder ────────────────────────────────────────────────────
def fire_reminder(doc_id: str, data: dict):
    lang     = data.get("language", "en")
    text     = data.get("text", "")
    endpoint = data.get("endpoint", "")
    p256dh   = data.get("p256dh", "")
    auth     = data.get("auth", "")
    fcm_tok  = data.get("fcm_token", "")

    title_map = {"en": "⏰ Reminder", "hi": "⏰ याद दिलाना", "te": "⏰ రిమైండర్"}
    title     = title_map.get(lang, "⏰ Reminder")
    payload   = {"title": title, "body": text, "lang": lang, "id": doc_id}

    sent = False

    if fcm_tok:
        try:
            send_fcm(fcm_tok, title, text, doc_id, lang)
            sent = True
        except Exception as e:
            print(f"[FCM] ❌ {e}")

    if not sent and endpoint:
        try:
            send_webpush(endpoint, p256dh, auth, payload)
            sent = True
            print(f"[WebPush] ✅ sent")
        except WebPushException as e:
            print(f"[WebPush] ❌ {e}")

    try:
        get_db().collection("reminders").document(doc_id).update({"sent": True})
        print(f"[Firestore] Marked {doc_id} as sent")
    except Exception as e:
        print(f"[Firestore] ❌ {e}")

# ── On startup: reload all pending reminders into scheduler ───────────────────
def reload_pending_reminders():
    """
    Called once at startup. Reschedules any reminders that were pending
    when the server last restarted (e.g. after Render woke up).
    Also immediately fires any that are already overdue.
    """
    db  = get_db()
    now = datetime.now(timezone.utc)
    print(f"[Startup] Loading pending reminders at {now.isoformat()}")

    docs  = db.collection("reminders").where("sent", "==", False).stream()
    count = 0
    for doc in docs:
        data = doc.to_dict()
        try:
            remind_at = datetime.fromisoformat(data["remind_at"])
            if remind_at.tzinfo is None:
                remind_at = remind_at.replace(tzinfo=timezone.utc)

            if remind_at <= now:
                # Already overdue — fire immediately
                print(f"[Startup] Overdue, firing now: {doc.id}")
                fire_reminder(doc.id, data)
            else:
                # Schedule for future
                schedule_reminder(doc.id, remind_at, data)
                count += 1
        except Exception as e:
            print(f"[Startup] Error for {doc.id}: {e}")

    print(f"[Startup] Scheduled {count} pending reminders")

# ── Lifespan ───────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        reload_pending_reminders()
    except Exception as e:
        print(f"[Startup] Failed: {e}")
    yield
    # Cancel all tasks on shutdown
    for task in _scheduled.values():
        task.cancel()

app = FastAPI(title="JarAI API", lifespan=lifespan)

# ── CORS ───────────────────────────────────────────────────────────────────────
@app.middleware("http")
async def cors(request: Request, call_next):
    if request.method == "OPTIONS":
        r = JSONResponse({}, status_code=200)
    else:
        r = await call_next(request)
    r.headers["Access-Control-Allow-Origin"]  = "*"
    r.headers["Access-Control-Allow-Methods"] = "GET,POST,PUT,DELETE,OPTIONS"
    r.headers["Access-Control-Allow-Headers"] = "Content-Type,Authorization"
    return r

# ── Routes ─────────────────────────────────────────────────────────────────────
@app.get("/")
def root():
    return {"status": "JarAI backend running ✅", "scheduled": len(_scheduled)}

@app.get("/ping")
def ping():
    """
    Legacy cron backup — still works as a last-resort safety net.
    But now the asyncio scheduler handles exact timing without needing this.
    """
    db  = get_db()
    now = datetime.now(timezone.utc)
    docs  = db.collection("reminders").where("sent", "==", False).stream()
    fired = 0
    for doc in docs:
        data = doc.to_dict()
        try:
            remind_at = datetime.fromisoformat(data["remind_at"])
            if remind_at.tzinfo is None:
                remind_at = remind_at.replace(tzinfo=timezone.utc)
            if remind_at <= now:
                fire_reminder(doc.id, data)
                fired += 1
        except Exception as e:
            print(f"[Ping] Error {doc.id}: {e}")
    return {"pong": True, "fired": fired, "scheduled": len(_scheduled)}

@app.get("/vapid-public-key")
def get_vapid():
    return {"publicKey": VAPID_PUBLIC_KEY}

# ── SSE keepalive — frontend connects to this to keep Render awake ─────────────
@app.get("/keepalive")
async def keepalive():
    """
    The frontend opens an EventSource to this endpoint.
    It sends a heartbeat every 25 seconds — Render's idle timeout is 15 min,
    so this keeps the server alive as long as any browser tab has JarAI open.
    """
    async def event_stream():
        count = 0
        while True:
            count += 1
            yield f"data: {json.dumps({'beat': count, 'scheduled': len(_scheduled)})}\n\n"
            await asyncio.sleep(25)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",   # disable nginx buffering on Render
        }
    )

@app.post("/reminders", response_model=ReminderOut)
async def create_reminder(body: ReminderCreate):
    db = get_db()
    try:
        remind_at = datetime.fromisoformat(body.remind_at)
        if remind_at.tzinfo is None:
            remind_at = remind_at.replace(tzinfo=timezone.utc)
    except ValueError:
        raise HTTPException(422, "Invalid remind_at — use ISO-8601")

    if remind_at <= datetime.now(timezone.utc):
        raise HTTPException(422, "remind_at must be in the future")

    ref = db.collection("reminders").document()
    data = {
        "text":       body.text,
        "remind_at":  body.remind_at,
        "language":   body.language,
        "sent":       False,
        "endpoint":   body.subscription.endpoint,
        "p256dh":     body.subscription.keys.p256dh,
        "auth":       body.subscription.keys.auth,
        "fcm_token":  body.fcm_token,
        "created_at": firestore.SERVER_TIMESTAMP,
    }
    ref.set(data)
    print(f"[API] Saved {ref.id} for {body.remind_at}")

    # Schedule exact-time firing in asyncio
    schedule_reminder(ref.id, remind_at, data)

    return ReminderOut(
        id=ref.id, text=body.text,
        remind_at=body.remind_at, language=body.language, sent=False
    )

@app.get("/reminders", response_model=list[ReminderOut])
def list_reminders():
    db = get_db()
    results = []
    for d in db.collection("reminders").order_by("remind_at").stream():
        data = d.to_dict()
        results.append(ReminderOut(
            id=d.id, text=data.get("text", ""),
            remind_at=data.get("remind_at", ""),
            language=data.get("language", "en"),
            sent=data.get("sent", False),
        ))
    return results

@app.delete("/reminders/{rid}")
def delete_reminder(rid: str):
    cancel_scheduled(rid)   # stop the asyncio timer too
    get_db().collection("reminders").document(rid).delete()
    return {"deleted": rid}

@app.post("/reminders/{rid}/mark-sent")
def mark_reminder_sent(rid: str):
    """Called by the client countdown timer — prevents double-firing."""
    cancel_scheduled(rid)   # server doesn't need to fire it anymore
    try:
        get_db().collection("reminders").document(rid).update({"sent": True})
        print(f"[API] Client marked {rid} as sent")
        return {"marked": rid}
    except Exception as e:
        raise HTTPException(500, str(e))