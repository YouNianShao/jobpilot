@echo off
cd /d E:\JobPilot
echo Stopping JobPilot dashboard (8699) and browser proxy (3457)...
for /f "tokens=5" %%a in ('netstat -ano 2^>nul ^| findstr /i "8699.*LISTENING"') do (
    taskkill /PID %%a /F >nul 2>&1
)
for /f "tokens=5" %%a in ('netstat -ano 2^>nul ^| findstr /i "3457.*LISTENING"') do (
    taskkill /PID %%a /F >nul 2>&1
)
echo Done. Chrome (9223) kept for next launch; close it manually if needed.
ping -n 2 127.0.0.1 >nul
exit