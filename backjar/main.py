import os
import json
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

# ── VAPID ─────────────────────────────────────────────────────────────────────
VAPID_PRIVATE_KEY = os.getenv("VAPID_PRIVATE_KEY", "")
VAPID_PUBLIC_KEY  = os.getenv("VAPID_PUBLIC_KEY", "")
VAPID_CLAIMS      = {"sub": f"mailto:{os.getenv('VAPID_EMAIL', 'admin@jarai.app')}"}

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
    fcm_token: str = ""

class ReminderOut(BaseModel):
    id: str
    text: str
    remind_at: str
    language: str
    sent: bool

# ── Push via FCM ──────────────────────────────────────────────────────────────
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
    resp = messaging.send(message)
    print(f"[FCM] ✅ sent: {resp}")

# ── Push via WebPush (desktop fallback) ───────────────────────────────────────
def send_webpush(endpoint: str, p256dh: str, auth: str, payload: dict):
    webpush(
        subscription_info={"endpoint": endpoint, "keys": {"p256dh": p256dh, "auth": auth}},
        data=json.dumps(payload),
        vapid_private_key=VAPID_PRIVATE_KEY,
        vapid_claims=VAPID_CLAIMS,
    )

# ── Core: fire one reminder ───────────────────────────────────────────────────
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

    # Try FCM first (reliable on mobile)
    if fcm_tok:
        try:
            send_fcm(fcm_tok, title, text, doc_id, lang)
            sent = True
        except Exception as e:
            print(f"[FCM] ❌ {e}")

    # Fallback to WebPush (desktop)
    if not sent and endpoint:
        try:
            send_webpush(endpoint, p256dh, auth, payload)
            sent = True
            print(f"[WebPush] ✅ sent")
        except WebPushException as e:
            print(f"[WebPush] ❌ {e}")

    # Mark as sent in Firestore
    try:
        get_db().collection("reminders").document(doc_id).update({"sent": True})
        print(f"[Firestore] Marked {doc_id} as sent")
    except Exception as e:
        print(f"[Firestore] ❌ {e}")

# ── /check-reminders — called by external cron every 5 min ───────────────────
def check_and_fire_due_reminders():
    db  = get_db()
    now = datetime.now(timezone.utc)
    print(f"[Cron] Checking due reminders at {now.isoformat()}")

    docs = db.collection("reminders").where("sent", "==", False).stream()
    fired = 0
    for doc in docs:
        data = doc.to_dict()
        try:
            remind_at = datetime.fromisoformat(data["remind_at"])
            if remind_at.tzinfo is None:
                remind_at = remind_at.replace(tzinfo=timezone.utc)

            if remind_at <= now:
                print(f"[Cron] Firing due reminder: {doc.id} | {data.get('text')}")
                fire_reminder(doc.id, data)
                fired += 1
        except Exception as e:
            print(f"[Cron] Error processing {doc.id}: {e}")

    print(f"[Cron] Done — fired {fired} reminders.")
    return fired

# ── Lifespan ──────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    # On startup, immediately check for any missed reminders
    try:
        check_and_fire_due_reminders()
    except Exception as e:
        print(f"[Startup] check failed: {e}")
    yield

app = FastAPI(title="JarAI API", lifespan=lifespan)

# ── CORS ──────────────────────────────────────────────────────────────────────
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
    # Called by external cron — also checks for due reminders every time
    fired = check_and_fire_due_reminders()
    return {"pong": True, "fired": fired, "time": datetime.now(timezone.utc).isoformat()}

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
        "text":       body.text,
        "remind_at":  body.remind_at,
        "language":   body.language,
        "sent":       False,
        "endpoint":   body.subscription.endpoint,
        "p256dh":     body.subscription.keys.p256dh,
        "auth":       body.subscription.keys.auth,
        "fcm_token":  body.fcm_token,
        "created_at": firestore.SERVER_TIMESTAMP,
    })
    print(f"[API] Saved reminder {ref.id} for {body.remind_at}")
    return ReminderOut(id=ref.id, text=body.text,
                       remind_at=body.remind_at, language=body.language, sent=False)

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
    get_db().collection("reminders").document(rid).delete()
    return {"deleted": rid}