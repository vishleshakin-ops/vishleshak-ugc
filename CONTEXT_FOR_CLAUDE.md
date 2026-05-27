# UGC Ads Tool — Context Document for Claude

Paste this file into any Claude chat session to instantly restore full context.

---

## Project
AI-powered UGC video ad generator at `D:\Future\ugc-jewelry-video`
- FastAPI backend, runs at `http://localhost:8000`
- Start: `cd D:\Future\ugc-jewelry-video && .\venv\Scripts\uvicorn.exe main:app --host 0.0.0.0 --port 8000`

## What it does
Upload a product image → get a UGC-style Instagram Reel video where an AI Indian female model speaks about the product in Hindi, wearing/holding/eating it naturally.

## Pipeline
1. Claude analyzes product → Hindi script + product_type
2. 4o Image API → model photo + product photo → new image of model using product
3. ElevenLabs TTS via kie.ai → Hindi audio
4. Kling AI Avatar → lip-sync model+product image with Hindi audio
5. FFmpeg crf=18 → high quality final video

## Model
- Professional Indian female, 25 years old, white top, grey studio background
- Saved locally: `D:\Future\ugc-jewelry-video\model\current_model.jpg`
- NEVER download from catbox.moe (blocked) — always read from local disk

## Key kie.ai endpoints
- File upload for 4o: `https://kieai.redpandaai.co/api/file-stream-upload` ← critical, NOT api.kie.ai
- Task create/poll: `https://api.kie.ai/api/v1/jobs/createTask` and `recordInfo`
- 4o Image: `https://api.kie.ai/api/v1/gpt4o-image/generate` and `record-info`

## Known issues & fixes already applied
- catbox.moe blocked by kie.ai → upload to kieai.redpandaai.co CDN instead
- Eyes closed → add "eyes wide open, looking directly at camera" to all prompts
- Necklace floating → 4o Image prompt says "sitting ON her skin, not floating"
- Video quality → crf=18, preset slow
- Bikini/swimwear → GPT-4o rejects it (Flux fallback not yet built)

## .env location
`D:\Future\ugc-jewelry-video\.env`
Keys: ANTHROPIC_API_KEY, KIE_API_KEY, VOICE_ID, MODEL_IMAGE_URL
