@echo off
title Kaspi Donation Launcher
cd /d "%~dp0"

start "Kaspi Donate Server" cmd /k py server.py
timeout /t 3 /nobreak > nul
start "Cloudflared Tunnel" cmd /k cloudflared tunnel --url http://127.0.0.1:5000

echo.
echo Started from: %~dp0
pause