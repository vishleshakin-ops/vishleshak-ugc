"""
WhatsApp Bot for Vishleshak UGC Tool
=====================================
Customers send a product photo → bot auto-creates order → processes video → sends it back.

Conversation flow:
  Step 1: Customer sends a photo
          Bot: "Got your photo! Please reply with details in this format:
                Product name, Style (1=Talking Head / 2=Cinematic), Language (Hindi/English)
                Example: Gold necklace, 2, Hindi"
  Step 2: Customer replies with details
          Bot: "Creating your video ad now! ⏳ Ready in ~10 minutes."
  Step 3: Video done → Bot sends the MP4 back
"""

import os
import uuid
import json
import asyncio
import httpx
from datetime import datetime

WHATSAPP_TOKEN    = os.getenv("WHATSAPP_TOKEN", "")
PHONE_NUMBER_ID   = os.getenv("PHONE_NUMBER_ID", "")
VERIFY_TOKEN      = os.getenv("WA_VERIFY_TOKEN", "vishleshak_ugc_2024")

WA_API_BASE = f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}"

# In-memory session store: phone → {"step": 1|2, "image_path": "...", "image_bytes": b"..."}
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
        # Step 1: get the URL
        r = await client.get(
            f"https://graph.facebook.com/v19.0/{media_id}",
            headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"},
        )
        url = r.json().get("url", "")
        if not url:
            raise Exception(f"Could not get media URL: {r.text}")
        # Step 2: download the file
        r2 = await client.get(url, headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"})
        return r2.content


# ── Message handler ───────────────────────────────────────────────────────────

async def handle_whatsapp_message(body: dict, process_order_func):
    """
    Main entry point — called from the POST /webhook route in main.py.
    process_order_func: the approve_order coroutine from main.py
    """
    try:
        entry   = body.get("entry", [{}])[0]
        changes = entry.get("changes", [{}])[0]
        value   = changes.get("value", {})
        messages = value.get("messages", [])

        if not messages:
            return  # status update, not a message

        msg  = messages[0]
        from_phone = msg.get("from", "")
        msg_type   = msg.get("type", "")

        print(f"[WA] Message from {from_phone}, type={msg_type}")

        # ── Image received ────────────────────────────────────────────────────
        if msg_type == "image":
            media_id   = msg["image"]["id"]
            caption    = msg["image"].get("caption", "").strip()

            await send_text(from_phone,
                "📸 Got your product photo!\n\n"
                "Please reply with your details in this format:\n"
                "*Product name, Style, Language*\n\n"
                "Style options:\n"
                "1️⃣ Talking Head (AI presenter speaks about product)\n"
                "2️⃣ Cinematic (elegant lifestyle motion video)\n\n"
                "Example:\n"
                "_Gold necklace, 2, Hindi_\n\n"
                "⏱ Your video will be ready in ~10 minutes!"
            )

            # Download and save image
            image_bytes = await download_wa_media(media_id)
            sessions[from_phone] = {
                "step": 2,
                "image_bytes": image_bytes,
                "image_mime": "image/jpeg",
                "caption": caption,
            }

            # If caption already has details, process immediately
            if caption and ("," in caption or len(caption.split()) >= 2):
                await process_details(from_phone, caption, process_order_func)

        # ── Text reply ────────────────────────────────────────────────────────
        elif msg_type == "text":
            text = msg["text"]["body"].strip()
            session = sessions.get(from_phone, {})

            if session.get("step") == 2:
                await process_details(from_phone, text, process_order_func)
            else:
                # No active session — welcome message
                await send_text(from_phone,
                    "👋 Welcome to *Vishleshak UGC Video Ads*!\n\n"
                    "Send me a photo of your product and I'll create a professional AI video ad for you.\n\n"
                    "📱 Formats supported: jewellery, clothing, food, electronics & more.\n"
                    "💰 Starting at just ₹999/video"
                )

    except Exception as e:
        print(f"[WA] Error handling message: {e}")
        import traceback
        traceback.print_exc()


async def process_details(from_phone: str, text: str, process_order_func):
    """Parse customer's text reply and create+process the order."""
    session = sessions.get(from_phone, {})
    if not session or "image_bytes" not in session:
        await send_text(from_phone, "Please send your product photo first! 📸")
        return

    # Parse: "product name, style, language"
    parts = [p.strip() for p in text.split(",")]
    product_name = parts[0] if parts else "Product"
    style_raw    = parts[1].strip() if len(parts) > 1 else "2"
    language_raw = parts[2].strip().lower() if len(parts) > 2 else "hindi"

    # Resolve style
    video_style = "veo3" if style_raw in ("2", "veo3", "cinematic", "veo") else "kling"

    # Resolve language
    language = "hindi" if "hindi" in language_raw else "english"

    # Create order
    order_id   = str(uuid.uuid4())
    image_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "order_uploads",
        f"{order_id}.jpg"
    )
    with open(image_path, "wb") as f:
        f.write(session["image_bytes"])

    order = {
        "id":                order_id,
        "status":            "pending",
        "customer_name":     from_phone,
        "customer_phone":    from_phone,
        "language":          language,
        "output_type":       "video",
        "video_duration":    "5",
        "video_quality":     "standard",
        "presenter_source":  "ai",
        "video_style":       video_style,
        "platform":          "instagram",
        "aspect_ratio":      "9:16",
        "notes":             f"WhatsApp order for: {product_name}",
        "custom_script":     "",
        "product_image_path": image_path,
        "job_id":            None,
        "created_at":        datetime.utcnow().isoformat(),
        "wa_from":           from_phone,
    }

    # Save order
    orders_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "orders.json")
    try:
        with open(orders_file, "r", encoding="utf-8") as f:
            all_orders = json.load(f)
    except Exception:
        all_orders = []
    all_orders.insert(0, order)
    with open(orders_file, "w", encoding="utf-8") as f:
        json.dump(all_orders, f, ensure_ascii=False, indent=2)

    style_name = "Talking Head" if video_style == "kling" else "Cinematic"
    await send_text(from_phone,
        f"✅ *Order received!*\n\n"
        f"📦 Product: {product_name}\n"
        f"🎬 Style: {style_name}\n"
        f"🗣 Language: {language.title()}\n\n"
        f"Our team will review your order and start creating your video shortly.\n"
        f"You'll receive the video here on WhatsApp once it's ready! ⏳"
    )

    # Clear session
    sessions.pop(from_phone, None)
    # Order is saved as "pending" — admin approves from dashboard, video is sent back via WhatsApp when done


async def process_and_notify(order_id: str, from_phone: str, product_name: str, process_order_func):
    """Process the order and send the video back when done."""
    try:
        print(f"[WA] Processing order {order_id} for {from_phone}")
        await process_order_func(order_id)

        # Find the video file
        videos_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "videos")

        # Check orders.json for job_id
        orders_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "orders.json")
        with open(orders_file, "r", encoding="utf-8") as f:
            all_orders = json.load(f)
        order = next((o for o in all_orders if o["id"] == order_id), {})
        job_id = order.get("job_id", order_id)

        video_path = os.path.join(videos_dir, f"{job_id}.mp4")

        if os.path.exists(video_path):
            # Upload to imgbb to get public URL for WhatsApp
            with open(video_path, "rb") as f:
                video_bytes = f.read()

            # Use the server's public URL (via ngrok)
            public_base = os.getenv("PUBLIC_URL", "").rstrip("/")
            if public_base:
                video_url = f"{public_base}/static/videos/{job_id}.mp4"
                await send_video(from_phone, video_url,
                    caption=f"🎬 Your UGC video ad for *{product_name}* is ready!\n\n"
                            f"Post this on Instagram/Facebook to boost sales! 🚀\n\n"
                            f"📞 Order more: wa.me/919953910987"
                )
            else:
                await send_text(from_phone,
                    f"✅ Your video for *{product_name}* is ready!\n\n"
                    f"Please contact us to receive the video file:\n"
                    f"📞 wa.me/919953910987"
                )
        else:
            await send_text(from_phone,
                f"✅ Your video ad for *{product_name}* has been created!\n\n"
                f"Please contact us to receive your video:\n"
                f"📞 +91 99539 10987"
            )

    except Exception as e:
        print(f"[WA] Error processing order {order_id}: {e}")
        await send_text(from_phone,
            f"Sorry, there was an issue creating your video. Our team will contact you shortly.\n"
            f"📞 +91 99539 10987"
        )
