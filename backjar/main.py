import os
import json
import asyncio
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from pywebpush import webpush, WebPushException
import firebase_admin
from firebase_admin import credentials, firestore
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="JarAI Reminder API")

# ── CORS ──────────────────────────────────────────────────────────────────────
# NOTE: allow_credentials=True + allow_origins=["*"] is blocked by browsers.
# We list origins explicitly instead.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:5500",
        "http://127.0.0.1:5500",
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:8080",
        "http://127.0.0.1:8080",
        "https://jarai-app.netlify.app/",
    ],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

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

# ── Pydantic models ───────────────────────────────────────────────────────────
class SubscriptionKeys(BaseModel):
    p256dh: str
    auth: str

class PushSubscription(BaseModel):
    endpoint: str
    keys: SubscriptionKeys

class ReminderCreate(BaseModel):
    text: str
    remind_at: str          # ISO-8601, e.g. "2025-06-01T11:00:00+05:30"
    language: str           # "en" | "hi" | "te"
    subscription: PushSubscription

class ReminderOut(BaseModel):
    id: str
    text: str
    remind_at: str
    language: str
    sent: bool

# ── Helpers ───────────────────────────────────────────────────────────────────
def send_push(subscription: PushSubscription, payload: dict):
    """Fire a single web-push notification."""
    sub_data = {
        "endpoint": subscription.endpoint,
        "keys": {
            "p256dh": subscription.keys.p256dh,
            "auth":   subscription.keys.auth,
        },
    }
    webpush(
        subscription_info=sub_data,
        data=json.dumps(payload),
        vapid_private_key=VAPID_PRIVATE_KEY,
        vapid_claims=VAPID_CLAIMS,
    )

async def schedule_reminder(reminder_id: str, remind_at: datetime,
                             text: str, language: str,
                             subscription: PushSubscription):
    """Wait until remind_at then push the notification and mark sent."""
    now = datetime.now(timezone.utc)
    delay = (remind_at - now).total_seconds()
    if delay > 0:
        await asyncio.sleep(delay)

    payload = {
        "title": {"en": "⏰ Reminder", "hi": "⏰ याद दिलाना", "te": "⏰ రిమైండర్"}.get(language, "⏰ Reminder"),
        "body":  text,
        "lang":  language,
        "id":    reminder_id,
    }

    try:
        send_push(subscription, payload)
    except WebPushException as e:
        print(f"Push failed for {reminder_id}: {e}")

    # mark as sent in Firestore
    try:
        db = get_db()
        db.collection("reminders").document(reminder_id).update({"sent": True})
    except Exception as e:
        print(f"Firestore update failed: {e}")

# ── Routes ────────────────────────────────────────────────────────────────────
@app.get("/")
def root():
    return {"status": "JarAI backend running ✅"}

@app.get("/vapid-public-key")
def vapid_public_key():
    return {"publicKey": VAPID_PUBLIC_KEY}

@app.post("/reminders", response_model=ReminderOut)
async def create_reminder(body: ReminderCreate, background_tasks: BackgroundTasks):
    db = get_db()

    # parse remind_at
    try:
        remind_at = datetime.fromisoformat(body.remind_at)
        if remind_at.tzinfo is None:
            remind_at = remind_at.replace(tzinfo=timezone.utc)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid remind_at format. Use ISO-8601.")

    if remind_at <= datetime.now(timezone.utc):
        raise HTTPException(status_code=422, detail="remind_at must be in the future.")

    # store in Firestore
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

    # schedule push in background
    background_tasks.add_task(
        schedule_reminder,
        ref.id, remind_at, body.text, body.language, body.subscription
    )

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
    return {"deleted": reminder_id}