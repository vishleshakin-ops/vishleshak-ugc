@echo off
title Vishleshak UGC Server
cd /d D:\Future\ugc-jewelry-video
echo Starting Vishleshak UGC Server...
echo Press Ctrl+C to stop.
echo.

:loop
echo [%time%] Server starting...
venv\Scripts\python.exe -m uvicorn main:app --port 8000 --host 127.0.0.1 --log-level info
echo [%time%] Server stopped. Restarting in 3 seconds...
timeout /t 3 /nobreak >nul
goto loop
