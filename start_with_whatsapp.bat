@echo off
title Vishleshak UGC Tool + WhatsApp Bot

echo ============================================
echo  Vishleshak UGC Tool - WhatsApp Bot Mode
echo ============================================
echo.

REM Check if ngrok is installed
where ngrok >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] ngrok not found. Installing...
    winget install ngrok.ngrok
    echo.
    echo Please run this script again after ngrok is installed.
    pause
    exit /b
)

echo [1/3] Starting UGC server on port 8000...
start "UGC Server" cmd /k "cd /d %~dp0 && venv\Scripts\python.exe -m uvicorn main:app --host 0.0.0.0 --port 8000 --reload"

echo Waiting for server to start...
timeout /t 4 /nobreak >nul

echo [2/3] Starting ngrok tunnel...
start "ngrok" cmd /k "ngrok http 8000"

echo.
echo [3/3] Waiting for ngrok to get public URL...
timeout /t 5 /nobreak >nul

echo.
echo ============================================
echo  NEXT STEPS:
echo ============================================
echo.
echo 1. Copy your ngrok URL (looks like: https://xxxx.ngrok-free.app)
echo 2. Paste it into .env as: PUBLIC_URL=https://xxxx.ngrok-free.app
echo 3. Go to: developers.facebook.com ^> your app ^> WhatsApp ^> Configuration
echo 4. Set Webhook URL to: https://xxxx.ngrok-free.app/webhook
echo 5. Set Verify Token to: vishleshak_ugc_2024
echo 6. Subscribe to: messages
echo.
echo Dashboard: http://localhost:8000
echo ngrok UI:  http://localhost:4040
echo.
pause
