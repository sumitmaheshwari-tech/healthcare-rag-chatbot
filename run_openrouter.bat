@echo off
title MedCare Assistant - OpenRouter Launcher
echo ===================================================
echo MedCare Assistant - OpenRouter Cloud GPU Launcher
echo ===================================================
echo.

REM 1. Free port 8000 if it is busy
echo [1/2] Checking if Port 8000 is busy...
powershell -Command "$port = Get-NetTCPConnection -LocalPort 8000 -ErrorAction SilentlyContinue; if ($port) { Stop-Process -Id $port.OwningProcess -Force -ErrorAction SilentlyContinue; Write-Host 'Port 8000 was busy and has been successfully freed.' } else { Write-Host 'Port 8000 is free.' }"
echo.

REM 2. Launch the FastAPI server
echo [2/2] Starting backend server with Llama 3.3 70B...
cd backend
python main.py
if %errorlevel% neq 0 (
    echo.
    echo [ERROR] The server failed to start. Check your Python installation or dependencies.
)
pause
