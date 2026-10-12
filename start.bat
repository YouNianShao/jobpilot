@echo off
rem Keep this file ASCII-only: cmd decodes .bat with the OEM code page, and
rem CJK comments get mis-decoded into broken commands (that is exactly how the
rem desktop shortcut silently stopped working). All logic lives in launch.ps1,
rem which is UTF-8 with BOM.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\launch.ps1"
exit /b %ERRORLEVEL%