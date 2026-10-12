@echo off
cd /d E:\JobPilot
title JobPilot

echo [1/2] Releasing port 8699...
for /f "tokens=5" %%a in ('netstat -ano 2^>nul ^| findstr /i "8699.*LISTENING"') do (
    taskkill /PID %%a /F >nul 2>&1
)
ping -n 2 127.0.0.1 >nul

echo [2/2] Launching JobPilot (silent, no extra console windows)...
start "" "E:\JobPilot\.venv\Scripts\pythonw.exe" -m jobpilot.gui --browser
exit