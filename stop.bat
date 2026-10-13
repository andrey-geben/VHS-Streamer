@echo off
REM =====================================================================
REM  VHS-Streamer — остановка сервера
REM =====================================================================
setlocal EnableDelayedExpansion
cd /d "%~dp0"

echo.
echo ============================================
echo   VHS-Streamer — остановка
echo ============================================
echo.

set FOUND=0

REM --- По PID-файлу ---
if exist run.pid (
    set /p SERVER_PID=<run.pid
    echo [*] Читаю run.pid: PID=!SERVER_PID!
    tasklist /FI "PID eq !SERVER_PID!" 2>nul | find /I "!SERVER_PID!" >nul
    if !errorlevel! == 0 (
        echo [*] Останавливаю python (PID=!SERVER_PID!) с дочерними процессами...
        taskkill /PID !SERVER_PID! /T /F >nul 2>&1
        set FOUND=1
    ) else (
        echo [i] Процесс !SERVER_PID! уже не запущен.
    )
    del run.pid >nul 2>&1
) else (
    echo [i] run.pid не найден.
)

REM --- Подчищаем MediaMTX, если остался висеть ---
tasklist /FI "IMAGENAME eq mediamtx.exe" 2>nul | find /I "mediamtx.exe" >nul
if !errorlevel! == 0 (
    echo [*] Останавливаю оставшиеся MediaMTX...
    taskkill /IM mediamtx.exe /F >nul 2>&1
    set FOUND=1
)

REM --- Подчищаем осиротевшие ffmpeg (только наши, у которых rtsp в аргументах) ---
echo [*] Проверяю осиротевшие ffmpeg...
for /f "tokens=2 delims=," %%p in ('tasklist /FI "IMAGENAME eq ffmpeg.exe" /FO CSV /NH 2^>nul') do (
    set "FPID=%%~p"
    REM Убиваем все ffmpeg — наши loopback-публикаторы, других быть не должно
    taskkill /PID !FPID! /F >nul 2>&1
)

if !FOUND! == 1 (
    echo.
    echo [OK] Сервер остановлен.
) else (
    echo.
    echo [i] Нечего останавливать — сервер не запущен.
)
echo.
endlocal
exit /b 0