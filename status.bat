@echo off
REM =====================================================================
REM  VHS-Streamer — статус
REM =====================================================================
setlocal EnableDelayedExpansion
cd /d "%~dp0"

echo.
echo ============================================
echo   VHS-Streamer — статус
echo ============================================
echo.

REM --- PID-файл ---
if exist run.pid (
    set /p SERVER_PID=<run.pid
    tasklist /FI "PID eq !SERVER_PID!" 2>nul | find /I "!SERVER_PID!" >nul
    if !errorlevel! == 0 (
        echo Python:    RUNNING ^(PID=!SERVER_PID!^)
    ) else (
        echo Python:    STOPPED ^(stale run.pid=!SERVER_PID!^)
    )
) else (
    echo Python:    STOPPED ^(no run.pid^)
)

REM --- MediaMTX ---
tasklist /FI "IMAGENAME eq mediamtx.exe" 2>nul | find /I "mediamtx.exe" >nul
if !errorlevel! == 0 (
    for /f "tokens=2 delims=," %%p in ('tasklist /FI "IMAGENAME eq mediamtx.exe" /FO CSV /NH 2^>nul') do (
        echo MediaMTX:  RUNNING ^(PID=%%~p^)
    )
) else (
    echo MediaMTX:  STOPPED
)

REM --- Порт 8000 ---
netstat -ano | find ":8000 " | find "LISTENING" >nul
if !errorlevel! == 0 (
    for /f "tokens=5" %%a in ('netstat -ano ^| find ":8000 " ^| find "LISTENING"') do (
        echo Порт 8000: LISTENING ^(PID=%%a^)
        goto :p8000done
    )
) else (
    echo Порт 8000: closed
)
:p8000done

REM --- Порт 8554 ---
netstat -ano | find ":8554 " | find "LISTENING" >nul
if !errorlevel! == 0 (
    for /f "tokens=5" %%a in ('netstat -ano ^| find ":8554 " ^| find "LISTENING"') do (
        echo Порт 8554: LISTENING ^(PID=%%a^)
        goto :p8554done
    )
) else (
    echo Порт 8554: closed
)
:p8554done

REM --- Порт 9997 ---
netstat -ano | find ":9997 " | find "LISTENING" >nul
if !errorlevel! == 0 (
    for /f "tokens=5" %%a in ('netstat -ano ^| find ":9997 " ^| find "LISTENING"') do (
        echo Порт 9997: LISTENING ^(PID=%%a^)
        goto :p9997done
    )
) else (
    echo Порт 9997: closed
)
:p9997done

echo.
endlocal
exit /b 0