@echo off
cd /d "%~dp0"
echo [voice-ai] stopping any running instances...
powershell -ExecutionPolicy Bypass -File "%~dp0stop.ps1"
timeout /t 2 /nobreak >nul
echo [voice-ai] starting...
venv\Scripts\python.exe server.py
