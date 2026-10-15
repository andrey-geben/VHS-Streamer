@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"
echo Останавливаю VHS-Streamer...

if exist run.pid (
    set /p PID=<run.pid
    taskkill /PID !PID! /T /F >nul 2>&1
    del run.pid >nul 2>&1
)

taskkill /IM VHS-Streamer.exe /F >nul 2>&1
taskkill /IM mediamtx.exe /F >nul 2>&1
taskkill /IM ffmpeg.exe /F >nul 2>&1

echo [OK] Остановлено.
timeout /t 2 /nobreak >nul
endlocal
exit /b 0