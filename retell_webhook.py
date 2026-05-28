"""
Retell AI Webhook Handler — Dr. Akshay Midha Dental Clinic
============================================================
Retell calls this endpoint after every call.

POST /api/retell-webhook
  → Parse call data + post-call extraction
  → WhatsApp confirmation to patient
  → WhatsApp notification to clinic owner
  → Schedule 24-hour appointment reminder
"""

import os
import asyncio
import httpx
from datetime import datetime, timedelta

# ── Config ─────────────────────────────────────────────────────────────────────
WHATSAPP_TOKEN  = os.getenv("WHATSAPP_TOKEN", "")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID", "")
CLINIC_OWNER_WA = os.getenv("CLINIC_OWNER_WA", os.getenv("OWNER_WHATSAPP", "919953910987"))
RETELL_SECRET   = os.getenv("RETELL_WEBHOOK_SECRET", "")   # optional signing check

WA_API_BASE = f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}"

CLINIC_NAME    = "Dr. Akshay Midha Multi Speciality Dental Clinic"
CLINIC_PHONE   = "+91 98765 43210"   # update in .env as CLINIC_PHONE
CLINIC_ADDRESS = "Sector 14, Gurugram"  # update in .env as CLINIC_ADDRESS


def _clinic_name():
    return os.getenv("CLINIC_NAME", CLINIC_NAME)

def _clinic_phone():
    return os.getenv("CLINIC_PHONE", CLINIC_PHONE)

def _clinic_address():
    return os.getenv("CLINIC_ADDRESS", CLINIC_ADDRESS)


# ── WhatsApp helpers ───────────────────────────────────────────────────────────

async def _wa_send(to: str, text: str):
    """Send a WhatsApp text message. Silently skips if not configured."""
    if not WHATSAPP_TOKEN or not PHONE_NUMBER_ID:
        print(f"[Retell/WA] Not configured — would send to {to}:\n{text}")
        return
    # Ensure number is in E.164 without +
    to = to.lstrip("+").replace(" ", "").replace("-", "")
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                f"{WA_API_BASE}/messages",
                headers={
                    "Authorization": f"Bearer {WHATSAPP_TOKEN}",
                    "Content-Type": "application/json",
                },
                json={
                    "messaging_product": "whatsapp",
                    "to": to,
                    "type": "text",
                    "text": {"body": text},
                },
            )
        print(f"[Retell/WA] → {to} : {resp.status_code}")
    except Exception as e:
        print(f"[Retell/WA] send failed to {to}: {e}")


# ── Reminder scheduler ─────────────────────────────────────────────────────────

async def _schedule_reminder(patient_phone: str, patient_name: str,
                              appt_date: str, appt_time: str, appt_type: str,
                              delay_seconds: float):
    """Wait then send a 24-hour reminder message."""
    await asyncio.sleep(delay_seconds)
    msg = (
        f"⏰ *Appointment Reminder*\n\n"
        f"Hi {patient_name or 'there'}, this is a reminder from *{_clinic_name()}*.\n\n"
        f"📅 *Date:* {appt_date}\n"
        f"🕐 *Time:* {appt_time}\n"
        f"🦷 *Treatment:* {appt_type or 'Dental Consultation'}\n\n"
        f"📍 *Address:* {_clinic_address()}\n"
        f"📞 *Questions?* Call us at {_clinic_phone()}\n\n"
        f"_Please arrive 5 minutes early. See you soon! 😊_"
    )
    await _wa_send(patient_phone, msg)
    print(f"[Retell/WA] 24hr reminder sent to {patient_phone}")


# ── Core handler ───────────────────────────────────────────────────────────────

async def handle_retell_webhook(body: dict):
    """
    Main entry point — called from POST /api/retell-webhook in main.py.

    Retell payload shape:
    {
      "event": "call_ended" | "call_started" | "call_analyzed",
      "call": {
        "call_id": "...",
        "call_status": "ended",
        "from_number": "+91...",
        "to_number": "+91...",
        "transcript": "...",
        "call_analysis": {
          "call_summary": "...",
          "custom_analysis_data": {
            "patient_name": "...",
            "appointment_date": "...",
            "appointment_time": "...",
            "appointment_type": "...",
            "booking_confirmed": true | false
          }
        }
      }
    }
    """
    event = body.get("event", "")
    call  = body.get("call", {})

    print(f"[Retell] Event: {event} | call_id: {call.get('call_id', '?')}")

    # Only act on call_ended (or call_analyzed — Retell fires both)
    if event not in ("call_ended", "call_analyzed"):
        print(f"[Retell] Ignored event: {event}")
        return {"status": "ignored", "event": event}

    call_status  = call.get("call_status", "")
    from_number  = call.get("from_number", "")
    transcript   = call.get("transcript", "")
    call_id      = call.get("call_id", "unknown")

    # Pull post-call extraction data
    analysis     = call.get("call_analysis", {}) or {}
    summary      = analysis.get("call_summary", "")
    custom       = analysis.get("custom_analysis_data", {}) or {}

    # Flexible field names — Retell uses whatever you defined in your extraction config
    patient_name  = (custom.get("patient_name")
                     or custom.get("name")
                     or custom.get("caller_name")
                     or "").strip()
    appt_date     = (custom.get("appointment_date")
                     or custom.get("date")
                     or custom.get("booking_date")
                     or "").strip()
    appt_time     = (custom.get("appointment_time")
                     or custom.get("time")
                     or custom.get("booking_time")
                     or "").strip()
    appt_type     = (custom.get("appointment_type")
                     or custom.get("treatment")
                     or custom.get("reason")
                     or custom.get("concern")
                     or "Dental Consultation").strip()
    booking_ok    = custom.get("booking_confirmed", False)

    # Normalise booking_ok — could be bool or string "true"
    if isinstance(booking_ok, str):
        booking_ok = booking_ok.lower() in ("true", "yes", "1", "confirmed")

    print(f"[Retell] Booking={booking_ok} | Patient={patient_name} | "
          f"Date={appt_date} | Time={appt_time} | Type={appt_type}")

    # ── 1. Patient confirmation ────────────────────────────────────────────────
    if from_number and booking_ok:
        name_part = f"Hi {patient_name}," if patient_name else "Hi,"
        patient_msg = (
            f"✅ *Appointment Confirmed!*\n\n"
            f"{name_part} your appointment at *{_clinic_name()}* is booked.\n\n"
            f"📅 *Date:* {appt_date or 'As discussed'}\n"
            f"🕐 *Time:* {appt_time or 'As discussed'}\n"
            f"🦷 *Treatment:* {appt_type}\n\n"
            f"📍 *Address:* {_clinic_address()}\n"
            f"📞 *Helpline:* {_clinic_phone()}\n\n"
            f"_You will receive a reminder 24 hours before your appointment._\n"
            f"_Please carry a valid ID. See you soon! 😊_"
        )
        await _wa_send(from_number, patient_msg)

    elif from_number and not booking_ok:
        # Call happened but no booking was made — send a soft follow-up
        name_part = f"Hi {patient_name}," if patient_name else "Hi,"
        followup_msg = (
            f"👋 {name_part} thanks for calling *{_clinic_name()}*!\n\n"
            f"It seems we couldn't complete your booking. We'd love to help!\n\n"
            f"📞 *Call us:* {_clinic_phone()}\n"
            f"Or simply reply to this message and we'll get back to you shortly. 😊"
        )
        await _wa_send(from_number, followup_msg)

    # ── 2. Owner / clinic notification ────────────────────────────────────────
    if CLINIC_OWNER_WA:
        status_icon = "✅" if booking_ok else "📵"
        owner_msg = (
            f"{status_icon} *{'New Booking' if booking_ok else 'Missed Booking'} — {_clinic_name()}*\n\n"
            f"📞 *Patient:* {patient_name or 'Unknown'} ({from_number or 'No number'})\n"
            f"📅 *Date:* {appt_date or '—'}\n"
            f"🕐 *Time:* {appt_time or '—'}\n"
            f"🦷 *Treatment:* {appt_type}\n"
            f"✅ *Confirmed:* {'Yes' if booking_ok else 'No'}\n\n"
        )
        if summary:
            # Keep summary short for WA
            short_summary = summary[:300] + ("…" if len(summary) > 300 else "")
            owner_msg += f"📝 *Summary:* {short_summary}\n\n"
        owner_msg += f"🆔 Call ID: `{call_id}`"
        await _wa_send(CLINIC_OWNER_WA, owner_msg)

    # ── 3. 24-hour reminder ────────────────────────────────────────────────────
    if booking_ok and from_number and appt_date and appt_time:
        delay = _compute_reminder_delay(appt_date, appt_time)
        if delay and delay > 60:  # only schedule if > 1 minute away
            asyncio.create_task(
                _schedule_reminder(
                    from_number, patient_name, appt_date, appt_time, appt_type, delay
                )
            )
            print(f"[Retell] 24hr reminder scheduled in {delay/3600:.1f} hrs for {from_number}")
        else:
            print(f"[Retell] Skipped reminder — appointment too soon or date not parseable")

    return {"status": "ok", "booking_confirmed": booking_ok}


# ── Date parsing helper ────────────────────────────────────────────────────────

_DATE_FORMATS = [
    "%d %B %Y",      # 5 June 2026
    "%d %b %Y",      # 5 Jun 2026
    "%B %d %Y",      # June 5 2026
    "%d/%m/%Y",      # 05/06/2026
    "%d-%m-%Y",      # 05-06-2026
    "%Y-%m-%d",      # 2026-06-05
    "%d %B",         # 5 June  (assumes current year)
    "%d %b",         # 5 Jun
    "%B %d",         # June 5
]

_TIME_FORMATS = [
    "%I:%M %p",    # 10:30 AM
    "%I:%M%p",     # 10:30AM
    "%H:%M",       # 10:30
    "%I %p",       # 10 AM
    "%I%p",        # 10AM
]


def _parse_appt_datetime(appt_date: str, appt_time: str):
    """Best-effort parse of date+time strings into a datetime. Returns None on failure."""
    if not appt_date or not appt_time:
        return None
    now = datetime.now()
    # Try combining
    combined = f"{appt_date.strip()} {appt_time.strip()}"
    for df in _DATE_FORMATS:
        for tf in _TIME_FORMATS:
            fmt = f"{df} {tf}"
            try:
                dt = datetime.strptime(combined, fmt)
                # If year is missing, assume next occurrence
                if dt.year == 1900:
                    dt = dt.replace(year=now.year)
                    if dt < now:
                        dt = dt.replace(year=now.year + 1)
                return dt
            except ValueError:
                continue
    return None


def _compute_reminder_delay(appt_date: str, appt_time: str) -> float | None:
    """Return seconds until 24 hrs before the appointment. None if can't parse."""
    appt_dt = _parse_appt_datetime(appt_date, appt_time)
    if not appt_dt:
        return None
    reminder_at = appt_dt - timedelta(hours=24)
    delay = (reminder_at - datetime.now()).total_seconds()
    return delay if delay > 0 else None
