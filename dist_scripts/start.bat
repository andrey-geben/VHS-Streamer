@echo off
REM VHS-Streamer — запуск (для собранного .exe)
setlocal EnableDelayedExpansion
cd /d "%~dp0"

if exist run.pid (
    set /p OLD_PID=<run.pid
    tasklist /FI "PID eq !OLD_PID!" 2>nul | find /I "!OLD_PID!" >nul
    if !errorlevel! == 0 (
        echo [!] Сервер уже запущен ^(PID=!OLD_PID!^).
        pause
        exit /b 1
    )
    del run.pid >nul 2>&1
)

if not exist "VHS-Streamer.exe" (
    echo [!] Не найден VHS-Streamer.exe в текущей папке.
    pause
    exit /b 1
)

netstat -ano | find ":8000 " | find "LISTENING" >nul
if %errorlevel% == 0 (
    echo [!] Порт 8000 занят другим процессом.
    pause
    exit /b 1
)

echo [*] Запускаю VHS-Streamer...
start "VHS-Streamer" cmd /k VHS-Streamer.exe

set /a WAIT=0
:waitloop
timeout /t 1 /nobreak >nul
netstat -ano | find ":8000 " | find "LISTENING" >nul
if %errorlevel% == 0 goto found
set /a WAIT+=1
if !WAIT! LSS 20 goto waitloop
echo [!] Сервер не поднялся за 20 секунд.
pause
exit /b 1

:found
for /f "tokens=5" %%a in ('netstat -ano ^| find ":8000 " ^| find "LISTENING"') do (
    set SERVER_PID=%%a
    goto :savepid
)
:savepid
echo !SERVER_PID!> run.pid
echo [OK] Сервер запущен. PID=!SERVER_PID!
echo      Web UI:  http://localhost:8000
timeout /t 3 /nobreak >nul
endlocal
exit /b 0