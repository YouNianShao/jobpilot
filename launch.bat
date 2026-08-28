@echo off
cd /d E:\JobPilot
title JobPilot
echo.
echo ===== JobPilot starting =====
echo [1/2] Releasing port 8699...
for /f "tokens=5" %%a in ('netstat -ano 2^>nul ^| findstr /i "8699.*LISTENING"') do (
    taskkill /PID %%a /F >nul 2>&1
)
ping -n 2 127.0.0.1 >nul
echo [2/2] Launching JobPilot dashboard...
echo       Opening http://127.0.0.1:8699
echo.
"E:\JobPilot\.venv\Scripts\python.exe" -m jobpilot.gui --browser
if errorlevel 1 (
    echo Launch failed. Try the other .bat or run server manually.
    pause
)
