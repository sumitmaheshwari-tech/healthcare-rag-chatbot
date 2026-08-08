@echo off
title MedCare Assistant - Local Ollama Launcher
echo ===================================================
echo MedCare Assistant - Local Ollama Launcher
echo ===================================================
echo.

REM 1. Free port 8000 if it is busy
echo [1/6] Checking if Port 8000 is busy...
powershell -Command "$port = Get-NetTCPConnection -LocalPort 8000 -ErrorAction SilentlyContinue; if ($port) { Stop-Process -Id $port.OwningProcess -Force -ErrorAction SilentlyContinue; Write-Host 'Port 8000 was busy and has been successfully freed.' } else { Write-Host 'Port 8000 is free.' }"
echo.

REM 2. Reset Ollama processes to apply CPU configuration
echo [2/6] Resetting Ollama background processes...
taskkill /f /im "ollama.exe" >nul 2>nul
taskkill /f /im "ollama app.exe" >nul 2>nul
ping 127.0.0.1 -n 2 >nul

REM 3. Set environment variables to completely disable Vulkan & GPU runtimes
set OLLAMA_VULKAN=false
set GGML_VK_VISIBLE_DEVICES=-1
set CUDA_VISIBLE_DEVICES=""
set ROCR_VISIBLE_DEVICES=""
set HIP_VISIBLE_DEVICES=""

REM 4. Start the Ollama background daemon manually
echo [3/6] Starting Ollama server in CPU-Safe Mode...
start "" /b ollama serve
echo Waiting for Ollama to initialize (5 seconds)...
ping 127.0.0.1 -n 6 >nul

REM 5. Pull the Llama 3.2 base model
echo [4/6] Downloading / updating base model (llama3.2)...
ollama pull llama3.2
echo.

REM 6. Compile the custom low-latency MedCare model configuration
echo [5/6] Compiling low-latency model from Modelfile...
ollama create medcare-model -f Modelfile
echo.

REM 7. Switch the configuration in .env to point to medcare-model
echo [6/6] Updating environment configuration...
powershell -Command "if (Test-Path '.env') { (Get-Content .env) -replace 'LLM_PROVIDER=groq', 'LLM_PROVIDER=ollama' -replace 'OLLAMA_MODEL=llama3.1', 'OLLAMA_MODEL=medcare-model' -replace 'OLLAMA_MODEL=llama3.2', 'OLLAMA_MODEL=medcare-model' | Set-Content .env; Write-Host 'Updated .env.' } else { Write-Host 'No .env found.' }"
echo.

REM 8. Launch the FastAPI server
echo Starting backend server...
cd backend
python main.py
if %errorlevel% neq 0 (
    echo.
    echo [ERROR] The server failed to start.
)
pause
