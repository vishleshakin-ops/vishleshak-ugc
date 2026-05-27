@echo off
cd /d D:\Future\ugc-jewelry-video
echo Testing imports one by one...
echo Testing httpx... & venv\Scripts\python.exe -c "import httpx; print('httpx OK')"
echo Testing edge_tts... & venv\Scripts\python.exe -c "import edge_tts; print('edge_tts OK')"
echo Testing fal_client... & venv\Scripts\python.exe -c "import fal_client; print('fal_client OK')"
echo Testing anthropic... & venv\Scripts\python.exe -c "import anthropic; print('anthropic OK')"
echo Testing fastapi... & venv\Scripts\python.exe -c "from fastapi import FastAPI; print('fastapi OK')"
echo Testing PIL... & venv\Scripts\python.exe -c "from PIL import Image; print('PIL OK')"
echo ALL DONE
pause
