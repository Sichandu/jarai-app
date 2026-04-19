import os
import json
import asyncio
import httpx
from datetime import datetime, timezone
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, BackgroundTasks, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from pywebpush import webpush, WebPushException
import firebase_admin
from firebase_admin import credentials, firestore
from dotenv import load_dotenv
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.jobstores.memory import MemoryJobStore

load_dotenv()

# ── Firebase init ─────────────────────────────────────────────────────────────
_firebase_initialized = False

def get_db():
    global _firebase_initialized
    if not _firebase_initialized:
        service_account_json = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON")
        if not service_account_json:
            raise RuntimeError("FIREBASE_SERVICE_ACCOUNT_JSON env var not set")
        service_account_info = json.loads(service_account_json)
        cred = credentials.Certificate(service_account_info)
        firebase_admin.initialize_app(cred)
        _firebase_initialized = True
    return firestore.client()

# ── VAPID config ──────────────────────────────────────────────────────────────
VAPID_PRIVATE_KEY = os.getenv("VAPID_PRIVATE_KEY", "")
VAPID_PUBLIC_KEY  = os.getenv("VAPID_PUBLIC_KEY", "")
VAPID_CLAIMS      = {"sub": f"mailto:{os.getenv('VAPID_EMAIL', 'your@email.com')}"}
SELF_URL          = os.getenv("SELF_URL", "")   # your Render URL e.g. https://jarai-backend.onrender.com

# ── Scheduler ─────────────────────────────────────────────────────────────────
scheduler = AsyncIOScheduler(jobstores={"default": MemoryJobStore()})

# ── Pydantic models ───────────────────────────────────────────────────────────
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

class ReminderOut(BaseModel):
    id: str
    text: str
    remind_at: str
    language: str
    sent: bool

# ── Push helper ───────────────────────────────────────────────────────────────
def send_push(endpoint: str, p256dh: str, auth: str, payload: dict):
    sub_data = {
        "endpoint": endpoint,
        "keys": {"p256dh": p256dh, "auth": auth},
    }
    webpush(
        subscription_info=sub_data,
        data=json.dumps(payload),
        vapid_private_key=VAPID_PRIVATE_KEY,
        vapid_claims=VAPID_CLAIMS,
    )

# ── Job function (called by APScheduler at exact time) ────────────────────────
def fire_reminder(reminder_id: str, text: str, language: str,
                  endpoint: str, p256dh: str, auth: str):
    print(f"[Scheduler] Firing reminder {reminder_id} — {text}")

    payload = {
        "title": {"en": "⏰ Reminder", "hi": "⏰ याद दिलाना", "te": "⏰ రిమైండర్"}.get(language, "⏰ Reminder"),
        "body":  text,
        "lang":  language,
        "id":    reminder_id,
    }

    try:
        send_push(endpoint, p256dh, auth, payload)
        print(f"[Scheduler] Push sent for {reminder_id}")
    except WebPushException as e:
        print(f"[Scheduler] Push FAILED for {reminder_id}: {e}")

    # mark as sent
    try:
        db = get_db()
        db.collection("reminders").document(reminder_id).update({"sent": True})
    except Exception as e:
        print(f"[Scheduler] Firestore update failed: {e}")

# ── Self-ping to prevent Render free tier sleep ───────────────────────────────
async def keep_alive():
    if not SELF_URL:
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{SELF_URL}/ping")
            print(f"[KeepAlive] ping → {r.status_code}")
    except Exception as e:
        print(f"[KeepAlive] ping failed: {e}")

# ── Restore all pending reminders from Firestore on startup ──────────────────
def restore_pending_reminders():
    print("[Startup] Restoring pending reminders from Firestore...")
    try:
        db = get_db()
        docs = db.collection("reminders").where("sent", "==", False).stream()
        count = 0
        now = datetime.now(timezone.utc)

        for doc in docs:
            data = doc.to_dict()
            try:
                remind_at = datetime.fromisoformat(data["remind_at"])
                if remind_at.tzinfo is None:
                    remind_at = remind_at.replace(tzinfo=timezone.utc)

                if remind_at <= now:
                    # already past due — fire immediately (1 second delay)
                    from datetime import timedelta
                    fire_at = now + timedelta(seconds=2)
                    print(f"[Startup] Past-due reminder {doc.id}, firing in 2s")
                else:
                    fire_at = remind_at
                    print(f"[Startup] Scheduled reminder {doc.id} at {remind_at}")

                scheduler.add_job(
                    fire_reminder,
                    trigger="date",
                    run_date=fire_at,
                    id=doc.id,
                    replace_existing=True,
                    args=[
                        doc.id,
                        data.get("text", ""),
                        data.get("language", "en"),
                        data.get("endpoint", ""),
                        data.get("p256dh", ""),
                        data.get("auth", ""),
                    ],
                )
                count += 1
            except Exception as e:
                print(f"[Startup] Skipping reminder {doc.id}: {e}")

        print(f"[Startup] Restored {count} pending reminders.")
    except Exception as e:
        print(f"[Startup] Could not restore reminders: {e}")

# ── Lifespan (startup + shutdown) ─────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    # startup
    scheduler.start()
    restore_pending_reminders()

    # ping every 10 minutes to keep Render awake
    if SELF_URL:
        scheduler.add_job(keep_alive, "interval", minutes=10, id="keep_alive")
        print(f"[KeepAlive] Will ping {SELF_URL} every 10 minutes")

    yield

    # shutdown
    scheduler.shutdown(wait=False)

# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(title="JarAI Reminder API", lifespan=lifespan)

# ── CORS manual middleware (works on all hosts) ───────────────────────────────
@app.middleware("http")
async def add_cors_headers(request: Request, call_next):
    if request.method == "OPTIONS":
        response = JSONResponse(content={}, status_code=200)
    else:
        response = await call_next(request)
    response.headers["Access-Control-Allow-Origin"]  = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    return response

# ── Routes ────────────────────────────────────────────────────────────────────
@app.get("/")
def root():
    return {"status": "JarAI backend running ✅"}

@app.get("/ping")
def ping():
    return {"pong": True}

@app.get("/vapid-public-key")
def vapid_public_key():
    return {"publicKey": VAPID_PUBLIC_KEY}

@app.post("/reminders", response_model=ReminderOut)
async def create_reminder(body: ReminderCreate):
    db = get_db()

    try:
        remind_at = datetime.fromisoformat(body.remind_at)
        if remind_at.tzinfo is None:
            remind_at = remind_at.replace(tzinfo=timezone.utc)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid remind_at format. Use ISO-8601.")

    if remind_at <= datetime.now(timezone.utc):
        raise HTTPException(status_code=422, detail="remind_at must be in the future.")

    # save to Firestore
    ref = db.collection("reminders").document()
    doc = {
        "text":       body.text,
        "remind_at":  body.remind_at,
        "language":   body.language,
        "sent":       False,
        "endpoint":   body.subscription.endpoint,
        "p256dh":     body.subscription.keys.p256dh,
        "auth":       body.subscription.keys.auth,
        "created_at": firestore.SERVER_TIMESTAMP,
    }
    ref.set(doc)

    # schedule with APScheduler
    scheduler.add_job(
        fire_reminder,
        trigger="date",
        run_date=remind_at,
        id=ref.id,
        replace_existing=True,
        args=[
            ref.id,
            body.text,
            body.language,
            body.subscription.endpoint,
            body.subscription.keys.p256dh,
            body.subscription.keys.auth,
        ],
    )
    print(f"[API] Scheduled reminder {ref.id} at {remind_at}")

    return ReminderOut(id=ref.id, text=body.text,
                       remind_at=body.remind_at, language=body.language, sent=False)

@app.get("/reminders", response_model=list[ReminderOut])
def list_reminders():
    db = get_db()
    docs = db.collection("reminders").order_by("remind_at").stream()
    results = []
    for d in docs:
        data = d.to_dict()
        results.append(ReminderOut(
            id=d.id,
            text=data.get("text", ""),
            remind_at=data.get("remind_at", ""),
            language=data.get("language", "en"),
            sent=data.get("sent", False),
        ))
    return results

@app.delete("/reminders/{reminder_id}")
def delete_reminder(reminder_id: str):
    db = get_db()
    db.collection("reminders").document(reminder_id).delete()
    # also remove from scheduler if pending
    try:
        scheduler.remove_job(reminder_id)
    except Exception:
        pass
    return {"deleted": reminder_id}