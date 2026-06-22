import os
import uuid
import json
import base64
import asyncio
import subprocess
import tempfile
import io
import smtplib
import random
import re
import hmac
import hashlib
import sqlite3
import html
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import formataddr, parseaddr
from urllib.parse import quote
import httpx
import fal_client
import threading

# Import edge_tts with a 10s timeout to avoid hanging on startup
_edge_tts_result = [None]
def _import_edge_tts():
    try:
        import edge_tts as _et
        _edge_tts_result[0] = _et
    except Exception:
        pass
_t = threading.Thread(target=_import_edge_tts, daemon=True)
_t.start()
_t.join(timeout=10)
edge_tts = _edge_tts_result[0]
if edge_tts is None:
    print("[WARNING] edge_tts failed to import or timed out — TTS will be unavailable")
from datetime import datetime, timedelta
from fastapi import FastAPI, File, Form, UploadFile, HTTPException, BackgroundTasks, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, PlainTextResponse
import anthropic
import imageio_ffmpeg
from PIL import Image, ImageDraw, ImageFont, ImageOps
from dotenv import load_dotenv, set_key

ENV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
load_dotenv(dotenv_path=ENV_FILE, override=True)

# ── Google Calendar ────────────────────────────────────────────────────────────
GCAL_CALENDAR_ID   = os.getenv("GCAL_CALENDAR_ID", "vishleshak.in@gmail.com")
GCAL_CREDENTIALS   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dental_clinic_calendar.json")

def _gcal_service():
    """Return authenticated Google Calendar service, or None if not configured."""
    try:
        import json
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
        # Railway: read from env var; local: read from file
        creds_json = os.getenv("GCAL_CREDENTIALS_JSON")
        if creds_json:
            # Support both raw JSON and base64-encoded JSON
            try:
                import base64
                decoded = base64.b64decode(creds_json).decode("utf-8")
                info = json.loads(decoded)
            except Exception:
                info = json.loads(creds_json)
            creds = service_account.Credentials.from_service_account_info(
                info, scopes=["https://www.googleapis.com/auth/calendar"]
            )
        elif os.path.exists(GCAL_CREDENTIALS):
            creds = service_account.Credentials.from_service_account_file(
                GCAL_CREDENTIALS, scopes=["https://www.googleapis.com/auth/calendar"]
            )
        else:
            print("[GCal] No credentials found")
            return None
        return build("calendar", "v3", credentials=creds)
    except Exception as e:
        print(f"[GCal] Service init failed: {e}")
        return None

_DAY_NAMES = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

def _clinic_now() -> datetime:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Asia/Kolkata"))
    except Exception:
        return datetime.now()


def _ordinal_day(day: int) -> str:
    if 10 <= day % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    return f"{day}{suffix}"


def _display_date_option(dt: datetime, prefix: str = "") -> str:
    label = f"{dt.strftime('%A')}, {_ordinal_day(dt.day)} {dt.strftime('%B %Y')}"
    return f"{prefix}{label}" if prefix else label


def _format_hour_value(hour_value: int | float) -> str:
    hour = int(hour_value)
    minute = 30 if isinstance(hour_value, float) and hour_value % 1 else 0
    period = "AM" if hour < 12 else "PM"
    display_hour = hour if 1 <= hour <= 12 else (hour - 12 if hour > 12 else 12)
    if minute:
        return f"{display_hour}:{minute:02d} {period}"
    return f"{display_hour} {period}"


def _extract_appointment_time(value: str) -> dict | None:
    import re

    raw = value.strip()
    explicit = re.search(r'\b(\d{1,2})(?:[\.:](\d{2}))?\s*(am|pm)\b', raw, re.IGNORECASE)
    bare = None
    if not explicit:
        bare = re.search(r'\b([1-9]|1[0-2])[\.:](\d{2})\b', raw)
        month_names = (
            "jan", "january", "feb", "february", "mar", "march", "apr", "april",
            "may", "jun", "june", "jul", "july", "aug", "august", "sep",
            "sept", "september", "oct", "october", "nov", "november", "dec", "december",
        )
        if not bare and not any(m in raw.lower() for m in month_names):
            bare = re.search(r'\b([1-9]|1[0-2])\b', raw)
    match = explicit or bare
    if not match:
        return None

    hour = int(match.group(1))
    minute_text = match.group(2) if match.lastindex and match.lastindex >= 2 else None
    minute = int(minute_text or 0)
    period = explicit.group(3).lower() if explicit else None

    if period == "am":
        hour24 = 0 if hour == 12 else hour
    elif period == "pm":
        hour24 = 12 if hour == 12 else hour + 12
    elif 9 <= hour <= 12:
        hour24 = hour
    else:
        hour24 = hour + 12

    hour_value = hour24 + (0.5 if minute >= 30 else 0)
    label = _format_hour_value(hour_value)
    date_text = (raw[:match.start()] + " " + raw[match.end():]).strip()
    date_text = re.sub(r'\b(?:at|for|on)\b', ' ', date_text, flags=re.IGNORECASE)
    date_text = re.sub(r'\s+', ' ', date_text).strip(" ,.-")

    return {"hour": hour_value, "label": label, "date_text": date_text}


def _same_weekday_date_options(date_text: str) -> dict | None:
    text = date_text.lower().strip()
    if text not in _DAY_NAMES:
        return None

    today = _clinic_now()
    days_ahead = (_DAY_NAMES.index(text) - today.weekday()) % 7
    if days_ahead != 0:
        return None

    following_match = today + timedelta(days=7)
    return {
        "today_value": today.strftime("%d %B %Y"),
        "today_label": _display_date_option(today, "Today, "),
        "next_value": following_match.strftime("%d %B %Y"),
        "next_label": _display_date_option(following_match),
    }


def _parse_event_datetime(date_str: str, hour: int) -> datetime:
    """Parse date string and return datetime with given hour. Always returns a future date."""
    from dateutil import parser as dateparser
    now = _clinic_now()
    today = now.date()
    dl = date_str.lower().strip()
    try:
        if dl in ("today", "aaj"):
            event_date = now
        elif dl in ("tomorrow", "tmrw", "kal"):
            event_date = now + timedelta(days=1)
        else:
            event_date = dateparser.parse(date_str, dayfirst=True)
        if not event_date:
            event_date = now + timedelta(days=1)
        elif event_date.date() < today:
            # If user said a weekday name (e.g. "Tuesday") and it parsed to the past,
            # roll forward to next occurrence of that day
            if any(d in dl for d in _DAY_NAMES):
                event_date += timedelta(days=7)
            else:
                event_date = now + timedelta(days=1)
    except Exception:
        event_date = now + timedelta(days=1)
    minute = 30 if isinstance(hour, float) and hour % 1 else 0
    return event_date.replace(hour=int(hour), minute=minute, second=0, microsecond=0)

def _format_date(date_str: str, hour: int | None = None) -> str:
    """Return a nicely formatted date string like 'Tuesday, 3 June 2026'."""
    try:
        dt = _parse_event_datetime(date_str, hour or 9)
        return f"{dt.strftime('%A')}, {dt.day} {dt.strftime('%B %Y')}"
    except Exception:
        return date_str


def _clinic_hours_issue(date_str: str, hour_value: int | float) -> str | None:
    appt_dt = _parse_event_datetime(date_str, hour_value)
    now = _clinic_now()
    if appt_dt.tzinfo is None and now.tzinfo is not None:
        now = now.replace(tzinfo=None)
    if appt_dt <= now:
        return "That time has already passed. Please choose a future time."

    is_sunday = appt_dt.weekday() == 6
    is_saturday = appt_dt.weekday() == 5
    close_hour = 18 if is_saturday else 20

    if is_sunday:
        return "The clinic is closed on Sunday. Please choose Monday to Saturday."
    if hour_value < 9 or hour_value >= close_hour:
        return "Sorry, that time is *outside our working hours*."
    return None


async def get_available_slots(date_str: str, max_slots: int = 6, period: str = "all") -> list[str]:
    """Return up to max_slots free 30-min slots for the given day within a period.
    period: 'morning' (9-12), 'afternoon' (12-16), 'evening' (16-20), 'all' (9-20)
    """
    import asyncio
    try:
        from dateutil import parser as dateparser
        try:
            day = dateparser.parse(date_str, dayfirst=True)
            if not day or day.date() < datetime.now().date():
                if any(d in date_str.lower() for d in _DAY_NAMES):
                    day += timedelta(days=7)
                else:
                    day = datetime.now() + timedelta(days=1)
        except Exception:
            day = datetime.now() + timedelta(days=1)

        # Saturday closes at 18:00, others at 20:00
        is_saturday = day.weekday() == 5
        day_close = 18 if is_saturday else 20
        period_ranges = {"morning": (9,12), "afternoon": (12,16), "evening": (16, day_close), "all": (9, day_close)}
        open_h, close_h = period_ranges.get(period, (9, day_close))

        svc = await asyncio.get_event_loop().run_in_executor(None, _gcal_service)
        if not svc:
            return []

        # Fetch all events for the day
        day_start = day.replace(hour=open_h,  minute=0, second=0, microsecond=0)
        day_end   = day.replace(hour=close_h, minute=0, second=0, microsecond=0)
        result = await asyncio.get_event_loop().run_in_executor(None, lambda: svc.events().list(
            calendarId=GCAL_CALENDAR_ID,
            timeMin=day_start.isoformat() + "+05:30",
            timeMax=day_end.isoformat()   + "+05:30",
            singleEvents=True, orderBy="startTime"
        ).execute())
        booked = result.get("items", [])

        # Build set of booked start times
        booked_starts = set()
        for ev in booked:
            st = ev.get("start", {}).get("dateTime", "")
            if st:
                try:
                    dt = datetime.fromisoformat(st.replace("Z", "+00:00"))
                    booked_starts.add((dt.hour, dt.minute))
                except Exception:
                    pass

        # Walk 30-min slots
        free = []
        slot = day_start
        while slot < day_end:
            if (slot.hour, slot.minute) not in booked_starts:
                h, m = slot.hour, slot.minute
                period = "AM" if h < 12 else "PM"
                dh = h if h <= 12 else h - 12
                if dh == 0: dh = 12
                free.append(f"{dh}:{m:02d} {period}")
            slot += timedelta(minutes=30)
            if len(free) >= max_slots:
                break
        return free
    except Exception as e:
        print(f"[GCal] get_available_slots failed: {e}")
        return []


async def check_gcal_conflict(date_str: str, start_hour: int) -> list[str]:
    """
    Check Google Calendar for conflicts at given time.
    Returns list of alternative time strings if conflict found, else empty list.
    """
    import asyncio
    try:
        from pytz import timezone
        IST = timezone("Asia/Kolkata")
    except Exception:
        from datetime import timezone as tz
        IST = tz(timedelta(hours=5, minutes=30))

    try:
        start_dt = _parse_event_datetime(date_str, start_hour)
        end_dt   = start_dt + timedelta(minutes=30)

        svc = await asyncio.get_event_loop().run_in_executor(None, _gcal_service)
        if not svc:
            return []

        result = await asyncio.get_event_loop().run_in_executor(None, lambda: svc.events().list(
            calendarId=GCAL_CALENDAR_ID,
            timeMin=start_dt.isoformat() + "+05:30",
            timeMax=end_dt.isoformat() + "+05:30",
            singleEvents=True
        ).execute())

        events = result.get("items", [])
        if not events:
            return []  # No conflict

        # Generate up to 3 alternative slots in 30-min steps (nearest first)
        def _fmt_slot(h: int, m: int) -> str:
            period = "AM" if h < 12 else "PM"
            disp_h = h if h <= 12 else h - 12
            if disp_h == 0: disp_h = 12
            return f"{disp_h}:{m:02d} {period}"

        # Build candidate offsets in 30-min steps: +30, -30, +60, -60, +90, -90 ...
        candidates = []
        for steps in range(1, 10):
            candidates.append(steps * 30)
            candidates.append(-steps * 30)

        alternatives = []
        for delta_min in candidates:
            alt_start = start_dt + timedelta(minutes=delta_min)
            alt_h, alt_m = alt_start.hour, alt_start.minute
            # Within clinic hours: 9:00–19:30 (last slot starts at 19:30 ends 20:00)
            if not (9 <= alt_h < 20 and (alt_h < 19 or alt_m == 0)):
                continue
            alt_end = alt_start + timedelta(minutes=30)
            alt_result = await asyncio.get_event_loop().run_in_executor(None, lambda s=alt_start, e=alt_end: svc.events().list(
                calendarId=GCAL_CALENDAR_ID,
                timeMin=s.isoformat() + "+05:30",
                timeMax=e.isoformat() + "+05:30",
                singleEvents=True
            ).execute())
            if not alt_result.get("items"):
                label = _fmt_slot(alt_h, alt_m)
                if label not in alternatives:
                    alternatives.append(label)
            if len(alternatives) >= 3:
                break
        return alternatives

    except Exception as e:
        print(f"[GCal] Conflict check failed: {e}")
        return []


async def create_gcal_event(name: str, service: str, date_str: str, time_slot: str, patient_phone: str, specific_hour: int | None = None) -> str | None:
    """Create a Google Calendar event. Returns event URL or None."""
    import asyncio
    try:
        slot_hours = {"Morning (9am–12pm)": 9, "Afternoon (12pm–4pm)": 12, "Evening (4pm–8pm)": 16}
        start_hour = specific_hour if specific_hour else slot_hours.get(time_slot, 10)

        start_dt = _parse_event_datetime(date_str, start_hour)
        end_dt   = start_dt + timedelta(minutes=30)

        event = {
            "summary": f"🦷 {service} — {name}",
            "description": f"Patient: {name}\nService: {service}\nWhatsApp: {patient_phone}",
            "start": {"dateTime": start_dt.isoformat(), "timeZone": "Asia/Kolkata"},
            "end":   {"dateTime": end_dt.isoformat(),   "timeZone": "Asia/Kolkata"},
        }

        svc = await asyncio.get_event_loop().run_in_executor(None, _gcal_service)
        if not svc:
            return None
        result = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: svc.events().insert(calendarId=GCAL_CALENDAR_ID, body=event).execute()
        )
        return result.get("htmlLink")
    except Exception as e:
        print(f"[GCal] Event creation failed: {e}")
        return None

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
KIE_API_KEY       = os.getenv("KIE_API_KEY")
IMGBB_API_KEY     = os.getenv("IMGBB_API_KEY")
DID_API_KEY       = os.getenv("DID_API_KEY")
FAL_KEY           = os.getenv("FAL_KEY")        # fal.ai — SadTalker lip-sync
VOICE_ID          = os.getenv("VOICE_ID", "cgSgspJ2msm6clMCkdW9")

# Notification config
OWNER_EMAIL        = os.getenv("OWNER_EMAIL", "")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "")
OWNER_WHATSAPP     = os.getenv("OWNER_WHATSAPP", "919953910987")
CALLMEBOT_API_KEY  = os.getenv("CALLMEBOT_API_KEY", "")
RESEND_API_KEY     = os.getenv("RESEND_API_KEY", "").strip()
RESEND_FROM_EMAIL  = os.getenv("RESEND_FROM_EMAIL", OWNER_EMAIL).strip()
RESEND_API_URL     = os.getenv("RESEND_API_URL", "https://api.resend.com/emails").strip()
DEFAULT_RESEND_FROM_EMAIL = os.getenv("DEFAULT_RESEND_FROM_EMAIL", "noreply@mail.vishleshak.in").strip()
FAST2SMS_API_KEY   = os.getenv("FAST2SMS_API_KEY", "").strip()
FAST2SMS_WHATSAPP_API_KEY = os.getenv("FAST2SMS_WHATSAPP_API_KEY", "").strip() or FAST2SMS_API_KEY
FAST2SMS_WHATSAPP_VERSION = os.getenv("FAST2SMS_WHATSAPP_VERSION", "v24.0").strip()
FAST2SMS_WHATSAPP_PHONE_NUMBER_ID = os.getenv("FAST2SMS_WHATSAPP_PHONE_NUMBER_ID", "").strip()
FAST2SMS_WHATSAPP_TEMPLATE_NAME = os.getenv("FAST2SMS_WHATSAPP_TEMPLATE_NAME", "").strip()
FAST2SMS_WHATSAPP_TEMPLATE_LANGUAGE = os.getenv("FAST2SMS_WHATSAPP_TEMPLATE_LANGUAGE", "en_US").strip()
FAST2SMS_WHATSAPP_OTP_URL_TEMPLATE = os.getenv("FAST2SMS_WHATSAPP_OTP_URL_TEMPLATE", "").strip().splitlines()[0] if os.getenv("FAST2SMS_WHATSAPP_OTP_URL_TEMPLATE", "").strip() else ""
TWOFACTOR_API_KEY = os.getenv("TWOFACTOR_API_KEY", "").strip()
TWOFACTOR_TEMPLATE_NAME = os.getenv("TWOFACTOR_TEMPLATE_NAME", "Vishleshak_UGC").strip()
TWOFACTOR_MODE = os.getenv("TWOFACTOR_MODE", "autogen").strip().lower()
TWOFACTOR_OTP_URL_TEMPLATE = os.getenv(
    "TWOFACTOR_OTP_URL_TEMPLATE",
    "https://2factor.in/API/V1/{api_key}/SMS/{phone}/{otp}",
).strip().splitlines()[0]
TWOFACTOR_AUTOGEN_URL_TEMPLATE = os.getenv(
    "TWOFACTOR_AUTOGEN_URL_TEMPLATE",
    "https://2factor.in/API/V1/{api_key}/SMS/{phone}/AUTOGEN/{template_name}",
).strip().splitlines()[0]
TWOFACTOR_VERIFY_URL_TEMPLATE = os.getenv(
    "TWOFACTOR_VERIFY_URL_TEMPLATE",
    "https://2factor.in/API/V1/{api_key}/SMS/VERIFY/{session_id}/{otp}",
).strip().splitlines()[0]
CREDIT_OTP_CHANNEL_ORDER = os.getenv(
    "CREDIT_OTP_CHANNEL_ORDER",
    os.getenv("FAST2SMS_OTP_CHANNEL_ORDER", "twofactor,whatsapp,sms,callmebot"),
).strip()

# Razorpay test/live keys are read from env vars only. Never commit secrets.
RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID", "")
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET", "")
RAZORPAY_WEBHOOK_SECRET = os.getenv("RAZORPAY_WEBHOOK_SECRET", "")
RAZORPAY_ENABLED = bool(RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET)
credit_otp_store: dict[str, dict] = {}
last_credit_otp_error = ""
last_credit_otp_channel = ""
last_credit_email_error = ""
TRACKING_DB_FILE = os.getenv(
    "TRACKING_DB_FILE",
    os.path.join(os.path.dirname(__file__), "client_tracking.sqlite3"),
)
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
TRACKING_DB_IS_POSTGRES = bool(DATABASE_URL)
CREDIT_COST_IMAGE = int(os.getenv("CREDIT_COST_IMAGE", "49"))
CREDIT_COST_VIDEO = int(os.getenv("CREDIT_COST_VIDEO", "499"))

# URLs
RAILWAY_URL = os.getenv("RAILWAY_URL", "").rstrip("/")
PUBLIC_URL  = os.getenv("PUBLIC_URL", "").rstrip("/")

# Cloudinary
CLOUDINARY_CLOUD_NAME  = os.getenv("CLOUDINARY_CLOUD_NAME", "")
CLOUDINARY_API_KEY     = os.getenv("CLOUDINARY_API_KEY", "")
CLOUDINARY_API_SECRET  = os.getenv("CLOUDINARY_API_SECRET", "")
if CLOUDINARY_CLOUD_NAME and CLOUDINARY_API_KEY and CLOUDINARY_API_SECRET:
    import cloudinary
    import cloudinary.api
    import cloudinary.uploader
    cloudinary.config(
        cloud_name = CLOUDINARY_CLOUD_NAME,
        api_key    = CLOUDINARY_API_KEY,
        api_secret = CLOUDINARY_API_SECRET,
        secure     = True,
    )
    _CLOUDINARY_READY = True
else:
    _CLOUDINARY_READY = False

# In-memory list of new order IDs for browser polling
_new_order_ids: list = []

model_image_url: str | None = os.getenv("MODEL_IMAGE_URL") or None
model_image_bytes: bytes | None = None

KIE_BASE = "https://api.kie.ai/api/v1"

MODEL_LOCAL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "model", "current_model.jpg")

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

jobs: dict = {}

ORDERS_FILE = os.path.join(os.path.dirname(__file__), "orders.json")
ORDERS_UPLOAD_DIR = os.path.join(os.path.dirname(__file__), "order_uploads")
os.makedirs(ORDERS_UPLOAD_DIR, exist_ok=True)

def load_orders() -> list:
    if not os.path.exists(ORDERS_FILE):
        return []
    try:
        with open(ORDERS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []

def sort_orders_latest_first(orders: list) -> list:
    return sorted(orders, key=lambda o: o.get("created_at") or "", reverse=True)

def save_order(order: dict):
    all_orders = load_orders()
    all_orders = [o for o in all_orders if o.get("id") != order.get("id")]
    all_orders.insert(0, order)
    all_orders = sort_orders_latest_first(all_orders)
    with open(ORDERS_FILE, "w", encoding="utf-8") as f:
        json.dump(all_orders, f, ensure_ascii=False, indent=2)

PACKAGE_DEFS = {
    "food_creatives_999": {
        "name": "Food Creative Pack",
        "price_inr": 999,
        "validity_days": 30,
        "credits": 999,
        "image_credits": 10,
        "video_credits": 0,
        "whatsapp_active": 0,
        "chatbot_active": 0,
        "payment_flow_active": 0,
        "followup_active": 0,
        "features": ["10 AI food creatives"],
    },
    "food_growth_2999": {
        "name": "Food Growth Pack",
        "price_inr": 2999,
        "validity_days": 30,
        "credits": 2999,
        "image_credits": 10,
        "video_credits": 2,
        "whatsapp_active": 0,
        "chatbot_active": 0,
        "payment_flow_active": 0,
        "followup_active": 0,
        "features": ["10 AI creatives", "2 short videos"],
    },
    "monthly_order_flow_7999": {
        "name": "Monthly Order Flow",
        "price_inr": 7999,
        "validity_days": 30,
        "credits": 7999,
        "image_credits": 12,
        "video_credits": 0,
        "whatsapp_active": 1,
        "chatbot_active": 0,
        "payment_flow_active": 1,
        "followup_active": 0,
        "features": ["Weekly offers", "WhatsApp order flow", "Payment links"],
    },
    "growth_automation_14999": {
        "name": "Growth Automation",
        "price_inr": 14999,
        "validity_days": 30,
        "credits": 14999,
        "image_credits": 20,
        "video_credits": 4,
        "whatsapp_active": 1,
        "chatbot_active": 1,
        "payment_flow_active": 1,
        "followup_active": 1,
        "features": ["Content engine", "Chatbot", "Payment flow", "Follow-up automation"],
    },
}

CREDIT_PACK_DEFS = {
    "image_once_49": {
        "name": "Starter Credits",
        "price_inr": 49,
        "credits": CREDIT_COST_IMAGE,
        "validity_days": 30,
        "description": "Good for 1 image, usable across image or video orders",
    },
    "image_five_199": {
        "name": "Creator Credits",
        "price_inr": 199,
        "credits": CREDIT_COST_IMAGE * 5,
        "validity_days": 30,
        "description": "Good for 5 images, usable across image or video orders",
    },
    "short_video_499": {
        "name": "Pro Credits",
        "price_inr": 499,
        "credits": CREDIT_COST_VIDEO,
        "validity_days": 30,
        "description": "Good for 1 short video, usable across image or video orders",
    },
    "short_video_three_999": {
        "name": "Growth Credits",
        "price_inr": 1199,
        "credits": CREDIT_COST_VIDEO * 3,
        "validity_days": 30,
        "description": "Good for 3 short videos, usable across image or video orders",
    },
}

def _utc_now_iso() -> str:
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

def _normalize_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) > 10 and digits.startswith("91"):
        digits = digits[-10:]
    return digits

def _normalize_email(email: str) -> str:
    parsed = parseaddr(email or "")[1].strip().lower()
    if not parsed or "@" not in parsed:
        return ""
    return parsed

def _normalize_sender_email(sender: str) -> tuple[str, str]:
    name, email = parseaddr(sender or "")
    email = _normalize_email(email)
    if not email:
        return "", ""
    if name:
        return formataddr((name.strip(), email)), email
    return email, email

def _resend_sender_candidates() -> list[tuple[str, str]]:
    candidates = []
    for raw_sender in (RESEND_FROM_EMAIL, DEFAULT_RESEND_FROM_EMAIL):
        sender, sender_email = _normalize_sender_email(raw_sender)
        if sender_email and (sender, sender_email) not in candidates:
            candidates.append((sender, sender_email))
    return candidates

def _mask_email(email: str) -> str:
    normalized = _normalize_email(email)
    if not normalized or "@" not in normalized:
        return ""
    name, domain = normalized.split("@", 1)
    if len(name) <= 2:
        masked_name = name[:1] + "*"
    else:
        masked_name = name[:1] + ("*" * min(4, len(name) - 2)) + name[-1:]
    return f"{masked_name}@{domain}"

def _credit_otp_secret() -> str:
    return RAZORPAY_WEBHOOK_SECRET or RAZORPAY_KEY_SECRET or CALLMEBOT_API_KEY or "vishleshak-credit-otp"

def _credit_otp_hash(phone: str, otp: str, method: str = "phone", email: str = "") -> str:
    msg = f"{_normalize_phone(phone)}:{_normalize_email(email)}:{method}:{otp}".encode("utf-8")
    return hmac.new(_credit_otp_secret().encode("utf-8"), msg, hashlib.sha256).hexdigest()

def _credit_token_signature(payload_b64: str) -> str:
    return hmac.new(_credit_otp_secret().encode("utf-8"), payload_b64.encode("utf-8"), hashlib.sha256).hexdigest()

def _sign_credit_otp_token(phone: str) -> str:
    payload = {
        "phone": _normalize_phone(phone),
        "exp": int((datetime.utcnow() + timedelta(minutes=30)).timestamp()),
    }
    payload_b64 = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8")).decode("ascii").rstrip("=")
    return f"{payload_b64}.{_credit_token_signature(payload_b64)}"

def _verify_credit_otp_token(phone: str, token: str) -> bool:
    if not token or "." not in token:
        return False
    payload_b64, signature = token.rsplit(".", 1)
    expected = _credit_token_signature(payload_b64)
    if not hmac.compare_digest(signature, expected):
        return False
    try:
        padded = payload_b64 + ("=" * (-len(payload_b64) % 4))
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    except Exception:
        return False
    if payload.get("phone") != _normalize_phone(phone):
        return False
    return int(payload.get("exp") or 0) >= int(datetime.utcnow().timestamp())

def _cleanup_credit_otps():
    now_ts = datetime.utcnow().timestamp()
    for phone, record in list(credit_otp_store.items()):
        if float(record.get("expires_at") or 0) < now_ts:
            credit_otp_store.pop(phone, None)

class _TrackingConnection:
    def __init__(self):
        self.conn = None

    def __enter__(self):
        if TRACKING_DB_IS_POSTGRES:
            try:
                import psycopg
                from psycopg.rows import dict_row
            except Exception as e:
                raise RuntimeError("DATABASE_URL is set but psycopg is not installed") from e
            self.conn = psycopg.connect(DATABASE_URL, row_factory=dict_row)
        else:
            self.conn = sqlite3.connect(TRACKING_DB_FILE)
            self.conn.row_factory = sqlite3.Row
        return self

    def __exit__(self, exc_type, exc, tb):
        if not self.conn:
            return
        try:
            if exc_type:
                self.conn.rollback()
            else:
                self.conn.commit()
        finally:
            self.conn.close()

    def execute(self, sql: str, params: tuple = ()):
        if TRACKING_DB_IS_POSTGRES:
            sql = sql.replace("?", "%s")
        return self.conn.execute(sql, params)

def _tracking_conn():
    return _TrackingConnection()

def _ensure_column(conn, table: str, column: str, ddl: str):
    if TRACKING_DB_IS_POSTGRES:
        existing = {
            row["column_name"]
            for row in conn.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_name=? AND table_schema='public'
                """,
                (table,),
            ).fetchall()
        }
    else:
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")

def _row_to_dict(row) -> dict:
    data = dict(row)
    if "features" in data and isinstance(data["features"], str):
        try:
            data["features"] = json.loads(data["features"])
        except Exception:
            data["features"] = []
    return data

def init_tracking_db():
    with _tracking_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS packages (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                price_inr INTEGER NOT NULL,
                validity_days INTEGER NOT NULL,
                credits INTEGER NOT NULL DEFAULT 0,
                image_credits INTEGER NOT NULL DEFAULT 0,
                video_credits INTEGER NOT NULL DEFAULT 0,
                whatsapp_active INTEGER NOT NULL DEFAULT 0,
                chatbot_active INTEGER NOT NULL DEFAULT 0,
                payment_flow_active INTEGER NOT NULL DEFAULT 0,
                followup_active INTEGER NOT NULL DEFAULT 0,
                features TEXT NOT NULL DEFAULT '[]',
                active INTEGER NOT NULL DEFAULT 1
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS clients (
                id TEXT PRIMARY KEY,
                business_name TEXT NOT NULL,
                contact_name TEXT NOT NULL DEFAULT '',
                phone TEXT NOT NULL UNIQUE,
                email TEXT NOT NULL DEFAULT '',
                niche TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'lead',
                package_id TEXT NOT NULL DEFAULT '',
                package_name TEXT NOT NULL DEFAULT '',
                package_started_at TEXT NOT NULL DEFAULT '',
                package_expires_at TEXT NOT NULL DEFAULT '',
                credits_total INTEGER NOT NULL DEFAULT 0,
                credits_used INTEGER NOT NULL DEFAULT 0,
                image_credits_total INTEGER NOT NULL DEFAULT 0,
                image_credits_used INTEGER NOT NULL DEFAULT 0,
                video_credits_total INTEGER NOT NULL DEFAULT 0,
                video_credits_used INTEGER NOT NULL DEFAULT 0,
                whatsapp_active INTEGER NOT NULL DEFAULT 0,
                chatbot_active INTEGER NOT NULL DEFAULT 0,
                payment_flow_active INTEGER NOT NULL DEFAULT 0,
                followup_active INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS usage_logs (
                id TEXT PRIMARY KEY,
                client_id TEXT NOT NULL,
                order_id TEXT NOT NULL DEFAULT '',
                usage_type TEXT NOT NULL,
                quantity INTEGER NOT NULL DEFAULT 1,
                note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS package_payments (
                id TEXT PRIMARY KEY,
                client_id TEXT NOT NULL,
                package_id TEXT NOT NULL,
                order_id TEXT NOT NULL DEFAULT '',
                razorpay_payment_link_id TEXT NOT NULL DEFAULT '',
                razorpay_payment_id TEXT NOT NULL DEFAULT '',
                amount_inr INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                paid_at TEXT NOT NULL DEFAULT ''
            )
        """)
        _ensure_column(conn, "packages", "credits", "credits INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "clients", "credits_total", "credits_total INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "clients", "credits_used", "credits_used INTEGER NOT NULL DEFAULT 0")
        for package_id, package_data in PACKAGE_DEFS.items():
            conn.execute("""
                INSERT INTO packages (
                    id, name, price_inr, validity_days, credits, image_credits, video_credits,
                    whatsapp_active, chatbot_active, payment_flow_active, followup_active, features, active
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(id) DO UPDATE SET
                    name=excluded.name,
                    price_inr=excluded.price_inr,
                    validity_days=excluded.validity_days,
                    credits=excluded.credits,
                    image_credits=excluded.image_credits,
                    video_credits=excluded.video_credits,
                    whatsapp_active=excluded.whatsapp_active,
                    chatbot_active=excluded.chatbot_active,
                    payment_flow_active=excluded.payment_flow_active,
                    followup_active=excluded.followup_active,
                    features=excluded.features,
                    active=1
            """, (
                package_id,
                package_data["name"],
                package_data["price_inr"],
                package_data["validity_days"],
                package_data["credits"],
                package_data["image_credits"],
                package_data["video_credits"],
                package_data["whatsapp_active"],
                package_data["chatbot_active"],
                package_data["payment_flow_active"],
                package_data["followup_active"],
                json.dumps(package_data["features"]),
            ))

def _get_package(package_id: str) -> dict | None:
    with _tracking_conn() as conn:
        row = conn.execute("SELECT * FROM packages WHERE id=? AND active=1", (package_id,)).fetchone()
        return _row_to_dict(row) if row else None

def _get_client_by_phone(phone: str) -> dict | None:
    normalized = _normalize_phone(phone)
    if not normalized:
        return None
    with _tracking_conn() as conn:
        row = conn.execute("SELECT * FROM clients WHERE phone=?", (normalized,)).fetchone()
        return _row_to_dict(row) if row else None

def _get_client_by_id(client_id: str) -> dict | None:
    with _tracking_conn() as conn:
        row = conn.execute("SELECT * FROM clients WHERE id=?", (client_id,)).fetchone()
        return _row_to_dict(row) if row else None

def _upsert_client_from_order(order: dict) -> dict | None:
    phone = _normalize_phone(order.get("customer_phone", ""))
    if not phone:
        return None
    now = _utc_now_iso()
    existing = _get_client_by_phone(phone)
    with _tracking_conn() as conn:
        if existing:
            conn.execute("""
                UPDATE clients
                SET business_name=?, contact_name=?, email=?, updated_at=?
                WHERE id=?
            """, (
                order.get("image_brand_name") or order.get("video_brand_name") or existing["business_name"],
                order.get("customer_name", ""),
                _normalize_email(order.get("customer_email", "")) or existing.get("email", ""),
                now,
                existing["id"],
            ))
            return _get_client_by_id(existing["id"])
        client_id = str(uuid.uuid4())
        conn.execute("""
            INSERT INTO clients (
                id, business_name, contact_name, phone, email, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'lead', ?, ?)
        """, (
            client_id,
            order.get("image_brand_name") or order.get("video_brand_name") or order.get("customer_name") or "New Client",
            order.get("customer_name", ""),
            phone,
            order.get("customer_email", ""),
            now,
            now,
        ))
    return _get_client_by_id(client_id)

def _assign_package_to_client(client_id: str, package_id: str, note: str = "") -> dict:
    package = _get_package(package_id)
    if not package:
        raise HTTPException(status_code=404, detail="Package not found")
    now_dt = datetime.utcnow()
    expires_at = (now_dt + timedelta(days=int(package["validity_days"]))).strftime("%Y-%m-%dT%H:%M:%SZ")
    now = now_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    with _tracking_conn() as conn:
        cur = conn.execute("SELECT * FROM clients WHERE id=?", (client_id,))
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail="Client not found")
        conn.execute("""
            UPDATE clients SET
                status='active',
                package_id=?,
                package_name=?,
                package_started_at=?,
                package_expires_at=?,
                credits_total=?,
                credits_used=0,
                image_credits_total=?,
                image_credits_used=0,
                video_credits_total=?,
                video_credits_used=0,
                whatsapp_active=?,
                chatbot_active=?,
                payment_flow_active=?,
                followup_active=?,
                updated_at=?
            WHERE id=?
        """, (
            package_id,
            package["name"],
            now,
            expires_at,
            package["credits"],
            package["image_credits"],
            package["video_credits"],
            package["whatsapp_active"],
            package["chatbot_active"],
            package["payment_flow_active"],
            package["followup_active"],
            now,
            client_id,
        ))
        conn.execute("""
            INSERT INTO usage_logs (id, client_id, usage_type, quantity, note, created_at)
            VALUES (?, ?, 'package_assigned', 0, ?, ?)
        """, (str(uuid.uuid4()), client_id, note or f"Assigned {package['name']}", now))
    return _get_client_by_id(client_id)

def _apply_credit_pack_to_client(client_id: str, pack_id: str, note: str = "") -> dict:
    pack = CREDIT_PACK_DEFS.get(pack_id)
    if not pack:
        raise HTTPException(status_code=404, detail="Credit pack not found")
    client = _get_client_by_id(client_id)
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")
    now_dt = datetime.utcnow()
    current_expiry = client.get("package_expires_at") or ""
    try:
        base_expiry = datetime.strptime(current_expiry, "%Y-%m-%dT%H:%M:%SZ")
        if base_expiry < now_dt:
            base_expiry = now_dt
    except Exception:
        base_expiry = now_dt
    expires_at = (base_expiry + timedelta(days=int(pack["validity_days"]))).strftime("%Y-%m-%dT%H:%M:%SZ")
    now = now_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    with _tracking_conn() as conn:
        conn.execute("""
            UPDATE clients SET
                status='active',
                package_id=?,
                package_name=?,
                package_started_at=CASE WHEN package_started_at='' THEN ? ELSE package_started_at END,
                package_expires_at=?,
                credits_total=credits_total+?,
                updated_at=?
            WHERE id=?
        """, (
            pack_id,
            pack["name"],
            now,
            expires_at,
            int(pack["credits"]),
            now,
            client_id,
        ))
        conn.execute("""
            INSERT INTO usage_logs (id, client_id, usage_type, quantity, note, created_at)
            VALUES (?, ?, 'credit_pack_purchased', ?, ?, ?)
        """, (
            str(uuid.uuid4()),
            client_id,
            int(pack["credits"]),
            note or f"Purchased {pack['name']}",
            now,
        ))
    return _get_client_by_id(client_id)

def _client_credit_status(client: dict) -> dict:
    credits_left = max(0, int(client.get("credits_total") or 0) - int(client.get("credits_used") or 0))
    expires_at = client.get("package_expires_at") or ""
    active = client.get("status") == "active" and bool(expires_at) and expires_at >= _utc_now_iso()
    return {"active": active, "credits_left": credits_left}

def _client_display_package_name(client: dict) -> str:
    package_name = client.get("package_name") or ""
    package_id = client.get("package_id") or ""
    if package_id in CREDIT_PACK_DEFS:
        return "UGC Credit Wallet"
    if package_id == "food_creatives_999" and int(client.get("credits_total") or 0) != int(PACKAGE_DEFS["food_creatives_999"]["credits"]):
        return "UGC Credit Wallet"
    return package_name or "UGC Credit Wallet"

def _credit_cost_for_order(order: dict) -> int:
    return max(1, _estimate_order_amount_inr(order))

def _consume_client_credit(client_id: str, order: dict) -> tuple[bool, str]:
    client = _get_client_by_id(client_id)
    if not client:
        return False, "client_not_found"
    status = _client_credit_status(client)
    if not status["active"]:
        return False, "package_inactive_or_expired"
    usage_type = "image" if order.get("output_type") == "image" else "video"
    credits_to_use = _credit_cost_for_order(order)
    if status["credits_left"] < credits_to_use:
        return False, "not_enough_credits"
    now = _utc_now_iso()
    with _tracking_conn() as conn:
        legacy_col = "image_credits_used" if usage_type == "image" else "video_credits_used"
        conn.execute(
            f"UPDATE clients SET credits_used=credits_used+?, {legacy_col}={legacy_col}+1, updated_at=? WHERE id=?",
            (credits_to_use, now, client_id),
        )
        conn.execute("""
            INSERT INTO usage_logs (id, client_id, order_id, usage_type, quantity, note, created_at)
            VALUES (?, ?, ?, ?, 1, ?, ?)
        """, (
            str(uuid.uuid4()),
            client_id,
            order.get("id", ""),
            usage_type,
            f"Used {credits_to_use} wallet credit(s) for {usage_type}",
            now,
        ))
    return True, "credit_used"

def _estimate_order_amount_inr(order: dict) -> int:
    if order.get("output_type") == "image":
        return 49
    price = 599 if order.get("video_style") == "cinematic" else 499
    if str(order.get("video_duration") or "5") == "10":
        price += 200
    if order.get("presenter_source") == "uploaded":
        price += 100
    return price

def _public_base_url() -> str:
    return (PUBLIC_URL or RAILWAY_URL or "http://127.0.0.1:8000").rstrip("/")

async def _create_razorpay_payment_link(order: dict) -> dict:
    if not RAZORPAY_ENABLED:
        raise RuntimeError("Razorpay keys are not configured")
    amount_inr = _estimate_order_amount_inr(order)
    order_id = order.get("id", "")
    payload = {
        "amount": amount_inr * 100,
        "currency": "INR",
        "accept_partial": False,
        "description": f"Vishleshak {order.get('output_type', 'UGC')} order {order_id[:8]}",
        "reference_id": order_id,
        "customer": {
            "name": order.get("customer_name", ""),
            "contact": re.sub(r"\D", "", order.get("customer_phone", ""))[-10:],
            "email": order.get("customer_email", ""),
        },
        "notify": {"sms": False, "email": False},
        "reminder_enable": True,
        "callback_url": f"{_public_base_url()}/order/result/{order_id}",
        "callback_method": "get",
        "notes": {"order_id": order_id, "source": "vishleshak_ugc"},
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            "https://api.razorpay.com/v1/payment_links",
            auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET),
            json=payload,
        )
    if resp.status_code >= 400:
        raise RuntimeError(f"Razorpay payment link failed: {resp.text}")
    return resp.json()

async def _create_credit_pack_payment_link(client: dict, pack_id: str) -> dict:
    if not RAZORPAY_ENABLED:
        raise RuntimeError("Razorpay keys are not configured")
    pack = CREDIT_PACK_DEFS.get(pack_id)
    if not pack:
        raise HTTPException(status_code=404, detail="Credit pack not found")
    payment_id = str(uuid.uuid4())
    reference_id = f"cp_{payment_id.replace('-', '')[:24]}"
    payload = {
        "amount": int(pack["price_inr"]) * 100,
        "currency": "INR",
        "accept_partial": False,
        "description": f"Vishleshak {pack['name']}",
        "reference_id": reference_id,
        "customer": {
            "name": client.get("contact_name") or client.get("business_name") or "Vishleshak Customer",
            "contact": _normalize_phone(client.get("phone", "")),
            "email": client.get("email", ""),
        },
        "notify": {"sms": False, "email": False},
        "reminder_enable": True,
        "callback_url": f"{_public_base_url()}/order?credits=paid",
        "callback_method": "get",
        "notes": {
            "source": "vishleshak_credit_pack",
            "payment_id": payment_id,
            "client_id": client["id"],
            "pack_id": pack_id,
        },
    }
    async with httpx.AsyncClient(timeout=30.0) as client_http:
        resp = await client_http.post(
            "https://api.razorpay.com/v1/payment_links",
            auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET),
            json=payload,
        )
    if resp.status_code >= 400:
        raise RuntimeError(f"Razorpay credit pack link failed: {resp.text}")
    link = resp.json()
    now = _utc_now_iso()
    with _tracking_conn() as conn:
        conn.execute("""
            INSERT INTO package_payments (
                id, client_id, package_id, order_id, razorpay_payment_link_id,
                amount_inr, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
        """, (
            payment_id,
            client["id"],
            pack_id,
            reference_id,
            link.get("id", ""),
            int(pack["price_inr"]),
            now,
        ))
    return {"payment_id": payment_id, "payment_url": link.get("short_url", ""), "pack": pack}

MODEL_DIR  = os.path.join(os.path.dirname(__file__), "model")
VIDEOS_DIR = os.path.join(os.path.dirname(__file__), "static", "videos")
IMAGES_DIR = os.path.join(os.path.dirname(__file__), "static", "images")
RAW_IMAGES_DIR = os.path.join(os.path.dirname(__file__), "static", "images", "raw")
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
VIDEO_EXTS = {".mp4", ".mov", ".webm", ".m4v"}
SAMPLE_IMAGES_DIR = os.getenv("SAMPLE_IMAGES_DIR", r"D:\Sample Images")
SAMPLE_VIDEOS_DIR = os.getenv("SAMPLE_VIDEOS_DIR", r"D:\Sample Videos")
CLOUDINARY_SAMPLE_IMAGE_PREFIX = os.getenv("CLOUDINARY_SAMPLE_IMAGE_PREFIX", "vishleshak-samples/images")
CLOUDINARY_SAMPLE_VIDEO_PREFIX = os.getenv("CLOUDINARY_SAMPLE_VIDEO_PREFIX", "vishleshak-samples/videos")

# "admin" on local machine, "client" on Railway (set APP_MODE=client env var)
APP_MODE = os.getenv("APP_MODE", "admin")
RAILWAY_URL = os.getenv("RAILWAY_URL", "")

HISTORY_FILE = os.path.join(os.path.dirname(__file__), "video_history.json")

os.makedirs(VIDEOS_DIR, exist_ok=True)
os.makedirs(IMAGES_DIR, exist_ok=True)
os.makedirs(RAW_IMAGES_DIR, exist_ok=True)
os.makedirs(MODEL_DIR, exist_ok=True)
init_tracking_db()

def _sample_media_files(kind: str) -> list[dict]:
    """Return sample media from the external library, falling back to bundled files."""
    base_dir = SAMPLE_IMAGES_DIR if kind == "image" else SAMPLE_VIDEOS_DIR
    fallback_dir = os.path.join(os.path.dirname(__file__), "static", "images" if kind == "image" else "samples")
    extensions = IMAGE_EXTS if kind == "image" else VIDEO_EXTS
    cloudinary_prefix = CLOUDINARY_SAMPLE_IMAGE_PREFIX if kind == "image" else CLOUDINARY_SAMPLE_VIDEO_PREFIX
    cloudinary_items = _cloudinary_sample_media_files(kind, cloudinary_prefix)
    if cloudinary_items:
        return cloudinary_items

    if os.path.isdir(base_dir):
        items = []
        for name in os.listdir(base_dir):
            path = os.path.join(base_dir, name)
            if os.path.isfile(path) and os.path.splitext(name)[1].lower() in extensions:
                items.append({
                    "name": name,
                    "url": f"/api/sample-media/{kind}/{quote(name)}",
                    "source": "library",
                })
        if items:
            return sorted(items, key=lambda item: item["name"].lower())

    items = []
    if os.path.isdir(fallback_dir):
        static_folder = "images" if kind == "image" else "samples"
        for name in os.listdir(fallback_dir):
            path = os.path.join(fallback_dir, name)
            if os.path.isfile(path) and os.path.splitext(name)[1].lower() in extensions:
                items.append({
                    "name": name,
                    "url": f"/static/{static_folder}/{quote(name)}",
                    "source": "bundled",
                })
    return sorted(items, key=lambda item: item["name"].lower())


def _cloudinary_sample_media_files(kind: str, prefix: str) -> list[dict]:
    if not _CLOUDINARY_READY or not prefix:
        return []

    try:
        resource_type = "image" if kind == "image" else "video"
        result = cloudinary.api.resources(
            type="upload",
            resource_type=resource_type,
            prefix=prefix.rstrip("/") + "/",
            max_results=100,
        )
        items = []
        for item in result.get("resources", []):
            url = item.get("secure_url") or item.get("url")
            public_id = item.get("public_id") or ""
            filename = public_id.rsplit("/", 1)[-1] or item.get("asset_id", "sample")
            if url:
                items.append({
                    "name": filename,
                    "url": url,
                    "source": "cloudinary",
                })
        return sorted(items, key=lambda item: item["name"].lower())
    except Exception as e:
        print(f"[Cloudinary samples] list failed for {kind}: {e}")
        return []


def _sample_caption(filename: str) -> str:
    base = os.path.splitext(filename)[0]
    cleaned = " ".join(base.replace("_", " ").replace("-", " ").split())
    if cleaned.lower().startswith("vishleshak ugc"):
        return "UGC Sample"
    if cleaned.lower().startswith("kie ai"):
        return "AI Video Sample"
    return cleaned[:40] or "Sample"


def _sample_media_payload(kind: str, count: int = 6) -> dict:
    items = _sample_media_files(kind)
    random.SystemRandom().shuffle(items)
    count = max(1, min(count, 24))
    selected = items[:count]
    for item in selected:
        item["caption"] = _sample_caption(item["name"])
    return {"items": selected, "count": len(selected)}


# ── History helpers ────────────────────────────────────────────────────────────

def load_history() -> list:
    """Load video history from JSON file."""
    if not os.path.exists(HISTORY_FILE):
        return []
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def save_to_history(entry: dict):
    """Append a completed video entry to the history file."""
    history = load_history()
    # Remove any existing entry with the same id
    history = [h for h in history if h.get("id") != entry.get("id")]
    history.insert(0, entry)  # newest first
    # Keep only last 100 entries
    history = history[:100]
    try:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"Warning: could not save history: {e}")


@app.on_event("startup")
async def load_model_on_startup():
    global model_image_bytes
    # Load model photo from disk if it exists — do NOT delete it on restart
    if os.path.exists(MODEL_LOCAL_PATH):
        with open(MODEL_LOCAL_PATH, "rb") as f:
            model_image_bytes = f.read()
        print(f"[startup] Model photo loaded from disk ({len(model_image_bytes)//1024} KB)")

    # Rebuild history from existing MP4 files that aren't already tracked
    history = load_history()
    tracked_ids = {h.get("id") for h in history}
    for fname in os.listdir(VIDEOS_DIR):
        if not fname.endswith(".mp4"):
            continue
        job_id = fname[:-4]
        if job_id in tracked_ids:
            continue
        fpath = os.path.join(VIDEOS_DIR, fname)
        mtime = os.path.getmtime(fpath)
        date_str = datetime.utcfromtimestamp(mtime).strftime("%Y-%m-%dT%H:%M:%SZ")
        save_to_history({
            "id": job_id,
            "date": date_str,
            "script": "",
            "video_url": f"/static/videos/{fname}",
            "product_type": "other",
            "language": "hindi",
        })


# ── Image helpers ─────────────────────────────────────────────────────────────

def _to_jpeg_bytes(image_bytes: bytes) -> bytes:
    """Convert any image format to JPEG bytes using Pillow. Safe fallback."""
    try:
        from PIL import Image as PilImage
        import io
        img = PilImage.open(io.BytesIO(image_bytes))
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=92)
        return buf.getvalue()
    except Exception as e:
        print(f"[to_jpeg] conversion failed: {e} — using original")
        return image_bytes


async def upload_image_to_public(image_bytes: bytes, fname: str, mime: str) -> str:
    """Upload image to imgbb. Returns a permanent public URL."""
    img_b64 = base64.b64encode(image_bytes).decode()
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(
            "https://api.imgbb.com/1/upload",
            data={"key": IMGBB_API_KEY, "image": img_b64, "name": fname},
        )
    data = resp.json()
    if not data.get("success"):
        raise Exception(f"imgbb upload failed: {data}")
    return data["data"]["url"]


async def upload_image_to_kie(image_bytes: bytes, fname: str, mime: str) -> str:
    """Upload image to kie.ai's own CDN (required for 4o Image API). Returns a temporary URL valid for 3 days."""
    # Always upload as JPEG — kie.ai rejects HEIC and other exotic formats
    image_bytes = _to_jpeg_bytes(image_bytes)
    mime = "image/jpeg"
    fname = fname.rsplit(".", 1)[0] + ".jpg"
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(
            "https://kieai.redpandaai.co/api/file-stream-upload",
            headers={"Authorization": f"Bearer {KIE_API_KEY}"},
            files={"file": (fname, image_bytes, mime)},
            data={"uploadPath": "ugc-tool"},
        )
    data = resp.json()
    url = data.get("data", {}).get("downloadUrl", "")
    if not url:
        raise Exception(f"kie.ai file upload failed: {data}")
    return url


SCENE_BACKGROUNDS = {
    "studio":  "professional grey studio background with soft studio lighting",
    "clinic":  "premium modern clinic or pharmacy counter with clean white-blue medical lighting, subtle medical shelves, and a trustworthy healthcare mood",
    "beach":   "sunny beach with ocean waves and clear blue sky in the background",
    "ramp":    "high-fashion runway with dramatic spotlights and blurred audience",
    "cafe":    "cozy modern cafe with warm ambient lighting and bokeh background",
    "garden":  "beautiful garden with flowers and soft natural sunlight",
    "outdoor": "vibrant Indian market street scene, golden hour natural light",
}

SKIN_DESCRIPTORS = {
    "fair":     "fair/light complexioned skin",
    "wheatish": "natural wheatish Indian skin tone",
    "dusky":    "dusky/olive dark skin tone",
    "dark":     "deep dark brown skin tone",
}

PRODUCT_ACTION_GUIDE = (
    "  - Necklace/bridal set -> necklace sits ON collarbone/skin, model touches pendant gently, slow elegant neck turn, no floating jewelry\n"
    "  - Ring -> close-up hand pose, model rotates fingers under light, ring worn correctly on finger, sparkle visible\n"
    "  - Earrings -> model tucks hair behind ear, turns head slowly, earrings attached to earlobes naturally\n"
    "  - Bangles/bracelet -> model adjusts bangles on wrist, soft hand movement, jewelry rests on wrist naturally\n"
    "  - Watch -> close-up wrist shot, model fastens strap, checks time, confident wrist turn toward camera\n"
    "  - Wallet -> model opens wallet, shows premium finish, places card or cash inside, slips it into pocket or bag\n"
    "  - Handbag/purse/tote/clutch -> carries on shoulder or forearm, opens and peeks inside, poses with bag at side\n"
    "  - Sunglasses -> model wears sunglasses, adjusts frame with both hands, slight head turn, confident smile\n"
    "  - Perfume -> model sprays on wrist or neck, smells wrist, smiles elegantly, bottle label visible\n"
    "  - Clothing/saree/lehenga/dress -> model wears it, walks or twirls once, shows fabric and fit naturally\n"
    "  - Footwear/shoes/sandals/heels -> model walks, crosses legs or points toe, footwear clearly visible\n"
    "  - Phone/gadget/electronics -> model holds device naturally, taps or uses it, reacts with satisfaction\n"
    "  - Beauty/skincare -> model applies small amount on cheek/hand, gentle massage, glowing smile\n"
    "  - Makeup -> model applies product with mirror-like gaze, smiles confidently, product visible in hand\n"
    "  - Medical/surgical/stethoscope -> presenter wears clean doctor attire or white coat, holds/uses product professionally in clinic setting, trusted healthcare mood\n"
    "  - Food/snacks -> model presents pack/plate, picks one piece, smiles warmly, no messy eating\n"
    "  - Beverage -> model holds cup/bottle, brings close to lips, enjoys aroma/taste, label visible\n"
    "  - Home decor -> model places product carefully, steps back, admires the room styling\n"
    "  - Fitness/sports equipment -> model demonstrates use briefly, energetic pose, product in action\n"
    "  - Kids product -> child or parent presents product playfully, safe joyful motion, big smile\n"
    "  - Stationery/books -> model opens/flips pages, holds cover to camera, thoughtful nod\n"
    "  - Organic/herbal -> model holds jar/bottle, opens and smells, nods approvingly, natural setting\n"
    "  - Other -> model holds product at chest level, looks at product then camera, confident smile"
)

PRODUCT_TYPE_LIST = (
    "'necklace','ring','earrings','bangle','watch','wallet','handbag','sunglasses','perfume',"
    "'clothing','footwear','electronics','skincare','makeup','medical','food','beverage','home_decor',"
    "'fitness','sports_equipment','kids','stationery','organic','jewelry','bag','accessory','other'"
)

PRODUCT_FALLBACK_PROMPTS = {
    "necklace": "wearing necklace on collarbone, touches pendant gently, turns neck elegantly, smiles",
    "ring": "wearing ring on finger, rotates hand under light, shows sparkle close to camera",
    "earrings": "wearing earrings, tucks hair behind ear, turns head slowly, smiles elegantly",
    "bangle": "adjusts bangles on wrist, soft hand movement, jewelry resting naturally",
    "watch": "fastens watch strap, checks time, turns wrist confidently toward camera",
    "wallet": "opens wallet, places card inside, shows premium finish, slips it into pocket",
    "handbag": "carries handbag on shoulder, opens and peeks inside with smile, poses confidently",
    "sunglasses": "wears sunglasses, adjusts frame with both hands, slight head turn, confident smile",
    "perfume": "sprays perfume on wrist, smells wrist, smiles elegantly with bottle visible",
    "jewelry": "wearing jewelry, turns head slowly to show, touches gently with fingertips, smiles elegantly",
    "clothing": "wearing outfit, twirls once showing fabric, strikes confident pose, smiles at camera",
    "bag": "carries bag on shoulder, opens and peeks inside with smile, poses confidently",
    "footwear": "walks elegantly showing footwear, crosses legs to show shoes, smiles at camera",
    "accessory": "wears accessory confidently, adjusts with both hands, poses and smiles",
    "skincare": "applies product on cheek with fingertip, gently massages in, glows and smiles",
    "makeup": "applies makeup product, looks in imaginary mirror, smiles confidently at camera",
    "medical": "wears clean doctor attire or white coat, holds the medical product professionally in a clinic setting, trustworthy expression",
    "food": "gestures toward food with open hand, picks one piece and holds it up, smiles warmly",
    "beverage": "holds cup with both hands, brings close to lips, closes eyes enjoying the aroma",
    "electronics": "holds device naturally, uses it confidently, reacts with excitement",
    "home_decor": "places product carefully, steps back, tilts head admiring it with warm smile",
    "fitness": "holds product with energy, demonstrates use briefly, looks strong and motivated",
    "sports_equipment": "uses sports product in action, energetic pose, confident game-ready expression",
    "kids": "holds product up playfully, shows with joy and big smile, waves at camera",
    "stationery": "opens and flips pages, holds up cover facing camera, nods thoughtfully",
    "organic": "holds bottle or jar up, opens and smells with delight, nods approvingly",
    "other": "holds product naturally at chest level, looks at it then at camera, smiles confidently",
}

VIDEO_ACTION_GUIDE = {
    "sports_equipment": "keeps the sports product close to camera, performs a small controlled demo move, then celebrates while the product remains visible",
    "sports": "keeps the sports product close to camera, performs a small controlled demo move, then celebrates while the product remains visible",
    "jewelry": "gently turns toward the light and touches the jewelry so it catches highlights without changing design",
    "necklace": "gently turns toward the light and touches the necklace so it catches highlights without changing design",
    "ring": "slowly raises the hand close to camera and rotates slightly so the ring stays sharp and visible",
    "earrings": "slowly turns the head and smiles so the earrings stay visible and sparkling",
    "watch": "raises the wrist close to camera, gently rotates it, and smiles confidently",
    "wallet": "holds the wallet close to camera, opens it slightly, then presents it clearly",
    "handbag": "carries the handbag naturally, then lifts it slightly toward camera so the shape stays clear",
    "perfume": "holds the bottle close to camera and makes a soft spray gesture while the bottle remains visible",
    "clothing": "makes a small pose and fabric-detail gesture while the clothing stays clearly visible",
    "electronics": "holds the device close, taps once, and looks impressed while the device remains visible",
    "food": "holds the food close, smiles, and makes a small tasting gesture without hiding the product",
    "beverage": "holds the drink label-facing, takes a small sip, and smiles while the packaging remains visible",
    "other": "presents the product close to camera with small natural movement while keeping it visible",
}


def _clean_image_generation_text(value: str) -> str:
    """Remove admin/branding metadata before sending a visual prompt to KIE."""
    if not value:
        return ""
    blocked_prefixes = (
        "ad goal:",
        "tone:",
        "platform:",
        "presenter preference:",
        "image brand overlay",
        "brand name:",
        "mobile / whatsapp:",
        "mobile:",
        "whatsapp:",
        "image offer text:",
        "offer text:",
        "image cta text:",
        "cta text:",
        "video end card",
        "end card brand name:",
        "end card mobile",
        "end card details:",
        "ending cta:",
    )
    cleaned = []
    for raw_line in value.replace("\r", "\n").split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        lower = line.lower()
        if lower.startswith("creative notes:"):
            note = line.split(":", 1)[1].strip()
            if note:
                cleaned.append(note)
            continue
        if any(lower.startswith(prefix) for prefix in blocked_prefixes):
            continue
        cleaned.append(line)
    return " ".join(cleaned).strip()


def _build_image_text_guidance(customization: dict) -> str:
    """Tell the image model which marketing text it may render."""
    if customization.get("output_type") != "image":
        return (
            "Do not add any new text, captions, slogans, phone numbers, prices, badges, logos, watermarks, poster typography, "
            "brand graphics, call-to-action text, or decorative lettering anywhere in the generated image. "
            "Only preserve text or logos that already exist physically on the uploaded product itself."
        )

    branding = customization.get("image_branding") or {}
    brand = (branding.get("brand_name") or "").strip()
    mobile = _format_display_phone(branding.get("brand_mobile") or "")
    offer = (branding.get("offer_text") or "").strip()
    cta = (branding.get("cta_text") or "").strip()
    provided_lines = [text for text in (offer, cta) if text]

    rules = [
        "Image text priority rule: any readable marketing text must respect the client's fields first.",
        "Use clean, tasteful social-ad typography that matches the image mood and does not cover the product or face.",
    ]
    if brand:
        rules.append(f"Brand name must be exactly: \"{brand}\". Do not invent, rename, abbreviate, or replace the brand.")
    else:
        rules.append("Do not invent a new brand name, logo, or fake company name.")

    if provided_lines:
        rules.append(
            "Client-provided tagline/CTA has priority; use only this wording if adding tagline or CTA text: "
            + " | ".join(f"\"{line}\"" for line in provided_lines)
        )
    else:
        rules.append(
            "If it improves the ad, you may create one short tasteful tagline or CTA that suits the product category."
        )

    if mobile:
        rules.append(
            f"If showing contact details, display this exact mobile/WhatsApp number only: \"{mobile}\". "
            "A small phone or WhatsApp icon is allowed if it matches the design."
        )
    else:
        rules.append("Do not add any phone number or contact details.")

    rules.append(
        "Do not add any other readable text, fake logo, watermark, price, QR code, or extra brand graphic. "
        "Only preserve text or logos that already exist physically on the uploaded product itself."
    )
    return " ".join(rules)


def _format_display_phone(value: str) -> str:
    """Format Indian 10-digit mobile numbers for cleaner ad display."""
    raw = (value or "").strip()
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    if len(digits) == 10:
        return f"{digits[:5]}-{digits[5:]}"
    return raw


def _combined_generation_text(customization: dict | None = None, *values: str) -> str:
    c = customization or {}
    branding = c.get("image_branding") or {}
    end_card = c.get("video_end_card") or {}
    parts = [
        c.get("model_action", ""),
        c.get("custom_instructions", ""),
        c.get("custom_script", ""),
        branding.get("brand_name", ""),
        branding.get("offer_text", ""),
        branding.get("cta_text", ""),
        end_card.get("brand_name", ""),
        end_card.get("details", ""),
        end_card.get("cta_text", ""),
        *values,
    ]
    return " ".join(str(part or "") for part in parts).lower()


def _force_product_type(product_type: str, text: str) -> str:
    """Use explicit user/product wording to recover from weak AI classification."""
    normalized = (text or "").lower()
    if re.search(r"\b(stethoscope|surgical|medical|doctor|clinic|healthcare|diagnostic|diagnosis)\b", normalized):
        return "medical"
    return (product_type or "other").lower()


def _force_scene_for_product(scene: str, product_type: str, text: str) -> str:
    if _force_product_type(product_type, text) == "medical":
        return "clinic"
    return scene


PERSON_RESTRICTED_PRODUCT_TERMS = (
    "condom",
    "condoms",
    "contraceptive",
    "sexual wellness",
    "adult product",
    "adult toy",
    "intimate product",
    "lingerie",
    "bra",
    "panty",
    "panties",
    "underwear",
    "bikini",
    "swimwear",
    "swim wear",
    "swimsuit",
    "swim suit",
    "swimming costume",
)


def _person_restricted_reason(*values: str) -> str:
    text = " ".join(str(value or "") for value in values).lower()
    for term in PERSON_RESTRICTED_PRODUCT_TERMS:
        pattern = r"\b" + re.escape(term).replace(r"\ ", r"[\s-]?") + r"\b"
        if re.search(pattern, text, flags=re.I):
            return term
    return ""


def _enforce_person_restricted_product_policy(
    presenter_source: str,
    has_model_reference: bool,
    *values: str,
) -> None:
    reason = _person_restricted_reason(*values)
    if not reason:
        return
    if presenter_source == "uploaded" or has_model_reference:
        raise HTTPException(
            status_code=400,
            detail=(
                "For swimwear, intimate, condoms, and sexual-wellness products, "
                "uploaded/reference person photos are not allowed. Please choose AI generated or Product only."
            ),
        )


async def generate_model_with_product(
    model_url: str,
    product_bytes: bytes,
    product_mime: str,
    product_type: str,
    avatar_prompt: str,
    customization: dict | None = None,
) -> str:
    """
    Use 4o Image API to generate a realistic photo of the model
    actually wearing / holding / using the product.
    Returns a public URL of the generated image.
    """
    c = customization or {}
    presenter_source = c.get("presenter_source", "uploaded")
    gender           = c.get("model_gender", "female")
    skin_tone        = c.get("skin_tone", "wheatish")
    scene            = c.get("scene", "studio")
    custom_scene     = c.get("custom_scene", "").strip()
    model_action     = _clean_image_generation_text(c.get("model_action", "").strip())
    custom_instr     = _clean_image_generation_text(c.get("custom_instructions", "").strip())
    aspect_ratio     = c.get("aspect_ratio", "9:16")
    image_branding   = c.get("image_branding") or {}
    video_end_card   = c.get("video_end_card") or {}
    generation_text  = _combined_generation_text(c, avatar_prompt, product_type)
    product_type     = _force_product_type(product_type, generation_text)
    scene            = _force_scene_for_product(scene, product_type, generation_text)
    _enforce_person_restricted_product_policy(
        presenter_source,
        bool(c.get("order_model_path")),
        c.get("model_action", ""),
        c.get("custom_instructions", ""),
        avatar_prompt,
        image_branding.get("brand_name", ""),
        image_branding.get("offer_text", ""),
        image_branding.get("cta_text", ""),
        video_end_card.get("brand_name", ""),
        video_end_card.get("details", ""),
        video_end_card.get("cta_text", ""),
    )

    # KIE's 4o Image endpoint rejects raw video ratios like 9:16.
    # Use provider-safe still-image ratios, then FFmpeg handles final video padding.
    _SIZE_MAP = {"9:16": "2:3", "16:9": "3:2", "1:1": "1:1"}
    _FRAME_MAP = {
        "9:16": "Vertical 9:16 portrait frame",
        "16:9": "Horizontal 16:9 landscape frame",
        "1:1":  "Square 1:1 frame",
    }
    kie_image_size = _SIZE_MAP.get(aspect_ratio, "9:16")
    frame_desc     = _FRAME_MAP.get(aspect_ratio, "Vertical 9:16 portrait frame")

    # Resolve background description
    if scene == "custom" and custom_scene:
        background_desc = custom_scene
    else:
        background_desc = SCENE_BACKGROUNDS.get(scene, SCENE_BACKGROUNDS["studio"])

    skin_desc = SKIN_DESCRIPTORS.get(skin_tone, SKIN_DESCRIPTORS["wheatish"])
    if gender == "girl_kid":
        gender_word = "girl"
        gender_adj  = "Indian girl child, 6-10 years old"
    elif gender == "boy_kid":
        gender_word = "boy"
        gender_adj  = "Indian boy child, 6-10 years old"
    elif gender == "male":
        gender_word = "man"
        gender_adj  = "Indian male model"
    else:
        gender_word = "woman"
        gender_adj  = "Indian female model"

    EYES_OPEN = "eyes fully open, making direct eye contact with camera, bright and alert expression."
    PRODUCT_LOCK = (
        "Critical product identity rule: reproduce the exact product from the uploaded product image, "
        "including its color, shape, logo/markings, texture, proportions, and visible details. "
        "Do not substitute it with a generic object, different ball, different jewelry, different packaging, "
        "or a similar-looking product. The product must remain the hero object and be clearly visible."
    )
    IMAGE_TEXT_GUIDANCE = _build_image_text_guidance(c)
    MODEL_LOCK = (
        "Critical reference-person identity rule: the first uploaded image is the exact person to preserve. "
        "Preserve the real phone-photo likeness: same face structure, age, body proportions, natural expression, "
        "skin tone, hairstyle, and hair accessories. Commercial polish is allowed: cleaner lighting, sharper photo quality, "
        "better background, neat grooming, and ad-ready styling are fine as long as the person still looks like the same real person "
        "with a recognizable expression from the reference image. If the reference person is a child, keep them as a child; "
        "do not adultify, replace, or change them into a generic model. Do not change gender or age. "
        "Wardrobe may change when it helps sell the product, but it must stay logical and product-led: "
        "if the product is clothing, the same person should wear that clothing category naturally (lehenga as lehenga, saree as saree, suit as suit, dress as dress); "
        "if the product is medical/surgical such as a stethoscope, doctor coat or clean medical attire is allowed; "
        "if the product is a bag, keep normal outfit styling and show the bag carried, worn, opened, or used naturally. "
        "Do not randomly change clothing into an unrelated category. "
        "Only adjust pose/composition enough to naturally include the product."
    )

    CATEGORY_STYLE_GUIDE = {
        "medical": (
            "MANDATORY healthcare ad composition: premium clinic or pharmacy counter, clean white-blue trust palette. "
            "The presenter MUST wear a doctor coat or clean medical attire. If the preserved reference person is a child, make it a tasteful 'little doctor' ad concept: "
            "same child identity, child remains a child, wearing a clean white doctor coat over neat clothes, holding or wearing the stethoscope professionally. "
            "Do not keep the original casual dress as the main visible outfit. Do not show a plain grey wall. Do not make the stethoscope feel like a toy."
        ),
        "food": (
            "Food ad composition: warm appetizing close-up, steam/garnish/table styling, realistic kitchen or cafe context, "
            "product/dish looks fresh and order-worthy. Avoid flat grey backgrounds."
        ),
        "bag": (
            "Bag/fashion ad composition: lifestyle boutique, school, cafe, travel, or street context as suitable. "
            "Show the bag hanging on shoulder/arm, being opened, or with books/items being placed inside. Product must feel useful and stylish."
        ),
        "handbag": (
            "Bag/fashion ad composition: lifestyle boutique, cafe, travel, or street context. "
            "Show the handbag carried naturally, opened, or styled with outfit. Product should not look pasted beside the model."
        ),
        "wallet": (
            "Wallet ad composition: close-up lifestyle or desk/travel context. "
            "Show the wallet opened, with card or cash placed inside, premium finish visible."
        ),
        "accessory": (
            "Accessory ad composition: wearable lifestyle styling. "
            "Show the accessory being worn or adjusted naturally, with product detail visible."
        ),
        "clothing": (
            "Fashion ad composition: the same person should wear the uploaded clothing category naturally. "
            "Lehenga remains lehenga, saree remains saree, suit remains suit, dress remains dress. Show fit, fabric, and pattern clearly."
        ),
        "footwear": (
            "Footwear ad composition: show the footwear worn correctly on feet, walking or posed naturally, with product design clearly visible."
        ),
        "jewelry": (
            "Jewelry ad composition: premium Indian festive or luxury lighting. Jewelry must sit correctly on the body, "
            "not float or paste over skin."
        ),
        "kids": (
            "Kids product ad composition: bright, safe, cheerful, playful but premium. Child interacts naturally with the product; avoid clutter."
        ),
        "skincare": (
            "Beauty ad composition: soft vanity or bathroom styling, clean packaging close-up, glowing natural skin, premium minimal props."
        ),
        "makeup": (
            "Beauty ad composition: soft vanity or studio lighting, product in hand or near mirror, polished but realistic skin."
        ),
        "electronics": (
            "Tech ad composition: modern desk, home, or office context, clean lighting, product used naturally, no fake UI clutter."
        ),
        "home_decor": (
            "Home decor ad composition: styled room scene, cozy interior light, product placed naturally as the room accent."
        ),
        "fitness": (
            "Fitness ad composition: energetic gym or outdoor context, product in use, strong pose, clear product visibility."
        ),
        "sports_equipment": (
            "Sports ad composition: active outdoor or training context, product in use, energetic pose, clear product visibility."
        ),
    }

    def build_prompt(base_action: str) -> str:
        action = model_action if model_action else base_action
        category_style = CATEGORY_STYLE_GUIDE.get(product_type, "")
        category_sentence = f" Category-specific creative direction: {category_style}" if category_style else ""
        if presenter_source == "product":
            p = (
                f"Premium product-only advertising image. "
                f"Use the uploaded product as the exact hero object with no human presenter, no face, and no hands unless essential for scale. "
                f"Action or composition: {action}. "
                f"{PRODUCT_LOCK} "
                f"Background: {background_desc}. "
                f"{category_sentence} "
                f"Clean commercial lighting, realistic shadows, natural reflections, high-end catalog and social ad quality. "
                f"{frame_desc}. {IMAGE_TEXT_GUIDANCE} No replacement packaging."
            )
        elif presenter_source == "ai":
            p = (
                f"Professional UGC creator photo for Instagram Reels. "
                f"Subject: a real-looking {gender_adj}, 24-28 years old, {skin_desc}, "
                f"naturally beautiful with subtle makeup, styled hair, wearing a stylish casual Indian outfit. "
                f"Action: {action}. "
                f"The product from the uploaded image must be clearly visible, held or worn naturally — not floating, not pasted on. "
                f"{PRODUCT_LOCK} "
                f"Background: {background_desc}. "
                f"{category_sentence} "
                f"Shot on Sony A7III, 85mm f/1.8 lens, shallow depth of field, soft bokeh background. "
                f"Soft diffused lighting with natural skin highlights. "
                f"Hyper-realistic skin texture, visible pores, natural imperfections — NOT AI-looking, NOT plastic skin, NOT CGI. "
                f"Real human face with natural asymmetry. {frame_desc}. "
                f"Ultra high quality, 8K, magazine-grade photography. {EYES_OPEN} {IMAGE_TEXT_GUIDANCE}"
            )
        else:
            p = (
                f"Using the first image as the exact reference person and the second image as the product, "
                f"generate a photorealistic ad image of the same person "
                f"{action}. "
                f"{MODEL_LOCK} "
                f"{PRODUCT_LOCK} "
                f"Background: {background_desc}. "
                f"{category_sentence} "
                f"Do not invent a different model, different face, different age, different hairstyle, different outfit category, or different body. "
                f"{EYES_OPEN} High quality. {IMAGE_TEXT_GUIDANCE}"
            )
        if custom_instr:
            p += f" Additional: {custom_instr}."
        return p

    INTERACTION_PROMPTS = {
        **{key: build_prompt(action) for key, action in PRODUCT_FALLBACK_PROMPTS.items()},
        "jewelry": build_prompt(
            "wearing this jewelry naturally — necklace/bracelet/earrings properly placed on the body, "
            "sitting ON the skin, not floating"
        ),
        "clothing": build_prompt(
            "wearing this clothing item, garment fitting naturally on the body, posing and showing it off"
        ),
        "food": build_prompt(
            "holding this food dish with both hands, smiling with excitement, ready to eat"
        ),
        "sports": build_prompt(
            "holding this sports equipment naturally in an active confident pose"
        ),
        "electronics": build_prompt(
            "holding this device naturally in hand, looking at it with excitement"
        ),
        "other": build_prompt(avatar_prompt or "holding the product naturally, smiling at camera"),
    }

    prompt = INTERACTION_PROMPTS.get(product_type, INTERACTION_PROMPTS["other"])

    # Upload both images to kie.ai CDN (catbox.moe is blocked by 4o Image API)
    ext = product_mime.split("/")[-1] if "/" in product_mime else "jpg"
    product_kie_url = await upload_image_to_kie(product_bytes, f"product.{ext}", product_mime)
    files_url = [product_kie_url]

    # Read model photo — priority: order-specific → global admin model → AI fallback
    local_model_bytes = None
    if presenter_source not in ("ai", "product"):
        order_model_path = c.get("order_model_path", "")
        if order_model_path and os.path.exists(order_model_path):
            # Customer uploaded their own photo
            with open(order_model_path, "rb") as f:
                local_model_bytes = f.read()
            print(f"[composite] Using customer model photo: {order_model_path}")
        elif model_image_bytes:
            # Use the admin's uploaded model
            local_model_bytes = model_image_bytes
        elif os.path.exists(MODEL_LOCAL_PATH):
            with open(MODEL_LOCAL_PATH, "rb") as f:
                local_model_bytes = f.read()
        else:
            # No model photo anywhere — fall back to AI mode
            print(f"[composite] No model photo found — falling back to AI mode")
            presenter_source = "ai"

    if presenter_source not in ("ai", "product") and local_model_bytes:
        model_kie_url = await upload_image_to_kie(local_model_bytes, "model.jpg", "image/jpeg")
        files_url = [model_kie_url, product_kie_url]

    async def submit_image_task(size: str) -> dict:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                "https://api.kie.ai/api/v1/gpt4o-image/generate",
                headers={"Authorization": f"Bearer {KIE_API_KEY}", "Content-Type": "application/json"},
                json={
                    "prompt": prompt,
                    "size": size,
                    "nVariants": 1,
                    "isEnhance": False,
                    "filesUrl": files_url,
                },
            )
        return resp.json()

    data = await submit_image_task(kie_image_size)
    if data.get("code") != 200 and "size" in str(data.get("msg", "")).lower() and kie_image_size != "2:3":
        print(f"[4o-image] size {kie_image_size} rejected, retrying with 2:3")
        data = await submit_image_task("2:3")
    if data.get("code") != 200:
        raise Exception(f"4o Image API error: {data.get('msg')}")

    task_id = data["data"]["taskId"]

    # Poll until done — image generation with reference images can take up to 10 min
    for _ in range(120):
        await asyncio.sleep(5)
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                r = await client.get(
                    "https://api.kie.ai/api/v1/gpt4o-image/record-info",
                    headers={"Authorization": f"Bearer {KIE_API_KEY}"},
                    params={"taskId": task_id},
                )
            info = r.json().get("data", {})
            status = info.get("status", "")
            success_flag = info.get("successFlag", 0)

            if status == "SUCCESS" or success_flag == 1:
                urls = info.get("response", {}).get("resultUrls", [])
                if urls:
                    return urls[0]
                raise Exception("4o Image API returned no image URL")
            if status in ("FAILED", "FAIL", "ERROR"):
                raise Exception(f"4o Image API task failed (id={task_id}): {info.get('errorMessage', '')}")
        except httpx.TimeoutException:
            continue  # transient timeout, keep polling

    raise Exception("4o Image API timed out after 10 minutes")


# ── Video helper ──────────────────────────────────────────────────────────────

ASPECT_RATIO_FILTERS = {
    "9:16":  "scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black",
    "1:1":   "scale=1080:1080:force_original_aspect_ratio=decrease,pad=1080:1080:(ow-iw)/2:(oh-ih)/2:black",
    "16:9":  "scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2:black",
}

ASPECT_RATIO_DIMS = {
    "9:16": (1080, 1920),
    "1:1": (1080, 1080),
    "16:9": (1920, 1080),
}


async def download_and_reencode_video(video_url: str, job_id: str, aspect_ratio: str = "9:16", audio_url: str = "", audio_bytes: bytes = None) -> str:
    """Download avatar video and re-encode at high quality with the given aspect ratio.
    Pass audio_url (http) or audio_bytes to merge a separate voiceover (used for Seedance)."""
    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()

    async with httpx.AsyncClient(timeout=180.0) as client:
        resp = await client.get(video_url)
        video_bytes = resp.content

    if not audio_bytes and audio_url:
        async with httpx.AsyncClient(timeout=60.0) as client:
            ar = await client.get(audio_url)
            audio_bytes = ar.content

    output_path = os.path.join(VIDEOS_DIR, f"{job_id}.mp4")
    vf_filter = ASPECT_RATIO_FILTERS.get(aspect_ratio, ASPECT_RATIO_FILTERS["9:16"])

    with tempfile.TemporaryDirectory() as tmpdir:
        video_path = os.path.join(tmpdir, "avatar.mp4")
        with open(video_path, "wb") as f:
            f.write(video_bytes)

        if audio_bytes:
            mp3_path = os.path.join(tmpdir, "voice.mp3")
            wav_path = os.path.join(tmpdir, "voice.wav")
            with open(mp3_path, "wb") as f:
                f.write(audio_bytes)
            # Convert MP3 → WAV to eliminate MP3 encoder delay (causes 1s audio dropout)
            await asyncio.to_thread(subprocess.run, [
                ffmpeg_exe, "-y", "-i", mp3_path,
                "-ar", "44100", "-ac", "1",
                wav_path,
            ], capture_output=True, timeout=30)
            audio_path = wav_path if os.path.exists(wav_path) else mp3_path
            cmd = [
                ffmpeg_exe, "-y",
                "-i", video_path,
                "-i", audio_path,
                "-map", "0:v", "-map", "1:a",
                "-vf", vf_filter,
                "-af", "afade=t=in:st=0:d=0.1",
                "-c:v", "libx264", "-preset", "slow", "-crf", "18",
                "-c:a", "aac", "-b:a", "192k", "-ar", "44100",
                "-shortest",
                output_path,
            ]
        else:
            # Kling: audio is already embedded — still fix any gaps in re-encode
            cmd = [
                ffmpeg_exe, "-y",
                "-i", video_path,
                "-vf", vf_filter,
                "-af", "aresample=async=1000:first_pts=0",
                "-c:v", "libx264", "-preset", "slow", "-crf", "18",
                "-c:a", "aac", "-b:a", "192k", "-ar", "44100",
                output_path,
            ]
        result = await asyncio.to_thread(
            subprocess.run, cmd, capture_output=True, text=True, timeout=300
        )
        if result.returncode != 0:
            raise Exception(f"Video re-encode failed: {result.stderr[-400:]}")

    return f"/static/videos/{job_id}.mp4"


def _load_overlay_font(size: int, bold: bool = False):
    candidates = [
        r"C:\Windows\Fonts\arialbd.ttf" if bold else r"C:\Windows\Fonts\arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    ]
    for path in candidates:
        try:
            if path and os.path.exists(path):
                return ImageFont.truetype(path, size)
        except Exception:
            pass
    return ImageFont.load_default()


def _text_size(draw: ImageDraw.ImageDraw, text: str, font) -> tuple[int, int]:
    if not text:
        return (0, 0)
    bbox = draw.textbbox((0, 0), text, font=font)
    return (bbox[2] - bbox[0], bbox[3] - bbox[1])


def _fit_text(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    if _text_size(draw, text, font)[0] <= max_width:
        return text
    ellipsis = "..."
    while text and _text_size(draw, text + ellipsis, font)[0] > max_width:
        text = text[:-1].rstrip()
    return (text + ellipsis) if text else ""


def _wrap_text(draw: ImageDraw.ImageDraw, text: str, font, max_width: int, max_lines: int = 2) -> list[str]:
    words = (text or "").strip().split()
    if not words:
        return []
    lines = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if _text_size(draw, candidate, font)[0] <= max_width:
            current = candidate
            continue
        if current:
            lines.append(current)
        current = word
        if len(lines) >= max_lines:
            break
    if current and len(lines) < max_lines:
        lines.append(current)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
    if len(lines) == max_lines:
        lines[-1] = _fit_text(draw, lines[-1], font, max_width)
    return lines


def _apply_image_brand_overlay(image_bytes: bytes, branding: dict | None = None) -> bytes:
    branding = branding or {}
    brand = (branding.get("brand_name") or "").strip()
    mobile = _format_display_phone(branding.get("brand_mobile") or "")
    offer = (branding.get("offer_text") or "").strip()
    cta = (branding.get("cta_text") or "").strip()
    if not any([brand, mobile, offer, cta]):
        return image_bytes

    image = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
    width, height = image.size
    padding = max(int(width * 0.04), 28)
    radius = max(int(width * 0.018), 14)

    layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)

    brand_font = _load_overlay_font(max(int(width * 0.032), 22), bold=True)
    small_font = _load_overlay_font(max(int(width * 0.022), 15), bold=False)

    if brand or mobile:
        brand_max = int(width * 0.38)
        brand_line = _fit_text(draw, brand or "Contact us", brand_font, brand_max)
        mobile_line = _fit_text(draw, mobile, small_font, brand_max) if mobile else ""
        brand_w, brand_h = _text_size(draw, brand_line, brand_font)
        mobile_w, mobile_h = _text_size(draw, mobile_line, small_font)
        badge_pad_x = max(int(width * 0.018), 14)
        badge_pad_y = max(int(width * 0.012), 10)
        line_gap = max(int(width * 0.008), 7) if mobile_line else 0
        badge_w = max(brand_w, mobile_w) + badge_pad_x * 2
        badge_h = brand_h + mobile_h + line_gap + badge_pad_y * 2
        badge_x1 = width - padding
        badge_y0 = padding
        badge_x0 = max(padding, badge_x1 - badge_w)
        draw.rounded_rectangle(
            (badge_x0, badge_y0, badge_x1, badge_y0 + badge_h),
            radius=radius,
            fill=(12, 9, 28, 178),
            outline=(255, 255, 255, 76),
            width=max(1, width // 700),
        )
        draw.text(
            (badge_x0 + badge_pad_x, badge_y0 + badge_pad_y),
            brand_line,
            font=brand_font,
            fill=(255, 255, 255, 245),
        )
        if mobile_line:
            draw.text(
                (badge_x0 + badge_pad_x, badge_y0 + badge_pad_y + brand_h + line_gap),
                mobile_line,
                font=small_font,
                fill=(255, 255, 255, 225),
            )

    composed = Image.alpha_composite(image, layer).convert("RGB")
    out = io.BytesIO()
    composed.save(out, format="JPEG", quality=94, optimize=True)
    return out.getvalue()


def _has_branding(branding: dict | None, keys: list[str]) -> bool:
    branding = branding or {}
    return any((branding.get(key) or "").strip() for key in keys)


def _draw_centered_text(draw, xy, text: str, font, fill):
    x, y, w, h = xy
    box = draw.textbbox((0, 0), text, font=font)
    tw = box[2] - box[0]
    th = box[3] - box[1]
    draw.text(
        (x + (w - tw) / 2 - box[0], y + (h - th) / 2 - box[1]),
        text,
        font=font,
        fill=fill,
    )


def _render_video_end_card(job_id: str, aspect_ratio: str, branding: dict) -> str:
    brand = (branding.get("brand_name") or "").strip()
    mobile = _format_display_phone(branding.get("brand_mobile") or "")
    details = (branding.get("details") or "").strip()
    cta = (branding.get("cta_text") or "").strip()
    product_image_path = (branding.get("product_image_path") or "").strip()
    width, height = ASPECT_RATIO_DIMS.get(aspect_ratio, ASPECT_RATIO_DIMS["9:16"])
    videos_dir = os.path.join(os.path.dirname(__file__), "static", "videos")
    os.makedirs(videos_dir, exist_ok=True)

    image = Image.new("RGB", (width, height), (34, 26, 44))
    draw = ImageDraw.Draw(image)

    for y in range(height):
        mix = y / max(height - 1, 1)
        color = (
            int(32 + 20 * mix),
            int(24 + 12 * mix),
            int(44 + 28 * mix),
        )
        draw.line((0, y, width, y), fill=color)

    pink = (244, 122, 164)
    cream = (255, 246, 250)
    muted = (223, 205, 218)
    panel = (255, 255, 255)

    logo_font = _load_overlay_font(max(int(width * 0.12), 78), bold=True)
    brand_font = _load_overlay_font(max(int(width * 0.043), 34), bold=True)
    cta_font = _load_overlay_font(max(int(width * 0.06), 46), bold=True)
    details_font = _load_overlay_font(max(int(width * 0.032), 24), bold=True)
    phone_font = _load_overlay_font(max(int(width * 0.048), 38), bold=True)
    footer_font = _load_overlay_font(max(int(width * 0.024), 18), bold=True)

    margin = max(int(width * 0.065), 46)
    center_x = width // 2
    y = max(int(height * 0.07), 50)

    if product_image_path and os.path.exists(product_image_path):
        try:
            product = Image.open(product_image_path).convert("RGB")
            image_w = int(width * 0.74)
            image_h = int(height * 0.38)
            image_x = (width - image_w) // 2
            product.thumbnail((image_w, image_h), Image.LANCZOS)
            frame = Image.new("RGB", (image_w, image_h), (42, 34, 54))
            paste_x = (image_w - product.width) // 2
            paste_y = (image_h - product.height) // 2
            frame.paste(product, (paste_x, paste_y))
            mask = Image.new("L", (image_w, image_h), 0)
            mdraw = ImageDraw.Draw(mask)
            mdraw.rounded_rectangle((0, 0, image_w, image_h), radius=max(width // 34, 20), fill=255)
            image.paste(frame, (image_x, y), mask)
            draw.rounded_rectangle(
                (image_x, y, image_x + image_w, y + image_h),
                radius=max(width // 34, 20),
                outline=(255, 255, 255),
                width=max(width // 200, 3),
            )
            y += image_h + max(int(height * 0.045), 32)
        except Exception:
            y = max(int(height * 0.12), 80)

    initials = "".join(part[0] for part in re.findall(r"[A-Za-z0-9]+", brand or "Vishleshak")[:2]).upper() or "V"
    logo_r = max(int(width * 0.095), 54)
    draw.ellipse((center_x - logo_r, y, center_x + logo_r, y + logo_r * 2), outline=pink, width=max(3, width // 260))
    _draw_centered_text(draw, (center_x - logo_r, y, logo_r * 2, logo_r * 2), initials[:2], logo_font, cream)
    y += logo_r * 2 + max(int(height * 0.025), 20)

    title = _fit_text(draw, (brand or "Thank you").upper(), brand_font, width - margin * 2)
    tw, th = _text_size(draw, title, brand_font)
    draw.text((center_x - tw / 2, y), title, font=brand_font, fill=cream)
    y += th + max(int(height * 0.045), 34)

    cta_line = cta or "Ready to Order"
    cta_lines = _wrap_text(draw, cta_line.replace("\n", " "), cta_font, width - margin * 2, max_lines=2)
    for idx, line in enumerate(cta_lines[:2]):
        line = _fit_text(draw, line, cta_font, width - margin * 2)
        tw, th = _text_size(draw, line, cta_font)
        draw.text((center_x - tw / 2, y), line, font=cta_font, fill=pink if idx == 0 else cream)
        y += th + max(int(height * 0.012), 10)

    if details:
        y += max(int(height * 0.018), 14)
        for line in _wrap_text(draw, details, details_font, width - margin * 2, max_lines=2):
            fitted = _fit_text(draw, line, details_font, width - margin * 2)
            tw, th = _text_size(draw, fitted, details_font)
            draw.text((center_x - tw / 2, y), fitted, font=details_font, fill=muted)
            y += th + max(int(height * 0.012), 10)

    if mobile:
        y += max(int(height * 0.025), 20)
        phone_line = _fit_text(draw, mobile, phone_font, width - margin * 2)
        tw, th = _text_size(draw, phone_line, phone_font)
        pill_w = min(width - margin * 2, tw + max(int(width * 0.16), 110))
        pill_h = max(int(height * 0.07), th + max(int(height * 0.032), 26))
        pill = (center_x - pill_w / 2, y, center_x + pill_w / 2, y + pill_h)
        draw.rounded_rectangle(pill, radius=pill_h // 2, fill=pink)
        _draw_centered_text(draw, (pill[0], pill[1], pill_w, pill_h), phone_line, phone_font, (255, 255, 255))
        y += pill_h + max(int(height * 0.035), 28)

    footer = "Powered by Vishleshak AI"
    tw, th = _text_size(draw, footer, footer_font)
    draw.text((center_x - tw // 2, height - max(int(height * 0.055), 42) - th), footer, font=footer_font, fill=(184, 166, 214))

    path = os.path.join(videos_dir, f"{job_id}_end_card.jpg")
    image.save(path, format="JPEG", quality=94, optimize=True)
    return path


def _extract_end_card_preview(ffmpeg_exe: str, input_path: str, job_id: str) -> str:
    videos_dir = os.path.join(os.path.dirname(__file__), "static", "videos")
    preview_path = os.path.join(videos_dir, f"{job_id}_end_preview.jpg")
    cmd = [
        ffmpeg_exe,
        "-y",
        "-ss",
        "0.35",
        "-i",
        input_path,
        "-frames:v",
        "1",
        "-q:v",
        "2",
        preview_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode == 0 and os.path.exists(preview_path):
        return preview_path
    return ""


async def append_video_end_card(final_url: str, job_id: str, aspect_ratio: str, branding: dict | None = None) -> str:
    if not _has_branding(branding, ["brand_name", "brand_mobile", "details", "cta_text"]):
        return final_url

    branding = dict(branding or {})
    videos_dir = os.path.join(os.path.dirname(__file__), "static", "videos")
    input_path = os.path.join(os.path.dirname(__file__), final_url.lstrip("/").replace("/", os.sep))
    if not os.path.exists(input_path):
        return final_url

    width, height = ASPECT_RATIO_DIMS.get(aspect_ratio, ASPECT_RATIO_DIMS["9:16"])
    output_path = os.path.join(videos_dir, f"{job_id}_endcard_tmp.mp4")
    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
    preview_path = await asyncio.to_thread(_extract_end_card_preview, ffmpeg_exe, input_path, job_id)
    if preview_path:
        branding["product_image_path"] = preview_path
    card_path = await asyncio.to_thread(_render_video_end_card, job_id, aspect_ratio, branding)
    vf0 = f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black,setsar=1,fps=24,format=yuv420p,setpts=PTS-STARTPTS"
    vf1 = f"scale={width}:{height},setsar=1,fps=24,format=yuv420p,setpts=PTS-STARTPTS"
    end_card_seconds = "3"
    with_audio = [
        ffmpeg_exe, "-y",
        "-i", input_path,
        "-loop", "1", "-t", end_card_seconds, "-i", card_path,
        "-f", "lavfi", "-t", end_card_seconds, "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
        "-filter_complex",
        f"[0:v]{vf0}[v0];[1:v]{vf1}[v1];[0:a]aresample=48000,asetpts=PTS-STARTPTS[a0];[2:a]aresample=48000,asetpts=PTS-STARTPTS[a1];[v0][a0][v1][a1]concat=n=2:v=1:a=1[v][a]",
        "-map", "[v]", "-map", "[a]",
        "-c:v", "libx264", "-preset", "fast", "-crf", "20",
        "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart",
        output_path,
    ]
    no_audio = [
        ffmpeg_exe, "-y",
        "-i", input_path,
        "-loop", "1", "-t", end_card_seconds, "-i", card_path,
        "-filter_complex",
        f"[0:v]{vf0}[v0];[1:v]{vf1}[v1];[v0][v1]concat=n=2:v=1:a=0[v]",
        "-map", "[v]",
        "-c:v", "libx264", "-preset", "fast", "-crf", "20", "-movflags", "+faststart",
        output_path,
    ]

    try:
        result = await asyncio.to_thread(subprocess.run, with_audio, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            result = await asyncio.to_thread(subprocess.run, no_audio, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            raise Exception(result.stderr[-400:])
        os.replace(output_path, input_path)
    finally:
        if os.path.exists(card_path):
            try:
                os.remove(card_path)
            except Exception:
                pass
        if os.path.exists(output_path):
            try:
                os.remove(output_path)
            except Exception:
                pass
    return final_url


async def download_and_save_image(
    image_url: str,
    job_id: str,
    branding: dict | None = None,
    apply_overlay: bool = True,
) -> str:
    """Download a generated composite image and save it locally."""
    os.makedirs(IMAGES_DIR, exist_ok=True)
    os.makedirs(RAW_IMAGES_DIR, exist_ok=True)
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.get(image_url)
        image_bytes = resp.content
    raw_ext = ".png" if ".png" in image_url.lower().split("?")[0] else ".jpg"
    raw_path = os.path.join(RAW_IMAGES_DIR, f"{job_id}{raw_ext}")
    with open(raw_path, "wb") as f:
        f.write(image_bytes)
    if apply_overlay:
        image_bytes = await asyncio.to_thread(_apply_image_brand_overlay, image_bytes, branding)
    else:
        image_bytes = await asyncio.to_thread(_to_jpeg_bytes, image_bytes)
    output_path = os.path.join(IMAGES_DIR, f"{job_id}.jpg")
    with open(output_path, "wb") as f:
        f.write(image_bytes)
    return f"/static/images/{job_id}.jpg"


# ── API routes ────────────────────────────────────────────────────────────────

@app.get("/api/pipeline-info")
async def pipeline_info():
    """Return current pipeline details and cost/time estimate."""
    if KIE_API_KEY:
        return {"pipeline": "kie", "estimate_cost": "~₹35", "estimate_time": "8-12 min", "quality": "Best"}
    elif FAL_KEY and "kling" in os.getenv("FAL_MODEL", ""):
        return {"pipeline": "fal_kling", "estimate_cost": "~₹120", "estimate_time": "10-15 min", "quality": "High"}
    elif FAL_KEY:
        return {"pipeline": "fal", "estimate_cost": "~₹5", "estimate_time": "2-4 min", "quality": "Good"}
    else:
        return {"pipeline": "free", "estimate_cost": "Free", "estimate_time": "1-2 min", "quality": "Basic"}


@app.get("/api/history")
async def get_history():
    """Return the last 12 completed video entries."""
    history = load_history()
    return history[:12]


def _rss_node_text(parent, tag: str) -> str:
    child = parent.find(tag)
    if child is None or child.text is None:
        return ""
    return html.unescape(child.text.strip())


def _rss_items_from_xml(xml_text: str, limit: int = 8) -> list[dict]:
    import xml.etree.ElementTree as ET
    from email.utils import parsedate_to_datetime

    root = ET.fromstring(xml_text)
    channel = root.find("channel")
    items = channel.findall("item") if channel is not None else root.findall(".//item")
    parsed = []
    for item in items[: max(limit * 3, limit)]:
        title = _rss_node_text(item, "title")
        link = _rss_node_text(item, "link")
        description = re.sub(r"<[^>]+>", " ", _rss_node_text(item, "description"))
        description = re.sub(r"\s+", " ", description).strip()
        published = _rss_node_text(item, "pubDate")
        published_iso = published
        if published:
            try:
                published_iso = parsedate_to_datetime(published).isoformat()
            except Exception:
                pass
        if title and link:
            parsed.append({
                "title": title,
                "link": link,
                "summary": description[:220],
                "published": published_iso,
            })
        if len(parsed) >= limit:
            break
    return parsed


@app.get("/api/news/feed")
async def news_feed(limit: int = 8):
    """Local feed test for Vishleshak Market Brief."""
    result = await _fetch_news_feed(limit)
    return result


async def _fetch_news_feed(limit: int = 8) -> dict:
    feed_urls = [
        url.strip()
        for url in os.getenv(
            "NEWS_FEED_URLS",
            "https://www.business-standard.com/rss/markets/stock-market-news-10618.rss,"
            "https://news.google.com/rss/search?q=Indian%20stock%20market%20Sensex%20Nifty&hl=en-IN&gl=IN&ceid=IN:en,"
            "https://www.moneycontrol.com/rss/latestnews.xml,"
            "https://www.moneycontrol.com/rss/business.xml,"
            "https://www.moneycontrol.com/rss/marketreports.xml",
        ).split(",")
        if url.strip()
    ]
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/125.0 Safari/537.36",
        "Accept": "application/rss+xml, application/xml, text/xml, */*",
    }
    last_error = ""
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True, headers=headers) as client:
        for feed_url in feed_urls:
            try:
                resp = await client.get(feed_url)
                resp.raise_for_status()
                items = _rss_items_from_xml(resp.text, max(1, min(limit, 20)))
                if items:
                    return {
                        "status": "ok",
                        "source": feed_url,
                        "fetched_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "items": items,
                    }
                last_error = f"No RSS items found at {feed_url}"
            except Exception as exc:
                last_error = f"{feed_url}: {exc}"
                print(f"[NewsFeed] {last_error}")
    return {
        "status": "error",
        "source": "",
        "fetched_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "error": last_error or "No feed URL configured",
        "items": [],
    }


@app.get("/api/news/brief")
async def news_brief(slot: str = "morning", limit: int = 6):
    """Create a local template-based brief preview from fetched headlines."""
    feed = await _fetch_news_feed(max(3, min(limit, 10)))
    slot = (slot or "morning").strip().lower()
    items = feed.get("items", [])
    top_titles = [item.get("title", "").strip() for item in items if item.get("title")]
    if slot not in {"morning", "close"}:
        slot = "morning"

    if slot == "morning":
        title = "9 AM Market Watchlist"
        hook = "3 things smart money watches before 10 AM"
        caption = (
            "Vishleshak Market Brief - 9 AM watchlist. "
            "Top market headlines and cues to track before the first hour settles. "
            "Educational only. Not investment advice."
        )
        bullets = top_titles[:3]
    else:
        title = "4 PM Market Close"
        hook = "Today's market close in simple takeaways"
        caption = (
            "Vishleshak Market Brief - 4 PM close. "
            "Top movers, sector mood, and simple market takeaways. "
            "Educational only. Not investment advice."
        )
        bullets = top_titles[:5]

    if not bullets:
        bullets = [
            "Feed could not fetch live headlines yet.",
            "Check source availability or add another RSS/API.",
            "Template preview is still working locally.",
        ]

    return {
        "status": feed.get("status", "error"),
        "slot": slot,
        "title": title,
        "hook": hook,
        "caption": caption,
        "source": feed.get("source", ""),
        "fetched_at": feed.get("fetched_at", datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")),
        "bullets": bullets,
        "source_items": items[: len(bullets)],
        "error": feed.get("error", ""),
    }


_NSE_HDRS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9,hi;q=0.8",
    "Referer": "https://www.nseindia.com/",
}
_NSE_WANTED = {"NIFTY 50", "NIFTY BANK", "NIFTY IT", "NIFTY MIDCAP 100", "NIFTY SMLCAP 100", "INDIA VIX"}


@app.get("/api/market-snapshot")
async def market_snapshot():
    """Live Indian market data from NSE India + ExchangeRate-API. No API key required."""
    result: dict = {
        "status": "ok",
        "source": "NSE India",
        "fetched_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "indices": {},
        "gainers": [],
        "losers": [],
        "forex": {},
        "market_open": False,
        "error": "",
    }
    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            # Must hit homepage first to receive session cookies NSE requires
            await client.get("https://www.nseindia.com", headers=_NSE_HDRS)

            # All indices (Nifty 50, Bank Nifty, VIX, etc.)
            ir = await client.get("https://www.nseindia.com/api/allIndices", headers=_NSE_HDRS)
            ir.raise_for_status()
            for idx in ir.json().get("data", []):
                name = idx.get("index", "")
                if name in _NSE_WANTED:
                    result["indices"][name] = {
                        "price": idx.get("last"),
                        "change": idx.get("change"),
                        "pctChange": idx.get("percentChange"),
                        "open": idx.get("open"),
                        "high": idx.get("high"),
                        "low": idx.get("low"),
                    }

            # Nifty 50 constituents for gainers / losers
            nr = await client.get(
                "https://www.nseindia.com/api/equity-stockIndices?index=NIFTY%2050",
                headers=_NSE_HDRS,
            )
            nr.raise_for_status()
            stocks = nr.json().get("data", [])[1:]  # row 0 is the index summary
            stocks.sort(key=lambda s: float(s.get("pChange") or 0), reverse=True)

            def _stock(s: dict) -> dict:
                return {
                    "symbol": s.get("symbol", ""),
                    "price": s.get("lastPrice"),
                    "change": s.get("change"),
                    "pctChange": s.get("pChange"),
                }

            result["gainers"] = [_stock(s) for s in stocks[:5]]
            result["losers"] = [_stock(s) for s in stocks[-5:][::-1]]
            result["market_open"] = bool(stocks)

        # Forex from ExchangeRate-API (free, no key)
        async with httpx.AsyncClient(timeout=10.0) as fx_client:
            fxr = await fx_client.get("https://api.exchangerate-api.com/v4/latest/USD")
            rates = fxr.json().get("rates", {})
            inr = float(rates.get("INR", 84))
            eur = float(rates.get("EUR", 0.92))
            gbp = float(rates.get("GBP", 0.79))
            result["forex"] = {
                "USD_INR": round(inr, 2),
                "EUR_INR": round(inr / eur, 2) if eur else None,
                "GBP_INR": round(inr / gbp, 2) if gbp else None,
            }

    except Exception as exc:
        result["status"] = "error"
        result["error"] = str(exc)
        print(f"[MarketSnapshot] {exc}")

    return result


@app.post("/api/generate-script")
async def generate_script_endpoint(
    image: UploadFile = File(...),
    presenter_source: str = Form("uploaded"),
    auto_mode: str = Form("true"),
    language: str = Form("hindi"),
    model_gender: str = Form("female"),
    skin_tone: str = Form("wheatish"),
    scene: str = Form("studio"),
    custom_scene: str = Form(""),
    model_action: str = Form(""),
    custom_instructions: str = Form(""),
):
    """Run only Claude analysis and return script + metadata (no video generation)."""
    if not image.content_type or not image.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="File must be an image")
    image_data = await image.read()
    if len(image_data) > 15 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Image must be under 15MB")

    customization = {
        "auto_mode":           auto_mode == "true",
        "language":            language,
        "model_gender":        model_gender,
        "skin_tone":           skin_tone,
        "scene":               scene,
        "custom_scene":        custom_scene,
        "model_action":        model_action,
        "custom_instructions": custom_instructions,
    }

    image_b64 = base64.b64encode(image_data).decode("utf-8")
    try:
        script, avatar_prompt, product_type, ai_settings = await asyncio.to_thread(
            generate_script, image_b64, image.content_type, customization
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return {
        "script": script,
        "avatar_prompt": avatar_prompt,
        "product_type": product_type,
        "ai_settings": ai_settings,
    }


@app.post("/api/improve-creative-notes")
async def improve_creative_notes_endpoint(
    image: UploadFile = File(...),
    notes: str = Form(""),
    output_type: str = Form("video"),
    video_style: str = Form("kling"),
    platform: str = Form("instagram"),
    aspect_ratio: str = Form("9:16"),
    language: str = Form("english"),
):
    """Rewrite rough customer notes into a product-safe generation brief."""
    if not image.content_type or not image.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="File must be an image")
    image_data = await image.read()
    if len(image_data) > 15 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Image must be under 15MB")

    raw_notes = (notes or "").strip()
    if not raw_notes:
        raw_notes = "Create a premium UGC creative that highlights the uploaded product clearly."

    image_b64 = base64.b64encode(image_data).decode("utf-8")
    instructions = (
        "Look at the uploaded product image and rewrite the customer's rough creative notes into a better production brief.\n"
        "Reply with ONLY valid JSON, no markdown, in this format: {\"improved_notes\":\"...\"}\n\n"
        f"Customer notes: {raw_notes}\n"
        f"Output type: {output_type}\n"
        f"Video style: {video_style}\n"
        f"Platform: {platform}\n"
        f"Aspect ratio: {aspect_ratio}\n"
        f"Language: {language}\n\n"
        "Rules:\n"
        "- Keep the customer's main idea and mood, but make it safer for AI generation.\n"
        "- Make the uploaded product the hero object and keep it clearly visible.\n"
        "- Preserve exact product color, shape, markings/logo, texture, proportions, and packaging/details.\n"
        "- Avoid scene jumps, product swaps, extreme action that hides the product, subtitles, captions, watermarks, or on-screen text.\n"
        "- For cinematic video, prefer small realistic motion and product-first camera direction.\n"
        "- If the customer asks for a big story scene, convert it into a controlled product ad moment.\n"
        "- Write 2 to 4 concise sentences that can be used directly as generation instructions."
    )
    try:
        message = anthropic_client.messages.create(
            model="claude-opus-4-7",
            max_tokens=350,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": image.content_type, "data": image_b64}},
                    {"type": "text", "text": instructions},
                ],
            }],
        )
        raw = message.content[0].text.strip()
        try:
            data = json.loads(raw)
            improved = data.get("improved_notes", "").strip()
        except Exception:
            improved = raw.strip().strip("`")
        if not improved:
            improved = raw_notes
        return {"improved_notes": improved}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/order-result")
async def receive_order_result(request: Request):
    """Called by local admin server after video generation — updates order status on Railway so customer result page shows the video."""
    body = await request.json()
    order_id  = body.get("order_id")
    status    = body.get("status", "completed")
    video_url = body.get("video_url", "")
    image_url = body.get("image_url", "")
    script    = body.get("script", "")
    if not order_id:
        return {"status": "error", "msg": "missing order_id"}
    orders = load_orders()
    updated = False
    for o in orders:
        if o.get("id") == order_id:
            o["status"]    = status
            o["video_url"] = video_url
            o["image_url"] = image_url
            if script:
                o["script"] = script
            if body.get("job_id"):
                o["job_id"] = body.get("job_id")
            updated = True
            break

    if not updated:
        # Order was lost on Railway redeploy — create a minimal record so result page works
        orders.insert(0, {
            "id":         order_id,
            "status":     status,
            "video_url":  video_url,
            "image_url":  image_url,
            "script":     script,
            "job_id":     body.get("job_id", ""),
            "customer_name":  body.get("customer_name", ""),
            "customer_phone": body.get("customer_phone", ""),
            "created_at": datetime.utcnow().isoformat(),
        })

    with open(ORDERS_FILE, "w", encoding="utf-8") as f:
        json.dump(orders, f, ensure_ascii=False, indent=2)
    return {"status": "updated" if updated else "created"}


@app.delete("/api/orders/{order_id}/model-photo")
async def delete_model_photo(order_id: str):
    """Called by local server after generation — removes customer model photo for privacy."""
    path = os.path.join(ORDERS_UPLOAD_DIR, f"{order_id}_model.jpg")
    if os.path.exists(path):
        try:
            os.remove(path)
            print(f"[privacy] Deleted Railway model photo for order {order_id[:8]}")
            return {"status": "deleted"}
        except Exception as e:
            return {"status": "error", "msg": str(e)}
    return {"status": "not_found"}


@app.post("/api/sync-order")
async def sync_order(request: Request):
    """Accept an order from Railway and store it locally (admin only)."""
    order = await request.json()
    orders = load_orders()
    if any(o.get("id") == order.get("id") for o in orders):
        return {"status": "exists"}
    # Preserve payment-gated orders so local admin cannot approve unpaid orders.
    if not order.get("status"):
        order["status"] = "pending"

    # Download product image from Railway if not already local
    order_id = order.get("id", "")
    railway_base = RAILWAY_URL.rstrip("/")
    local_img = os.path.join(ORDERS_UPLOAD_DIR, f"{order_id}.jpg")
    if order_id and not os.path.exists(local_img):
        img_url = f"{railway_base}/order_uploads/{order_id}.jpg"
        try:
            import urllib.request
            urllib.request.urlretrieve(img_url, local_img)
            order["product_image_path"] = local_img
        except Exception:
            pass  # Image download failed — thumbnail will be blank, generation still works

    # Download customer model photo from Railway if they uploaded one
    local_model = os.path.join(ORDERS_UPLOAD_DIR, f"{order_id}_model.jpg")
    if order_id and not os.path.exists(local_model):
        model_url = f"{railway_base}/order_uploads/{order_id}_model.jpg"
        try:
            import urllib.request
            urllib.request.urlretrieve(model_url, local_model)
            order["model_image_path"] = local_model
            print(f"[sync-order] Customer model photo downloaded for {order_id}")
        except Exception:
            pass  # No model photo — will use global model or AI

    orders.insert(0, order)
    with open(ORDERS_FILE, "w", encoding="utf-8") as f:
        json.dump(orders, f, ensure_ascii=False, indent=2)

    # Notify owner via WhatsApp Business API
    try:
        msg = (
            f"🛍️ *New Order — Vishleshak UGC*\n\n"
            f"👤 *Name:* {order.get('customer_name', '—')}\n"
            f"📱 *Phone:* {order.get('customer_phone', '—')}\n"
            f"🎬 *Type:* {order.get('output_type', 'video')} · {order.get('video_duration', '30')}s · {order.get('language', '—')}\n"
            f"📝 *Notes:* {order.get('notes') or '—'}\n\n"
            f"🔗 *Dashboard:* http://127.0.0.1:8000"
        )
        asyncio.create_task(wa_send_text(OWNER_WHATSAPP, msg))
    except Exception as e:
        print(f"[sync-order] WhatsApp notify failed: {e}")

    # Also send email notification
    asyncio.create_task(_send_order_email(order))

    return {"status": "synced"}


@app.post("/api/resync-railway")
async def resync_railway():
    """Push all locally completed orders to Railway — use after Railway redeploys wipe its orders.json."""
    if not RAILWAY_URL:
        return {"status": "error", "msg": "RAILWAY_URL not configured"}
    orders = load_orders()
    completed = [o for o in orders if o.get("status") == "completed" and o.get("video_url")]
    pushed = []
    failed = []
    async with httpx.AsyncClient(timeout=20.0) as client:
        for o in completed:
            try:
                resp = await client.post(f"{RAILWAY_URL}/api/order-result", json={
                    "order_id": o["id"],
                    "status": "completed",
                    "video_url": o["video_url"],
                    "image_url": o.get("image_url", ""),
                    "script": o.get("script", o.get("custom_script", "")),
                    "job_id": o.get("job_id", ""),
                    "customer_name": o.get("customer_name", ""),
                    "customer_phone": o.get("customer_phone", ""),
                })
                pushed.append(o["id"][:8])
            except Exception as e:
                failed.append(f"{o['id'][:8]}: {e}")
    return {"status": "done", "pushed": len(pushed), "failed": failed}


@app.post("/api/recover-veo3")
async def recover_veo3(request: Request):
    """Admin endpoint: fetch a completed Veo3 video from kie.ai by task ID,
    upload to Cloudinary, and push the result to Railway.
    Body: {order_id, job_id (optional), task_id}
    """
    body = await request.json()
    order_id = body.get("order_id", "").strip()
    job_id   = body.get("job_id", "").strip()
    task_id  = body.get("task_id", "").strip()
    if not order_id or not task_id:
        return {"status": "error", "msg": "order_id and task_id are required"}

    # Poll kie.ai once to get the video URL
    try:
        video_url = await poll_veo3_task(task_id)
    except Exception as e:
        return {"status": "error", "msg": f"kie.ai poll failed: {e}"}

    # Download the video locally
    vid_id = job_id or order_id[:8]
    local_path = os.path.join(os.path.dirname(__file__), "static", "videos", f"{vid_id}.mp4")
    try:
        async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
            resp = await client.get(video_url)
            resp.raise_for_status()
            with open(local_path, "wb") as f:
                f.write(resp.content)
    except Exception as e:
        return {"status": "error", "msg": f"Video download failed: {e}"}

    # Upload to Cloudinary
    cdn_url = await _upload_to_cloudinary(local_path, vid_id)
    public_url = cdn_url or video_url

    # Push to Railway result page
    await _push_result_to_railway(order_id, public_url, "", "")

    # Update local orders.json
    orders = load_orders()
    for o in orders:
        if o.get("id") == order_id:
            o["status"] = "completed"
            o["video_url"] = public_url
            break
    with open(ORDERS_FILE, "w", encoding="utf-8") as f:
        json.dump(orders, f, ensure_ascii=False, indent=2)

    return {"status": "recovered", "video_url": public_url}


@app.post("/api/clear-model")
async def clear_model():
    global model_image_bytes, model_image_url
    for fname in os.listdir(MODEL_DIR):
        fpath = os.path.join(MODEL_DIR, fname)
        if os.path.isfile(fpath):
            os.remove(fpath)
    model_image_bytes = None
    model_image_url = None
    return {"status": "cleared"}

@app.post("/api/reload-model")
async def reload_model_from_folder():
    global model_image_url
    for fname in os.listdir(MODEL_DIR):
        ext = os.path.splitext(fname)[1].lower()
        if ext not in IMAGE_EXTS:
            continue
        path = os.path.join(MODEL_DIR, fname)
        mime = "image/jpeg" if ext in (".jpg", ".jpeg") else f"image/{ext[1:]}"
        image_bytes = open(path, "rb").read()
        url = await upload_image_to_public(image_bytes, fname, mime)
        model_image_url = url
        set_key(ENV_FILE, "MODEL_IMAGE_URL", url)
        return {"success": True, "image_url": url, "file": fname}
    raise HTTPException(status_code=404, detail="No image found in model/ folder")


@app.get("/api/config")
async def get_config():
    return {"mode": APP_MODE, "railway_url": RAILWAY_URL}


@app.get("/api/sample-media/list/{kind}")
async def sample_media_list(kind: str, count: int = 6):
    if kind not in ("image", "video"):
        raise HTTPException(status_code=400, detail="kind must be image or video")
    return _sample_media_payload(kind, count)


@app.get("/api/sample-media/{kind}/{filename:path}")
async def sample_media_file(kind: str, filename: str):
    if kind not in ("image", "video"):
        raise HTTPException(status_code=400, detail="kind must be image or video")

    base_dir = SAMPLE_IMAGES_DIR if kind == "image" else SAMPLE_VIDEOS_DIR
    extensions = IMAGE_EXTS if kind == "image" else VIDEO_EXTS
    safe_name = os.path.basename(filename)
    path = os.path.abspath(os.path.join(base_dir, safe_name))
    base_abs = os.path.abspath(base_dir)

    if not path.startswith(base_abs + os.sep):
        raise HTTPException(status_code=400, detail="Invalid sample path")
    if not os.path.isfile(path) or os.path.splitext(path)[1].lower() not in extensions:
        raise HTTPException(status_code=404, detail="Sample not found")
    return FileResponse(path)


@app.get("/api/model-status")
async def model_status():
    has_local_model = os.path.exists(MODEL_LOCAL_PATH)
    configured = has_local_model or bool(model_image_url)
    image_url = "/api/model-image" if has_local_model else model_image_url
    return {"configured": configured, "image_url": image_url}


@app.get("/api/model-image")
async def model_image():
    if not os.path.exists(MODEL_LOCAL_PATH):
        raise HTTPException(status_code=404, detail="Model image not found")
    return FileResponse(MODEL_LOCAL_PATH)


@app.post("/api/upload-model")
async def upload_model(image: UploadFile = File(...)):
    global model_image_url, model_image_bytes
    if not image.content_type or not image.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="File must be an image")
    image_data = await image.read()
    if len(image_data) > 10 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Model photo must be under 10MB")

    # Save locally for kie.ai uploads (catbox URLs can't be downloaded programmatically)
    with open(MODEL_LOCAL_PATH, "wb") as f:
        f.write(image_data)
    model_image_bytes = image_data

    model_image_url = "/api/model-image"
    set_key(ENV_FILE, "MODEL_IMAGE_URL", model_image_url)
    return {"image_url": model_image_url}


@app.post("/api/generate-video")
async def generate_video(
    background_tasks: BackgroundTasks,
    image: UploadFile = File(...),
    presenter_source: str = Form("uploaded"),
    output_type: str = Form("video"),
    video_duration: str = Form("5"),
    video_quality: str = Form("high"),
    auto_mode: str = Form("true"),
    language: str = Form("hindi"),
    model_gender: str = Form("female"),
    skin_tone: str = Form("wheatish"),
    scene: str = Form("studio"),
    custom_scene: str = Form(""),
    model_action: str = Form(""),
    custom_instructions: str = Form(""),
    aspect_ratio: str = Form("9:16"),
    image_brand_name: str = Form(""),
    image_brand_mobile: str = Form(""),
    image_offer_text: str = Form(""),
    image_cta_text: str = Form(""),
    video_brand_name: str = Form(""),
    video_brand_mobile: str = Form(""),
    video_brand_details: str = Form(""),
    video_cta_text: str = Form(""),
    custom_script: str = Form(""),
):
    if not image.content_type or not image.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="File must be an image")
    image_data = await image.read()
    if len(image_data) > 15 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Image must be under 15MB")
    if presenter_source not in ("uploaded", "ai", "product"):
        raise HTTPException(status_code=400, detail="Invalid presenter source")
    _enforce_person_restricted_product_policy(
        presenter_source,
        False,
        model_action,
        custom_instructions,
        image_brand_name,
        image_offer_text,
        image_cta_text,
        video_brand_name,
        video_brand_details,
        video_cta_text,
        custom_script,
        image.filename,
    )
    if presenter_source == "uploaded" and not model_image_url and not os.path.exists(MODEL_LOCAL_PATH):
        raise HTTPException(status_code=400, detail="Model photo not set. Please upload your model photo first or choose AI presenter.")
    if output_type == "image" and not KIE_API_KEY:
        raise HTTPException(status_code=400, detail="Image generation requires KIE API (4o Image). Please configure KIE_API_KEY.")
    if presenter_source in ("ai", "product") and not KIE_API_KEY:
        raise HTTPException(status_code=400, detail="AI/product-only generation requires KIE_API_KEY because it creates the final product image.")

    customization = {
        "presenter_source":    presenter_source,
        "output_type":         output_type,
        "video_duration":      video_duration,
        "video_quality":       video_quality,
        "auto_mode":           auto_mode == "true",
        "language":            language,
        "model_gender":        model_gender,
        "skin_tone":           skin_tone,
        "scene":               scene,
        "custom_scene":        custom_scene,
        "model_action":        model_action,
        "custom_instructions": custom_instructions,
        "aspect_ratio":        aspect_ratio,
        "image_branding":      {
            "brand_name": image_brand_name,
            "brand_mobile": image_brand_mobile,
            "offer_text": image_offer_text,
            "cta_text": image_cta_text,
        },
        "video_end_card":      {
            "brand_name": video_brand_name,
            "brand_mobile": video_brand_mobile,
            "details": video_brand_details,
            "cta_text": video_cta_text,
        },
        "custom_script":       custom_script.strip(),
    }

    job_id = str(uuid.uuid4())
    jobs[job_id] = {
        "status": "processing",
        "step": "analyzing",
        "script": None,
        "video_url": None,
        "image_url": None,
        "error": None,
    }
    background_tasks.add_task(process_job, job_id, image_data, image.content_type, model_image_url, customization)
    return {"job_id": job_id}


@app.get("/api/status/{job_id}")
async def get_status(job_id: str):
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")
    return jobs[job_id]


@app.get("/")
async def serve_root():
    return FileResponse("static/index.html")

@app.get("/admin")
async def serve_admin_dashboard():
    return FileResponse("static/index.html")

@app.get("/favicon.svg")
async def serve_favicon_svg():
    return FileResponse("static/favicon.svg", media_type="image/svg+xml")

@app.get("/favicon.ico")
async def serve_favicon_ico():
    return FileResponse("static/favicon.ico", media_type="image/x-icon")

@app.get("/pitch")
async def serve_pitch():
    return FileResponse("static/pitch.html")


@app.post("/api/orders")
async def submit_order(
    customer_name: str = Form(...),
    customer_phone: str = Form(...),
    customer_email: str = Form(""),
    language: str = Form("hindi"),
    output_type: str = Form("video"),
    video_duration: str = Form("5"),
    presenter_source: str = Form("ai"),
    video_quality: str = Form("high"),
    platform: str = Form("instagram"),
    aspect_ratio: str = Form("9:16"),
    notes: str = Form(""),
    image_brand_name: str = Form(""),
    image_brand_mobile: str = Form(""),
    image_offer_text: str = Form(""),
    image_cta_text: str = Form(""),
    video_brand_name: str = Form(""),
    video_brand_mobile: str = Form(""),
    video_brand_details: str = Form(""),
    video_cta_text: str = Form(""),
    custom_script: str = Form(""),
    video_style: str = Form("kling"),
    credit_otp_token: str = Form(""),
    product_image: UploadFile = File(...),
    model_reference: UploadFile = File(None),
):
    _enforce_person_restricted_product_policy(
        presenter_source,
        bool(model_reference and model_reference.filename),
        notes,
        image_brand_name,
        image_offer_text,
        image_cta_text,
        video_brand_name,
        video_brand_details,
        video_cta_text,
        custom_script,
        product_image.filename,
    )
    image_data = await product_image.read()
    if len(image_data) > 15 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Image must be under 15MB")
    # Convert to JPEG at upload time — handles HEIC, BMP, TIFF, etc.
    image_data = _to_jpeg_bytes(image_data)
    order_id = str(uuid.uuid4())
    img_path = os.path.join(ORDERS_UPLOAD_DIR, f"{order_id}.jpg")
    with open(img_path, "wb") as f:
        f.write(image_data)

    # Save customer model photo if provided
    model_img_path = None
    if model_reference and model_reference.filename:
        model_data = await model_reference.read()
        if model_data:
            model_data = _to_jpeg_bytes(model_data)
            model_img_path = os.path.join(ORDERS_UPLOAD_DIR, f"{order_id}_model.jpg")
            with open(model_img_path, "wb") as f:
                f.write(model_data)

    order = {
        "id": order_id,
        "created_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "status": "pending",
        "customer_name": customer_name,
        "customer_phone": customer_phone,
        "customer_email": customer_email,
        "language": language,
        "output_type": output_type,
        "video_duration": video_duration,
        "presenter_source": presenter_source,
        "video_quality": video_quality,
        "platform": platform,
        "aspect_ratio": aspect_ratio,
        "notes": notes,
        "image_brand_name": image_brand_name,
        "image_brand_mobile": image_brand_mobile,
        "image_offer_text": image_offer_text,
        "image_cta_text": image_cta_text,
        "video_brand_name": video_brand_name,
        "video_brand_mobile": video_brand_mobile,
        "video_brand_details": video_brand_details,
        "video_cta_text": video_cta_text,
        "custom_script": custom_script,
        "video_style": video_style,
        "product_image_path": img_path,
        "model_image_path": model_img_path,
        "product_mime": product_image.content_type or "image/jpeg",
        "job_id": None,
        "video_url": None,
        "image_url": None,
        "script": None,
    }
    amount_inr = _estimate_order_amount_inr(order)
    order["amount_inr"] = amount_inr
    order["payment_status"] = "not_required"
    client = _upsert_client_from_order(order)
    if client:
        order["client_id"] = client["id"]
        credit_status = _client_credit_status(client)
        credits_needed = _credit_cost_for_order(order)
        if credit_status["active"] and credit_status["credits_left"] >= credits_needed:
            if _verify_credit_otp_token(customer_phone, credit_otp_token):
                credit_used, credit_reason = _consume_client_credit(client["id"], order)
            else:
                credit_used, credit_reason = False, "otp_required"
        else:
            credit_used, credit_reason = _consume_client_credit(client["id"], order)
        order["package_credit_status"] = credit_reason
        if credit_used:
            order["payment_status"] = "package_credit"
            order["status"] = "pending"

    if RAZORPAY_ENABLED and order.get("payment_status") != "package_credit":
        order["status"] = "payment_pending"
        order["payment_status"] = "pending"
        try:
            payment_link = await _create_razorpay_payment_link(order)
            order["razorpay_payment_link_id"] = payment_link.get("id")
            order["razorpay_payment_link_url"] = payment_link.get("short_url")
            order["razorpay_reference_id"] = payment_link.get("reference_id") or order_id
        except Exception as e:
            order["status"] = "payment_error"
            order["payment_status"] = "link_failed"
            order["payment_error"] = str(e)
    save_order(order)
    _new_order_ids.append(order_id)
    asyncio.create_task(_send_order_email(order))
    asyncio.create_task(_send_whatsapp_notification(order))
    return {
        "order_id": order_id,
        "status": order["status"],
        "amount_inr": amount_inr,
        "payment_url": order.get("razorpay_payment_link_url", ""),
        "payment_status": order.get("payment_status", "not_required"),
    }


def _send_order_email_sync(order: dict):
    if not OWNER_EMAIL or not GMAIL_APP_PASSWORD:
        return
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"🛍️ New Order from {order['customer_name']} — Vishleshak UGC"
        msg["From"]    = OWNER_EMAIL
        msg["To"]      = OWNER_EMAIL
        approve_url = f"http://127.0.0.1:8000/?approve={order['id']}"
        body = f"""
<html><body style="font-family:sans-serif;max-width:500px;margin:auto">
  <h2 style="color:#c9a84c">New Order Received!</h2>
  <table style="width:100%;border-collapse:collapse">
    <tr><td style="padding:6px 0;color:#64748b">Customer</td><td><strong>{order['customer_name']}</strong></td></tr>
    <tr><td style="padding:6px 0;color:#64748b">WhatsApp</td><td>{order['customer_phone']}</td></tr>
    <tr><td style="padding:6px 0;color:#64748b">Output</td><td>{order['output_type']} · {order['video_duration']}s · {order['language']}</td></tr>
    <tr><td style="padding:6px 0;color:#64748b">Notes</td><td>{order.get('notes') or '—'}</td></tr>
    <tr><td style="padding:6px 0;color:#64748b">Order ID</td><td style="font-size:12px;color:#94a3b8">{order['id']}</td></tr>
  </table>
  <br>
  <a href="{approve_url}" style="background:#c9a84c;color:#000;padding:12px 24px;border-radius:8px;text-decoration:none;font-weight:700;display:inline-block">
    ✅ Open Dashboard to Approve
  </a>
  <p style="color:#94a3b8;font-size:12px;margin-top:20px">Vishleshak UGC — AI Video Ad Studio</p>
</body></html>
"""
        msg.attach(MIMEText(body, "html"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(OWNER_EMAIL, GMAIL_APP_PASSWORD)
            server.sendmail(OWNER_EMAIL, OWNER_EMAIL, msg.as_string())
    except Exception as e:
        print(f"Email notification failed: {e}")


async def _send_order_email(order: dict):
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _send_order_email_sync, order)


def _send_credit_email_otp_sync(email: str, otp: str, package_name: str = ""):
    recipient = _normalize_email(email)
    if not OWNER_EMAIL or not GMAIL_APP_PASSWORD:
        raise RuntimeError("Email OTP is not configured")
    if not recipient:
        raise RuntimeError("Valid email is required")
    msg = MIMEMultipart("alternative")
    msg["Subject"] = "Your Vishleshak credit verification code"
    msg["From"] = OWNER_EMAIL
    msg["To"] = recipient
    safe_package = html.escape(package_name or "your package")
    body = f"""
<html><body style="font-family:Arial,sans-serif;max-width:520px;margin:auto;color:#111827">
  <h2 style="margin:0 0 12px;color:#4f46e5">Vishleshak credit verification</h2>
  <p>Use this code to verify credits for <strong>{safe_package}</strong>:</p>
  <div style="font-size:28px;font-weight:800;letter-spacing:4px;background:#f3f4f6;border-radius:10px;padding:16px 20px;text-align:center">{otp}</div>
  <p style="color:#6b7280">This code is valid for 10 minutes. Do not share it with anyone.</p>
  <p style="color:#9ca3af;font-size:12px">Vishleshak AI Content Studio</p>
</body></html>
"""
    msg.attach(MIMEText(body, "html"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=12) as server:
        server.login(OWNER_EMAIL, GMAIL_APP_PASSWORD)
        server.sendmail(OWNER_EMAIL, recipient, msg.as_string())


async def _send_credit_email_otp_resend(email: str, otp: str, package_name: str = ""):
    recipient = _normalize_email(email)
    if not RESEND_API_KEY:
        raise RuntimeError("RESEND_API_KEY is not configured")
    if not RESEND_FROM_EMAIL:
        raise RuntimeError("RESEND_FROM_EMAIL is not configured")
    if not recipient:
        raise RuntimeError("Valid email is required")
    sender_candidates = _resend_sender_candidates()
    if not sender_candidates:
        raise RuntimeError("RESEND_FROM_EMAIL must be a valid email address")
    safe_package = html.escape(package_name or "your package")
    body = f"""
<html><body style="font-family:Arial,sans-serif;max-width:520px;margin:auto;color:#111827">
  <h2 style="margin:0 0 12px;color:#4f46e5">Vishleshak credit verification</h2>
  <p>Use this code to verify credits for <strong>{safe_package}</strong>:</p>
  <div style="font-size:28px;font-weight:800;letter-spacing:4px;background:#f3f4f6;border-radius:10px;padding:16px 20px;text-align:center">{otp}</div>
  <p style="color:#6b7280">This code is valid for 10 minutes. Do not share it with anyone.</p>
  <p style="color:#9ca3af;font-size:12px">Vishleshak AI Content Studio</p>
</body></html>
"""
    payload_base = {
        "to": [recipient],
        "subject": "Your Vishleshak credit verification code",
        "html": body,
    }
    last_resp = None
    async with httpx.AsyncClient(timeout=20.0) as client:
        for sender, sender_email in sender_candidates:
            resp = await client.post(
                RESEND_API_URL,
                headers={
                    "Authorization": f"Bearer {RESEND_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={"from": sender, **payload_base},
            )
            last_resp = resp
            if resp.status_code < 400:
                return
            if resp.status_code == 422 and sender != sender_email and "from" in resp.text.lower():
                resp = await client.post(
                    RESEND_API_URL,
                    headers={
                        "Authorization": f"Bearer {RESEND_API_KEY}",
                        "Content-Type": "application/json",
                    },
                    json={"from": sender_email, **payload_base},
                )
                last_resp = resp
                if resp.status_code < 400:
                    return
    if last_resp and last_resp.status_code >= 400:
        raise RuntimeError(f"Resend rejected email OTP: {last_resp.status_code} {last_resp.text[:200]}")
    raise RuntimeError("Resend rejected email OTP")


async def _send_credit_email_otp(email: str, otp: str, package_name: str = ""):
    if RESEND_API_KEY:
        await _send_credit_email_otp_resend(email, otp, package_name)
        return
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _send_credit_email_otp_sync, email, otp, package_name)


async def _upload_to_cloudinary(local_path: str, public_id: str, resource_type: str = "video") -> str:
    """Upload a local file to Cloudinary. Returns the secure CDN URL."""
    if not _CLOUDINARY_READY:
        return ""
    try:
        result = await asyncio.to_thread(
            cloudinary.uploader.upload,
            local_path,
            resource_type = resource_type,
            public_id     = f"vishleshak-ugc/{public_id}",
            overwrite     = True,
        )
        url = result.get("secure_url", "")
        print(f"[Cloudinary] Uploaded ({resource_type}): {url}")
        return url
    except Exception as e:
        print(f"[Cloudinary] Upload failed: {e}")
        return ""


async def _delete_model_photos(order_id: str):
    """Delete customer model photo from local disk and Railway after generation — privacy cleanup."""
    # Delete local copy
    local_model = os.path.join(ORDERS_UPLOAD_DIR, f"{order_id}_model.jpg")
    if os.path.exists(local_model):
        try:
            os.remove(local_model)
            print(f"[cleanup] Deleted local model photo for order {order_id[:8]}")
        except Exception as e:
            print(f"[cleanup] Failed to delete local model photo: {e}")

    # Tell Railway to delete its copy too
    if RAILWAY_URL:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.delete(f"{RAILWAY_URL}/api/orders/{order_id}/model-photo")
        except Exception:
            pass  # Best-effort — not critical if it fails


def _order_image_branding(order: dict) -> dict:
    return {
        "brand_name": order.get("image_brand_name", ""),
        "brand_mobile": order.get("image_brand_mobile", ""),
        "offer_text": order.get("image_offer_text", ""),
        "cta_text": order.get("image_cta_text", ""),
    }


def _order_video_end_card(order: dict) -> dict:
    return {
        "brand_name": order.get("video_brand_name", ""),
        "brand_mobile": order.get("video_brand_mobile", ""),
        "details": order.get("video_brand_details", ""),
        "cta_text": order.get("video_cta_text", ""),
        "product_image_path": order.get("product_image_path", ""),
    }


def _start_order_generation(order: dict, background_tasks: BackgroundTasks):
    order_id = order["id"]
    img_path = order["product_image_path"]
    if not os.path.exists(img_path):
        raise HTTPException(status_code=400, detail="Product image missing")
    with open(img_path, "rb") as f:
        image_data = f.read()
    job_id = str(uuid.uuid4())
    jobs[job_id] = {
        "status": "processing",
        "step": "analyzing",
        "script": None,
        "video_url": None,
        "image_url": None,
        "error": None,
        "order_id": order_id,
    }
    customization = {
        "presenter_source": order.get("presenter_source", "ai"),
        "output_type": order.get("output_type", "video"),
        "video_duration": order.get("video_duration", "5"),
        "video_quality": order.get("video_quality", "high"),
        "auto_mode": True,
        "language": order.get("language", "hindi"),
        "model_gender": "female",
        "skin_tone": "wheatish",
        "scene": "studio",
        "custom_scene": "",
        "model_action": order.get("notes", ""),
        "custom_instructions": order.get("notes", ""),
        "aspect_ratio": order.get("aspect_ratio", "9:16"),
        "custom_script": order.get("custom_script", ""),
        "order_model_path": order.get("model_image_path") or "",
        "image_branding": _order_image_branding(order),
        "video_end_card": _order_video_end_card(order),
    }
    order["status"] = "processing"
    order["job_id"] = job_id
    save_order(order)
    video_style = order.get("video_style")
    if video_style in ("cinematic", "veo3"):
        pipeline = process_job_veo3
    elif video_style == "seedance":
        pipeline = process_job_seedance
    else:
        pipeline = process_job
    background_tasks.add_task(
        pipeline,
        job_id,
        image_data,
        order.get("product_mime", "image/jpeg"),
        model_image_url,
        customization,
    )
    wa_from = order.get("wa_from")
    if wa_from:
        product_name = order.get("notes", "your product").replace("WhatsApp order for: ", "")
        background_tasks.add_task(notify_wa_on_complete, job_id, order_id, wa_from, product_name)
    return job_id


async def _persist_result_to_cloudinary(order_id: str, payload: dict):
    """Upload order result as a raw JSON to Cloudinary so it survives Railway redeploys."""
    if not _CLOUDINARY_READY:
        return
    try:
        import cloudinary.uploader as _cup
        data_bytes = json.dumps(payload).encode("utf-8")
        _cup.upload(
            data_bytes,
            resource_type="raw",
            public_id=f"vishleshak-orders/{order_id}",
            overwrite=True,
            format="json",
        )
        print(f"[Cloudinary persist] saved order result for {order_id[:8]}")
    except Exception as e:
        print(f"[Cloudinary persist] failed: {e}")


async def _push_result_to_railway(order_id: str, video_url: str, image_url: str, script: str):
    """After local generation, push completed result to Railway AND persist to Cloudinary."""
    # Build absolute public URL using ngrok PUBLIC_URL
    def make_public(url: str) -> str:
        if not url:
            return url
        if url.startswith("http"):
            return url
        return f"{PUBLIC_URL}{url}" if PUBLIC_URL else url

    payload = {
        "order_id": order_id,
        "status": "completed",
        "video_url": make_public(video_url),
        "image_url": make_public(image_url),
        "script": script,
    }

    # Always persist to Cloudinary first — survives Railway redeploys
    await asyncio.to_thread(_persist_result_to_cloudinary_sync, order_id, payload)

    if not RAILWAY_URL:
        return
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(f"{RAILWAY_URL}/api/order-result", json=payload)
            print(f"[Railway sync] {resp.status_code}: {resp.text[:100]}")
    except Exception as e:
        print(f"[Railway sync] failed: {e}")


def _persist_result_to_cloudinary_sync(order_id: str, payload: dict):
    """Sync wrapper for Cloudinary raw upload."""
    if not _CLOUDINARY_READY:
        return
    try:
        import cloudinary.uploader as _cup
        data_bytes = json.dumps(payload).encode("utf-8")
        _cup.upload(
            data_bytes,
            resource_type="raw",
            public_id=f"vishleshak-orders/{order_id}",
            overwrite=True,
            format="json",
        )
        print(f"[Cloudinary persist] saved result for {order_id[:8]}")
    except Exception as e:
        print(f"[Cloudinary persist] failed: {e}")


async def _send_whatsapp_notification(order: dict):
    """Send WhatsApp alert via CallMeBot free API."""
    if not CALLMEBOT_API_KEY:
        return
    try:
        approve_link = f"http://127.0.0.1:8000/api/orders/{order['id']}/approve-quick?token={CALLMEBOT_API_KEY}"
        msg = (
            f"🛍️ *New Order — Vishleshak UGC*\n\n"
            f"👤 *Name:* {order['customer_name']}\n"
            f"📱 *Phone:* {order['customer_phone']}\n"
            f"🎬 *Type:* {order['output_type']} · {order['video_duration']}s · {order['language']}\n"
            f"📝 *Notes:* {order.get('notes') or '—'}\n\n"
            f"✅ *Approve:* {approve_link}\n"
            f"🔗 *Dashboard:* http://127.0.0.1:8000"
        )
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.get(
                "https://api.callmebot.com/whatsapp.php",
                params={
                    "phone": OWNER_WHATSAPP,
                    "text": msg,
                    "apikey": CALLMEBOT_API_KEY,
                }
            )
    except Exception as e:
        print(f"WhatsApp notification failed: {e}")


def _otp_channel_order() -> list[str]:
    channels = []
    for raw in (CREDIT_OTP_CHANNEL_ORDER or "").split(","):
        channel = raw.strip().lower()
        if channel in ("twofactor", "whatsapp", "sms", "callmebot") and channel not in channels:
            channels.append(channel)
    return channels or ["twofactor", "whatsapp", "sms", "callmebot"]


def _fast2sms_whatsapp_ready() -> bool:
    if FAST2SMS_WHATSAPP_OTP_URL_TEMPLATE and FAST2SMS_WHATSAPP_API_KEY:
        return True
    return bool(
        FAST2SMS_WHATSAPP_API_KEY
        and FAST2SMS_WHATSAPP_PHONE_NUMBER_ID
        and FAST2SMS_WHATSAPP_TEMPLATE_NAME
    )


async def _send_credit_otp_fast2sms_whatsapp(phone: str, otp: str) -> bool:
    normalized_phone = _normalize_phone(phone)
    if not _fast2sms_whatsapp_ready():
        raise RuntimeError("Fast2SMS WhatsApp OTP template/API is not configured")

    if FAST2SMS_WHATSAPP_OTP_URL_TEMPLATE:
        url = FAST2SMS_WHATSAPP_OTP_URL_TEMPLATE.format(
            api_key=quote(FAST2SMS_WHATSAPP_API_KEY),
            phone=normalized_phone,
            mobile_number=normalized_phone,
            phone_with_country=f"91{normalized_phone}",
            mobile_number_with_country=f"91{normalized_phone}",
            otp=quote(otp),
            variables_values=quote(otp),
        )
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url)
        if resp.status_code >= 400:
            raise RuntimeError(f"Fast2SMS WhatsApp rejected OTP: {resp.status_code} {resp.text[:200]}")
        return "success" in resp.text.lower() or "sent" in resp.text.lower() or resp.status_code < 400

    url = (
        "https://www.fast2sms.com/dev/whatsapp/"
        f"{FAST2SMS_WHATSAPP_VERSION}/{FAST2SMS_WHATSAPP_PHONE_NUMBER_ID}/messages"
    )
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": f"91{normalized_phone}",
        "type": "template",
        "template": {
            "name": FAST2SMS_WHATSAPP_TEMPLATE_NAME,
            "language": {"code": FAST2SMS_WHATSAPP_TEMPLATE_LANGUAGE},
            "components": [
                {
                    "type": "body",
                    "parameters": [{"type": "text", "text": otp}],
                },
                {
                    "type": "button",
                    "sub_type": "url",
                    "index": "0",
                    "parameters": [{"type": "text", "text": otp}],
                },
            ],
        },
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            url,
            headers={
                "Authorization": FAST2SMS_WHATSAPP_API_KEY,
                "Content-Type": "application/json",
            },
            json=payload,
        )
    if resp.status_code >= 400:
        raise RuntimeError(f"Fast2SMS WhatsApp rejected OTP: {resp.status_code} {resp.text[:200]}")
    return True


async def _send_credit_otp_fast2sms_sms(phone: str, otp: str) -> bool:
    normalized_phone = _normalize_phone(phone)
    if not FAST2SMS_API_KEY:
        raise RuntimeError("FAST2SMS_API_KEY is not configured")
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            "https://www.fast2sms.com/dev/bulkV2",
            headers={
                "authorization": FAST2SMS_API_KEY,
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={
                "variables_values": otp,
                "route": "otp",
                "numbers": normalized_phone,
            },
        )
    if resp.status_code >= 400:
        raise RuntimeError(f"Fast2SMS SMS rejected OTP: {resp.status_code} {resp.text[:200]}")
    try:
        return bool(resp.json().get("return"))
    except Exception:
        return True


async def _send_credit_otp_twofactor(phone: str, otp: str) -> bool:
    normalized_phone = _normalize_phone(phone)
    if not TWOFACTOR_API_KEY:
        raise RuntimeError("TWOFACTOR_API_KEY is not configured")
    if not TWOFACTOR_TEMPLATE_NAME:
        raise RuntimeError("TWOFACTOR_TEMPLATE_NAME is not configured")
    if TWOFACTOR_MODE == "autogen":
        url = TWOFACTOR_AUTOGEN_URL_TEMPLATE.format(
            api_key=quote(TWOFACTOR_API_KEY),
            phone=normalized_phone,
            phone_with_country=f"91{normalized_phone}",
            otp=quote(otp),
            template_name=quote(TWOFACTOR_TEMPLATE_NAME),
        )
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url)
        if resp.status_code >= 400:
            raise RuntimeError(f"2Factor rejected OTP: {resp.status_code} {resp.text[:200]}")
        payload = resp.json()
        if str(payload.get("Status", "")).lower() != "success":
            raise RuntimeError(f"2Factor did not send OTP: {resp.text[:200]}")
        session_id = str(payload.get("Details", "")).strip()
        if not session_id:
            raise RuntimeError(f"2Factor did not return session id: {resp.text[:200]}")
        return {"provider": "twofactor", "twofactor_session_id": session_id, "otp_hash": ""}

    url = TWOFACTOR_OTP_URL_TEMPLATE.format(
        api_key=quote(TWOFACTOR_API_KEY),
        phone=normalized_phone,
        phone_with_country=f"91{normalized_phone}",
        otp=quote(otp),
        template_name=quote(TWOFACTOR_TEMPLATE_NAME),
    )
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(url)
    if resp.status_code >= 400:
        raise RuntimeError(f"2Factor rejected OTP: {resp.status_code} {resp.text[:200]}")
    try:
        payload = resp.json()
        if str(payload.get("Status", "")).lower() == "success":
            return {"provider": "local"}
        raise RuntimeError(f"2Factor did not send OTP: {resp.text[:200]}")
    except Exception:
        body = resp.text.lower()
        if "success" in body or "sent" in body:
            return {"provider": "local"}
        raise RuntimeError(f"2Factor did not send OTP: {resp.text[:200]}")


async def _verify_credit_otp_twofactor(session_id: str, otp: str) -> bool:
    if not TWOFACTOR_API_KEY:
        raise RuntimeError("TWOFACTOR_API_KEY is not configured")
    if not session_id:
        return False
    url = TWOFACTOR_VERIFY_URL_TEMPLATE.format(
        api_key=quote(TWOFACTOR_API_KEY),
        session_id=quote(session_id),
        otp=quote(otp),
    )
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(url)
    if resp.status_code >= 400:
        raise RuntimeError(f"2Factor rejected OTP verify: {resp.status_code} {resp.text[:200]}")
    try:
        payload = resp.json()
        return str(payload.get("Status", "")).lower() == "success"
    except Exception:
        return "success" in resp.text.lower()


async def _send_credit_otp_callmebot(phone: str, otp: str) -> bool:
    normalized_phone = _normalize_phone(phone)
    if not CALLMEBOT_API_KEY:
        raise RuntimeError("CALLMEBOT_API_KEY is not configured")
    msg = (
        f"Your Vishleshak credit OTP is {otp}. "
        "It is valid for 10 minutes. Do not share it with anyone."
    )
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            "https://api.callmebot.com/whatsapp.php",
            params={
                "phone": f"91{normalized_phone}",
                "text": msg,
                "apikey": CALLMEBOT_API_KEY,
            },
        )
    if resp.status_code >= 400:
        raise RuntimeError(f"CallMeBot WhatsApp rejected OTP: {resp.status_code} {resp.text[:200]}")
    return True


async def _send_credit_otp(phone: str, otp: str):
    """Send package-credit OTP with configured fallbacks."""
    global last_credit_otp_error, last_credit_otp_channel
    last_credit_otp_error = ""
    last_credit_otp_channel = ""
    errors = []
    senders = {
        "twofactor": _send_credit_otp_twofactor,
        "whatsapp": _send_credit_otp_fast2sms_whatsapp,
        "sms": _send_credit_otp_fast2sms_sms,
        "callmebot": _send_credit_otp_callmebot,
    }
    for channel in _otp_channel_order():
        try:
            result = await senders[channel](phone, otp)
            if result:
                last_credit_otp_channel = channel
                return result if isinstance(result, dict) else {"provider": "local"}
            errors.append(f"{channel}: provider returned false")
        except Exception as e:
            errors.append(f"{channel}: {e}")
            print(f"Credit OTP {channel} failed: {e}")
    last_credit_otp_error = " | ".join(errors)
    return False


@app.get("/api/orders/poll-new")
async def poll_new_orders():
    """Frontend polls this to detect new orders for browser notifications."""
    ids = list(_new_order_ids)
    _new_order_ids.clear()
    return {"new_order_ids": ids}


@app.get("/api/orders")
async def list_orders():
    orders = load_orders()
    changed = False
    for o in orders:
        job_id = o.get("job_id")
        if job_id and job_id in jobs and o.get("status") == "processing":
            job = jobs[job_id]
            o["job_status"] = job.get("status")
            o["job_step"] = job.get("step")
            if job.get("status") == "completed":
                o["status"] = "completed"
                o["video_url"] = job.get("video_url") or o.get("video_url")
                o["image_url"] = job.get("image_url") or o.get("image_url")
                o["script"] = job.get("script") or o.get("script")
                changed = True
            elif job.get("status") == "failed":
                o["status"] = "failed"
                o["error"] = job.get("error", "Unknown error")
                changed = True
    if changed:
        for o in orders:
            save_order(o)
        orders = load_orders()
    visible_orders = [o for o in orders if o.get("status") != "rejected"]
    return sort_orders_latest_first(visible_orders)


@app.get("/api/orders/{order_id}")
async def get_order(order_id: str):
    orders = load_orders()
    for o in orders:
        if o["id"] == order_id:
            if o.get("job_id") and o["job_id"] in jobs:
                job = jobs[o["job_id"]]
                o["job_status"] = job.get("status")
                o["job_step"] = job.get("step")
                if job.get("status") == "completed":
                    o["status"] = "completed"
                    o["video_url"] = job.get("video_url") or o.get("video_url")
                    o["image_url"] = job.get("image_url") or o.get("image_url")
                    o["script"] = job.get("script") or o.get("script")
                    save_order(o)
                elif job.get("status") == "failed":
                    o["status"] = "failed"
                    o["error"] = job.get("error")
                    save_order(o)
            return o
    raise HTTPException(status_code=404, detail="Order not found")


@app.get("/api/packages")
async def list_packages():
    with _tracking_conn() as conn:
        rows = conn.execute("SELECT * FROM packages WHERE active=1 ORDER BY price_inr").fetchall()
    return [_row_to_dict(row) for row in rows]


@app.get("/api/credit-packs")
async def list_credit_packs():
    return [
        {"id": pack_id, **pack}
        for pack_id, pack in CREDIT_PACK_DEFS.items()
    ]


@app.post("/api/credit-packs/checkout")
async def create_credit_pack_checkout(request: Request):
    body = await request.json()
    pack_id = (body.get("pack_id") or "").strip()
    pack = CREDIT_PACK_DEFS.get(pack_id)
    if not pack:
        raise HTTPException(status_code=404, detail="Credit pack not found")
    phone = _normalize_phone(body.get("phone", ""))
    email = _normalize_email(body.get("email", ""))
    name = (body.get("name") or "").strip() or "Vishleshak Customer"
    if len(phone) != 10:
        raise HTTPException(status_code=400, detail="Enter a valid WhatsApp number before buying credits.")
    if not email:
        raise HTTPException(status_code=400, detail="Enter an email before buying credits.")
    client = _get_client_by_phone(phone)
    now = _utc_now_iso()
    if client:
        with _tracking_conn() as conn:
            conn.execute("""
                UPDATE clients SET contact_name=?, business_name=?, email=?, updated_at=? WHERE id=?
            """, (name, client.get("business_name") or name, email, now, client["id"]))
        client = _get_client_by_id(client["id"])
    else:
        client_id = str(uuid.uuid4())
        with _tracking_conn() as conn:
            conn.execute("""
                INSERT INTO clients (
                    id, business_name, contact_name, phone, email, niche, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'UGC credits', 'lead', ?, ?)
            """, (client_id, name, name, phone, email, now, now))
        client = _get_client_by_id(client_id)
    try:
        return await _create_credit_pack_payment_link(client, pack_id)
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Could not create payment link: {str(e)[:180]}")


@app.get("/api/clients")
async def list_clients():
    with _tracking_conn() as conn:
        rows = conn.execute("SELECT * FROM clients ORDER BY updated_at DESC").fetchall()
    clients = []
    for row in rows:
        client = _row_to_dict(row)
        client.update(_client_credit_status(client))
        clients.append(client)
    return clients


@app.get("/api/clients/lookup")
async def lookup_client(phone: str):
    client = _get_client_by_phone(phone)
    if not client:
        return {"found": False}
    status = _client_credit_status(client)
    return {
        "found": True,
        "business_name": client.get("business_name", ""),
        "package_name": client.get("package_name", ""),
        "display_package_name": _client_display_package_name(client),
        "package_expires_at": client.get("package_expires_at", ""),
        "credits_left": status["credits_left"],
        "image_credit_cost": CREDIT_COST_IMAGE,
        "video_credit_cost": CREDIT_COST_VIDEO,
        "email_fallback_available": bool(_normalize_email(client.get("email", ""))),
        "masked_email": _mask_email(client.get("email", "")),
        "active": status["active"],
    }


@app.post("/api/clients/send-credit-otp")
async def send_credit_otp(request: Request):
    body = await request.json()
    phone = _normalize_phone(body.get("phone", ""))
    if len(phone) != 10:
        raise HTTPException(status_code=400, detail="Enter a valid WhatsApp number.")
    client = _get_client_by_phone(phone)
    if not client:
        return {"sent": False, "reason": "client_not_found"}
    status = _client_credit_status(client)
    if not status["active"] or status["credits_left"] <= 0:
        return {"sent": False, "reason": "no_active_credits"}
    _cleanup_credit_otps()
    otp = f"{random.SystemRandom().randint(100000, 999999)}"
    credit_otp_store[phone] = {
        "otp_hash": _credit_otp_hash(phone, otp, "phone"),
        "method": "phone",
        "expires_at": (datetime.utcnow() + timedelta(minutes=10)).timestamp(),
        "attempts": 0,
    }
    send_meta = await _send_credit_otp(phone, otp)
    if not send_meta:
        credit_otp_store.pop(phone, None)
        raise HTTPException(status_code=503, detail="Could not send OTP. Please pay online for this order.")
    credit_otp_store[phone].update(send_meta)
    return {"sent": True, "expires_in_seconds": 600}


@app.post("/api/clients/send-credit-email-otp")
async def send_credit_email_otp(request: Request):
    global last_credit_email_error
    last_credit_email_error = ""
    body = await request.json()
    phone = _normalize_phone(body.get("phone", ""))
    email = _normalize_email(body.get("email", ""))
    if len(phone) != 10:
        raise HTTPException(status_code=400, detail="Enter a valid WhatsApp number.")
    if not email:
        raise HTTPException(status_code=400, detail="Enter the package email to receive email OTP.")
    client = _get_client_by_phone(phone)
    if not client:
        return {"sent": False, "reason": "client_not_found"}
    status = _client_credit_status(client)
    if not status["active"] or status["credits_left"] <= 0:
        return {"sent": False, "reason": "no_active_credits"}
    client_email = _normalize_email(client.get("email", ""))
    if not client_email:
        raise HTTPException(status_code=400, detail="No email is saved for this package. Please use phone OTP.")
    if email != client_email:
        raise HTTPException(status_code=403, detail="Email does not match this package. Use the package email.")
    _cleanup_credit_otps()
    otp = f"{random.SystemRandom().randint(100000, 999999)}"
    credit_otp_store[phone] = {
        "otp_hash": _credit_otp_hash(phone, otp, "email", email),
        "method": "email",
        "email": email,
        "expires_at": (datetime.utcnow() + timedelta(minutes=10)).timestamp(),
        "attempts": 0,
    }
    try:
        await _send_credit_email_otp(email, otp, client.get("package_name", ""))
    except Exception as e:
        credit_otp_store.pop(phone, None)
        last_credit_email_error = str(e)
        print(f"Credit email OTP failed: {e}")
        detail = f"Could not send email OTP: {last_credit_email_error[:180]}"
        raise HTTPException(status_code=503, detail=detail)
    return {"sent": True, "expires_in_seconds": 600, "email": email}


@app.get("/api/clients/credit-otp-status")
async def credit_otp_status():
    return {
        "twofactor_configured": bool(TWOFACTOR_API_KEY),
        "twofactor_template": bool(TWOFACTOR_TEMPLATE_NAME),
        "twofactor_mode": TWOFACTOR_MODE,
        "email_otp_configured": bool(RESEND_API_KEY and RESEND_FROM_EMAIL) or bool(OWNER_EMAIL and GMAIL_APP_PASSWORD),
        "resend_configured": bool(RESEND_API_KEY and RESEND_FROM_EMAIL),
        "last_email_error": last_credit_email_error,
        "fast2sms_configured": bool(FAST2SMS_API_KEY),
        "fast2sms_whatsapp_configured": _fast2sms_whatsapp_ready(),
        "fast2sms_whatsapp_template": bool(FAST2SMS_WHATSAPP_TEMPLATE_NAME),
        "fast2sms_whatsapp_phone_number_id": bool(FAST2SMS_WHATSAPP_PHONE_NUMBER_ID),
        "fast2sms_whatsapp_url_template": bool(FAST2SMS_WHATSAPP_OTP_URL_TEMPLATE),
        "callmebot_configured": bool(CALLMEBOT_API_KEY),
        "channel_order": _otp_channel_order(),
        "last_channel": last_credit_otp_channel,
        "last_error": last_credit_otp_error,
    }


@app.post("/api/clients/verify-credit-otp")
async def verify_credit_otp(request: Request):
    body = await request.json()
    phone = _normalize_phone(body.get("phone", ""))
    otp = re.sub(r"\D", "", str(body.get("otp", "")))[:6]
    method = (body.get("method") or "phone").strip().lower()
    email = _normalize_email(body.get("email", ""))
    record = credit_otp_store.get(phone)
    if len(phone) != 10 or len(otp) != 6 or not record:
        raise HTTPException(status_code=400, detail="Invalid or expired OTP.")
    if float(record.get("expires_at") or 0) < datetime.utcnow().timestamp():
        credit_otp_store.pop(phone, None)
        raise HTTPException(status_code=400, detail="OTP expired. Please send it again.")
    if int(record.get("attempts") or 0) >= 5:
        credit_otp_store.pop(phone, None)
        raise HTTPException(status_code=429, detail="Too many OTP attempts. Please send a new OTP.")
    record["attempts"] = int(record.get("attempts") or 0) + 1
    if record.get("method") == "email":
        if method != "email" or email != _normalize_email(record.get("email", "")):
            raise HTTPException(status_code=400, detail="Email OTP does not match this verification request.")
        if not hmac.compare_digest(record.get("otp_hash", ""), _credit_otp_hash(phone, otp, "email", email)):
            raise HTTPException(status_code=400, detail="Incorrect OTP.")
    elif record.get("provider") == "twofactor" and record.get("twofactor_session_id"):
        try:
            verified = await _verify_credit_otp_twofactor(record.get("twofactor_session_id", ""), otp)
        except Exception as e:
            print(f"2Factor OTP verify failed: {e}")
            raise HTTPException(status_code=400, detail="Could not verify OTP. Please try again.")
        if not verified:
            raise HTTPException(status_code=400, detail="Incorrect OTP.")
    elif not hmac.compare_digest(record.get("otp_hash", ""), _credit_otp_hash(phone, otp, "phone")):
        raise HTTPException(status_code=400, detail="Incorrect OTP.")
    credit_otp_store.pop(phone, None)
    return {"verified": True, "credit_otp_token": _sign_credit_otp_token(phone)}


@app.post("/api/clients")
async def create_or_update_client(request: Request):
    body = await request.json()
    phone = _normalize_phone(body.get("phone", ""))
    if not phone:
        raise HTTPException(status_code=400, detail="Phone is required")
    now = _utc_now_iso()
    existing = _get_client_by_phone(phone)
    business_name = (body.get("business_name") or "").strip() or "New Client"
    with _tracking_conn() as conn:
        if existing:
            conn.execute("""
                UPDATE clients SET
                    business_name=?,
                    contact_name=?,
                    email=?,
                    niche=?,
                    updated_at=?
                WHERE id=?
            """, (
                business_name,
                body.get("contact_name", ""),
                _normalize_email(body.get("email", "")) or existing.get("email", ""),
                body.get("niche", ""),
                now,
                existing["id"],
            ))
            client_id = existing["id"]
        else:
            client_id = str(uuid.uuid4())
            conn.execute("""
                INSERT INTO clients (
                    id, business_name, contact_name, phone, email, niche, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'lead', ?, ?)
            """, (
                client_id,
                business_name,
                body.get("contact_name", ""),
                phone,
                body.get("email", ""),
                body.get("niche", ""),
                now,
                now,
            ))
    client = _get_client_by_id(client_id)
    client.update(_client_credit_status(client))
    return client


@app.post("/api/clients/{client_id}/assign-package")
async def assign_client_package(client_id: str, request: Request):
    body = await request.json()
    client = _assign_package_to_client(client_id, body.get("package_id", ""), body.get("note", ""))
    client.update(_client_credit_status(client))
    return client


@app.post("/api/clients/{client_id}/adjust-credits")
async def adjust_client_credits(client_id: str, request: Request):
    body = await request.json()
    credit_delta = int(body.get("credit_delta") if body.get("credit_delta") is not None else body.get("credits_delta") or 0)
    image_delta = int(body.get("image_delta") or 0)
    video_delta = int(body.get("video_delta") or 0)
    note = body.get("note") or "Manual credit adjustment"
    now = _utc_now_iso()
    existing_client = _get_client_by_id(client_id)
    if not existing_client:
        raise HTTPException(status_code=404, detail="Client not found")
    next_credits = max(0, int(existing_client.get("credits_total") or 0) + credit_delta)
    next_image_total = max(0, int(existing_client.get("image_credits_total") or 0) + image_delta)
    next_video_total = max(0, int(existing_client.get("video_credits_total") or 0) + video_delta)
    with _tracking_conn() as conn:
        conn.execute("""
            UPDATE clients
            SET credits_total=?,
                image_credits_total=?,
                video_credits_total=?,
                updated_at=?
            WHERE id=?
        """, (next_credits, next_image_total, next_video_total, now, client_id))
        conn.execute("""
            INSERT INTO usage_logs (id, client_id, usage_type, quantity, note, created_at)
            VALUES (?, ?, 'credit_adjustment', 0, ?, ?)
        """, (str(uuid.uuid4()), client_id, f"{note}: credits {credit_delta}", now))
    client = _get_client_by_id(client_id)
    client.update(_client_credit_status(client))
    return client


@app.post("/api/clients/adjust-credits-by-phone")
async def adjust_client_credits_by_phone(request: Request):
    body = await request.json()
    phone = _normalize_phone(body.get("phone", ""))
    if len(phone) != 10:
        raise HTTPException(status_code=400, detail="Enter a valid phone number.")
    client = _get_client_by_phone(phone)
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")
    return await adjust_client_credits(client["id"], request)


@app.get("/api/clients/{client_id}/usage")
async def client_usage(client_id: str):
    if not _get_client_by_id(client_id):
        raise HTTPException(status_code=404, detail="Client not found")
    with _tracking_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM usage_logs WHERE client_id=? ORDER BY created_at DESC LIMIT 200",
            (client_id,),
        ).fetchall()
    return [_row_to_dict(row) for row in rows]


@app.get("/api/orders/{order_id}/approve-quick")
async def approve_order_quick(order_id: str, token: str, background_tasks: BackgroundTasks):
    """One-tap approve from WhatsApp link. Token must match CALLMEBOT_API_KEY."""
    if not CALLMEBOT_API_KEY or token != CALLMEBOT_API_KEY:
        raise HTTPException(status_code=403, detail="Invalid token")
    orders = load_orders()
    order = next((o for o in orders if o["id"] == order_id), None)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    if order["status"] != "pending":
        return FileResponse("static/approve-done.html") if os.path.exists("static/approve-done.html") else {"status": order["status"], "message": "Order already processed"}
    # Trigger generation
    img_path = order["product_image_path"]
    if not os.path.exists(img_path):
        raise HTTPException(status_code=400, detail="Product image missing")
    with open(img_path, "rb") as f:
        image_data = f.read()
    job_id = str(uuid.uuid4())
    jobs[job_id] = {"status": "processing", "step": "analyzing", "script": None, "video_url": None, "image_url": None, "error": None, "order_id": order_id}
    customization = {
        "presenter_source": order.get("presenter_source", "ai"), "output_type": order.get("output_type", "video"),
        "video_duration": order.get("video_duration", "30"), "video_quality": order.get("video_quality", "high"),
        "auto_mode": True, "language": order.get("language", "hindi"),
        "model_gender": "female", "skin_tone": "wheatish", "scene": "studio",
        "custom_scene": "", "model_action": order.get("notes", ""),
        "custom_instructions": order.get("notes", ""), "aspect_ratio": order.get("aspect_ratio", "9:16"), "custom_script": order.get("custom_script", ""),
        "image_branding": _order_image_branding(order),
        "video_end_card": _order_video_end_card(order),
    }
    order["status"] = "processing"
    order["job_id"] = job_id
    save_order(order)
    background_tasks.add_task(process_job, job_id, image_data, order.get("product_mime", "image/jpeg"), model_image_url, customization)
    from fastapi.responses import HTMLResponse
    return HTMLResponse("""
    <html><body style="font-family:sans-serif;text-align:center;padding:60px;background:#f0fdf4">
      <h1 style="color:#16a34a">✅ Order Approved!</h1>
      <p>Video generation has started. You can close this page.</p>
      <p style="color:#64748b;font-size:13px">Check your dashboard at <a href="http://127.0.0.1:8000">127.0.0.1:8000</a></p>
    </body></html>
    """)


@app.post("/api/orders/{order_id}/approve")
async def approve_order(order_id: str, background_tasks: BackgroundTasks):
    orders = load_orders()
    order = next((o for o in orders if o["id"] == order_id), None)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    if order["status"] not in ("pending", "failed", "paid"):
        raise HTTPException(status_code=400, detail="Order already processed")
    if order.get("payment_status") == "pending":
        raise HTTPException(status_code=402, detail="Payment is pending")
    job_id = _start_order_generation(order, background_tasks)
    return {"job_id": job_id, "status": "processing"}


@app.post("/api/razorpay/webhook")
async def razorpay_webhook(request: Request, background_tasks: BackgroundTasks):
    if not RAZORPAY_WEBHOOK_SECRET:
        raise HTTPException(status_code=500, detail="Razorpay webhook secret is not configured")
    raw_body = await request.body()
    signature = request.headers.get("X-Razorpay-Signature", "")
    expected = hmac.new(
        RAZORPAY_WEBHOOK_SECRET.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise HTTPException(status_code=400, detail="Invalid Razorpay signature")

    payload = json.loads(raw_body.decode("utf-8"))
    event = payload.get("event", "")
    entity = (payload.get("payload", {}).get("payment_link", {}) or {}).get("entity", {}) or {}
    payment_entity = (payload.get("payload", {}).get("payment", {}) or {}).get("entity", {}) or {}
    order_id = (
        entity.get("reference_id")
        or (entity.get("notes") or {}).get("order_id")
        or (payment_entity.get("notes") or {}).get("order_id")
    )
    notes = entity.get("notes") or payment_entity.get("notes") or {}
    if notes.get("source") == "vishleshak_credit_pack":
        payment_id = notes.get("payment_id", "")
        client_id = notes.get("client_id", "")
        pack_id = notes.get("pack_id", "")
        if not payment_id and order_id:
            with _tracking_conn() as conn:
                row = conn.execute("SELECT * FROM package_payments WHERE order_id=?", (order_id,)).fetchone()
                payment = _row_to_dict(row) if row else None
            if payment:
                payment_id = payment["id"]
                client_id = payment["client_id"]
                pack_id = payment["package_id"]
        if event in ("payment_link.paid", "payment.captured") and client_id and pack_id:
            with _tracking_conn() as conn:
                row = conn.execute("SELECT * FROM package_payments WHERE id=?", (payment_id,)).fetchone()
                existing_payment = _row_to_dict(row) if row else None
            if existing_payment and existing_payment.get("status") == "paid":
                return {"status": "credit_pack_already_applied", "client_id": client_id, "pack_id": pack_id}
            client = _apply_credit_pack_to_client(client_id, pack_id, f"Razorpay credit pack payment {payment_entity.get('id') or entity.get('id') or ''}")
            with _tracking_conn() as conn:
                conn.execute("""
                    UPDATE package_payments
                    SET status='paid', razorpay_payment_id=?, razorpay_payment_link_id=?, paid_at=?
                    WHERE id=?
                """, (
                    payment_entity.get("id", ""),
                    entity.get("id", ""),
                    _utc_now_iso(),
                    payment_id,
                ))
            client.update(_client_credit_status(client))
            return {"status": "credit_pack_applied", "client_id": client_id, "pack_id": pack_id}
        if event in ("payment.failed", "payment_link.cancelled", "payment_link.expired") and payment_id:
            with _tracking_conn() as conn:
                conn.execute("UPDATE package_payments SET status=? WHERE id=?", ("failed", payment_id))
            return {"status": "credit_pack_payment_failed", "payment_id": payment_id}
        return {"status": "credit_pack_ignored", "event": event}
    if not order_id:
        return {"status": "ignored", "reason": "missing_order_id"}

    orders = load_orders()
    order = next((o for o in orders if o.get("id") == order_id), None)
    if not order:
        return {"status": "ignored", "reason": "order_not_found"}

    order["razorpay_event"] = event
    order["razorpay_payment_id"] = payment_entity.get("id") or order.get("razorpay_payment_id")
    order["razorpay_payment_link_id"] = entity.get("id") or order.get("razorpay_payment_link_id")

    if event in ("payment_link.paid", "payment.captured"):
        if order.get("status") in ("processing", "completed"):
            save_order(order)
            return {"status": "already_processing", "order_id": order_id}
        order["payment_status"] = "paid"
        order["paid_at"] = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        order["status"] = "paid"
        job_id = _start_order_generation(order, background_tasks)
        return {"status": "generation_started", "order_id": order_id, "job_id": job_id}

    if event in ("payment.failed", "payment_link.cancelled", "payment_link.expired"):
        order["payment_status"] = "failed"
        order["status"] = "payment_failed"
        save_order(order)
        return {"status": "payment_failed", "order_id": order_id}

    save_order(order)
    return {"status": "ignored", "event": event, "order_id": order_id}


@app.post("/api/orders/{order_id}/approve-seedance")
async def approve_order_seedance(order_id: str, background_tasks: BackgroundTasks):
    """Approve and generate using Seedance 2.0 (animated motion + voiceover, no lip-sync)."""
    orders = load_orders()
    order = next((o for o in orders if o["id"] == order_id), None)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    if order["status"] not in ("pending", "failed"):
        raise HTTPException(status_code=400, detail="Order already processed")
    img_path = order["product_image_path"]
    if not os.path.exists(img_path):
        raise HTTPException(status_code=400, detail="Product image missing")
    with open(img_path, "rb") as f:
        image_data = f.read()
    job_id = str(uuid.uuid4())
    jobs[job_id] = {"status": "processing", "step": "analyzing", "script": None, "video_url": None, "image_url": None, "error": None, "order_id": order_id}
    customization = {
        "presenter_source": order.get("presenter_source", "ai"),
        "output_type": "video",
        "video_duration": "5",
        "video_quality": "standard",
        "seedance_resolution": "480p",
        "auto_mode": True,
        "language": order.get("language", "hindi"),
        "model_gender": "female",
        "skin_tone": "wheatish",
        "scene": "studio",
        "custom_scene": "",
        "model_action": order.get("notes", ""),
        "custom_instructions": order.get("notes", ""),
        "aspect_ratio": order.get("aspect_ratio", "9:16"),
        "custom_script": order.get("custom_script", ""),
        "image_branding": _order_image_branding(order),
        "video_end_card": _order_video_end_card(order),
    }
    order["status"] = "processing"
    order["job_id"] = job_id
    save_order(order)
    background_tasks.add_task(process_job_seedance, job_id, image_data, order.get("product_mime", "image/jpeg"), model_image_url, customization)
    # If order came from WhatsApp, notify customer when done
    wa_from = order.get("wa_from")
    if wa_from:
        product_name = order.get("notes", "your product").replace("WhatsApp order for: ", "")
        background_tasks.add_task(notify_wa_on_complete, job_id, order_id, wa_from, product_name)
    return {"job_id": job_id, "status": "processing"}


@app.post("/api/orders/{order_id}/approve-veo3")
async def approve_order_veo3(order_id: str, background_tasks: BackgroundTasks):
    """Approve and generate using Veo 3 Fast (Google) — 8s cinematic video with native audio."""
    orders = load_orders()
    order = next((o for o in orders if o["id"] == order_id), None)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    if order["status"] not in ("pending", "failed"):
        raise HTTPException(status_code=400, detail="Order already processed")
    img_path = order["product_image_path"]
    if not os.path.exists(img_path):
        raise HTTPException(status_code=400, detail="Product image missing")
    with open(img_path, "rb") as f:
        image_data = f.read()
    job_id = str(uuid.uuid4())
    jobs[job_id] = {"status": "processing", "step": "analyzing", "script": None, "video_url": None, "image_url": None, "error": None, "order_id": order_id}
    customization = {
        "presenter_source": order.get("presenter_source", "ai"),
        "output_type": "video",
        "video_duration": "6",
        "video_quality": "standard",
        "auto_mode": True,
        "language": order.get("language", "hindi"),
        "model_gender": "female",
        "skin_tone": "wheatish",
        "scene": "studio",
        "custom_scene": "",
        "model_action": order.get("notes", ""),
        "custom_instructions": order.get("notes", ""),
        "aspect_ratio": order.get("aspect_ratio", "9:16"),
        "custom_script": order.get("custom_script", ""),
        "order_model_path": order.get("model_image_path") or "",
        "image_branding": _order_image_branding(order),
        "video_end_card": _order_video_end_card(order),
    }
    order["status"] = "processing"
    order["job_id"] = job_id
    order["video_style"] = "veo3"
    save_order(order)
    background_tasks.add_task(process_job_veo3, job_id, image_data, order.get("product_mime", "image/jpeg"), model_image_url, customization)
    wa_from = order.get("wa_from")
    if wa_from:
        product_name = order.get("notes", "your product").replace("WhatsApp order for: ", "")
        background_tasks.add_task(notify_wa_on_complete, job_id, order_id, wa_from, product_name)
    return {"job_id": job_id, "status": "processing"}


@app.post("/api/orders/{order_id}/approve-gemini-omni")
async def approve_order_gemini_omni(order_id: str, background_tasks: BackgroundTasks):
    """Approve and generate using Gemini Omni (Google I/O 2026) — multimodal cinematic UGC video + voiceover."""
    orders = load_orders()
    order = next((o for o in orders if o["id"] == order_id), None)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    if order["status"] not in ("pending", "failed"):
        raise HTTPException(status_code=400, detail="Order already processed")
    img_path = order["product_image_path"]
    if not os.path.exists(img_path):
        raise HTTPException(status_code=400, detail="Product image missing")
    with open(img_path, "rb") as f:
        image_data = f.read()
    job_id = str(uuid.uuid4())
    jobs[job_id] = {"status": "processing", "step": "analyzing", "script": None, "video_url": None, "image_url": None, "error": None, "order_id": order_id}
    customization = {
        "presenter_source":    order.get("presenter_source", "ai"),
        "output_type":         "video",
        "video_duration":      str(order.get("duration", "8")),
        "duration":            int(order.get("duration", 8)),
        "video_quality":       "standard",
        "auto_mode":           True,
        "language":            order.get("language", "hindi"),
        "model_gender":        "female",
        "skin_tone":           "wheatish",
        "scene":               "studio",
        "custom_scene":        "",
        "model_action":        order.get("notes", ""),
        "custom_instructions": order.get("notes", ""),
        "aspect_ratio":        order.get("aspect_ratio", "9:16"),
        "custom_script":       order.get("custom_script", ""),
        "order_model_path":    order.get("model_image_path") or "",
        "image_branding":      _order_image_branding(order),
        "video_end_card":      _order_video_end_card(order),
    }
    order["status"]      = "processing"
    order["job_id"]      = job_id
    order["video_style"] = "gemini_omni"
    save_order(order)
    background_tasks.add_task(
        process_job_gemini_omni,
        job_id, image_data, order.get("product_mime", "image/jpeg"),
        model_image_url, customization,
    )
    wa_from = order.get("wa_from")
    if wa_from:
        product_name = order.get("notes", "your product").replace("WhatsApp order for: ", "")
        background_tasks.add_task(notify_wa_on_complete, job_id, order_id, wa_from, product_name)
    return {"job_id": job_id, "status": "processing", "pipeline": "gemini_omni"}


@app.post("/api/orders/{order_id}/reject")
async def reject_order(order_id: str, reason: str = Form("")):
    orders = load_orders()
    order = next((o for o in orders if o["id"] == order_id), None)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    order["status"] = "rejected"
    order["rejected_reason"] = reason
    save_order(order)
    return {"status": "rejected"}


@app.get("/order_uploads/{filename}")
async def serve_order_upload(filename: str):
    path = os.path.join(ORDERS_UPLOAD_DIR, filename)
    if not os.path.exists(path):
        raise HTTPException(status_code=404)
    return FileResponse(path)


@app.get("/order")
async def serve_order_page():
    return FileResponse("static/order.html", headers={"Cache-Control": "no-store"})

@app.get("/order/result/{order_id}")
async def serve_order_result_page(order_id: str):
    return FileResponse("static/order-result.html", headers={"Cache-Control": "no-store"})


# ── WhatsApp Webhook ──────────────────────────────────────────────────────────

from whatsapp_bot import handle_whatsapp_message, VERIFY_TOKEN as WA_VERIFY_TOKEN, send_text as wa_send_text, send_video as wa_send_video
from retell_webhook import handle_retell_webhook
from restaurant_bot import handle_restaurant_message, send_text as restaurant_send_text, sessions as restaurant_sessions, WELCOME_MSG as RESTAURANT_WELCOME

# Router: tracks which bot each phone number is currently using
# Values: None (not chosen yet) | "restaurant" | "ugc"
_router_sessions: dict = {}
# RESTAURANT_MODE env var is no longer needed — router handles both bots automatically

COMBINED_WELCOME = (
    "👋 *Welcome to Vishleshak AI!*\n\n"
    "What are you looking for today?\n\n"
    "1️⃣  🍽️ *BTT Restaurant* — Menu, Orders & Table Booking\n"
    "2️⃣  🎬 *UGC Video Ads* — AI video ads for your business\n"
    "3️⃣  🦷 *Dental Appointment* — Book at Dr. Akshay Midha Clinic\n\n"
    "_Reply with 1, 2 or 3 to get started!_"
)

# Dental appointment sessions: { phone: { "step": str, "name": str, "service": str, "date": str } }
_dental_sessions: dict = {}
DENTAL_KB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dental_kb.md")


def _load_dental_kb() -> str:
    try:
        with open(DENTAL_KB_PATH, "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        print(f"[Dental] Failed to load KB file: {e}")
        return (
            "Business Name: Dr Akshay Midha Multi Speciality Dental Clinic\n"
            "Phone Number: +91 9868018541\n"
            "Address: C 156, near Moti Nagar Rd, behind Govt Hospital, New Delhi, Delhi 110015\n"
            "Hours: Monday-Friday 9:00 AM-8:00 PM, Saturday 9:00 AM-6:00 PM, Sunday Closed\n"
        )


async def notify_wa_on_complete(job_id: str, order_id: str, wa_from: str, product_name: str):
    """Poll job status and send the finished video to the WhatsApp customer."""
    import asyncio as _asyncio
    for _ in range(120):          # poll up to 20 minutes (120 × 10s)
        await _asyncio.sleep(10)
        job = jobs.get(job_id, {})
        status = job.get("status")
        if status == "completed":
            # Update order to completed
            all_orders = load_orders()
            ord_ = next((o for o in all_orders if o["id"] == order_id), None)
            if ord_:
                ord_["status"] = "completed"
                save_order(ord_)
            # Prefer Cloudinary URL (permanent) → Railway result page fallback
            cdn_video_url = ord_.get("video_url", "") if ord_ else ""
            if cdn_video_url and cdn_video_url.startswith("http"):
                await wa_send_video(
                    wa_from, cdn_video_url,
                    caption=f"🎬 Your video ad for *{product_name}* is ready!\n\nPost it on Instagram/Facebook to boost your sales! 🚀\n\n📞 Order more: wa.me/919953910987"
                )
            elif RAILWAY_URL:
                result_link = f"{RAILWAY_URL}/order/result/{order_id}"
                await wa_send_text(wa_from,
                    f"✅ Your video ad for *{product_name}* is ready!\n\nDownload here: {result_link}\n\n📞 Order more: wa.me/919953910987"
                )
            else:
                await wa_send_text(wa_from,
                    f"✅ Your video ad for *{product_name}* is ready!\n\nPlease contact us to receive the file:\n📞 +91 99539 10987"
                )
            return
        elif status == "failed":
            all_orders = load_orders()
            ord_ = next((o for o in all_orders if o["id"] == order_id), None)
            if ord_:
                ord_["status"] = "failed"
                save_order(ord_)
            await wa_send_text(wa_from,
                f"Sorry, there was an issue creating your video. Our team will contact you shortly.\n📞 +91 99539 10987"
            )
            return

@app.get("/webhook")
async def whatsapp_verify(request: Request):
    """Meta webhook verification — called once when setting up the webhook."""
    params = dict(request.query_params)
    mode      = params.get("hub.mode", "")
    token     = params.get("hub.verify_token", "")
    challenge = params.get("hub.challenge", "")
    if mode == "subscribe" and token == WA_VERIFY_TOKEN:
        print(f"[WA] Webhook verified ✅")
        return PlainTextResponse(challenge)
    return PlainTextResponse("Forbidden", status_code=403)


@app.post("/webhook")
async def whatsapp_receive(request: Request):
    """Receive incoming WhatsApp messages — routes to restaurant or UGC bot."""
    body = await request.json()

    # Extract sender + text for routing
    try:
        messages  = body.get("entry", [{}])[0].get("changes", [{}])[0].get("value", {}).get("messages", [])
        if not messages:
            return {"status": "ok"}
        msg        = messages[0]
        from_phone = msg.get("from", "")
        msg_type   = msg.get("type", "")
        text       = msg.get("text", {}).get("body", "").strip() if msg_type == "text" else ""
    except Exception:
        return {"status": "ok"}

    print(f"[Router] {from_phone}: '{text[:60]}'")

    RESET_WORDS = ("hi", "hello", "hey", "start", "help", "hlo", "hii")

    # ── Reset to main menu ──────────────────────────────────────────
    if text.lower() in RESET_WORDS:
        _router_sessions[from_phone] = None
        restaurant_sessions.pop(from_phone, None)
        _dental_sessions.pop(from_phone, None)
        try:
            await wa_send_text(from_phone, COMBINED_WELCOME)
        except Exception as e:
            print(f"[Router] Failed to send combined welcome: {e}")
        return {"status": "ok"}

    current_bot = _router_sessions.get(from_phone)

    # ── No bot chosen yet — show combined welcome or handle choice ──
    if current_bot is None:
        try:
            if text == "1":
                _router_sessions[from_phone] = "restaurant"
                restaurant_sessions[from_phone] = {"state": "main_menu", "cart": [], "booking": {}}
                await restaurant_send_text(from_phone, RESTAURANT_WELCOME)
            elif text == "2":
                _router_sessions[from_phone] = "ugc"
                await wa_send_text(from_phone,
                    "🎬 *Welcome to Vishleshak UGC Video Ads!*\n\n"
                    "Send me a photo of your product and I'll create a professional AI video ad for you.\n\n"
                    "📱 Formats supported: jewellery, clothing, food, electronics & more.\n"
                    "💰 Starting at just ₹499/video"
                )
            elif text == "3":
                _router_sessions[from_phone] = "dental"
                _dental_sessions[from_phone] = {"step": "ask_name"}
                await wa_send_text(from_phone,
                    "🦷 *Dr. Akshay Midha Multi Speciality Dental Clinic*\n"
                    "📍 C 156, near Moti Nagar Rd, behind Govt Hospital, New Delhi 110015\n"
                    "📞 +91 9868018541\n\n"
                    "Let's book your appointment! 😊\n\n"
                    "What's your *full name*?"
                )
            else:
                await wa_send_text(from_phone, COMBINED_WELCOME)
        except Exception as e:
            print(f"[Router] Failed to send welcome: {e}")
        return {"status": "ok"}

    # ── Route to restaurant bot ─────────────────────────────────────
    if current_bot == "restaurant":
        try:
            await handle_restaurant_message(body)
        except Exception as e:
            import traceback
            print(f"[Router] Restaurant bot crashed: {e}")
            traceback.print_exc()
            try:
                await restaurant_send_text(from_phone, f"Sorry, something went wrong! 😅 Please type *hi* to restart.")
            except Exception:
                pass
        return {"status": "ok"}

    # ── Route to dental bot ─────────────────────────────────────────
    if current_bot == "dental":
        dental = _dental_sessions.get(from_phone, {"step": "ask_name"})
        step = dental.get("step")

        DENTAL_KB = """
Business Name: Dr Akshay Midha Multi Speciality Dental Clinic
Phone: +91 9868018541
Address: C 156, near Moti Nagar Rd, behind Govt Hospital, New Delhi 110015

Hours: Mon–Fri 9am–8pm | Saturday 9am–6pm | Sunday CLOSED

Services: Checkups, Cleaning, Fillings, Root Canal, Extractions, X-rays, Teeth Whitening, Smile Design, Veneers, Crowns, Bridges, Dentures, Implants, Braces, Invisalign, Retainers, Children's Dentistry, Emergency Care

Payments: Cash, UPI, Credit/Debit cards. Insurance: select providers, contact clinic to verify.

New patients: arrive 10–15 min early. Cancellation: 24 hours notice. Standard appointment: 30 min.
Walk-ins: accepted based on availability. Cleanings: recommended every 6 months.

Emergency (severe swelling, heavy bleeding, difficulty breathing, knocked-out tooth, extreme pain): go to clinic immediately or call.
"""

        STEP_PROMPTS = {
            "ask_name": "What's your *full name*?",
            "ask_service": "What type of appointment do you need?\n\n1️⃣ Routine Checkup / Cleaning\n2️⃣ Root Canal / Filling\n3️⃣ Teeth Whitening / Smile Design\n4️⃣ Braces / Invisalign\n5️⃣ Tooth Pain / Emergency\n6️⃣ Other\n\n_Reply with a number_",
            "ask_date": "📅 What *date* works for you?\n\n_Example: Monday 2 June or Tomorrow_",
            "ask_time": "What time works for you? e.g. *2 PM*, *10:30 AM*, *4:30 PM*",
        }

        import re as _re
        _tl = text.lower().strip()
        # Normalise EVERYWHERE: "4 PM" → "4pm", "10 AM" → "10am", "2.30" → "2:30pm"
        _tl = _re.sub(r'\b(\d{1,2})\s+(am|pm)\b', r'\1\2', _tl)
        _tl = _re.sub(r'\b(\d{1,2})[\.:](\d{2})\s*(am|pm)\b', r'\1:\2\3', _tl)
        _tl = _re.sub(r'\b(\d{1,2})[\.:](\d{2})\b', r'\1:\2pm', _tl)

        def _looks_like_time_answer(value: str) -> bool:
            v = value.lower().strip()
            v = _re.sub(r'\b(\d{1,2})\s+(am|pm)\b', r'\1\2', v)
            return (
                v in ("1", "2", "3")
                or any(w in v for w in ("am", "pm", "morning", "afternoon", "evening", "noon"))
                or bool(_re.search(r'\b(?:[1-9]|1[0-2])[\.:]\d{2}\b', v))
                or bool(_re.fullmatch(r'(?:[4-9]|1[0-2])', v))
            )

        # Acknowledgment words — just re-prompt, don't treat as FAQ
        _ACK_WORDS = ("sorry", "ok", "okay", "thanks", "thank you", "got it", "alright", "fine", "sure", "noted", "yes", "yep")
        if _tl in _ACK_WORDS and step in STEP_PROMPTS:
            await wa_send_text(from_phone, STEP_PROMPTS[step])
            return {"status": "ok"}

        if step == "ask_time":
            dental["_raw_time_text"] = text.strip()

        if step == "ask_time":
            # If alt_slots exist, handle carefully
            _alt_slots = dental.get("alt_slots", [])
            if _alt_slots:
                if text.strip() in ("1", "2", "3"):
                    # User picked a numbered alternative
                    idx = int(text.strip()) - 1
                    if idx < len(_alt_slots):
                        _picked = _alt_slots[idx]
                        import re as _re2
                        _m = _re2.search(r'(\d+):?\d*\s*(am|pm)', _picked.lower())
                        if _m:
                            _ph = int(_m.group(1))
                            if _m.group(2) == "pm" and _ph != 12:
                                _ph += 12
                            dental["gcal_hour"] = _ph
                        dental["time_label"] = _picked
                        dental["alt_slots"] = []
                        text = _picked
                        _tl = _picked.lower()
                    else:
                        await wa_send_text(from_phone, f"Please reply with *1*, *2*, or *3* to pick one of the available slots.")
                        return {"status": "ok"}
                else:
                    # User typed something else — check if it's a NEW valid time (not the booked one)
                    # Normalize to see if it's a time
                    _try_tl = text.lower().strip()
                    _try_tl = _re.sub(r'\b(\d{1,2})[\.:](\d{2})\s*(am|pm)\b', r'\1:\2\3', _try_tl)
                    _try_tl = _re.sub(r'\b(\d{1,2})[\.:](\d{2})\b', r'\1:\2pm', _try_tl)
                    _has_time = any(w in _try_tl for w in ["am","pm","morning","afternoon","evening"])
                    if not _has_time:
                        # Not a recognizable time — remind them
                        emojis = ["1.", "2.", "3."]
                        alts = "\n".join([f"{emojis[i]} {a}" for i, a in enumerate(_alt_slots)])
                        await wa_send_text(from_phone,
                            f"Please pick one of the available slots:\n{alts}\n\n"
                            f"_Reply with 1, 2 or 3_"
                        )
                        return {"status": "ok"}
                    else:
                        # They gave a new time — clear alt_slots and process normally
                        dental["alt_slots"] = []
                        _dental_sessions[from_phone] = dental

            # If already a valid slot number (no alt_slots), use directly — skip normalisation
            if not dental.get("alt_slots") and _looks_like_time_answer(text):
                _slot_labels = {
                    "1": "Morning (9am-12pm)",
                    "2": "Afternoon (12pm-4pm)",
                    "3": "Evening (4pm-8pm)",
                }
                if text.strip() in _slot_labels:
                    dental["time_label"] = _slot_labels[text.strip()]
                    dental["gcal_hour"] = {"1": 9, "2": 12, "3": 16}[text.strip()]
                # Normalise dot/colon notation: "2.30"→"2:30pm", "7.00"→"7:00pm"
                _tl = _re.sub(r'\b(\d{1,2})[\.:](\d{2})\s*(am|pm)\b', r'\1:\2\3', _tl)
                _tl = _re.sub(r'\b(\d{1,2})[\.:](\d{2})\b', r'\1:\2pm', _tl)
                # Bare hour: "at 5", "book for 7" → "5pm", "7pm" (only 1–8 treated as pm)
                def _bare_hour(m):
                    if text.strip() in ("1", "2", "3"):
                        return m.group(0)
                    h = int(m.group(1))
                    if 9 <= h <= 12:
                        return f"{h}am"
                    return f"{h}pm" if 1 <= h <= 8 else m.group(0)
                _tl = _re.sub(r'\b(\d{1,2})\b(?!\s*(?:am|pm|[:\.])\d?)', _bare_hour, _tl)

                # Detect after-hours and warn
                _after_hours = any(w in _tl for w in ("9pm","10pm","11pm","12am","midnight"))
                _sat_after   = any(w in _tl for w in ("7pm","8pm")) and "saturday" in _tl
                if _after_hours or _sat_after:
                    await wa_send_text(from_phone,
                        "Sorry, that time is *outside our working hours*.\n\n"
                        "Clinic hours: Mon-Fri 9am-8pm | Sat 9am-6pm\n\n"
                        "Please choose a valid *time slot*:\n\n"
                        "1. Morning (9am-12pm)\n"
                        "2. Afternoon (12pm-4pm)\n"
                        "3. Evening (4pm-8pm)\n\n"
                        "_Reply with 1, 2 or 3_"
                    )
                    return {"status": "ok"}

                # Lookup specific hour from normalised text
                _time_map = {
                    "9am":9,"9:00am":9,"10am":10,"10:30am":10.5,"11am":11,"11:30am":11.5,
                    "12pm":12,"12:30pm":12.5,"1pm":13,"1:30pm":13.5,"2pm":14,"2:30pm":14.5,"3pm":15,"3:30pm":15.5,
                    "4pm":16,"4:30pm":16.5,"5pm":17,"5:30pm":17.5,"6pm":18,"6:30pm":18.5,
                    "7pm":19,"7:30pm":19.5,"8pm":20
                }
                _specific_hour = None
                for t, h in _time_map.items():
                    if t in _tl:
                        _specific_hour = h
                        break

                # Map to slot number using specific hour (most reliable) or keyword
                if _specific_hour is not None and text.strip() not in ("1", "2", "3"):
                    dental["gcal_hour"] = _specific_hour
                    _raw_time = dental.get("_raw_time_text") or text
                    _bare_time = _re.fullmatch(r'(\d{1,2})(?:[\.:](\d{2}))?', _raw_time.strip())
                    if _bare_time:
                        _display_hour = int(_specific_hour) if _specific_hour <= 12 else int(_specific_hour) - 12
                        _display_minute = _bare_time.group(2)
                        _display_period = "AM" if _specific_hour < 12 else "PM"
                        if _display_minute:
                            dental["time_label"] = f"{_display_hour}:{_display_minute} {_display_period}"
                        else:
                            dental["time_label"] = f"{_display_hour} {_display_period}"
                    elif _re.fullmatch(r'(?:[4-9]|1[0-2])', _raw_time.strip()):
                        _display_hour = _specific_hour if _specific_hour <= 12 else _specific_hour - 12
                        _display_period = "AM" if _specific_hour < 12 else "PM"
                        dental["time_label"] = f"{_display_hour} {_display_period}"
                    else:
                        dental["time_label"] = _raw_time
                    text = "1" if _specific_hour < 12 else ("2" if _specific_hour < 16 else "3")
                elif text.strip() in ("1", "2", "3"):
                    pass
                elif any(w in _tl for w in ("morning",)):
                    dental["time_label"] = "Morning (9am-12pm)"
                    text = "1"
                elif any(w in _tl for w in ("afternoon","noon")):
                    dental["time_label"] = "Afternoon (12pm-4pm)"
                    text = "2"
                elif any(w in _tl for w in ("evening",)):
                    dental["time_label"] = "Evening (4pm-8pm)"
                    text = "3"

        # Normalise natural language service inputs at ask_service step
        if step == "ask_service":
            if any(w in _tl for w in ("checkup", "cleaning", "check up")):
                text = "1"
            elif any(w in _tl for w in ("root canal", "filling", "cavity")):
                text = "2"
            elif any(w in _tl for w in ("whitening", "white", "smile")):
                text = "3"
            elif any(w in _tl for w in ("brace", "invisalign", "align")):
                text = "4"
            elif any(w in _tl for w in ("pain", "emergency", "urgent", "ache")):
                text = "5"

        # Detect if input is a question rather than a step answer
        _is_question = (
            "?" in text
            or (step == "ask_service" and text.strip() not in ("1","2","3","4","5","6"))
            or (step == "ask_time" and not _looks_like_time_answer(text))
            or (step == "ask_date" and not _extract_appointment_time(text) and any(w in _tl for w in ("sunday", "closed", "holiday", "open", "hours", "timing")))
        )

        if _is_question:
            try:
                resp = anthropic_client.messages.create(
                    model="claude-haiku-4-5",
                    max_tokens=200,
                    system=(
                        "You are a WhatsApp assistant for a dental clinic. "
                        "Answer the patient's question using ONLY the knowledge base below. "
                        "Be brief (2-3 sentences max), friendly, use WhatsApp formatting (*bold*). "
                        "Never give medical advice or diagnoses.\n\n"
                        f"KNOWLEDGE BASE:\n{_load_dental_kb()}"
                    ),
                    messages=[{"role": "user", "content": text}]
                )
                answer = resp.content[0].text.strip()
            except Exception as e:
                print(f"[Dental FAQ] Claude error: {e}")
                answer = "For accurate information please call 📞 *+91 9868018541*."
            await wa_send_text(from_phone, answer + "\n\n" + STEP_PROMPTS.get(step, "What's your *full name*?"))
            return {"status": "ok"}

        try:
            if step == "ask_name":
                dental["name"] = text
                dental["step"] = "ask_service"
                _dental_sessions[from_phone] = dental
                await wa_send_text(from_phone,
                    f"Nice to meet you, *{text}*! 😊\n\n"
                    "What type of appointment do you need?\n\n"
                    "1️⃣ Routine Checkup / Cleaning\n"
                    "2️⃣ Root Canal / Filling\n"
                    "3️⃣ Teeth Whitening / Smile Design\n"
                    "4️⃣ Braces / Invisalign\n"
                    "5️⃣ Tooth Pain / Emergency\n"
                    "6️⃣ Other\n\n"
                    "_Reply with a number_"
                )
            elif step == "ask_service":
                services = {"1": "Routine Checkup / Cleaning", "2": "Root Canal / Filling", "3": "Teeth Whitening / Smile Design", "4": "Braces / Invisalign", "5": "Tooth Pain / Emergency", "6": "Other"}
                dental["service"] = services.get(text, text)
                dental["step"] = "ask_date"
                _dental_sessions[from_phone] = dental
                await wa_send_text(from_phone,
                    "📅 What *date* works for you?\n\n"
                    "_Example: Monday 2 June or Tomorrow_"
                )
            elif step == "clarify_date":
                options = dental.get("date_options", {})
                if text.strip() not in ("1", "2") or not options:
                    today_label = options.get("today_label", "today")
                    next_label = options.get("next_label", "next week")
                    await wa_send_text(from_phone,
                        "Please confirm the date:\n\n"
                        f"1. {today_label}\n"
                        f"2. {next_label}\n\n"
                        "_Reply with 1 or 2_"
                    )
                    return {"status": "ok"}

                dental["date"] = options["today_value"] if text.strip() == "1" else options["next_value"]
                dental.pop("date_options", None)
                dental["step"] = "ask_time"
                _dental_sessions[from_phone] = dental
                await wa_send_text(from_phone,
                    "What time works for you?\n\n"
                    "_e.g. 10:30 AM, 2 PM, 4:30 PM_\n\n"
                    "Clinic hours: Mon-Fri 9am-8pm | Sat 9am-6pm\n\n"
                )
            elif step == "ask_date":
                date_options = _same_weekday_date_options(text)
                if date_options:
                    dental["date_options"] = date_options
                    dental["step"] = "clarify_date"
                    _dental_sessions[from_phone] = dental
                    await wa_send_text(from_phone,
                        "Do you mean:\n\n"
                        f"1. {date_options['today_label']}\n"
                        f"2. {date_options['next_label']}\n\n"
                        "_Reply with 1 or 2_"
                    )
                    return {"status": "ok"}

                combined_time = _extract_appointment_time(text)
                if combined_time and not combined_time["date_text"]:
                    await wa_send_text(from_phone,
                        "Please include the date as well.\n\n"
                        "_Example: Today 4 PM, Tomorrow 11 AM, or Friday 4:30 PM_"
                    )
                    return {"status": "ok"}

                if combined_time and combined_time["date_text"]:
                    dental["date"] = combined_time["date_text"]
                    dental["time"] = combined_time["label"]
                    dental["gcal_hour"] = combined_time["hour"]

                    hours_issue = _clinic_hours_issue(dental["date"], dental["gcal_hour"])
                    if hours_issue:
                        await wa_send_text(from_phone,
                            f"{hours_issue}\n\n"
                            "Clinic hours: Mon-Fri 9am-8pm | Sat 9am-6pm\n\n"
                            "Please choose another *date and time*:\n\n"
                            "_Example: Monday 2 June or Tuesday 5 PM_"
                        )
                        return {"status": "ok"}

                    await wa_send_text(
                        from_phone,
                        f"Got it. Checking availability for {_format_date(dental['date'], dental.get('gcal_hour'))} at {dental['time']}..."
                    )

                    _conflicts = await check_gcal_conflict(dental["date"], dental["gcal_hour"])
                    if _conflicts:
                        emojis = ["1.", "2.", "3."]
                        alts = "\n".join([f"{emojis[i]} {a}" for i, a in enumerate(_conflicts)])
                        dental["alt_slots"] = _conflicts
                        dental["step"] = "ask_time"
                        _dental_sessions[from_phone] = dental
                        await wa_send_text(from_phone,
                            f"Sorry, *{dental['time']}* on that day is already booked.\n\n"
                            f"Available slots:\n{alts}\n\n"
                            f"_Reply with 1, 2 or 3 to pick a slot, or type another time._"
                        )
                        return {"status": "ok"}

                    owner_wa = os.getenv("CLINIC_OWNER_WA", "919953910987")
                    _fmt_date = _format_date(dental["date"], dental.get("gcal_hour"))
                    summary = (
                        f"*New Appointment Request*\n\n"
                        f"Name: {dental['name']}\n"
                        f"Service: {dental['service']}\n"
                        f"Date: {_fmt_date}\n"
                        f"Time: {dental['time']}\n"
                        f"WhatsApp: {from_phone}"
                    )
                    cal_link = await create_gcal_event(
                        dental["name"], dental["service"], dental["date"], dental["time"], from_phone, dental.get("gcal_hour")
                    )
                    try:
                        await wa_send_text(owner_wa, summary)
                    except Exception as e:
                        print(f"[Dental] Failed to notify owner: {e}")
                    cal_line = f"\nCalendar: {cal_link}" if cal_link else ""
                    await wa_send_text(from_phone,
                        f"*Appointment Request Sent!*\n\n"
                        f"*{dental['service']}*\n"
                        f"{_fmt_date} - {dental['time']}{cal_line}\n\n"
                        f"The clinic will confirm your slot shortly.\n\n"
                        f"*Dr. Akshay Midha Multi Speciality Dental Clinic*\n"
                        f"C 156, near Moti Nagar Rd, behind Govt Hospital, New Delhi 110015\n"
                        f"+91 9868018541\n\n"
                        f"_Type *hi* to go back to the main menu._"
                    )
                    _dental_sessions.pop(from_phone, None)
                    _router_sessions.pop(from_phone, None)
                    return {"status": "ok"}

                dental["date"] = text
                # Try to extract time from the date input
                _dl = text.lower()
                _dl = _re.sub(r'\b(\d{1,2})\s+(am|pm)\b', r'\1\2', _dl)
                _dl = _re.sub(r'\b(\d{1,2})[\.:](\d{2})\s*(am|pm)\b', r'\1:\2\3', _dl)
                _dl = _re.sub(r'\b(\d{1,2})[\.:](\d{2})\b', r'\1:\2pm', _dl)

                # Warn about after-hours times in the date input
                _after_hours_date = any(w in _dl for w in ("9pm", "9 pm", "10pm", "10 pm", "11pm", "11 pm", "12am", "midnight"))
                if _after_hours_date:
                    await wa_send_text(from_phone,
                        "*9 PM and later is outside our working hours.*\n\n"
                        "Clinic hours: Mon-Fri 9am-8pm | Sat 9am-6pm\n\n"
                        "Please choose another *date and time*:\n\n"
                        "_Example: Monday 2 June or Tuesday 5 PM_"
                    )
                    return {"status": "ok"}

                # Detect specific hour and slot from date input
                _time_map_date = {
                    "9am": 9, "9 am": 9, "10am": 10, "10 am": 10, "11am": 11, "11 am": 11,
                    "12pm": 12, "12 pm": 12, "1pm": 13, "1 pm": 13, "2pm": 14, "2 pm": 14, "3pm": 15, "3 pm": 15,
                    "4pm": 16, "4 pm": 16, "5pm": 17, "5 pm": 17, "6pm": 18, "6 pm": 18,
                    "7pm": 19, "7 pm": 19, "8pm": 20, "8 pm": 20,
                    "10:30am": 10.5, "11:30am": 11.5, "12:30pm": 12.5, "1:30pm": 13.5,
                    "2:30pm": 14.5, "3:30pm": 15.5, "4:30pm": 16.5, "5:30pm": 17.5,
                    "6:30pm": 18.5, "7:30pm": 19.5
                }
                _auto_slot = None
                _auto_hour = None
                for t, h in _time_map_date.items():
                    if t in _dl:
                        _auto_hour = h
                        break
                if any(w in _dl for w in ("morning",)) or (_auto_hour and _auto_hour < 12):
                    _auto_slot = "Morning (9am–12pm)"
                elif any(w in _dl for w in ("noon", "afternoon")) or (_auto_hour and 12 <= _auto_hour < 16):
                    _auto_slot = "Afternoon (12pm–4pm)"
                elif any(w in _dl for w in ("evening",)) or (_auto_hour and _auto_hour >= 16):
                    _auto_slot = "Evening (4pm–8pm)"

                if _auto_slot:
                    # Time already specified — check conflict first
                    dental["time"] = _auto_slot
                    if _auto_hour:
                        dental["gcal_hour"] = _auto_hour
                    hours_issue = _clinic_hours_issue(dental['date'], dental.get('gcal_hour') or 9)
                    if hours_issue:
                        await wa_send_text(from_phone,
                            f"{hours_issue}\n\n"
                            "Clinic hours: Mon-Fri 9am-8pm | Sat 9am-6pm\n\n"
                            "Please choose another *date and time*:\n\n"
                            "_Example: Monday 2 June or Tuesday 5 PM_"
                        )
                        return {"status": "ok"}
                    _conflicts = await check_gcal_conflict(dental['date'], _auto_hour or 9)
                    if _conflicts:
                        emojis = ["1.", "2.", "3."]
                        alts = "\n".join([f"{emojis[i]} {a}" for i, a in enumerate(_conflicts)])
                        dental["alt_slots"] = _conflicts
                        dental["step"] = "ask_time"
                        _dental_sessions[from_phone] = dental
                        await wa_send_text(from_phone,
                            f"Sorry, *{_format_hour_value(_auto_hour)}* on that day is already booked.\n\n"
                            f"Available slots:\n{alts}\n\n"
                            f"_Reply with 1, 2 or 3 to pick a slot, or type another time._"
                        )
                        return {"status": "ok"}
                    owner_wa = os.getenv("CLINIC_OWNER_WA", "919953910987")
                    summary = (
                        f"*New Appointment Request*\n\n"
                        f"Name: {dental['name']}\n"
                        f"Service: {dental['service']}\n"
                        f"Date: {_format_date(dental['date'], dental.get('gcal_hour'))}\n"
                        f"Time: {dental['time']}\n"
                        f"WhatsApp: {from_phone}"
                    )
                    cal_link = await create_gcal_event(dental['name'], dental['service'], dental['date'], dental['time'], from_phone, dental.get('gcal_hour'))
                    try:
                        await wa_send_text(owner_wa, summary)
                    except Exception as e:
                        print(f"[Dental] Failed to notify owner: {e}")
                    cal_line = f"\nCalendar: {cal_link}" if cal_link else ""
                    await wa_send_text(from_phone,
                        f"*Appointment Request Sent!*\n\n"
                        f"*{dental['service']}*\n"
                        f"{_format_date(dental['date'], dental.get('gcal_hour'))} - {dental['time']}{cal_line}\n\n"
                        f"The clinic will confirm your slot shortly.\n\n"
                        f"*Dr. Akshay Midha Multi Speciality Dental Clinic*\n"
                        f"C 156, near Moti Nagar Rd, behind Govt Hospital, New Delhi 110015\n"
                        f"+91 9868018541\n\n"
                        f"_Type *hi* to go back to the main menu._"
                    )
                    _dental_sessions.pop(from_phone, None)
                    _router_sessions.pop(from_phone, None)
                else:
                    # No time given — just ask for a specific time
                    dental["step"] = "ask_time"
                    _dental_sessions[from_phone] = dental
                    await wa_send_text(from_phone,
                        "What time works for you?\n\n"
                        "_e.g. 10:30 AM, 2 PM, 4:30 PM_\n\n"
                        "Clinic hours: Mon-Fri 9am-8pm | Sat 9am-6pm\n\n"
                    )
            elif step == "ask_time":
                dental["time"] = dental.pop("time_label", None) or dental.pop("_raw_time_text", None) or text
                _check_hour = dental.get("gcal_hour") or 9
                hours_issue = _clinic_hours_issue(dental['date'], _check_hour)
                if hours_issue:
                    await wa_send_text(from_phone,
                        f"{hours_issue}\n\n"
                        "Clinic hours: Mon-Fri 9am-8pm | Sat 9am-6pm\n\n"
                        "What time works for you?\n\n"
                        "_e.g. 10:30 AM, 2 PM, 4:30 PM_"
                    )
                    return {"status": "ok"}
                # Check for conflicts before booking
                _conflicts = await check_gcal_conflict(dental['date'], _check_hour)
                if _conflicts:
                    emojis = ["1.", "2.", "3."]
                    alts = "\n".join([f"{emojis[i]} {a}" for i, a in enumerate(_conflicts)])
                    dental["alt_slots"] = _conflicts
                    dental["step"] = "ask_time"
                    _dental_sessions[from_phone] = dental
                    await wa_send_text(from_phone,
                        f"Sorry, that slot is already *booked*.\n\n"
                        f"Available slots:\n{alts}\n\n"
                        f"_Reply with 1, 2 or 3 to pick a slot, or type another time._"
                    )
                    return {"status": "ok"}
                # Notify clinic owner
                owner_wa = os.getenv("CLINIC_OWNER_WA", "919953910987")
                _fmt_date = _format_date(dental['date'], dental.get('gcal_hour'))
                summary = (
                    f"*New Appointment Request*\n\n"
                    f"Name: {dental['name']}\n"
                    f"Service: {dental['service']}\n"
                    f"Date: {_fmt_date}\n"
                    f"Time: {dental['time']}\n"
                    f"WhatsApp: {from_phone}"
                )
                # Create Google Calendar event
                cal_link = await create_gcal_event(
                    dental['name'], dental['service'], dental['date'], dental['time'], from_phone, dental.get('gcal_hour')
                )
                try:
                    await wa_send_text(owner_wa, summary)
                except Exception as e:
                    print(f"[Dental] Failed to notify owner: {e}")
                # Confirm to patient
                cal_line = f"\nCalendar: {cal_link}" if cal_link else ""
                await wa_send_text(from_phone,
                    f"*Appointment Request Sent!*\n\n"
                    f"*{dental['service']}*\n"
                    f"{_fmt_date} - {dental['time']}{cal_line}\n\n"
                    f"The clinic will confirm your slot shortly.\n\n"
                    f"*Dr. Akshay Midha Multi Speciality Dental Clinic*\n"
                    f"C 156, near Moti Nagar Rd, behind Govt Hospital, New Delhi 110015\n"
                    f"+91 9868018541\n\n"
                    f"_Type *hi* to go back to the main menu._"
                )
                _dental_sessions.pop(from_phone, None)
                _router_sessions.pop(from_phone, None)
            else:
                _dental_sessions[from_phone] = {"step": "ask_name"}
                await wa_send_text(from_phone, "What's your *full name*?")
        except Exception as e:
            print(f"[Dental] Error: {e}")
            try:
                await wa_send_text(
                    from_phone,
                    "Sorry, something went wrong while booking that appointment. Please send the date and time again, e.g. *Today 4 PM* or *Friday 4:30 PM*."
                )
            except Exception:
                pass
        return {"status": "ok"}

    # ── Route to UGC bot ────────────────────────────────────────────

    async def _process_wa_order(order_id: str):
        """Wrapper to process a WhatsApp order using existing pipeline."""
        orders = load_orders()
        order = next((o for o in orders if o["id"] == order_id), None)
        if not order:
            raise Exception(f"Order {order_id} not found")
        img_path = order["product_image_path"]
        with open(img_path, "rb") as f:
            image_data = f.read()
        job_id = str(uuid.uuid4())
        jobs[job_id] = {"status": "processing", "step": "analyzing", "script": None, "video_url": None, "image_url": None, "error": None, "order_id": order_id}
        customization = {
            "presenter_source": "ai",
            "output_type": "video",
            "video_duration": "5",
            "video_quality": "standard",
            "seedance_resolution": "480p",
            "auto_mode": True,
            "language": order.get("language", "hindi"),
            "model_gender": "female",
            "skin_tone": "wheatish",
            "scene": "studio",
            "custom_scene": "",
            "model_action": order.get("notes", ""),
            "custom_instructions": order.get("notes", ""),
            "aspect_ratio": "9:16",
            "custom_script": "",
            "image_branding": _order_image_branding(order),
            "video_end_card": _order_video_end_card(order),
        }
        order["status"] = "processing"
        order["job_id"] = job_id
        save_order(order)
        pipeline = process_job_seedance if order.get("video_style") == "seedance" else process_job
        await pipeline(job_id, image_data, "image/jpeg", model_image_url, customization)
        # Update order status
        orders2 = load_orders()
        order2 = next((o for o in orders2 if o["id"] == order_id), None)
        if order2:
            order2["status"] = "completed"
            order2["job_id"] = job_id
            save_order(order2)

    asyncio.create_task(handle_whatsapp_message(body, _process_wa_order))
    return {"status": "ok"}


app.mount("/static", StaticFiles(directory="static"), name="static")


# ── Background job ────────────────────────────────────────────────────────────

async def process_job(job_id: str, image_data: bytes, content_type: str, avatar_url: str, customization: dict | None = None):
    try:
        c = customization or {}
        language       = c.get("language", "hindi")
        aspect_ratio   = c.get("aspect_ratio", "9:16")
        output_type    = c.get("output_type", "video")
        video_duration = c.get("video_duration", "5")
        video_quality  = c.get("video_quality", "high")
        custom_script  = c.get("custom_script", "").strip()

        # Derive script word target from duration — keep scripts SHORT to control Kling cost
        duration_word_targets = {"5": "8-12", "6": "10-14", "10": "15-20", "15": "20-30", "30": "50-65", "60": "100-120"}
        script_word_target = duration_word_targets.get(video_duration, "10-14")

        # Derive D-ID stitch setting and ElevenLabs model from quality
        did_stitch   = video_quality in ("high", "ultra")
        elevenlabs_model = "eleven_turbo_v2_5" if video_quality == "standard" else "eleven_multilingual_v2"

        if custom_script:
            # Skip Claude — use the user-provided (possibly edited) script
            jobs[job_id]["step"] = "analyzing"
            script = custom_script
            avatar_prompt = c.get("model_action", "").strip() or "talking to camera naturally, expressive"
            product_type  = _force_product_type("other", _combined_generation_text(c, script, avatar_prompt))
            if product_type == "medical":
                c = {**c, "scene": "clinic"}
                avatar_prompt = (
                    "same reference person styled in doctor coat or clean medical attire, "
                    "professionally presenting the stethoscope in a clinic"
                )
            ai_settings   = {}
            jobs[job_id]["script"] = script
        else:
            # Step 1: Claude analyzes product → script + avatar prompt + product type
            jobs[job_id]["step"] = "analyzing"
            image_b64 = base64.b64encode(image_data).decode("utf-8")
            script, avatar_prompt, product_type, ai_settings = await asyncio.to_thread(
                generate_script, image_b64, content_type, c
            )
            jobs[job_id]["script"] = script

        # If auto mode, override manual customization with AI-decided settings
        if c.get("auto_mode") and ai_settings:
            c = {**c, **ai_settings}
        product_type = _force_product_type(product_type, _combined_generation_text(c, script, avatar_prompt))
        if product_type == "medical":
            c = {**c, "scene": "clinic"}

        gender = c.get("model_gender", "female")

        if KIE_API_KEY:
            # ── KIE.AI PIPELINE (best quality) ───────────────────────────────
            # Step 1: Composite model + product via 4o Image
            jobs[job_id]["step"] = "compositing_product"
            model_with_product_url = await generate_model_with_product(
                avatar_url, image_data, content_type, product_type, avatar_prompt, c
            )

            # Image-only mode: skip audio/video and return just the composite image
            if output_type == "image":
                final_image_url = await download_and_save_image(
                    model_with_product_url,
                    job_id,
                    c.get("image_branding"),
                    apply_overlay=True,
                )
                jobs[job_id].update({"status": "completed", "step": "completed", "image_url": final_image_url})
                save_to_history({
                    "id": job_id,
                    "date": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "script": script,
                    "image_url": final_image_url,
                    "output_type": "image",
                    "product_type": product_type,
                    "language": language,
                })
                # Upload to Cloudinary → push CDN URL to Railway
                oid = jobs[job_id].get("order_id")
                if oid:
                    local_img = os.path.join(os.path.dirname(__file__), "static", "images", f"{job_id}.jpg")
                    cdn_url = await _upload_to_cloudinary(local_img, job_id, resource_type="image") if os.path.exists(local_img) else None
                    public_image_url = cdn_url or (f"{PUBLIC_URL}{final_image_url}" if PUBLIC_URL else final_image_url)
                    asyncio.create_task(_push_result_to_railway(oid, "", public_image_url, script))
                return

            # Step 2: TTS via edge-tts + fal.ai storage (0x0.st is dead)
            jobs[job_id]["step"] = "generating_audio"
            audio_url = await generate_audio_fal(script, gender, language)

            # Step 3: Kling Avatar lip-sync
            jobs[job_id]["step"] = "generating_video"
            video_task_id = await create_avatar_video(model_with_product_url, audio_url, avatar_prompt)

            jobs[job_id]["step"] = "processing_video"
            raw_video_url = await poll_task(video_task_id)

        elif FAL_KEY:
            # ── FAL.AI PIPELINE ───────────────────────────────────────────────
            jobs[job_id]["step"] = "generating_audio"
            audio_url, product_url = await asyncio.gather(
                generate_audio_fal(script, gender, language),
                upload_image_to_fal(image_data, content_type),
            )
            jobs[job_id]["step"] = "generating_video"
            raw_video_url = await create_fal_video(avatar_url, audio_url, avatar_prompt, product_url)

        elif DID_API_KEY:
            # ── D-ID PIPELINE ─────────────────────────────────────────────────
            jobs[job_id]["step"] = "generating_video"
            raw_video_url = await create_did_talk(avatar_url, script, gender)

        else:
            raise Exception("No API key configured. Please set KIE_API_KEY, FAL_KEY, or DID_API_KEY in .env")

        # Final: re-encode at correct aspect ratio
        final_url = await download_and_reencode_video(raw_video_url, job_id, aspect_ratio)
        final_url = await append_video_end_card(final_url, job_id, aspect_ratio, c.get("video_end_card"))

        jobs[job_id].update({"status": "completed", "step": "completed", "video_url": final_url})

        # Persist to history
        save_to_history({
            "id": job_id,
            "date": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "script": script,
            "video_url": final_url,
            "product_type": product_type,
            "language": language,
        })

        # Upload to Cloudinary → push CDN URL to Railway
        oid = jobs[job_id].get("order_id")
        if oid:
            local_path = os.path.join(os.path.dirname(__file__), "static", "videos", f"{job_id}.mp4")
            cdn_url = await _upload_to_cloudinary(local_path, job_id)
            public_video_url = cdn_url or (f"{PUBLIC_URL}{final_url}" if PUBLIC_URL else final_url)
            asyncio.create_task(_push_result_to_railway(oid, public_video_url, "", script))
            asyncio.create_task(_delete_model_photos(oid))

    except Exception as e:
        jobs[job_id].update({"status": "failed", "error": str(e)})


# ── Claude Vision ─────────────────────────────────────────────────────────────

def _ensure_jpeg_b64(image_b64: str) -> tuple[str, str]:
    """Convert any image format to JPEG and return (new_b64, 'image/jpeg').
    Falls back to original if conversion fails."""
    try:
        from PIL import Image as PilImage
        import io
        raw = base64.b64decode(image_b64)
        img = PilImage.open(io.BytesIO(raw))
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=92)
        return base64.b64encode(buf.getvalue()).decode("utf-8"), "image/jpeg"
    except Exception as e:
        print(f"[image convert] {e} — using original")
        return image_b64, "image/jpeg"


def generate_script(image_b64: str, media_type: str, customization: dict | None = None) -> tuple:
    """
    Returns (script, avatar_prompt, product_type, ai_settings).
    ai_settings is populated only in auto mode — contains AI-decided gender/skin/scene.
    """
    # Always convert to JPEG — Claude rejects HEIC and other phone formats
    image_b64, media_type = _ensure_jpeg_b64(image_b64)

    c            = customization or {}
    auto_mode    = c.get("auto_mode", False)
    language     = c.get("language", "hindi")
    model_action = c.get("model_action", "").strip()
    custom_instr = c.get("custom_instructions", "").strip()
    gender       = c.get("model_gender", "female")
    gender_hint  = "male Indian model" if gender == "male" else "female Indian model"

    # Language-specific script instruction — word count driven by video_duration
    _wt = c.get("video_duration", "5")
    _word_targets = {"5": "8-12", "6": "10-14", "10": "15-20", "15": "20-30", "30": "50-65", "60": "100-120"}
    _wc = _word_targets.get(_wt, "10-14")
    LANG_INSTRUCTIONS = {
        "hindi":    f"Write in Hindi (Devanagari script), {_wc} words, ends with call to action.",
        "english":  f"Write in English, {_wc} words, enthusiastic tone, ends with call to action.",
        "hinglish": f"Write in Hinglish (mix Hindi + English), casual Instagram style, {_wc} words, ends with call to action.",
    }
    script_lang_instr = LANG_INSTRUCTIONS.get(language, LANG_INSTRUCTIONS["hindi"])

    if auto_mode:
        # Ask Claude to decide everything
        json_schema = (
            '{"script":"...","avatar_prompt":"...","product_type":"...",'
            '"auto_gender":"female or male or girl_kid or boy_kid",'
            '"auto_skin_tone":"fair or wheatish or dusky or dark",'
            '"auto_scene":"studio or clinic or beach or ramp or cafe or garden or outdoor"}'
        )
        customer_notes = model_action or custom_instr or ""
        notes_instruction = (
            f"\n\nCUSTOMER SPECIAL REQUEST (MUST FOLLOW): \"{customer_notes}\"\n"
            "If the customer mentioned a specific location or scene (e.g. beach, goa, cafe, garden, ramp), "
            "you MUST set auto_scene to match it. Customer instructions override your own scene judgment."
        ) if customer_notes else ""

        instructions = (
            "You are an expert UGC video director for Instagram Reels targeting Indian audiences.\n"
            "Look at this product image carefully and reply with ONLY valid JSON, no markdown:\n"
            f"{json_schema}\n\n"
            f"script: {script_lang_instr}\n\n"
            "avatar_prompt: Describe exactly how the model interacts with this product. Under 20 words. "
            f"Prefer this detailed product handling guide when applicable:\n{PRODUCT_ACTION_GUIDE}\n"
            "Focus on natural, realistic body movement — avoid floating objects or impossible physics.\n"
            "  - Medical/surgical/stethoscope -> product_type MUST be medical, auto_scene MUST be clinic, presenter wears doctor coat/medical attire; if child reference is later used, it should become a tasteful little-doctor concept\n"
            "  - Jewellery (necklace/earrings/bangles/ring/maang tikka) → wearing it, turns head slowly to show, touches gently with fingertips, admires elegantly\n"
            "  - Clothing (dress/saree/lehenga/kurti/top/jeans) → wearing it, twirls once showing fabric, strikes confident pose, walks gracefully on ramp\n"
            "  - Bags (handbag/purse/tote/clutch/backpack) → carries on shoulder, opens and peeks inside with smile, poses holding it at side\n"
            "  - Footwear (sandals/heels/sneakers/flats/chappals) → walks elegantly showing footwear, crosses legs to show shoes, poses looking down then at camera\n"
            "  - Accessories (belt/watch/sunglasses/scarf/cap/hair clip) → wears it confidently, adjusts with both hands, poses and smiles\n"
            "  - Skincare/Beauty (cream/serum/moisturiser/sunscreen/face wash) → applies small amount on cheek or hand, gently massages in, glows and smiles\n"
            "  - Makeup (lipstick/kajal/foundation/blush/eyeshadow) → applies product, looks in imaginary mirror, smiles confidently at camera\n"
            "  - Food/snacks (chips/biscuits/sweets/namkeen) → gestures toward product with open hand, picks one piece and holds it up, smiles warmly\n"
            "  - Beverages (juice/tea/coffee/shake/cold drink) → holds cup/glass with both hands, brings close to lips, closes eyes enjoying aroma or taste\n"
            "  - Electronics (phone/earphones/tablet/smartwatch/gadget) → holds naturally, uses it confidently, reacts with excitement or satisfaction\n"
            "  - Home decor (candle/frame/plant/cushion/showpiece) → places product carefully, steps back, tilts head admiring it with a warm smile\n"
            "  - Fitness (yoga mat/dumbbells/protein/gym gear) → holds product with energy, demonstrates use briefly, looks strong and motivated\n"
            "  - Kids products (toy/kids clothing/shoes/bag) → holds up playfully, shows with joy and big smile, waves product at camera\n"
            "  - Stationery/books (notebook/pen/planner) → opens and flips pages, holds up cover facing camera, nods thoughtfully\n"
            "  - Organic/natural products (honey/seeds/oils/herbal) → holds up bottle/jar, opens and smells with delight, nods approvingly\n"
            "  - Other → holds product naturally at chest level, looks at it then at camera, smiles confidently\n\n"
            f"product_type: best matching category from: {PRODUCT_TYPE_LIST}\n\n"
            "auto_gender: Study the product image carefully. Who is this product MADE FOR? Choose exactly one: 'female', 'male', 'girl_kid', 'boy_kid'.\n"
            "  Think like a smart Indian marketer — look at the product size, design, colors, style, branding, and intended user. Do not guess randomly.\n\n"
            "auto_skin_tone: Choose the skin tone that best matches the target audience and product aesthetic "
            "('fair' for premium bridal/luxury, 'wheatish' for everyday Indian mainstream, 'dusky' for sporty/outdoor/bold, 'dark' for high-fashion/statement pieces).\n\n"
            "auto_scene: Best realistic background for this product "
            "('clinic' for medical/surgical/stethoscope/healthcare products, 'studio' for jewellery/electronics/premium/makeup products, 'beach' for sunscreen/swimwear/summer, 'ramp' for fashion/clothing/footwear, "
            "'cafe' for food/beverages/lifestyle, 'garden' for skincare/natural/organic/kids products, 'outdoor' for sports/fitness/adventure)."
            f"{notes_instruction}"
        )
    else:
        json_schema = '{"script":"...","avatar_prompt":"...","product_type":"..."}'
        extra = ""
        if model_action:
            extra += f'\nUser wants the model to: "{model_action}". Reflect this in the script energy.'
        if custom_instr:
            extra += f'\nExtra instructions: "{custom_instr}".'
        instructions = (
            f"You are a UGC creator for Instagram Reels. The presenter is a {gender_hint}.\n"
            f"Look at this product image and reply with ONLY valid JSON, no markdown:\n{json_schema}\n\n"
            f"script: {script_lang_instr}\n\n"
            "avatar_prompt: Describe exactly how the model interacts with this product. Under 20 words. "
            "Focus on natural, realistic body movement — avoid floating objects or impossible physics.\n"
            "  - Medical/surgical/stethoscope -> product_type MUST be medical; presenter wears doctor coat/medical attire and handles the product professionally in a clinic setting\n"
            "  - Jewellery (necklace/earrings/bangles/ring/maang tikka) → wearing it, turns head slowly to show, touches gently with fingertips, admires elegantly\n"
            "  - Clothing (dress/saree/lehenga/kurti/top/jeans) → wearing it, twirls once showing fabric, strikes confident pose, walks gracefully on ramp\n"
            "  - Bags (handbag/purse/tote/clutch/backpack) → carries on shoulder, opens and peeks inside with smile, poses holding it at side\n"
            "  - Footwear (sandals/heels/sneakers/flats/chappals) → walks elegantly showing footwear, crosses legs to show shoes, poses looking down then at camera\n"
            "  - Accessories (belt/watch/sunglasses/scarf/cap/hair clip) → wears it confidently, adjusts with both hands, poses and smiles\n"
            "  - Skincare/Beauty (cream/serum/moisturiser/sunscreen/face wash) → applies small amount on cheek or hand, gently massages in, glows and smiles\n"
            "  - Makeup (lipstick/kajal/foundation/blush/eyeshadow) → applies product, looks in imaginary mirror, smiles confidently at camera\n"
            "  - Food/snacks (chips/biscuits/sweets/namkeen) → gestures toward product with open hand, picks one piece and holds it up, smiles warmly\n"
            "  - Beverages (juice/tea/coffee/shake/cold drink) → holds cup/glass with both hands, brings close to lips, closes eyes enjoying aroma or taste\n"
            "  - Electronics (phone/earphones/tablet/smartwatch/gadget) → holds naturally, uses it confidently, reacts with excitement or satisfaction\n"
            "  - Home decor (candle/frame/plant/cushion/showpiece) → places product carefully, steps back, tilts head admiring it with a warm smile\n"
            "  - Fitness (yoga mat/dumbbells/protein/gym gear) → holds product with energy, demonstrates use briefly, looks strong and motivated\n"
            "  - Kids products (toy/kids clothing/shoes/bag) → holds up playfully, shows with joy and big smile, waves product at camera\n"
            "  - Stationery/books (notebook/pen/planner) → opens and flips pages, holds up cover facing camera, nods thoughtfully\n"
            "  - Organic/natural products (honey/seeds/oils/herbal) → holds up bottle/jar, opens and smells with delight, nods approvingly\n"
            "  - Other → holds product naturally at chest level, looks at it then at camera, smiles confidently\n\n"
            f"product_type: best matching category from: {PRODUCT_TYPE_LIST}."
            + extra
        )

    message = anthropic_client.messages.create(
        model="claude-opus-4-7",
        max_tokens=400,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": image_b64}},
                {"type": "text",  "text": instructions},
            ],
        }],
    )

    FALLBACK_PROMPTS = {
        **PRODUCT_FALLBACK_PROMPTS,
        "jewelry":     "wearing jewelry, turns head slowly to show, touches gently with fingertips, smiles elegantly",
        "clothing":    "wearing outfit, twirls once showing fabric, strikes confident pose, smiles at camera",
        "bag":         "carries bag on shoulder, opens and peeks inside with smile, poses confidently",
        "footwear":    "walks elegantly showing footwear, crosses legs to show shoes, smiles at camera",
        "accessory":   "wears accessory confidently, adjusts with both hands, poses and smiles",
        "skincare":    "applies product on cheek with fingertip, gently massages in, glows and smiles",
        "makeup":      "applies makeup product, looks in imaginary mirror, smiles confidently at camera",
        "food":        "gestures toward food with open hand, picks one piece and holds it up, smiles warmly",
        "beverage":    "holds cup with both hands, brings close to lips, closes eyes enjoying the aroma",
        "electronics": "holds device naturally, uses it confidently, reacts with excitement",
        "home_decor":  "places product carefully, steps back, tilts head admiring it with warm smile",
        "fitness":     "holds product with energy, demonstrates use briefly, looks strong and motivated",
        "kids":        "holds product up playfully, shows with joy and big smile, waves at camera",
        "stationery":  "opens and flips pages, holds up cover facing camera, nods thoughtfully",
        "organic":     "holds bottle or jar up, opens and smells with delight, nods approvingly",
        "other":       "holds product naturally at chest level, looks at it then at camera, smiles confidently",
    }

    raw = message.content[0].text.strip()
    try:
        data         = json.loads(raw)
        script       = data["script"]
        product_type = _force_product_type(data.get("product_type", "other"), _combined_generation_text(c, raw, data.get("avatar_prompt", "")))
        avatar_prompt = data.get("avatar_prompt", "").strip() or FALLBACK_PROMPTS.get(product_type, FALLBACK_PROMPTS["other"])
        auto_scene = _force_scene_for_product(data.get("auto_scene", "studio"), product_type, _combined_generation_text(c, raw, avatar_prompt))
        ai_settings  = {
            "model_gender": data.get("auto_gender", "female"),
            "skin_tone":    data.get("auto_skin_tone", "wheatish"),
            "scene":        auto_scene,
            "custom_scene": "",
            "model_action": avatar_prompt,
            "custom_instructions": "",
        } if auto_mode else {}
        return script, avatar_prompt, product_type, ai_settings
    except Exception:
        return raw, "looking at camera, talking enthusiastically", "other", {}


# ── kie.ai helpers ────────────────────────────────────────────────────────────

def _kie_headers() -> dict:
    return {"Authorization": f"Bearer {KIE_API_KEY}", "Content-Type": "application/json"}


async def generate_audio(script: str, gender: str = "female", language: str = "hindi") -> str:
    """
    Generate TTS using edge-tts (free, Microsoft Azure voices).
    Uploads the resulting MP3 to 0x0.st (free anonymous host) and returns a public URL.
    """
    voice = _pick_voice(gender, language)

    # Generate audio bytes in memory
    audio_path = os.path.join(tempfile.gettempdir(), f"ugc_audio_{uuid.uuid4().hex}.mp3")
    try:
        communicate = edge_tts.Communicate(script, voice)
        await communicate.save(audio_path)
        with open(audio_path, "rb") as f:
            audio_bytes = f.read()
    finally:
        if os.path.exists(audio_path):
            os.unlink(audio_path)

    # Upload to 0x0.st — free, anonymous, no account needed
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(
            "https://0x0.st",
            files={"file": ("audio.mp3", audio_bytes, "audio/mpeg")},
        )
    if resp.status_code != 200:
        raise Exception(f"Audio upload to 0x0.st failed: {resp.status_code} {resp.text[:200]}")
    url = resp.text.strip()
    if not url.startswith("http"):
        raise Exception(f"Unexpected response from 0x0.st: {url}")
    return url


def _pick_voice(gender: str, language: str) -> str:
    """Pick the right edge-tts voice based on gender and language."""
    is_male = gender in ("male", "boy_kid")
    if language == "hindi":
        return "hi-IN-MadhurNeural" if is_male else "hi-IN-SwaraNeural"
    else:
        return "en-IN-PrabhatNeural" if is_male else "en-IN-NeerjaNeural"


async def create_avatar_video(image_url: str, audio_url: str, avatar_prompt: str) -> str:
    """Submit Kling AI Avatar job. Returns task ID."""
    full_prompt = f"{avatar_prompt}, eyes wide open, looking directly at camera, expressive and engaging"
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{KIE_BASE}/jobs/createTask",
            headers=_kie_headers(),
            json={
                "model": "kling/ai-avatar-standard",
                "input": {
                    "image_url": image_url,
                    "audio_url": audio_url,
                    "prompt": full_prompt,
                },
            },
            timeout=30.0,
        )
    data = resp.json()
    if data.get("code") != 200:
        raise Exception(f"kie.ai avatar error: {data.get('msg')}")
    return data["data"]["taskId"]


async def poll_task(task_id: str) -> str:
    """Poll a kie.ai task until success. Returns the first result URL."""
    async with httpx.AsyncClient() as client:
        for _ in range(120):  # up to 10 minutes
            await asyncio.sleep(5)
            resp = await client.get(
                f"{KIE_BASE}/jobs/recordInfo",
                headers={"Authorization": f"Bearer {KIE_API_KEY}"},
                params={"taskId": task_id},
                timeout=10.0,
            )
            data = resp.json().get("data") or {}
            state = data.get("state")
            if state == "success":
                result = json.loads(data["resultJson"])
                return result["resultUrls"][0]
            if state == "fail":
                raise Exception(f"kie.ai task failed (id={task_id})")

    raise Exception(f"kie.ai task timed out after 10 minutes (id={task_id})")


async def poll_veo3_task(task_id: str) -> str:
    """Poll a kie.ai Veo3 task via /veo/record-info until success. Returns video URL."""
    SUCCESS_STATES = {"success", "succeed", "succeeded", "finish", "finished", "complete", "completed", "done"}
    FAIL_STATES    = {"fail", "failed", "error", "cancelled", "canceled"}

    async with httpx.AsyncClient() as client:
        for i in range(360):  # up to 30 minutes (veo3 can take 20-25 min)
            await asyncio.sleep(5)
            try:
                resp = await client.get(
                    f"{KIE_BASE}/veo/record-info",
                    headers={"Authorization": f"Bearer {KIE_API_KEY}"},
                    params={"taskId": task_id},
                    timeout=15.0,
                )
                body = resp.json()
            except Exception as e:
                print(f"[Veo3 poll #{i}] Request error: {e} — retrying")
                continue

            data  = body.get("data") or {}
            state = (data.get("state") or data.get("status") or "").lower().strip()

            # Log every 12 polls (~1 min) so we can see progress
            if i % 12 == 0:
                print(f"[Veo3 poll #{i}] taskId={task_id} state={state!r} keys={list(data.keys())}")

            # Check for video URL regardless of state — if kie.ai has it, we take it
            result_json_str = data.get("resultJson")
            if result_json_str:
                try:
                    result = json.loads(result_json_str)
                    urls = result.get("resultUrls") or result.get("videoUrls") or []
                    if urls:
                        print(f"[Veo3 poll #{i}] Got URL from resultJson (state={state!r})")
                        return urls[0]
                except Exception:
                    pass

            direct_urls = data.get("resultUrls") or data.get("videoUrls") or []
            if direct_urls:
                print(f"[Veo3 poll #{i}] Got URL from direct field (state={state!r})")
                return direct_urls[0]

            # kie.ai Veo3 nests the URL inside data.response.resultUrls
            response_obj = data.get("response") or {}
            nested_urls = response_obj.get("resultUrls") or response_obj.get("videoUrls") or []
            if nested_urls:
                print(f"[Veo3 poll #{i}] Got URL from response.resultUrls (state={state!r})")
                return nested_urls[0]

            # successFlag=1 means done — if we still have no URL, something is wrong
            if data.get("successFlag") == 1:
                raise Exception(f"Veo3 successFlag=1 but no URL found: {body}")

            if state in SUCCESS_STATES:
                raise Exception(f"Veo3 state={state} but no URL found: {body}")

            if state in FAIL_STATES:
                raise Exception(f"kie.ai Veo3 task failed (id={task_id}): {data.get('msg','')}")

    raise Exception(f"kie.ai Veo3 task timed out after 30 minutes (id={task_id})")


# ── D-ID free pipeline ────────────────────────────────────────────────────────

async def create_did_talk(image_url: str, script_text: str, gender: str = "female") -> str:
    """
    Create a lip-sync talk video using D-ID API (free tier: 5 videos).
    Uses D-ID's built-in Microsoft Azure TTS — no audio file upload needed.
    Basic auth: base64(DID_API_KEY) where DID_API_KEY is "base64(email):api_key"
    Returns the result video URL.
    """
    voice_id = "hi-IN-SwaraNeural" if gender != "male" else "hi-IN-MadhurNeural"
    # DID_API_KEY is in "base64(email):api_key" format — base64 encode the whole thing for Basic auth
    auth_token = base64.b64encode(DID_API_KEY.encode()).decode()
    headers = {
        "Authorization": f"Basic {auth_token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    payload = {
        "source_url": image_url,
        "script": {
            "type": "text",
            "input": script_text,
            "provider": {
                "type": "microsoft",
                "voice_id": voice_id,
            },
        },
        "config": {
            "fluent": True,
            "pad_audio": 0.0,
        },
    }

    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post("https://api.d-id.com/talks", headers=headers, json=payload)

    if resp.status_code not in (200, 201):
        raise Exception(f"D-ID create talk failed ({resp.status_code}): {resp.text[:400]}")

    talk_id = resp.json().get("id")
    if not talk_id:
        raise Exception(f"D-ID returned no talk ID: {resp.text[:400]}")

    # Poll until done (usually 30-90 seconds)
    for _ in range(120):
        await asyncio.sleep(5)
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(f"https://api.d-id.com/talks/{talk_id}", headers=headers)
        info = r.json()
        status = info.get("status", "")
        if status == "done":
            result_url = info.get("result_url")
            if not result_url:
                raise Exception("D-ID talk done but no result_url returned")
            return result_url
        if status == "error":
            raise Exception(f"D-ID talk failed: {info.get('error', {})}")

    raise Exception(f"D-ID talk timed out after 10 minutes (id={talk_id})")


# ── fal.ai pipeline ───────────────────────────────────────────────────────────

async def upload_image_to_fal(image_bytes: bytes, content_type: str) -> str:
    """Upload product image to fal.ai storage and return a public URL."""
    ext = content_type.split("/")[-1] if "/" in content_type else "jpg"
    tmp = os.path.join(tempfile.gettempdir(), f"ugc_product_{uuid.uuid4().hex}.{ext}")
    try:
        with open(tmp, "wb") as f:
            f.write(image_bytes)
        os.environ["FAL_KEY"] = FAL_KEY
        url = await fal_client.upload_file_async(tmp)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return url


async def generate_audio_fal(script: str, gender: str = "female", language: str = "hindi") -> str:
    """
    Generate audio with edge-tts (voice chosen by language), then upload to fal.ai storage.
    Returns a public fal.ai CDN URL for the audio file.
    """
    voice = _pick_voice(gender, language)
    audio_path = os.path.join(tempfile.gettempdir(), f"ugc_audio_{uuid.uuid4().hex}.mp3")
    try:
        communicate = edge_tts.Communicate(script, voice)
        await communicate.save(audio_path)
        with open(audio_path, "rb") as f:
            audio_bytes = f.read()
    finally:
        if os.path.exists(audio_path):
            os.unlink(audio_path)

    # Upload to fal.ai storage — guaranteed accessible by fal.ai models
    audio_path = os.path.join(tempfile.gettempdir(), f"ugc_upload_{uuid.uuid4().hex}.mp3")
    try:
        with open(audio_path, "wb") as f:
            f.write(audio_bytes)
        os.environ["FAL_KEY"] = FAL_KEY
        url = await fal_client.upload_file_async(audio_path)
    finally:
        if os.path.exists(audio_path):
            os.unlink(audio_path)
    if not url:
        raise Exception("fal_client upload returned no URL")
    return url


FAL_MODEL = os.getenv("FAL_MODEL", "fal-ai/wav2lip")  # swap to fal-ai/sadtalker for better quality

async def create_fal_video(image_url: str, audio_url: str, avatar_prompt: str = "", product_url: str = "") -> str:
    """
    Submit a talking-head video job on fal.ai.
    Default model: wav2lip (FREE). Set FAL_MODEL=fal-ai/sadtalker for higher quality (~$0.07/video).
    Both accept a still image + audio and return a lip-synced video URL.
    """
    headers = {
        "Authorization": f"Key {FAL_KEY}",
        "Content-Type": "application/json",
    }

    # Model-specific payload schemas
    if FAL_MODEL == "fal-ai/sadtalker":
        payload = {
            "source_image_url": image_url,
            "driven_audio_url": audio_url,
            "preprocess": "crop",
            "still_mode": False,
            "use_enhancer": True,
            "expression_scale": 1.2,
        }
    elif FAL_MODEL == "fal-ai/sync-lipsync":
        payload = {
            "video_url": image_url,
            "audio_url": audio_url,
            "model": "sync-1.9-beta",
            "sync_mode": "bounce",
        }
    elif "kling-video/ai-avatar" in FAL_MODEL:
        payload = {
            "image_url": image_url,
            "audio_url": audio_url,
            "duration": 5,
            "prompt": avatar_prompt if avatar_prompt else "talking to camera naturally, expressive and engaging",
        }
    else:
        # wav2lip (free)
        payload = {
            "face_url": image_url,
            "audio_url": audio_url,
        }

    # Submit to async queue
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"https://queue.fal.run/{FAL_MODEL}",
            headers=headers,
            json=payload,
        )
    if resp.status_code not in (200, 201):
        raise Exception(f"fal.ai {FAL_MODEL} submit failed ({resp.status_code}): {resp.text[:400]}")

    try:
        submit_data = resp.json()
    except Exception:
        raise Exception(f"fal.ai submit returned non-JSON ({resp.status_code}): {resp.text[:400]}")

    request_id = submit_data.get("request_id")
    if not request_id:
        raise Exception(f"fal.ai returned no request_id: {resp.text[:400]}")

    # Poll until done
    for _ in range(180):  # up to ~15 minutes (Kling Avatar can take 10-12 min on fal.ai)
        await asyncio.sleep(5)
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(
                f"https://queue.fal.run/{FAL_MODEL}/requests/{request_id}/status",
                headers=headers,
            )
        try:
            status_data = r.json()
        except Exception:
            continue  # empty/malformed status response — retry
        status = status_data.get("status", "")

        if status == "COMPLETED":
            async with httpx.AsyncClient(timeout=15.0) as client:
                result_resp = await client.get(
                    f"https://queue.fal.run/{FAL_MODEL}/requests/{request_id}",
                    headers=headers,
                )
            try:
                result = result_resp.json()
            except Exception:
                raise Exception(f"fal.ai result non-JSON: {result_resp.text[:400]}")
            # Try all known result key formats across models
            video_url = (
                result.get("video", {}).get("url")        # sadtalker / wav2lip
                or result.get("video_url")                 # some models
                or result.get("output_video", {}).get("url")  # sync-lipsync
                or result.get("output", {}).get("url")
                or (result.get("works") or [{}])[0].get("video", {}).get("url")  # kling avatar
            )
            if not video_url:
                raise Exception(f"fal.ai completed but no video URL found: {result}")
            return video_url

        if status in ("FAILED", "ERROR"):
            raise Exception(f"fal.ai {FAL_MODEL} failed: {status_data.get('error', status_data)}")

    raise Exception(f"fal.ai {FAL_MODEL} timed out after 12 minutes (request_id={request_id})")


# ── Veo 3 Fast pipeline (via kie.ai) ─────────────────────────────────────────

async def create_veo3_via_kie(image_url: str, prompt: str, aspect_ratio: str = "9:16", duration: int = 8, resolution: str = "720p") -> str:
    """Submit a Veo 3.1 Fast image-to-video job on kie.ai. Returns task ID.

    Notes:
    - REFERENCE_2_VIDEO only supports 16:9 aspect ratio
    - For 9:16 (portrait/Instagram), use FIRST_AND_LAST_FRAMES_2_VIDEO with image as first frame
    - Model: veo3_fast = $0.30/video (~₹28.5) — cheapest + best quality
    """
    valid_ratios = {"9:16", "16:9", "1:1", "4:3", "3:4"}
    valid_resolutions = {"720p", "1080p"}
    valid_durations = {4, 6, 8}
    ar = aspect_ratio if aspect_ratio in valid_ratios else "9:16"
    # REFERENCE_2_VIDEO only supports 16:9; use FIRST_AND_LAST_FRAMES_2_VIDEO for all other ratios
    generation_type = "REFERENCE_2_VIDEO" if ar == "16:9" else "FIRST_AND_LAST_FRAMES_2_VIDEO"
    payload = {
        "prompt": prompt,
        "model": "veo3_fast",
        "aspect_ratio": ar,
        "resolution": resolution if resolution in valid_resolutions else "720p",
        "duration": duration if duration in valid_durations else 8,
        "imageUrls": [image_url],
        "generationType": generation_type,
    }
    last_err = None
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    f"{KIE_BASE}/veo/generate",
                    headers=_kie_headers(),
                    json=payload,
                )
            data = resp.json()
            if data.get("code") != 200:
                raise Exception(f"kie.ai Veo3 submit error: {data.get('msg')} : {data}")
            return data["data"]["taskId"]
        except (httpx.ConnectError, httpx.TimeoutException, OSError) as e:
            last_err = e
            await asyncio.sleep(5 * (attempt + 1))
    raise Exception(f"kie.ai Veo3 submit failed after 3 attempts: {last_err}")


async def process_job_veo3(job_id: str, image_data: bytes, content_type: str, avatar_url: str, customization: dict | None = None):
    """Full pipeline using Veo 3 Fast (kie.ai) — composite image + native audio in one step."""
    try:
        c = customization or {}
        language       = c.get("language", "hindi")
        aspect_ratio   = c.get("aspect_ratio", "9:16")
        custom_script  = c.get("custom_script", "").strip()
        # Veo3 always 6s fixed (valid values: 4, 6, 8)
        veo3_duration = 6

        # Step 1: Script/product analysis via Claude Vision.
        # Even with a custom script, keep the vision pass so product_type and handling are anchored to the uploaded image.
        jobs[job_id]["step"] = "analyzing"
        image_b64 = base64.b64encode(image_data).decode("utf-8")
        analyzed_script, analyzed_avatar_prompt, product_type, ai_settings = await asyncio.to_thread(
            generate_script, image_b64, content_type, c
        )
        if custom_script:
            script = custom_script
            avatar_prompt = c.get("model_action", "").strip() or analyzed_avatar_prompt or "model presenting product elegantly, looking at camera"
        else:
            script = analyzed_script
            avatar_prompt = analyzed_avatar_prompt
        jobs[job_id]["script"] = script

        if c.get("auto_mode") and ai_settings:
            c = {**c, **ai_settings}
        product_type = _force_product_type(product_type, _combined_generation_text(c, script, avatar_prompt))
        if product_type == "medical":
            c = {**c, "scene": "clinic"}
        gender = c.get("model_gender", "female")

        # Step 2: Composite image only (no TTS needed — Veo 3 has native audio)
        jobs[job_id]["step"] = "compositing_product"
        composite_url = await generate_model_with_product(
            avatar_url, image_data, content_type, product_type, avatar_prompt, c
        )

        # Step 3: Veo 3 Fast — visual motion only, no text/subtitles burned in
        jobs[job_id]["step"] = "generating_video"
        safe_motion = VIDEO_ACTION_GUIDE.get(product_type, VIDEO_ACTION_GUIDE["other"])
        veo3_prompt = (
            "Animate the provided reference image as the exact first frame. "
            "Preserve the same person, same product, same product color, same markings/logo, same shape, and same outfit. "
            "The uploaded product must stay clearly visible as the hero object throughout the video; do not replace it with a generic object or a different product. "
            f"Product category: {product_type}. "
            f"Product-safe motion: {safe_motion}. "
            f"Creative direction to respect only if it does not conflict with product preservation: {avatar_prompt}. "
            "Use small realistic camera movement and natural body motion only. "
            "No scene jump, no product swap, no different team, no new main object, no text, no subtitles, no captions, no watermark."
        )
        task_id = await create_veo3_via_kie(composite_url, veo3_prompt, aspect_ratio, veo3_duration, "720p")
        jobs[job_id]["kie_task_id"] = task_id  # store so admin can recover if poll fails
        # Also persist task_id to orders.json so it survives server restart
        oid = jobs[job_id].get("order_id")
        if oid:
            _orders = load_orders()
            for _o in _orders:
                if _o.get("id") == oid:
                    _o["kie_task_id"] = task_id
                    break
            with open(ORDERS_FILE, "w", encoding="utf-8") as _f:
                json.dump(_orders, _f, ensure_ascii=False, indent=2)
        veo3_video_url = await poll_veo3_task(task_id)

        # Step 4: Re-encode to correct aspect ratio (no audio merge — audio already embedded)
        jobs[job_id]["step"] = "processing_video"
        final_url = await download_and_reencode_video(veo3_video_url, job_id, aspect_ratio)
        final_url = await append_video_end_card(final_url, job_id, aspect_ratio, c.get("video_end_card"))

        jobs[job_id].update({"status": "completed", "step": "completed", "video_url": final_url})
        save_to_history({
            "id": job_id,
            "date": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "script": script,
            "video_url": final_url,
            "product_type": product_type,
            "language": language,
        })

        # Upload to Cloudinary → push CDN URL to Railway
        oid = jobs[job_id].get("order_id")
        if oid:
            local_path = os.path.join(os.path.dirname(__file__), "static", "videos", f"{job_id}.mp4")
            cdn_url = await _upload_to_cloudinary(local_path, job_id)
            public_video_url = cdn_url or (f"{PUBLIC_URL}{final_url}" if PUBLIC_URL else final_url)
            asyncio.create_task(_push_result_to_railway(oid, public_video_url, "", script))
            asyncio.create_task(_delete_model_photos(oid))

    except Exception as e:
        jobs[job_id].update({"status": "failed", "error": str(e)})


# ── Gemini Omni pipeline (via kie.ai) ────────────────────────────────────────

# Product-type optimized prompts for Gemini Omni UGC videos
GEMINI_OMNI_PRODUCT_PROMPTS = {
    "jewelry": (
        "Close-up cinematic shot of the jewelry piece glowing under warm studio light. "
        "The model's hand gracefully lifts the product toward the camera — sparkle and shimmer catch the light beautifully. "
        "Slow 360-degree product rotation. Camera dollies in gently. Rich bokeh background. "
        "Elegant and luxurious mood. No text, no subtitles, no watermark."
    ),
    "clothing": (
        "Cinematic lifestyle shot. Model wearing the outfit walks confidently toward the camera on a softly lit street. "
        "Fabric flows naturally. Camera tracks smoothly alongside. Warm golden-hour light. "
        "Outfit clearly visible from head to toe. Fashion editorial mood. No text, no subtitles, no watermark."
    ),
    "boutique": (
        "Cinematic boutique display shot. Model picks up the product lovingly, holds it at eye level, smiles. "
        "Camera slowly pushes in. Warm interior store lighting. Lifestyle feel — aspirational and friendly. "
        "No text, no subtitles, no watermark."
    ),
    "salon": (
        "Beauty salon cinematic shot. Stylist or model demonstrates the beauty product or hairstyle with graceful hand motions. "
        "Soft diffused lighting. Camera slowly orbits around the model's face and hair. "
        "Fresh, clean, aspirational beauty mood. No text, no subtitles, no watermark."
    ),
    "skincare": (
        "Skincare product cinematic close-up. Model applies product to glowing skin with gentle fingertip motions. "
        "Soft natural daylight from the side. Extreme close-up on texture and absorption. "
        "Clean, fresh, dermatologist-approved aesthetic. No text, no subtitles, no watermark."
    ),
    "food": (
        "Cinematic food reveal shot. Steam rises slowly from the dish. Camera pulls back from extreme close-up. "
        "Rich warm lighting highlights the texture, color, and freshness of the food. "
        "Hands elegantly plate or garnish the dish. Appetite-inducing, editorial food photography style. "
        "No text, no subtitles, no watermark."
    ),
    "electronics": (
        "Cinematic product launch style shot. The device is placed on a sleek reflective surface. "
        "Subtle dramatic lighting with lens flare. Camera slowly orbits the product. "
        "Model's hand picks it up and interacts confidently. Tech, premium, aspirational feel. "
        "No text, no subtitles, no watermark."
    ),
    "fitness": (
        "High-energy cinematic fitness shot. Model demonstrates the product or exercise with dynamic motion. "
        "Camera follows the action with smooth tracking. Gym or outdoor setting. "
        "Motivational, energetic, powerful mood. Slow-motion moment highlights the product. "
        "No text, no subtitles, no watermark."
    ),
    "home_decor": (
        "Cinematic interior lifestyle shot. Product is beautifully placed in a stylish home setting. "
        "Camera slowly pushes into the scene, revealing the product as the hero. "
        "Warm ambient interior lighting. Aspirational home living aesthetic. "
        "No text, no subtitles, no watermark."
    ),
    "bags": (
        "Fashion editorial bag shot. Model confidently holds or carries the bag. "
        "Camera follows with smooth tracking on a lifestyle street or studio setting. "
        "Focus pulls to highlight the bag's texture, hardware, and craftsmanship. "
        "Luxury, aspirational, street-style mood. No text, no subtitles, no watermark."
    ),
    "kids_eyewear": (
        "Bright cheerful cinematic shot. An adorable child puts on the colorful eyeglasses and looks "
        "straight at the camera with a big confident smile. "
        "Camera slowly pushes in to a close-up on the glasses — vibrant colors and fun frame details pop. "
        "Child tilts head playfully. Soft natural daylight. Warm, joyful, playful mood. "
        "No text, no subtitles, no watermark."
    ),
    "eyewear": (
        "Cinematic eyewear showcase. Model puts on the glasses and gazes confidently at the camera. "
        "Camera slowly orbits around the face — frame design, lens clarity, and fit all visible. "
        "Soft studio rim lighting highlights the frame shape and texture. "
        "Smart, stylish, premium mood. No text, no subtitles, no watermark."
    ),
    # ── Indian Ethnic & Fashion ───────────────────────────────────────────────
    "saree": (
        "Cinematic ethnic fashion shot. Model drapes the saree elegantly and turns slowly toward the camera. "
        "Rich fabric shimmers under warm golden studio light — zari, embroidery, or print details pop beautifully. "
        "Camera does a graceful full-length sweep from feet to face. "
        "Festive, bridal, or everyday elegance mood. No text, no subtitles, no watermark."
    ),
    "kurta": (
        "Lifestyle cinematic shot. Model wearing the kurta walks through a softly lit ethnic interior or courtyard. "
        "Fabric drapes and flows naturally. Camera tracks alongside at medium distance. "
        "Ethnic Indian aesthetic — earthy warm tones, wooden furniture or floral background. "
        "Casual yet elegant Indian daily wear mood. No text, no subtitles, no watermark."
    ),
    "lehenga": (
        "Bridal cinematic spin shot. Model wearing the lehenga does a slow graceful spin. "
        "Heavy embroidery, mirror work, and dupatta fly out beautifully. "
        "Camera slowly pulls back to reveal the full outfit. Warm golden backlight creates a halo effect. "
        "Dreamy bridal fairytale mood. No text, no subtitles, no watermark."
    ),
    "ethnic_wear": (
        "Festive cinematic shot. Model dressed in rich ethnic outfit poses confidently in a traditionally decorated setting. "
        "Camera slowly pushes in from full body to medium close-up highlighting fabric detail and embroidery. "
        "Warm Diwali-style ambient lighting. Celebratory, proud Indian culture mood. "
        "No text, no subtitles, no watermark."
    ),
    "footwear": (
        "Cinematic footwear showcase. Close-up of model's feet stepping forward stylishly in the product. "
        "Camera follows at ground level, then rises to full body. "
        "Clean studio floor or cobblestone street setting. Warm lifestyle lighting. "
        "Confident, fashionable, all-day comfort mood. No text, no subtitles, no watermark."
    ),
    "mojari": (
        "Artisan cinematic shot. Close-up of the handcrafted mojari/jutis on a wooden surface. "
        "Intricate embroidery, sequins and thread work catch warm studio light. "
        "Model's hand lifts and turns the mojari gracefully. "
        "Heritage craftmanship, ethnic pride mood. No text, no subtitles, no watermark."
    ),
    "watches": (
        "Luxury product cinematic shot. Watch placed on a dark velvet surface, light reflecting off the dial. "
        "Camera slowly orbits around the watch — face, strap, crown all visible. "
        "Model's wrist slips on the watch and holds it up toward camera. "
        "Premium, precision, timeless mood. No text, no subtitles, no watermark."
    ),
    "sunglasses": (
        "Cinematic street style shot. Model puts on the sunglasses smoothly and looks at the camera confidently. "
        "Camera pushes in slowly. Sun flare catches the lens edge beautifully. "
        "Urban outdoor setting with lifestyle energy. "
        "Cool, bold, summer vibes mood. No text, no subtitles, no watermark."
    ),
    "dupatta": (
        "Flowing cinematic shot. Model holds the dupatta and lets it fly elegantly in slow motion. "
        "Rich fabric, embroidery and print details shimmer under golden light. "
        "Camera captures the full flow from close-up texture to wide lifestyle shot. "
        "Graceful, festive, feminine Indian mood. No text, no subtitles, no watermark."
    ),
    # ── Beauty & Personal Care ────────────────────────────────────────────────
    "makeup": (
        "Beauty editorial cinematic shot. Model applies makeup confidently — lipstick swipe, eyeshadow blend, or foundation. "
        "Extreme close-up on the eyes or lips. Camera slowly zooms out to reveal full glam face. "
        "Soft beauty lighting with ring light catch in the eyes. "
        "Bold, confident, glamorous mood. No text, no subtitles, no watermark."
    ),
    "haircare": (
        "Cinematic hair beauty shot. Model runs fingers through healthy, shiny, voluminous hair in slow motion. "
        "Camera captures the hair movement from close-up to full head shot. "
        "Soft backlit studio glow makes hair shimmer. "
        "Fresh, healthy, confident hair mood. No text, no subtitles, no watermark."
    ),
    "perfume": (
        "Luxury cinematic fragrance shot. Model holds the perfume bottle elegantly, sprays it on the neck. "
        "Slow-motion mist cloud catches dramatic side lighting. "
        "Camera pushes in on the bottle close-up — label, cap, liquid color all visible. "
        "Sensuous, premium, aspirational mood. No text, no subtitles, no watermark."
    ),
    "ayurvedic": (
        "Clean cinematic wellness shot. Ayurvedic product placed among natural ingredients — herbs, roots, flowers. "
        "Model applies or uses the product with a calm, mindful expression. "
        "Soft natural daylight from a window. Earthy green and gold tones. "
        "Ancient wisdom, natural healing, trusted wellness mood. No text, no subtitles, no watermark."
    ),
    "herbal": (
        "Cinematic nature-to-bottle story. Fresh herbs and botanicals fill the frame, camera transitions to the product. "
        "Model holds the product with trust and confidence. "
        "Soft green natural light. Earthy authentic feel. "
        "Pure, natural, chemical-free, trustworthy Indian brand mood. No text, no subtitles, no watermark."
    ),
    "supplements": (
        "Health cinematic shot. Model opens the supplement bottle confidently in a gym or bright kitchen setting. "
        "Close-up on the capsules or powder. Model flexes or shows energy after taking it. "
        "Clean white or gym-steel background. Energy and vitality lighting. "
        "Strong, healthy, performance mood. No text, no subtitles, no watermark."
    ),
    # ── Food & Beverage ───────────────────────────────────────────────────────
    "spices": (
        "Cinematic masala reveal shot. Vibrant spices pour slowly from a hand into a rustic bowl. "
        "Camera captures the color explosion — turmeric yellow, chili red, coriander green. "
        "Warm kitchen lighting with steam or dust particles catching light. "
        "Aromatic, authentic, home-cooked Indian flavor mood. No text, no subtitles, no watermark."
    ),
    "snacks": (
        "Appetizing cinematic snack shot. Hands reach into a bowl of crispy namkeen or snacks. "
        "Close-up on the texture and crunch. Camera pulls back to a lifestyle snack moment — family or friends. "
        "Warm fun lighting. Playful, crunchy, irresistible mood. "
        "No text, no subtitles, no watermark."
    ),
    "sweets": (
        "Festive cinematic mithai shot. A beautiful box of Indian sweets opens slowly — gulab jamun, ladoo, barfi. "
        "Camera pushes in on the rich golden and colorful sweets. "
        "A hand picks one up and takes a small bite with delight. "
        "Warm festive lighting. Celebratory, gifting, joyful Indian mood. No text, no subtitles, no watermark."
    ),
    "dry_fruits": (
        "Premium cinematic dry fruits shot. Assorted almonds, cashews, pistachios spill from an ornate bowl. "
        "Camera does a slow close-up sweep highlighting texture and freshness. "
        "Warm amber studio lighting on dark wood surface. "
        "Premium, healthy, gifting, festive Indian mood. No text, no subtitles, no watermark."
    ),
    "tea": (
        "Cinematic chai story. Hot chai pours from a saucepan into a kulhad — steam rises beautifully. "
        "Camera follows the steam upward then dollies in to the cup close-up. "
        "Model wraps both hands around the warm cup and closes eyes in contentment. "
        "Warm, cozy, desi chai moment mood. No text, no subtitles, no watermark."
    ),
    "coffee": (
        "Cinematic café-style shot. Coffee pours in slow motion into a cup — rich crema forms. "
        "Overhead camera transitions to eye-level close-up. "
        "Minimal clean background. Warm café morning light. "
        "Premium, energizing, modern Indian coffee culture mood. No text, no subtitles, no watermark."
    ),
    "organic_food": (
        "Farm-to-table cinematic shot. Fresh organic produce fills the frame — vegetables, fruits, grains. "
        "Camera transitions to the packaged product held by a model in a clean kitchen. "
        "Bright natural daylight. Green and earthy tones. "
        "Pure, healthy, chemical-free, trusted Indian family mood. No text, no subtitles, no watermark."
    ),
    "dairy": (
        "Pure cinematic dairy shot. Fresh milk pours in slow motion into a glass. "
        "Camera captures the splash and cream surface. Model drinks and smiles with satisfaction. "
        "Bright clean white and blue tones. Morning sunlight. "
        "Pure, fresh, nourishing, trusted desi dairy mood. No text, no subtitles, no watermark."
    ),
    "pickle": (
        "Homestyle cinematic shot. A glass jar of pickle opened — vibrant colors, mustard seeds, and oil visible. "
        "A spoon scoops out the pickle in slow motion. Close-up on texture and freshness. "
        "Warm rustic kitchen lighting. Nostalgic grandmother's recipe feel. "
        "Authentic, tangy, homemade Indian flavor mood. No text, no subtitles, no watermark."
    ),
    # ── Home & Kitchen ────────────────────────────────────────────────────────
    "cookware": (
        "Cinematic kitchen lifestyle shot. Model places a gleaming cookware piece on a stove. "
        "Camera close-up on the surface quality — non-stick, stainless, or iron. "
        "Warm kitchen ambient light. Steam rises as food cooks beautifully inside. "
        "Modern Indian kitchen, confident home chef mood. No text, no subtitles, no watermark."
    ),
    "kitchen_appliances": (
        "Cinematic kitchen product launch. The appliance is placed on a clean marble kitchen counter. "
        "Model presses the button — the machine hums to life confidently. "
        "Camera orbits slowly around the product. "
        "Modern, efficient, smart Indian kitchen mood. No text, no subtitles, no watermark."
    ),
    "bedding": (
        "Cinematic bedroom lifestyle shot. Model runs a hand over the soft bedsheet or pillow cover. "
        "Camera pushes in slowly on the fabric texture — softness and quality visible. "
        "Warm morning light floods a beautifully made bed. "
        "Comfortable, premium, peaceful home mood. No text, no subtitles, no watermark."
    ),
    "curtains": (
        "Cinematic home styling shot. Light breeze gently moves the curtain. "
        "Camera captures the fabric draping elegantly — color, pattern, and texture visible. "
        "Warm natural window light silhouettes the curtain beautifully. "
        "Elegant, airy, stylish Indian home interior mood. No text, no subtitles, no watermark."
    ),
    "lighting": (
        "Cinematic interior mood shot. The light switches on — warm glow fills the room slowly. "
        "Camera does a slow push-in toward the light fixture, capturing the design. "
        "Bokeh background of a well-lit room. "
        "Warm, cozy, design-forward modern Indian home mood. No text, no subtitles, no watermark."
    ),
    "furniture": (
        "Cinematic interior reveal. Camera slowly pans across a beautifully styled room highlighting the furniture piece. "
        "Model sits or interacts with the furniture naturally. "
        "Warm ambient interior lighting. "
        "Premium, modern Indian home living aspirational mood. No text, no subtitles, no watermark."
    ),
    # ── Handicrafts & Artisan ─────────────────────────────────────────────────
    "handicrafts": (
        "Artisan cinematic shot. Craftsperson's hands shape or display the handmade product with care. "
        "Camera transitions to a beauty close-up of the finished piece — colors, texture, detail. "
        "Warm earthy studio lighting. "
        "Indian heritage, skilled artisan, pride of craft mood. No text, no subtitles, no watermark."
    ),
    "pottery": (
        "Cinematic pottery story. Clay spins on a wheel — artisan's hands shape it beautifully. "
        "Camera transitions to the finished painted or glazed product on a natural surface. "
        "Earthy warm light. Raw clay texture and colors pop. "
        "Heritage Indian craft, handmade with love mood. No text, no subtitles, no watermark."
    ),
    "brass_copper": (
        "Cinematic metal craft shot. Gleaming brass or copper product placed on a dark stone surface. "
        "Warm studio light reflects off the polished surface. "
        "Camera orbits slowly highlighting engravings and craftsmanship. "
        "Traditional Indian décor, heritage, spiritual elegance mood. No text, no subtitles, no watermark."
    ),
    "paintings": (
        "Cinematic art reveal. Artist's hand adds a final brushstroke to the painting. "
        "Camera slowly pulls back to reveal the full artwork. "
        "Warm soft gallery lighting. Rich colors pop beautifully. "
        "Creative, cultural, proud Indian art mood. No text, no subtitles, no watermark."
    ),
    "puja_items": (
        "Spiritual cinematic shot. Puja thali or religious item placed on a decorated altar. "
        "Diya flame flickers gently. Camera slowly pushes in on the product — intricate design and craftsmanship visible. "
        "Warm golden candlelight. Incense haze softly in the background. "
        "Devotional, peaceful, sacred Indian home mood. No text, no subtitles, no watermark."
    ),
    # ── Kids & Baby ───────────────────────────────────────────────────────────
    "toys": (
        "Joyful cinematic kids shot. A child's eyes light up seeing the toy for the first time. "
        "Camera follows the child playing with the toy — expressive, fun, energetic. "
        "Bright colorful playroom lighting. "
        "Pure joy, imagination, childhood magic mood. No text, no subtitles, no watermark."
    ),
    "baby_products": (
        "Tender cinematic baby moment. Parent gently uses the baby product — lotion, diaper, or feeding item — on the baby. "
        "Camera close-up on the baby's happy, healthy skin or expression. "
        "Soft warm nursery lighting. "
        "Gentle, safe, loving, trusted parenting mood. No text, no subtitles, no watermark."
    ),
    "school_supplies": (
        "Motivational cinematic study shot. Child opens a fresh new notebook or picks up the stationery. "
        "Camera close-up on the product quality — smooth pages, vibrant colors. "
        "Bright clean desk lighting. "
        "Smart, curious, back-to-school excitement mood. No text, no subtitles, no watermark."
    ),
    # ── Sports & Outdoor ─────────────────────────────────────────────────────
    "sportswear": (
        "High-energy cinematic sports shot. Model in sportswear sprints, jumps, or stretches dynamically. "
        "Camera tracks with smooth slow-motion action. Sweat glistens. "
        "Outdoor track, gym, or stadium setting. "
        "Athletic, powerful, unstoppable Indian sports mood. No text, no subtitles, no watermark."
    ),
    "yoga": (
        "Serene cinematic yoga shot. Model transitions through a yoga pose gracefully in the yoga wear or with the product. "
        "Camera slowly orbits. Soft morning light or peaceful studio. "
        "Minimal, calm, zen aesthetic. "
        "Mindful, balanced, wellness Indian lifestyle mood. No text, no subtitles, no watermark."
    ),
    "cricket": (
        "Cinematic cricket lifestyle shot. Model grips the cricket bat or holds the product confidently. "
        "Dynamic camera movement — low angle to high. Cricket ground or stadium lighting. "
        "Passionate, energetic, Indian sports hero mood. No text, no subtitles, no watermark."
    ),
    # ── Tech & Accessories ───────────────────────────────────────────────────
    "mobile_accessories": (
        "Cinematic tech lifestyle shot. Model attaches the case, charger, or earbuds to a smartphone. "
        "Camera close-up on the product fit and finish — premium materials. "
        "Clean white or dark tech surface. Soft dramatic lighting. "
        "Modern, functional, sleek Indian tech user mood. No text, no subtitles, no watermark."
    ),
    "laptop_bags": (
        "Professional cinematic shot. Model confidently picks up the laptop bag and heads out. "
        "Camera tracks alongside — bag's pockets, zipper quality, and strap comfort highlighted. "
        "Modern office or urban setting. "
        "Ambitious, professional, on-the-go Indian career mood. No text, no subtitles, no watermark."
    ),
    # ── Travel & Lifestyle ────────────────────────────────────────────────────
    "luggage": (
        "Travel cinematic shot. Model wheels the luggage confidently through an airport or hotel lobby. "
        "Camera tracks alongside at ground level then rises to eye-level. "
        "Smooth, sturdy trolley motion. Premium lifestyle travel setting. "
        "Wanderlust, modern Indian traveler mood. No text, no subtitles, no watermark."
    ),
    "travel_accessories": (
        "Cinematic travel moment. Model packs or uses the travel product in a hotel room or airport lounge. "
        "Camera pushes in on the product functionality. "
        "Warm travel light. Globe-trotter aesthetic. "
        "Smart packing, prepared traveler, modern India mood. No text, no subtitles, no watermark."
    ),
    # ── Vehicle ──────────────────────────────────────────────────────────────
    "car_accessories": (
        "Cinematic car lifestyle shot. Model installs or demonstrates the car accessory confidently. "
        "Camera close-up on the product fit — quality, shine, and finish. "
        "Car exterior or interior ambient lighting. "
        "Car enthusiast, premium upgrade, pride of ownership mood. No text, no subtitles, no watermark."
    ),
    "bike_accessories": (
        "Cinematic riding lifestyle shot. Model on a bike demonstrates or fits the bike accessory. "
        "Camera low angle to high — bike and product clearly visible. "
        "Outdoor road or garage setting. Golden hour light. "
        "Freedom, adventure, Indian biker culture mood. No text, no subtitles, no watermark."
    ),
    # ── Gifts & Occasions ─────────────────────────────────────────────────────
    "gift_sets": (
        "Festive cinematic unboxing shot. Hands untie a ribbon and open a beautifully wrapped gift box. "
        "Camera pushes in slowly as the contents are revealed — premium packaging and products. "
        "Warm festive golden lighting. "
        "Joyful gifting, celebration, love and care Indian mood. No text, no subtitles, no watermark."
    ),
    "festive_decor": (
        "Cinematic festive home reveal. Camera pans across a beautifully decorated room — diyas, flowers, rangoli. "
        "Product placed elegantly as part of the decor. "
        "Warm Diwali or celebration ambient lighting. "
        "Festive, vibrant, proud Indian celebration mood. No text, no subtitles, no watermark."
    ),
    # ── Garden & Plants ──────────────────────────────────────────────────────
    "plants": (
        "Cinematic nature lifestyle shot. Model's hands gently repot or water the plant. "
        "Camera close-up on the healthy green leaves. "
        "Bright natural daylight from a window or balcony. "
        "Fresh, calming, urban Indian home garden mood. No text, no subtitles, no watermark."
    ),
    "garden_tools": (
        "Cinematic gardening lifestyle shot. Model uses the garden tool in a lush green garden. "
        "Camera captures the action — digging, pruning, or planting. "
        "Warm outdoor daylight. Earthy green tones. "
        "Therapeutic, productive, green thumb Indian home mood. No text, no subtitles, no watermark."
    ),
    # ── Pet Products ─────────────────────────────────────────────────────────
    "pet_products": (
        "Heartwarming cinematic pet shot. A pet dog or cat interacts happily with the product. "
        "Model and pet together — playful, loving moment. "
        "Camera close-up on the pet's happy expression and the product. "
        "Joyful, caring, pet-parent Indian family mood. No text, no subtitles, no watermark."
    ),
    # ── Stationery & Books ────────────────────────────────────────────────────
    "stationery": (
        "Cinematic desk aesthetic shot. Hands arrange premium stationery on a clean desk. "
        "Camera close-up on product quality — pen glide, notebook texture, vibrant colors. "
        "Warm study lamp lighting. Minimal clean workspace. "
        "Productive, creative, aspirational student or professional mood. No text, no subtitles, no watermark."
    ),
    "books": (
        "Cinematic reading moment. Model opens the book with curiosity and starts reading. "
        "Camera close-up on the book cover, then pulls back to lifestyle shot. "
        "Warm reading lamp or café light. "
        "Intellectual, curious, knowledge-seeker Indian mood. No text, no subtitles, no watermark."
    ),
    # ── Music ─────────────────────────────────────────────────────────────────
    "musical_instruments": (
        "Cinematic music performance shot. Model plays the instrument passionately. "
        "Camera slowly orbits — close-up on fingers, strings, or keys. "
        "Warm stage or studio lighting. "
        "Passionate, soulful, Indian musical tradition mood. No text, no subtitles, no watermark."
    ),
    "other": (
        "Cinematic product showcase. Model presents the product to camera with confidence and warmth. "
        "Smooth camera push-in. Soft studio lighting highlights product details. "
        "Aspirational lifestyle mood. Product clearly visible throughout. "
        "No text, no subtitles, no watermark."
    ),
}


def get_gemini_omni_prompt(product_type: str, avatar_prompt: str = "", aspect_ratio: str = "9:16") -> str:
    """Build an optimized Gemini Omni prompt for the given product type."""
    base = GEMINI_OMNI_PRODUCT_PROMPTS.get(product_type, GEMINI_OMNI_PRODUCT_PROMPTS["other"])
    orientation = "Vertical 9:16 portrait framing for Instagram Reels." if aspect_ratio == "9:16" else "Horizontal 16:9 cinematic framing."
    creative = f"Creative direction: {avatar_prompt}." if avatar_prompt else ""
    return f"{orientation} {base} {creative}".strip()


async def create_gemini_omni_via_kie(
    image_url: str,
    prompt: str,
    aspect_ratio: str = "9:16",
    duration: int = 8,
    resolution: str = "720p",
) -> str:
    """Submit a Gemini Omni video job on kie.ai. Returns task ID."""
    valid_ratios      = {"9:16", "16:9", "1:1"}
    valid_resolutions = {"720p", "1080p", "4k"}
    valid_durations   = {4, 6, 8, 10}

    ar  = aspect_ratio if aspect_ratio in valid_ratios else "9:16"
    res = resolution   if resolution   in valid_resolutions else "720p"
    dur = duration     if duration     in valid_durations   else 8

    payload = {
        "model": "gemini-omni-video",
        "input": {
            "prompt":       prompt,
            "image_urls":   [image_url],
            "duration":     str(dur),
            "aspect_ratio": ar,
            "resolution":   res,
        },
    }

    last_err = None
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    f"{KIE_BASE}/jobs/createTask",
                    headers=_kie_headers(),
                    json=payload,
                )
            data = resp.json()
            if data.get("code") != 200:
                raise Exception(f"kie.ai Gemini Omni submit error: {data.get('msg')} : {data}")
            task_id = data["data"]["taskId"]
            print(f"[GeminiOmni] Task submitted: {task_id} | {ar} {dur}s {res}")
            return task_id
        except (httpx.ConnectError, httpx.TimeoutException, OSError) as e:
            last_err = e
            await asyncio.sleep(5 * (attempt + 1))
    raise Exception(f"kie.ai Gemini Omni submit failed after 3 attempts: {last_err}")


async def poll_gemini_omni_task(task_id: str) -> str:
    """Poll a kie.ai Gemini Omni task until success. Returns video URL. Timeout: 20 minutes."""
    SUCCESS_STATES = {"success", "succeed", "succeeded", "finish", "finished", "complete", "completed", "done"}
    FAIL_STATES    = {"fail", "failed", "error", "cancelled", "canceled"}

    async with httpx.AsyncClient() as client:
        for i in range(240):  # 240 × 5s = 20 minutes
            await asyncio.sleep(5)
            try:
                resp = await client.get(
                    f"{KIE_BASE}/jobs/recordInfo",
                    headers={"Authorization": f"Bearer {KIE_API_KEY}"},
                    params={"taskId": task_id},
                    timeout=15.0,
                )
                body = resp.json()
            except Exception as e:
                print(f"[GeminiOmni poll #{i}] Request error: {e} — retrying")
                continue

            data  = body.get("data") or {}
            state = (data.get("state") or data.get("status") or "").lower().strip()

            if i % 12 == 0:
                print(f"[GeminiOmni poll #{i}] taskId={task_id} state={state!r}")

            # Check resultJson first
            result_json_str = data.get("resultJson")
            if result_json_str:
                try:
                    result = json.loads(result_json_str)
                    urls = result.get("resultUrls") or result.get("videoUrls") or []
                    if urls:
                        print(f"[GeminiOmni poll #{i}] Got URL from resultJson")
                        return urls[0]
                except Exception:
                    pass

            # Direct fields
            direct_urls = data.get("resultUrls") or data.get("videoUrls") or []
            if direct_urls:
                print(f"[GeminiOmni poll #{i}] Got URL from direct field")
                return direct_urls[0]

            # Nested in response
            response_obj = data.get("response") or {}
            nested_urls  = response_obj.get("resultUrls") or response_obj.get("videoUrls") or []
            if nested_urls:
                print(f"[GeminiOmni poll #{i}] Got URL from response.resultUrls")
                return nested_urls[0]

            if state in FAIL_STATES:
                raise Exception(f"Gemini Omni task failed: state={state!r} data={data}")

    raise Exception(f"Gemini Omni task timed out after 20 minutes (id={task_id})")


async def process_job_gemini_omni(
    job_id: str,
    image_data: bytes,
    content_type: str,
    avatar_url: str,
    customization: dict | None = None,
):
    """Full UGC pipeline using Gemini Omni (kie.ai) — composite + cinematic video + voiceover."""
    try:
        c            = customization or {}
        language     = c.get("language", "hindi")
        aspect_ratio = c.get("aspect_ratio", "9:16")
        custom_script = c.get("custom_script", "").strip()
        duration     = int(c.get("duration", 8))
        if duration not in {4, 6, 8, 10}:
            duration = 8

        # ── Step 1: Claude Vision — script + product type analysis ─────────────
        jobs[job_id]["step"] = "analyzing"
        image_b64 = base64.b64encode(image_data).decode("utf-8")
        analyzed_script, analyzed_avatar_prompt, product_type, ai_settings = await asyncio.to_thread(
            generate_script, image_b64, content_type, c
        )
        if custom_script:
            script        = custom_script
            avatar_prompt = c.get("model_action", "").strip() or analyzed_avatar_prompt or "model presenting product elegantly"
        else:
            script        = analyzed_script
            avatar_prompt = analyzed_avatar_prompt
        jobs[job_id]["script"] = script

        if c.get("auto_mode") and ai_settings:
            c = {**c, **ai_settings}
        product_type = _force_product_type(product_type, _combined_generation_text(c, script, avatar_prompt))
        gender       = c.get("model_gender", "female")

        print(f"[GeminiOmni] product_type={product_type} | duration={duration}s | aspect={aspect_ratio}")

        # ── Step 2: Composite image (model + product) ──────────────────────────
        jobs[job_id]["step"] = "compositing_product"
        composite_url = await generate_model_with_product(
            avatar_url, image_data, content_type, product_type, avatar_prompt, c
        )

        # ── Step 3: TTS voiceover (edge-tts) ──────────────────────────────────
        jobs[job_id]["step"] = "generating_audio"
        voice_map = {
            "hindi":    ("hi-IN-SwaraNeural"   if gender == "female" else "hi-IN-MadhurNeural"),
            "english":  ("en-IN-NeerjaNeural"  if gender == "female" else "en-IN-PrabhatNeural"),
            "hinglish": ("hi-IN-SwaraNeural"   if gender == "female" else "hi-IN-MadhurNeural"),
        }
        voice      = voice_map.get(language, "hi-IN-SwaraNeural")
        audio_path = os.path.join(tempfile.gettempdir(), f"{job_id}_omni_tts.mp3")

        if edge_tts:
            communicate = edge_tts.Communicate(script, voice)
            await communicate.save(audio_path)
        else:
            audio_path = None

        # ── Step 4: Gemini Omni video generation ──────────────────────────────
        jobs[job_id]["step"] = "generating_video"
        omni_prompt = get_gemini_omni_prompt(product_type, avatar_prompt, aspect_ratio)
        task_id     = await create_gemini_omni_via_kie(composite_url, omni_prompt, aspect_ratio, duration, "720p")
        jobs[job_id]["kie_task_id"] = task_id

        # Persist task_id to orders.json (survives server restart)
        oid = jobs[job_id].get("order_id")
        if oid:
            _orders = load_orders()
            for _o in _orders:
                if _o.get("id") == oid:
                    _o["kie_task_id"] = task_id
                    break
            with open(ORDERS_FILE, "w", encoding="utf-8") as _f:
                json.dump(_orders, _f, ensure_ascii=False, indent=2)

        omni_video_url = await poll_gemini_omni_task(task_id)

        # ── Step 5: Merge audio + re-encode ───────────────────────────────────
        jobs[job_id]["step"] = "processing_video"
        raw_path   = os.path.join(tempfile.gettempdir(), f"{job_id}_omni_raw.mp4")
        final_path = os.path.join(
            os.path.dirname(__file__), "static", "videos", f"{job_id}.mp4"
        )
        os.makedirs(os.path.dirname(final_path), exist_ok=True)

        async with httpx.AsyncClient(timeout=120.0) as client:
            r = await client.get(omni_video_url)
            r.raise_for_status()
        with open(raw_path, "wb") as f:
            f.write(r.content)

        ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
        if audio_path and os.path.exists(audio_path):
            # Merge TTS audio with video
            cmd = [
                ffmpeg_exe, "-y",
                "-i", raw_path,
                "-i", audio_path,
                "-filter_complex",
                "[1:a]aresample=async=1000,afade=t=in:st=0:d=0.15,volume=1.3[tts];"
                "[0:a][tts]amix=inputs=2:duration=first:dropout_transition=2[aout]",
                "-map", "0:v",
                "-map", "[aout]",
                "-c:v", "libx264", "-preset", "fast", "-crf", "22",
                "-c:a", "aac", "-b:a", "128k",
                "-movflags", "+faststart",
                final_path,
            ]
        else:
            # No TTS — just re-encode
            cmd = [
                ffmpeg_exe, "-y",
                "-i", raw_path,
                "-c:v", "libx264", "-preset", "fast", "-crf", "22",
                "-c:a", "aac", "-b:a", "128k",
                "-movflags", "+faststart",
                final_path,
            ]

        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"[GeminiOmni] ffmpeg warning: {result.stderr[-500:]}")

        # Cleanup temp files
        for p in [raw_path, audio_path]:
            if p and os.path.exists(p):
                try:
                    os.unlink(p)
                except Exception:
                    pass

        final_url = f"/static/videos/{job_id}.mp4"
        final_url = await append_video_end_card(final_url, job_id, aspect_ratio, c.get("video_end_card"))

        jobs[job_id].update({"status": "completed", "step": "completed", "video_url": final_url})
        save_to_history({
            "id":           job_id,
            "date":         datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "script":       script,
            "video_url":    final_url,
            "product_type": product_type,
            "language":     language,
            "pipeline":     "gemini_omni",
        })

        # ── Step 6: Upload to Cloudinary + push to Railway ────────────────────
        if oid:
            cdn_url          = await _upload_to_cloudinary(final_path, job_id)
            public_video_url = cdn_url or (f"{PUBLIC_URL}{final_url}" if PUBLIC_URL else final_url)
            asyncio.create_task(_push_result_to_railway(oid, public_video_url, "", script))
            asyncio.create_task(_delete_model_photos(oid))

    except Exception as e:
        jobs[job_id].update({"status": "failed", "error": str(e)})
        print(f"[GeminiOmni] Pipeline error: {e}")


# ── Seedance 2.0 pipeline (via kie.ai) ───────────────────────────────────────

async def create_seedance_via_kie(image_url: str, motion_prompt: str, aspect_ratio: str = "9:16", duration: int = 5, resolution: str = "480p") -> str:
    """Submit a Seedance 2.0 image-to-video job on kie.ai. Returns task ID."""
    valid_ratios = {"9:16", "16:9", "1:1", "4:3", "3:4", "21:9", "adaptive"}
    valid_resolutions = {"480p", "720p", "1080p"}
    dur = max(4, min(15, duration))  # kie.ai accepts 4-15s
    payload = {
        "model": "bytedance/seedance-2",
        "input": {
            "first_frame_url": image_url,
            "prompt": motion_prompt,
            "resolution": resolution if resolution in valid_resolutions else "480p",
            "aspect_ratio": aspect_ratio if aspect_ratio in valid_ratios else "9:16",
            "duration": dur,
            "generate_audio": False,
        },
    }
    last_err = None
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(f"{KIE_BASE}/jobs/createTask", headers=_kie_headers(), json=payload)
            data = resp.json()
            if data.get("code") != 200:
                raise Exception(f"kie.ai Seedance submit error: {data.get('msg')} — {data}")
            return data["data"]["taskId"]
        except (httpx.ConnectError, httpx.TimeoutException, OSError) as e:
            last_err = e
            await asyncio.sleep(5 * (attempt + 1))
    raise Exception(f"kie.ai Seedance submit failed after 3 attempts: {last_err}")


async def generate_audio_bytes(script: str, gender: str = "female", language: str = "hindi") -> bytes:
    """Generate TTS audio with edge-tts and return raw MP3 bytes (no upload needed)."""
    voice = _pick_voice(gender, language)
    audio_path = os.path.join(tempfile.gettempdir(), f"ugc_audio_{uuid.uuid4().hex}.mp3")
    try:
        communicate = edge_tts.Communicate(script, voice)
        await communicate.save(audio_path)
        with open(audio_path, "rb") as f:
            return f.read()
    finally:
        if os.path.exists(audio_path):
            os.unlink(audio_path)


async def process_job_seedance(job_id: str, image_data: bytes, content_type: str, avatar_url: str, customization: dict | None = None):
    """Full pipeline using Seedance 2.0 (kie.ai) for animation + edge-tts voiceover merged via ffmpeg."""
    try:
        c = customization or {}
        language       = c.get("language", "hindi")
        aspect_ratio   = c.get("aspect_ratio", "9:16")
        video_duration = c.get("video_duration", "5")
        custom_script  = c.get("custom_script", "").strip()

        # Step 1: Script
        jobs[job_id]["step"] = "analyzing"
        if custom_script:
            script = custom_script
            avatar_prompt = c.get("model_action", "").strip() or "talking expressively, gesturing naturally to camera"
            product_type = _force_product_type("other", _combined_generation_text(c, script, avatar_prompt))
            if product_type == "medical":
                c = {**c, "scene": "clinic"}
                avatar_prompt = (
                    "same reference person styled in doctor coat or clean medical attire, "
                    "professionally presenting the stethoscope in a clinic"
                )
            ai_settings = {}
        else:
            image_b64 = base64.b64encode(image_data).decode("utf-8")
            script, avatar_prompt, product_type, ai_settings = await asyncio.to_thread(
                generate_script, image_b64, content_type, c
            )
        jobs[job_id]["script"] = script

        if c.get("auto_mode") and ai_settings:
            c = {**c, **ai_settings}
        product_type = _force_product_type(product_type, _combined_generation_text(c, script, avatar_prompt))
        if product_type == "medical":
            c = {**c, "scene": "clinic"}
        gender = c.get("model_gender", "female")

        # Step 2: Composite image (GPT-4o via KIE) + TTS audio in parallel
        jobs[job_id]["step"] = "compositing_product"
        composite_url, audio_bytes = await asyncio.gather(
            generate_model_with_product(avatar_url, image_data, content_type, product_type, avatar_prompt, c),
            generate_audio_bytes(script, gender, language),
        )

        # Step 3: Seedance animation via kie.ai
        jobs[job_id]["step"] = "generating_video"
        motion_prompt = f"{avatar_prompt}. Cinematic natural movement, looking at camera."
        seedance_resolution = c.get("seedance_resolution", "480p")
        task_id = await create_seedance_via_kie(
            composite_url, motion_prompt, aspect_ratio, int(video_duration), seedance_resolution
        )
        seedance_video_url = await poll_task(task_id)

        # Step 4: Merge voiceover + re-encode (audio_bytes passed directly, no upload)
        jobs[job_id]["step"] = "processing_video"
        final_url = await download_and_reencode_video(
            seedance_video_url, job_id, aspect_ratio, audio_bytes=audio_bytes
        )
        final_url = await append_video_end_card(final_url, job_id, aspect_ratio, c.get("video_end_card"))

        jobs[job_id].update({"status": "completed", "step": "completed", "video_url": final_url})
        save_to_history({
            "id": job_id,
            "date": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "script": script,
            "video_url": final_url,
            "product_type": product_type,
            "language": language,
        })

    except Exception as e:
        jobs[job_id].update({"status": "failed", "error": str(e)})


# ── Retell AI Webhook ─────────────────────────────────────────────────────────

@app.get("/api/retell-webhook")
async def retell_webhook_ping():
    """Health-check — Retell or browser can GET this to confirm the endpoint is live."""
    return {"status": "ok", "service": "Retell webhook — Dr. Akshay Midha Dental Clinic"}


@app.post("/api/retell-webhook")
async def retell_webhook_receive(request: Request):
    """
    Retell calls this endpoint after every call ends.
    Paste this URL in your Retell dashboard → Agent → Webhook Settings.
    """
    body = await request.json()
    print(f"[Retell] Webhook received: {str(body)[:300]}")
    try:
        result = await handle_retell_webhook(body)
        return result
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"[Retell] Error: {e}")
        # Always return 200 so Retell doesn't retry aggressively
        return {"status": "error", "detail": str(e)}


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    host = "0.0.0.0" if os.getenv("RAILWAY_ENVIRONMENT") else "127.0.0.1"
    uvicorn.run("main:app", host=host, port=port, reload=False)
