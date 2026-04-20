import os
import json
import asyncio
import httpx
from datetime import datetime, timezone
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from pywebpush import webpush, WebPushException
import firebase_admin
from firebase_admin import credentials, firestore, messaging
from dotenv import load_dotenv
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.jobstores.memory import MemoryJobStore

load_dotenv()

# ── Firebase ──────────────────────────────────────────────────────────────────
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

# ── VAPID (web push fallback for desktop) ─────────────────────────────────────
VAPID_PRIVATE_KEY = os.getenv("VAPID_PRIVATE_KEY", "")
VAPID_PUBLIC_KEY  = os.getenv("VAPID_PUBLIC_KEY", "")
VAPID_CLAIMS      = {"sub": f"mailto:{os.getenv('VAPID_EMAIL', 'admin@jarai.app')}"}
SELF_URL          = os.getenv("SELF_URL", "")

# ── Scheduler ─────────────────────────────────────────────────────────────────
scheduler = AsyncIOScheduler(jobstores={"default": MemoryJobStore()})

# ── Models ────────────────────────────────────────────────────────────────────
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
    fcm_token: str = ""          # FCM token for mobile

class ReminderOut(BaseModel):
    id: str
    text: str
    remind_at: str
    language: str
    sent: bool

# ── Send via FCM (mobile — bypasses battery killing) ─────────────────────────
def send_fcm(fcm_token: str, title: str, body: str, reminder_id: str, lang: str):
    message = messaging.Message(
        notification=messaging.Notification(title=title, body=body),
        data={"id": reminder_id, "lang": lang, "body": body},
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
    response = messaging.send(message)
    print(f"[FCM] Sent: {response}")

# ── Send via Web Push (desktop fallback) ──────────────────────────────────────
def send_webpush(endpoint: str, p256dh: str, auth: str, payload: dict):
    webpush(
        subscription_info={"endpoint": endpoint, "keys": {"p256dh": p256dh, "auth": auth}},
        data=json.dumps(payload),
        vapid_private_key=VAPID_PRIVATE_KEY,
        vapid_claims=VAPID_CLAIMS,
    )

# ── Core fire function (called by scheduler) ──────────────────────────────────
def fire_reminder(reminder_id: str, text: str, language: str,
                  endpoint: str, p256dh: str, auth: str, fcm_token: str):
    print(f"[Scheduler] Firing: {reminder_id} | {text}")

    title_map = {"en": "⏰ Reminder", "hi": "⏰ याद दिलाना", "te": "⏰ రిమైండర్"}
    title = title_map.get(language, "⏰ Reminder")
    payload = {"title": title, "body": text, "lang": language, "id": reminder_id}

    sent = False

    # Try FCM first (works on mobile even when app is closed)
    if fcm_token:
        try:
            send_fcm(fcm_token, title, text, reminder_id, language)
            sent = True
            print(f"[FCM] ✅ Delivered via FCM")
        except Exception as e:
            print(f"[FCM] ❌ Failed: {e} — falling back to WebPush")

    # Fallback to WebPush (desktop)
    if not sent and endpoint:
        try:
            send_webpush(endpoint, p256dh, auth, payload)
            sent = True
            print(f"[WebPush] ✅ Delivered via WebPush")
        except WebPushException as e:
            print(f"[WebPush] ❌ Failed: {e}")

    # Mark sent in Firestore
    try:
        get_db().collection("reminders").document(reminder_id).update({"sent": True})
    except Exception as e:
        print(f"[Firestore] Update failed: {e}")

# ── Keep Render alive ─────────────────────────────────────────────────────────
async def keep_alive():
    if not SELF_URL:
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.get(f"{SELF_URL}/ping")
            print("[KeepAlive] ✅ pinged")
    except Exception as e:
        print(f"[KeepAlive] ❌ {e}")

# ── Restore pending reminders on startup ──────────────────────────────────────
def restore_pending():
    print("[Startup] Restoring pending reminders...")
    try:
        db = get_db()
        docs = db.collection("reminders").where("sent", "==", False).stream()
        now = datetime.now(timezone.utc)
        count = 0
        for doc in docs:
            d = doc.to_dict()
            try:
                remind_at = datetime.fromisoformat(d["remind_at"])
                if remind_at.tzinfo is None:
                    remind_at = remind_at.replace(tzinfo=timezone.utc)
                from datetime import timedelta
                fire_at = remind_at if remind_at > now else now + timedelta(seconds=3)
                scheduler.add_job(
                    fire_reminder, trigger="date", run_date=fire_at,
                    id=doc.id, replace_existing=True,
                    args=[doc.id, d.get("text",""), d.get("language","en"),
                          d.get("endpoint",""), d.get("p256dh",""),
                          d.get("auth",""), d.get("fcm_token","")],
                )
                count += 1
            except Exception as e:
                print(f"[Startup] Skip {doc.id}: {e}")
        print(f"[Startup] Restored {count} reminders.")
    except Exception as e:
        print(f"[Startup] Error: {e}")

# ── Lifespan ──────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler.start()
    restore_pending()
    if SELF_URL:
        scheduler.add_job(keep_alive, "interval", minutes=10, id="keep_alive")
    yield
    scheduler.shutdown(wait=False)

app = FastAPI(title="JarAI API", lifespan=lifespan)

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

# ── Routes ────────────────────────────────────────────────────────────────────
@app.get("/")
def root():
    return {"status": "JarAI backend running ✅"}

@app.get("/ping")
def ping():
    return {"pong": True}

@app.get("/vapid-public-key")
def get_vapid():
    return {"publicKey": VAPID_PUBLIC_KEY}

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
    ref.set({
        "text": body.text, "remind_at": body.remind_at,
        "language": body.language, "sent": False,
        "endpoint": body.subscription.endpoint,
        "p256dh": body.subscription.keys.p256dh,
        "auth": body.subscription.keys.auth,
        "fcm_token": body.fcm_token,
        "created_at": firestore.SERVER_TIMESTAMP,
    })

    scheduler.add_job(
        fire_reminder, trigger="date", run_date=remind_at,
        id=ref.id, replace_existing=True,
        args=[ref.id, body.text, body.language,
              body.subscription.endpoint,
              body.subscription.keys.p256dh,
              body.subscription.keys.auth,
              body.fcm_token],
    )
    print(f"[API] Scheduled {ref.id} at {remind_at}")
    return ReminderOut(id=ref.id, text=body.text,
                       remind_at=body.remind_at, language=body.language, sent=False)

@app.get("/reminders", response_model=list[ReminderOut])
def list_reminders():
    db = get_db()
    results = []
    for d in db.collection("reminders").order_by("remind_at").stream():
        data = d.to_dict()
        results.append(ReminderOut(
            id=d.id, text=data.get("text",""),
            remind_at=data.get("remind_at",""),
            language=data.get("language","en"),
            sent=data.get("sent", False),
        ))
    return results

@app.delete("/reminders/{rid}")
def delete_reminder(rid: str):
    get_db().collection("reminders").document(rid).delete()
    try: scheduler.remove_job(rid)
    except: pass
    return {"deleted": rid}