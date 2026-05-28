"""
WhatsApp Bot for Vishleshak UGC Tool
=====================================
Step-by-step conversation flow matching the order form:

  1. Customer sends a product photo
     Bot: auto-detects product name with Claude Vision, then asks style

  2. Bot: "Choose style:
           1 = Talking Ad — AI presenter speaks (5s, ₹499)
           2 = Cinematic  — Veo3 lifestyle video (6s, ₹599)"

  3. Customer types 1 or 2
     Bot: "Choose language: 1=Hindi / 2=English / 3=Hinglish"

  4. Customer types 1/2/3
     Bot: "✅ Order confirmed! [summary] — We'll start shortly."

  5. Admin approves → video generated → sent back on WhatsApp
"""

import os
import uuid
import json
import base64
import asyncio
import httpx
import anthropic
from datetime import datetime

WHATSAPP_TOKEN    = os.getenv("WHATSAPP_TOKEN", "")
PHONE_NUMBER_ID   = os.getenv("PHONE_NUMBER_ID", "")
VERIFY_TOKEN      = os.getenv("WA_VERIFY_TOKEN", "vishleshak_ugc_2024")

WA_API_BASE = f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}"

# In-memory session store per phone number
# session = {
#   "step": "await_name" | "await_style" | "await_duration" | "await_language",
#   "image_bytes": b"...",
#   "image_mime": "image/jpeg",
#   "product_name": "Gold necklace",
#   "video_style": "kling" | "seedance",
#   "video_duration": "5" | "10" | "15",
#   "language": "hindi" | "english",
# }
sessions: dict = {}


# ── Send helpers ──────────────────────────────────────────────────────────────

async def send_text(to: str, message: str):
    """Send a WhatsApp text message."""
    if not WHATSAPP_TOKEN or not PHONE_NUMBER_ID:
        print(f"[WA] WhatsApp not configured — would send to {to}: {message}")
        return
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{WA_API_BASE}/messages",
            headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"},
            json={
                "messaging_product": "whatsapp",
                "to": to,
                "type": "text",
                "text": {"body": message},
            },
        )
    print(f"[WA] send_text → {resp.status_code}: {resp.text[:200]}")


async def send_video(to: str, video_url: str, caption: str = ""):
    """Send a WhatsApp video message via public URL."""
    if not WHATSAPP_TOKEN or not PHONE_NUMBER_ID:
        print(f"[WA] WhatsApp not configured — would send video to {to}: {video_url}")
        return
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{WA_API_BASE}/messages",
            headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"},
            json={
                "messaging_product": "whatsapp",
                "to": to,
                "type": "video",
                "video": {"link": video_url, "caption": caption},
            },
        )
    print(f"[WA] send_video → {resp.status_code}: {resp.text[:200]}")


async def download_wa_media(media_id: str) -> bytes:
    """Download media from WhatsApp using media ID."""
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.get(
            f"https://graph.facebook.com/v19.0/{media_id}",
            headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"},
        )
        url = r.json().get("url", "")
        if not url:
            raise Exception(f"Could not get media URL: {r.text}")
        r2 = await client.get(url, headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"})
        return r2.content


# ── Product detection ─────────────────────────────────────────────────────────

async def detect_product_name(image_bytes: bytes) -> str:
    """Use Claude Vision to detect the product name from the photo."""
    try:
        api_key = os.getenv("ANTHROPIC_API_KEY", "")
        if not api_key:
            return "your product"
        client = anthropic.Anthropic(api_key=api_key)
        image_b64 = base64.b64encode(image_bytes).decode("utf-8")
        response = await asyncio.to_thread(
            client.messages.create,
            model="claude-haiku-4-5",
            max_tokens=30,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": "image/jpeg", "data": image_b64},
                    },
                    {
                        "type": "text",
                        "text": "What product is in this image? Reply with only the product name, 2-5 words, no punctuation.",
                    },
                ],
            }],
        )
        name = response.content[0].text.strip().strip(".")
        return name if name else "your product"
    except Exception as e:
        print(f"[WA-UGC] Product detection failed: {e}")
        return "your product"


# ── Step prompts ──────────────────────────────────────────────────────────────

ASK_STYLE = (
    "🎬 *Choose your video style:*\n\n"
    "1️⃣ *Talking Ad* — AI presenter lip-syncs about your product _(5s · ₹499)_\n"
    "2️⃣ *Cinematic* — Elegant Veo3 lifestyle video _(6s · ₹599)_\n\n"
    "Reply *1* or *2*"
)

ASK_LANGUAGE = (
    "🗣 *Choose language:*\n\n"
    "1️⃣ Hindi\n"
    "2️⃣ English\n"
    "3️⃣ Hinglish\n\n"
    "Reply *1*, *2* or *3*"
)


# ── Main handler ──────────────────────────────────────────────────────────────

async def handle_whatsapp_message(body: dict, process_order_func):
    """
    Main entry point — called from POST /webhook in main.py.
    process_order_func is the _process_wa_order wrapper from main.py.
    """
    try:
        entry    = body.get("entry", [{}])[0]
        changes  = entry.get("changes", [{}])[0]
        value    = changes.get("value", {})
        messages = value.get("messages", [])

        if not messages:
            return

        msg        = messages[0]
        from_phone = msg.get("from", "")
        msg_type   = msg.get("type", "")
        text       = msg.get("text", {}).get("body", "").strip() if msg_type == "text" else ""

        print(f"[WA-UGC] {from_phone}: type={msg_type} text='{text[:60]}'")

        session = sessions.get(from_phone, {})
        step    = session.get("step", "")

        # ── New product photo received ────────────────────────────────────────
        if msg_type == "image":
            media_id    = msg["image"]["id"]
            image_bytes = await download_wa_media(media_id)

            # Auto-detect product name with Claude Vision
            product_name = await detect_product_name(image_bytes)

            sessions[from_phone] = {
                "step":         "await_style",
                "image_bytes":  image_bytes,
                "image_mime":   "image/jpeg",
                "product_name": product_name,
            }
            await send_text(from_phone,
                f"📸 *Got your photo!*\n"
                f"🔍 Product detected: *{product_name}*\n\n"
                + ASK_STYLE
            )
            return

        # ── Text replies ──────────────────────────────────────────────────────
        if msg_type != "text":
            return

        # No active session
        if not step:
            await send_text(from_phone,
                "👋 Welcome to *Vishleshak UGC Video Ads!*\n\n"
                "Send me a 📸 *photo of your product* and I'll create a professional AI video ad for you.\n\n"
                "💰 Starting at just ₹499/video"
            )
            return

        # Step 1 — waiting for style choice
        if step == "await_style":
            if text == "1":
                session["video_style"]    = "kling"
                session["video_duration"] = "5"
            elif text == "2":
                session["video_style"]    = "veo3"
                session["video_duration"] = "6"
            else:
                await send_text(from_phone, "Please reply *1* for Talking Ad or *2* for Cinematic. 🎬")
                return
            session["step"] = "await_language"
            sessions[from_phone] = session
            await send_text(from_phone, ASK_LANGUAGE)
            return

        # Step 3 — waiting for language choice
        if step == "await_language":
            if text == "1":
                session["language"] = "hindi"
            elif text == "2":
                session["language"] = "english"
            elif text == "3":
                session["language"] = "hinglish"
            else:
                await send_text(from_phone, "Please reply *1* for Hindi, *2* for English, or *3* for Hinglish. 🗣")
                return

            # All info collected — place the order
            sessions.pop(from_phone, None)
            await place_order(from_phone, session, process_order_func)
            return

        # Fallback
        await send_text(from_phone,
            "Please send a 📸 *photo of your product* to get started!"
        )

    except Exception as e:
        print(f"[WA-UGC] Error: {e}")
        import traceback
        traceback.print_exc()


# ── Place order ───────────────────────────────────────────────────────────────

async def place_order(from_phone: str, session: dict, process_order_func):
    """Save the order to orders.json and confirm to customer."""
    product_name   = session.get("product_name", "Product")
    video_style    = session.get("video_style", "seedance")
    video_duration = session.get("video_duration", "5")
    language       = session.get("language", "hindi")
    image_bytes    = session.get("image_bytes", b"")

    style_name = "Talking Ad (Lip-sync)" if video_style == "kling" else "Cinematic (Veo3)"
    price_str  = "₹499" if video_style == "kling" else "₹599"

    order_id   = str(uuid.uuid4())
    image_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "order_uploads",
        f"{order_id}.jpg"
    )
    os.makedirs(os.path.dirname(image_path), exist_ok=True)
    with open(image_path, "wb") as f:
        f.write(image_bytes)

    order = {
        "id":                 order_id,
        "status":             "pending",
        "customer_name":      from_phone,
        "customer_phone":     from_phone,
        "language":           language,
        "output_type":        "video",
        "video_duration":     video_duration,
        "video_quality":      "standard",
        "presenter_source":   "ai",
        "video_style":        video_style,
        "platform":           "instagram",
        "aspect_ratio":       "9:16",
        "notes":              f"WhatsApp order for: {product_name}",
        "custom_script":      "",
        "product_image_path": image_path,
        "job_id":             None,
        "created_at":         datetime.utcnow().isoformat(),
        "wa_from":            from_phone,
    }

    # Save to orders.json
    orders_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "orders.json")
    try:
        with open(orders_file, "r", encoding="utf-8") as f:
            all_orders = json.load(f)
    except Exception:
        all_orders = []
    all_orders.insert(0, order)
    with open(orders_file, "w", encoding="utf-8") as f:
        json.dump(all_orders, f, ensure_ascii=False, indent=2)

    # Confirm to customer
    await send_text(from_phone,
        f"✅ *Order confirmed!*\n\n"
        f"📦 Product: {product_name}\n"
        f"🎬 Style: {style_name}\n"
        f"⏱ Duration: {video_duration} seconds\n"
        f"🗣 Language: {language.title()}\n"
        f"💰 Price: {price_str}\n\n"
        f"Our team will review your order and start creating your video shortly.\n"
        f"You'll receive the video here on WhatsApp once it's ready! ⏳\n\n"
        f"For queries: wa.me/919953910987"
    )


async def process_and_notify(order_id: str, from_phone: str, product_name: str, process_order_func):
    """Process the order and send the video back when done."""
    try:
        print(f"[WA] Processing order {order_id} for {from_phone}")
        await process_order_func(order_id)

        orders_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "orders.json")
        with open(orders_file, "r", encoding="utf-8") as f:
            all_orders = json.load(f)
        order  = next((o for o in all_orders if o["id"] == order_id), {})
        job_id = order.get("job_id", order_id)

        public_base = os.getenv("PUBLIC_URL", "").rstrip("/")
        video_path  = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "static", "videos", f"{job_id}.mp4"
        )

        if os.path.exists(video_path) and public_base:
            video_url = f"{public_base}/static/videos/{job_id}.mp4"
            await send_video(from_phone, video_url,
                caption=(
                    f"🎬 Your video ad for *{product_name}* is ready!\n\n"
                    f"Post it on Instagram/Facebook to boost sales! 🚀\n\n"
                    f"📞 Order more: wa.me/919953910987"
                )
            )
        else:
            await send_text(from_phone,
                f"✅ Your video ad for *{product_name}* is ready!\n\n"
                f"Please contact us to receive the file:\n"
                f"📞 +91 99539 10987"
            )
    except Exception as e:
        print(f"[WA] Error processing order {order_id}: {e}")
        await send_text(from_phone,
            f"Sorry, there was an issue creating your video. Our team will contact you shortly.\n"
            f"📞 +91 99539 10987"
        )
