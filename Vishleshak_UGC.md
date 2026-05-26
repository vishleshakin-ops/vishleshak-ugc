# Vishleshak UGC Tool — Developer Skill Reference

## Quick Start
```
venv\Scripts\python.exe main.py
# or double-click start_server.bat
```
- Admin dashboard: http://127.0.0.1:8000
- Customer order form: http://127.0.0.1:8000/order

---

## Architecture Overview

```
Customer (order form)
    ↓ POST /api/orders  [video_style: kling | seedance]
Admin Dashboard (approve)
    ↓ POST /api/orders/{id}/approve
    ├── video_style=kling    → process_job()          [Kling lip-sync pipeline]
    └── video_style=seedance → process_job_seedance() [Seedance motion pipeline]
```

---

## Pipeline 1 — Kling Avatar (Talking Ad)

**Use for:** Talking head UGC ads where presenter speaks to camera

```
Step 1: Claude Vision → script + avatar_prompt + gender/skin/scene
Step 2: kie.ai GPT-4o Image → composite (model + product) image
Step 3: edge-tts → voiceover MP3 (uploaded to fal.ai CDN)
Step 4: kie.ai Kling Avatar → lip-synced video (model: kling/ai-avatar-standard)
Step 5: ffmpeg → re-encode at correct aspect ratio → /static/videos/{job_id}.mp4
```

**Cost:** ~₹35 per video
**Time:** 8–12 min

---

## Pipeline 2 — Seedance 2.0 (Lifestyle/Fashion)

**Use for:** Cinematic motion reels, fashion/lifestyle product videos

```
Step 1: Claude Vision → script + avatar_prompt + gender/skin/scene
Step 2: kie.ai GPT-4o Image → composite image  ┐ (parallel)
Step 3: edge-tts → voiceover bytes (local only)  ┘
Step 4: kie.ai Seedance 2.0 → animated video (model: bytedance/seedance-2)
Step 5: ffmpeg → merge audio + re-encode with aresample fix → final MP4
```

**Cost:** ~₹30–45 for 5s at 480p
**Time:** 5–8 min
**Note:** No lip-sync. Natural body motion, walking, gesturing.

---

## Key API Calls

### kie.ai — Create Task (any model)
```python
POST https://api.kie.ai/api/v1/jobs/createTask
Authorization: Bearer {KIE_API_KEY}

# Kling Avatar
{
  "model": "kling/ai-avatar-standard",
  "input": { "image_url": "...", "audio_url": "...", "prompt": "..." }
}

# Seedance 2.0
{
  "model": "bytedance/seedance-2",
  "input": {
    "first_frame_url": "...",     # composite image
    "prompt": "...",              # motion description
    "resolution": "480p",         # 480p | 720p | 1080p
    "aspect_ratio": "9:16",       # 9:16 | 16:9 | 1:1 | 4:3 | 3:4
    "duration": 5,                # 4–15 seconds
    "generate_audio": false
  }
}
```

### kie.ai — Poll Task
```python
GET https://api.kie.ai/api/v1/jobs/recordInfo?taskId={task_id}
# Poll every 5s, check data.state == "success"
# Result: json.loads(data["resultJson"])["resultUrls"][0]
```

### kie.ai — 4o Image Composite
```python
POST https://api.kie.ai/api/v1/gpt4o-image/generate
# Poll: GET https://api.kie.ai/api/v1/gpt4o-image/record-info?taskId=...
# Result: data["response"]["resultUrls"][0]
```

---

## ffmpeg Audio Merge (Seedance)
```python
# Critical flags to avoid audio jerk:
"-af", "aresample=async=1000,afade=t=in:st=0:d=0.15"
"-ar", "44100"
"-shortest"
# aresample fixes MP3 encoder delay sync issue
# afade adds 150ms fade-in to mask any remaining click
```

---

## Voice Selection
```python
# Gender → voice mapping
female/girl_kid → hi-IN-SwaraNeural   (Hindi) / en-IN-NeerjaNeural  (English)
male/boy_kid   → hi-IN-MadhurNeural  (Hindi) / en-IN-PrabhatNeural (English)
```

---

## Order Fields (orders.json)
```json
{
  "id": "uuid",
  "status": "pending | processing | completed | failed | rejected",
  "customer_name": "",
  "customer_phone": "+919...",
  "language": "hindi | english",
  "output_type": "video | image",
  "video_duration": "5 | 10 | 15 | 30 | 60",
  "video_quality": "standard | high | ultra",
  "presenter_source": "ai | uploaded",
  "video_style": "kling | seedance",
  "platform": "instagram | facebook | youtube | x",
  "aspect_ratio": "9:16 | 16:9 | 1:1",
  "notes": "customer special request",
  "custom_script": "edited script from preview step",
  "product_image_path": "order_uploads/{id}.jpg",
  "job_id": "uuid (background job tracker)"
}
```

---

## Common Issues & Fixes

| Issue | Cause | Fix |
|---|---|---|
| Order stuck on "Processing" | List endpoint didn't merge job status | `/api/orders` now merges live job state |
| Failed order, no retry button | Status check was `== "pending"` only | Changed to `in ("pending", "failed")` |
| Audio jerk at start of Seedance video | MP3 encoder delay in ffmpeg merge | Added `aresample=async=1000` + `afade` |
| DNS error on kie.ai call | Transient network issue | 3x retry with 5/10/15s backoff |
| Male model, female voice | `model_gender` not sent in auto mode | Always append gender after Claude decides |
| Customer notes ignored | Not injected into Claude prompt in auto mode | Added MUST FOLLOW directive to Claude |
| Wrong pipeline used | `video_style` not saved to order | Added to `submit_order` Form params |

---

## Adding a New Video Provider

1. Add `async def create_{provider}_video(image_url, prompt, ...) -> str` — returns video URL
2. Add `async def process_job_{provider}(job_id, image_data, content_type, avatar_url, customization)` — full pipeline
3. Add `POST /api/orders/{order_id}/approve-{provider}` endpoint
4. Add button in `app.js` `renderOrderCard()` for pending/failed orders
5. Wire event listener in `loadOrdersAdmin()`
6. Add option to `video_style` segmented in `order.html`
7. Update routing in `approve_order()`: `pipeline = process_job_{provider} if order.get("video_style") == "{provider}" else ...`

---

## Environment Variables (.env)
```
ANTHROPIC_API_KEY=...
KIE_API_KEY=...          # main pipeline (Kling + Seedance + 4o Image)
FAL_KEY=...              # audio CDN upload only
DID_API_KEY=...          # fallback pipeline
IMGBB_API_KEY=...
OWNER_EMAIL=vishleshak.in@gmail.com
GMAIL_APP_PASSWORD=cdlu uxmc ubpf sljr
OWNER_WHATSAPP=919953910987
CALLMEBOT_API_KEY=       # empty — not working, Gmail used instead
```
