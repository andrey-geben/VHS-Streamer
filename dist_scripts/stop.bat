@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion
cd /d "%~dp0"
echo Останавливаю VHS-Streamer...

REM По PID из run.pid — вместе со всеми дочерними процессами (/T):
REM так гасятся и ffmpeg-публикаторы, запущенные MediaMTX.
if exist run.pid (
    set /p PID=<run.pid
    taskkill /PID !PID! /T /F >nul 2>&1
    del run.pid >nul 2>&1
)

REM Затем по имени: само приложение и MediaMTX с потомками
taskkill /IM VHS-Streamer.exe /T /F >nul 2>&1
taskkill /IM mediamtx.exe /T /F >nul 2>&1

echo [OK] Остановлено.
timeout /t 2 /nobreak >nul
endlocal
exit /b 0
