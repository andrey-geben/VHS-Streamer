@echo off
cd /d "%~dp0"
echo.
echo ============================================
echo   VHS-Streamer — статус
echo ============================================
echo.
if exist run.pid (
    set /p PID=<run.pid
    echo VHS-Streamer.exe: PID=!PID!
) else (
    echo VHS-Streamer.exe: не запущен
)
netstat -ano | find ":8000 " | find "LISTENING" >nul && echo Порт 8000: LISTENING || echo Порт 8000: closed
netstat -ano | find ":8554 " | find "LISTENING" >nul && echo Порт 8554: LISTENING || echo Порт 8554: closed
netstat -ano | find ":8889 " | find "LISTENING" >nul && echo Порт 8889: LISTENING || echo Порт 8889: closed
echo.
pause