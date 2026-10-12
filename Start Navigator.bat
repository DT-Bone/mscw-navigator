@echo off
rem Starts the MapleStory Classic World navigator as administrator.
rem MapleStory runs as administrator, and Windows hides mouse clicks from programs that don't
rem (right-clicking an item to open its info panel needs to see them). Windows asks for permission.
net session >nul 2>&1
if %errorlevel% neq 0 (
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)
cd /d "%~dp0"
title MSCW Navigator
py -3.11 -u nav_live.py
pause
