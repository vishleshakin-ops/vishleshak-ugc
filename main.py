import os
import uuid
import json
import base64
import asyncio
import subprocess
import tempfile
import io
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
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
from datetime import datetime
from fastapi import FastAPI, File, Form, UploadFile, HTTPException, BackgroundTasks, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, PlainTextResponse
import anthropic
import imageio_ffmpeg
from PIL import Image
from dotenv import load_dotenv, set_key

ENV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
load_dotenv(dotenv_path=ENV_FILE, override=True)

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

def save_order(order: dict):
    all_orders = load_orders()
    all_orders = [o for o in all_orders if o.get("id") != order.get("id")]
    all_orders.insert(0, order)
    with open(ORDERS_FILE, "w", encoding="utf-8") as f:
        json.dump(all_orders, f, ensure_ascii=False, indent=2)

MODEL_DIR  = os.path.join(os.path.dirname(__file__), "model")
VIDEOS_DIR = os.path.join(os.path.dirname(__file__), "static", "videos")
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}

HISTORY_FILE = os.path.join(os.path.dirname(__file__), "video_history.json")

os.makedirs(VIDEOS_DIR, exist_ok=True)
os.makedirs(MODEL_DIR, exist_ok=True)


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
    if os.path.exists(MODEL_LOCAL_PATH):
        with open(MODEL_LOCAL_PATH, "rb") as f:
            model_image_bytes = f.read()

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
    model_action     = c.get("model_action", "").strip()
    custom_instr     = c.get("custom_instructions", "").strip()

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

    def build_prompt(base_action: str) -> str:
        action = model_action if model_action else base_action
        if presenter_source == "ai":
            p = (
                f"Professional UGC creator photo for Instagram Reels. "
                f"Subject: a real-looking {gender_adj}, 24-28 years old, {skin_desc}, "
                f"naturally beautiful with subtle makeup, styled hair, wearing a stylish casual Indian outfit. "
                f"Action: {action}. "
                f"The product from the uploaded image must be clearly visible, held or worn naturally — not floating, not pasted on. "
                f"Background: {background_desc}. "
                f"Shot on Sony A7III, 85mm f/1.8 lens, shallow depth of field, soft bokeh background. "
                f"Soft diffused lighting with natural skin highlights. "
                f"Hyper-realistic skin texture, visible pores, natural imperfections — NOT AI-looking, NOT plastic skin, NOT CGI. "
                f"Real human face with natural asymmetry. Vertical 9:16 portrait frame. "
                f"Ultra high quality, 8K, magazine-grade photography. {EYES_OPEN}"
            )
        else:
            p = (
                f"Using the first image as the model and the second image as the product, "
                f"generate a photorealistic image of this exact {gender_word} ({gender_adj}, {skin_desc}) "
                f"{action}. "
                f"Background: {background_desc}. "
                f"Keep the model's face and distinguishing features identical. {EYES_OPEN} High quality."
            )
        if custom_instr:
            p += f" Additional: {custom_instr}."
        return p

    INTERACTION_PROMPTS = {
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

    # Read model from local disk — never download catbox.moe URLs
    if presenter_source != "ai" and model_image_bytes:
        local_model_bytes = model_image_bytes
    elif presenter_source != "ai" and os.path.exists(MODEL_LOCAL_PATH):
        with open(MODEL_LOCAL_PATH, "rb") as f:
            local_model_bytes = f.read()
    elif presenter_source != "ai":
        raise Exception("Model image not found locally. Please re-upload the model photo.")

    if presenter_source != "ai":
        model_kie_url = await upload_image_to_kie(local_model_bytes, "model.jpg", "image/jpeg")
        files_url = [model_kie_url, product_kie_url]

    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(
            "https://api.kie.ai/api/v1/gpt4o-image/generate",
            headers={"Authorization": f"Bearer {KIE_API_KEY}", "Content-Type": "application/json"},
            json={
                "prompt": prompt,
                "size": "2:3",
                "nVariants": 1,
                "isEnhance": False,
                "filesUrl": files_url,
            },
        )
    data = resp.json()
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
            audio_path = os.path.join(tmpdir, "voice.mp3")
            with open(audio_path, "wb") as f:
                f.write(audio_bytes)
            cmd = [
                ffmpeg_exe, "-y",
                "-i", video_path,
                "-i", audio_path,
                "-map", "0:v", "-map", "1:a",
                "-vf", vf_filter,
                "-af", "aresample=async=1000,afade=t=in:st=0:d=0.15",
                "-c:v", "libx264", "-preset", "slow", "-crf", "18",
                "-c:a", "aac", "-b:a", "128k", "-ar", "44100",
                "-shortest",
                output_path,
            ]
        else:
            cmd = [
                ffmpeg_exe, "-y",
                "-i", video_path,
                "-vf", vf_filter,
                "-c:v", "libx264", "-preset", "slow", "-crf", "18",
                "-c:a", "aac", "-b:a", "128k",
                output_path,
            ]
        result = await asyncio.to_thread(
            subprocess.run, cmd, capture_output=True, text=True, timeout=300
        )
        if result.returncode != 0:
            raise Exception(f"Video re-encode failed: {result.stderr[-400:]}")

    return f"/static/videos/{job_id}.mp4"


async def download_and_save_image(image_url: str, job_id: str) -> str:
    """Download a generated composite image and save it locally."""
    images_dir = os.path.join(os.path.dirname(__file__), "static", "images")
    os.makedirs(images_dir, exist_ok=True)
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.get(image_url)
        image_bytes = resp.content
    output_path = os.path.join(images_dir, f"{job_id}.jpg")
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
    video_duration: str = Form("30"),
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
    custom_script: str = Form(""),
):
    if not image.content_type or not image.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="File must be an image")
    image_data = await image.read()
    if len(image_data) > 15 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Image must be under 15MB")
    if presenter_source not in ("uploaded", "ai"):
        raise HTTPException(status_code=400, detail="Invalid presenter source")
    if presenter_source == "uploaded" and not model_image_url and not os.path.exists(MODEL_LOCAL_PATH):
        raise HTTPException(status_code=400, detail="Model photo not set. Please upload your model photo first or choose AI presenter.")
    if output_type == "image" and not KIE_API_KEY:
        raise HTTPException(status_code=400, detail="Image generation requires KIE API (4o Image). Please configure KIE_API_KEY.")
    if presenter_source == "ai" and not KIE_API_KEY:
        raise HTTPException(status_code=400, detail="AI presenter requires KIE_API_KEY because it creates a presenter image from the product.")

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
    video_duration: str = Form("30"),
    presenter_source: str = Form("ai"),
    video_quality: str = Form("high"),
    platform: str = Form("instagram"),
    aspect_ratio: str = Form("9:16"),
    notes: str = Form(""),
    custom_script: str = Form(""),
    video_style: str = Form("kling"),
    product_image: UploadFile = File(...),
):
    image_data = await product_image.read()
    if len(image_data) > 15 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Image must be under 15MB")
    order_id = str(uuid.uuid4())
    img_path = os.path.join(ORDERS_UPLOAD_DIR, f"{order_id}.jpg")
    with open(img_path, "wb") as f:
        f.write(image_data)
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
        "custom_script": custom_script,
        "video_style": video_style,
        "product_image_path": img_path,
        "product_mime": product_image.content_type or "image/jpeg",
        "job_id": None,
        "video_url": None,
        "image_url": None,
        "script": None,
    }
    save_order(order)
    _new_order_ids.append(order_id)
    asyncio.create_task(_send_order_email(order))
    asyncio.create_task(_send_whatsapp_notification(order))
    return {"order_id": order_id, "status": "pending"}


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
    return orders


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
    jobs[job_id] = {"status": "processing", "step": "analyzing", "script": None, "video_url": None, "image_url": None, "error": None}
    customization = {
        "presenter_source": order.get("presenter_source", "ai"), "output_type": order.get("output_type", "video"),
        "video_duration": order.get("video_duration", "30"), "video_quality": order.get("video_quality", "high"),
        "auto_mode": True, "language": order.get("language", "hindi"),
        "model_gender": "female", "skin_tone": "wheatish", "scene": "studio",
        "custom_scene": "", "model_action": order.get("notes", ""),
        "custom_instructions": order.get("notes", ""), "aspect_ratio": order.get("aspect_ratio", "9:16"), "custom_script": order.get("custom_script", ""),
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
    if order["status"] not in ("pending", "failed"):
        raise HTTPException(status_code=400, detail="Order already processed")
    img_path = order["product_image_path"]
    if not os.path.exists(img_path):
        raise HTTPException(status_code=400, detail="Product image missing")
    with open(img_path, "rb") as f:
        image_data = f.read()
    job_id = str(uuid.uuid4())
    jobs[job_id] = {"status": "processing", "step": "analyzing", "script": None, "video_url": None, "image_url": None, "error": None}
    customization = {
        "presenter_source": order.get("presenter_source", "ai"),
        "output_type": order.get("output_type", "video"),
        "video_duration": order.get("video_duration", "30"),
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
    }

    order["status"] = "processing"
    order["job_id"] = job_id
    save_order(order)
    pipeline = process_job_seedance if order.get("video_style") == "seedance" else process_job
    background_tasks.add_task(pipeline, job_id, image_data, order.get("product_mime", "image/jpeg"), model_image_url, customization)
    # If order came from WhatsApp, notify customer when done
    wa_from = order.get("wa_from")
    if wa_from:
        product_name = order.get("notes", "your product").replace("WhatsApp order for: ", "")
        background_tasks.add_task(notify_wa_on_complete, job_id, order_id, wa_from, product_name)
    return {"job_id": job_id, "status": "processing"}


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
    jobs[job_id] = {"status": "processing", "step": "analyzing", "script": None, "video_url": None, "image_url": None, "error": None}
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
    jobs[job_id] = {"status": "processing", "step": "analyzing", "script": None, "video_url": None, "image_url": None, "error": None}
    customization = {
        "presenter_source": order.get("presenter_source", "ai"),
        "output_type": "video",
        "video_duration": "8",
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
    return FileResponse("static/order.html")

@app.get("/order/result/{order_id}")
async def serve_order_result_page(order_id: str):
    return FileResponse("static/order-result.html")


# ── WhatsApp Webhook ──────────────────────────────────────────────────────────

from whatsapp_bot import handle_whatsapp_message, VERIFY_TOKEN as WA_VERIFY_TOKEN, send_text as wa_send_text, send_video as wa_send_video


async def notify_wa_on_complete(job_id: str, order_id: str, wa_from: str, product_name: str):
    """Poll job status and send the finished video to the WhatsApp customer."""
    import asyncio as _asyncio
    public_base = os.getenv("PUBLIC_URL", "").rstrip("/")
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
            # Send video to customer
            video_path = os.path.join(os.path.dirname(__file__), "static", "videos", f"{job_id}.mp4")
            if public_base and os.path.exists(video_path):
                video_url = f"{public_base}/static/videos/{job_id}.mp4"
                await wa_send_video(
                    wa_from, video_url,
                    caption=f"🎬 Your video ad for *{product_name}* is ready!\n\nPost it on Instagram/Facebook to boost your sales! 🚀\n\n📞 Order more: wa.me/919953910987"
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
    """Receive incoming WhatsApp messages."""
    body = await request.json()
    print(f"[WA] Webhook received: {str(body)[:300]}")

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
        jobs[job_id] = {"status": "processing", "step": "analyzing", "script": None, "video_url": None, "image_url": None, "error": None}
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
        video_duration = c.get("video_duration", "30")
        video_quality  = c.get("video_quality", "high")
        custom_script  = c.get("custom_script", "").strip()

        # Derive script word target from duration
        duration_word_targets = {"5": "8-12", "10": "15-20", "15": "20-30", "30": "50-65", "60": "100-120"}
        script_word_target = duration_word_targets.get(video_duration, "50-65")

        # Derive D-ID stitch setting and ElevenLabs model from quality
        did_stitch   = video_quality in ("high", "ultra")
        elevenlabs_model = "eleven_turbo_v2_5" if video_quality == "standard" else "eleven_multilingual_v2"

        if custom_script:
            # Skip Claude — use the user-provided (possibly edited) script
            jobs[job_id]["step"] = "analyzing"
            script = custom_script
            avatar_prompt = c.get("model_action", "").strip() or "talking to camera naturally, expressive"
            product_type  = "other"
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
                final_image_url = await download_and_save_image(model_with_product_url, job_id)
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

    except Exception as e:
        jobs[job_id].update({"status": "failed", "error": str(e)})


# ── Claude Vision ─────────────────────────────────────────────────────────────

def generate_script(image_b64: str, media_type: str, customization: dict | None = None) -> tuple:
    """
    Returns (script, avatar_prompt, product_type, ai_settings).
    ai_settings is populated only in auto mode — contains AI-decided gender/skin/scene.
    """
    c            = customization or {}
    auto_mode    = c.get("auto_mode", False)
    language     = c.get("language", "hindi")
    model_action = c.get("model_action", "").strip()
    custom_instr = c.get("custom_instructions", "").strip()
    gender       = c.get("model_gender", "female")
    gender_hint  = "male Indian model" if gender == "male" else "female Indian model"

    # Language-specific script instruction — word count driven by video_duration
    _wt = c.get("video_duration", "30")
    _word_targets = {"5": "8-12", "10": "15-20", "15": "20-30", "30": "50-65", "60": "100-120"}
    _wc = _word_targets.get(_wt, "50-65")
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
            '"auto_gender":"female or male",'
            '"auto_skin_tone":"fair or wheatish or dusky or dark",'
            '"auto_scene":"studio or beach or ramp or cafe or garden or outdoor"}'
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
            "avatar_prompt: Exactly how the model uses this product physically. Under 20 words.\n"
            "  - Food/drink → picks up, bites/sips, reacts with joy\n"
            "  - Sports → holds and plays actively\n"
            "  - Clothing → wears it, poses, shows off\n"
            "  - Jewelry → wears it, touches gently, admires\n"
            "  - Electronics → holds it, uses it, reacts\n"
            "  - Other → uses it naturally\n\n"
            "product_type: one of 'food','sports','clothing','jewelry','electronics','other'\n\n"
            "auto_gender: Study the product image carefully. Who is this product MADE FOR? Choose exactly one: 'female', 'male', 'girl_kid', 'boy_kid'.\n"
            "  Think like a smart Indian marketer — look at the product size, design, colors, style, branding, and intended user. Do not guess randomly.\n\n"
            "auto_skin_tone: Choose the skin tone that best matches the target audience and product aesthetic "
            "('fair' for premium bridal/luxury, 'wheatish' for everyday Indian mainstream, 'dusky' for sporty/outdoor/bold, 'dark' for high-fashion/statement pieces).\n\n"
            "auto_scene: Best realistic background for this product "
            "('studio' for jewellery/electronics/premium products, 'beach' for sunscreen/swimwear/summer, 'ramp' for fashion/clothing, "
            "'cafe' for food/beverages/lifestyle, 'garden' for skincare/natural/organic products, 'outdoor' for sports/adventure)."
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
            "avatar_prompt: Exactly how the model uses this product physically. Under 20 words.\n"
            "  - Food/drink → picks up, bites/sips, reacts with joy\n"
            "  - Sports → holds and plays actively\n"
            "  - Clothing → wears it, poses, shows off\n"
            "  - Jewelry → wears it, touches gently, admires\n"
            "  - Electronics → holds it, uses it, reacts\n"
            "  - Other → uses it naturally\n\n"
            "product_type: one of 'food','sports','clothing','jewelry','electronics','other'."
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
        "food":        "picking up food, taking a big bite, chewing happily, smiling with delight",
        "sports":      "holding sports equipment, swinging actively, looking energetic and confident",
        "clothing":    "wearing the clothing, posing and showing it off with a big smile",
        "jewelry":     "wearing jewelry naturally, touching it gently, admiring it, looking elegant",
        "electronics": "holding device, using it, reacting with excitement",
        "other":       "holding product, using it naturally, smiling at camera",
    }

    raw = message.content[0].text.strip()
    try:
        data         = json.loads(raw)
        script       = data["script"]
        product_type = data.get("product_type", "other").lower()
        avatar_prompt = data.get("avatar_prompt", "").strip() or FALLBACK_PROMPTS.get(product_type, FALLBACK_PROMPTS["other"])
        ai_settings  = {
            "model_gender": data.get("auto_gender", "female"),
            "skin_tone":    data.get("auto_skin_tone", "wheatish"),
            "scene":        data.get("auto_scene", "studio"),
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
            data = resp.json().get("data", {})
            state = data.get("state")
            if state == "success":
                result = json.loads(data["resultJson"])
                return result["resultUrls"][0]
            if state == "fail":
                raise Exception(f"kie.ai task failed (id={task_id})")

    raise Exception(f"kie.ai task timed out after 10 minutes (id={task_id})")


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

        # Step 1: Script via Claude Vision
        jobs[job_id]["step"] = "analyzing"
        if custom_script:
            script = custom_script
            avatar_prompt = c.get("model_action", "").strip() or "model presenting product elegantly, looking at camera"
            product_type  = "other"
            ai_settings   = {}
        else:
            image_b64 = base64.b64encode(image_data).decode("utf-8")
            script, avatar_prompt, product_type, ai_settings = await asyncio.to_thread(
                generate_script, image_b64, content_type, c
            )
        jobs[job_id]["script"] = script

        if c.get("auto_mode") and ai_settings:
            c = {**c, **ai_settings}
        gender = c.get("model_gender", "female")

        # Step 2: Composite image only (no TTS needed — Veo 3 has native audio)
        jobs[job_id]["step"] = "compositing_product"
        composite_url = await generate_model_with_product(
            avatar_url, image_data, content_type, product_type, avatar_prompt, c
        )

        # Step 3: Veo 3 Fast — visual + voiceover in one prompt
        jobs[job_id]["step"] = "generating_video"
        lang_instruction = "Hindi voiceover" if language == "hindi" else "English voiceover"
        veo3_prompt = (
            f"{avatar_prompt}. Cinematic lifestyle video, smooth natural motion. "
            f"{lang_instruction} saying: \"{script}\""
        )
        task_id = await create_veo3_via_kie(composite_url, veo3_prompt, aspect_ratio, 8, "720p")
        veo3_video_url = await poll_task(task_id)

        # Step 4: Re-encode to correct aspect ratio (no audio merge — audio already embedded)
        jobs[job_id]["step"] = "processing_video"
        final_url = await download_and_reencode_video(veo3_video_url, job_id, aspect_ratio)

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
            product_type = "other"
            ai_settings = {}
        else:
            image_b64 = base64.b64encode(image_data).decode("utf-8")
            script, avatar_prompt, product_type, ai_settings = await asyncio.to_thread(
                generate_script, image_b64, content_type, c
            )
        jobs[job_id]["script"] = script

        if c.get("auto_mode") and ai_settings:
            c = {**c, **ai_settings}
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


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    host = "0.0.0.0" if os.getenv("RAILWAY_ENVIRONMENT") else "127.0.0.1"
    uvicorn.run("main:app", host=host, port=port, reload=False)
