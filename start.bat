@echo off
title Radar de Empleo + Cloudflare Tunnel
echo ========================================================
echo   Iniciando Radar de Empleo (jobs.miguxld.site)
echo ========================================================
echo.
start "Uvicorn Server" cmd /k ".\venv\Scripts\uvicorn.exe server:app --host 0.0.0.0 --port 8000"
timeout /t 2 /nobreak >nul
start "Cloudflare Tunnel" cmd /k "cloudflared tunnel run job-agent"
echo.
echo [OK] Servicios iniciados:
echo - Local:  http://localhost:8000
echo - Remoto: https://jobs.miguxld.site
echo.
pause
