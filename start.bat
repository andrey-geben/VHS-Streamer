@echo off
REM =====================================================================
REM  VHS-Streamer — запуск сервера
REM =====================================================================
setlocal EnableDelayedExpansion
cd /d "%~dp0"

echo.
echo ============================================
echo   VHS-Streamer — запуск
echo ============================================
echo.

REM --- Проверка: уже запущен? ---
if exist run.pid (
    set /p OLD_PID=<run.pid
    tasklist /FI "PID eq !OLD_PID!" 2>nul | find /I "!OLD_PID!" >nul
    if !errorlevel! == 0 (
        echo [!] Сервер уже запущен (PID=!OLD_PID!).
        echo     Сначала остановите: stop.bat
        echo.
        pause
        exit /b 1
    ) else (
        REM Старый PID-файл, процесс уже мёртв — удаляем
        del run.pid >nul 2>&1
    )
)

REM --- Проверка порта 8000 ---
netstat -ano | find ":8000 " | find "LISTENING" >nul
if %errorlevel% == 0 (
    echo [!] Порт 8000 занят другим процессом.
    echo     Возможно, сервер уже запущен в другом окне.
    echo.
    pause
    exit /b 1
)

REM --- Проверка venv ---
if not exist ".venv\Scripts\python.exe" (
    echo [!] Не найдено виртуальное окружение .venv
    echo     Создайте его командой:  python -m venv .venv
    echo     И установите зависимости:  .venv\Scripts\pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)

REM --- Запуск в новом окне ---
echo [*] Запускаю сервер...
echo     Python: .venv\Scripts\python.exe
echo     Лог:    logs\vhs-streamer.log
echo.

REM start "заголовок окна" /MIN cmd /c "команда"
REM Используем powershell для получения PID дочернего процесса.
REM Проще: запустить python через start /B и захватить $LASTEXITCODE — но так не получится PID.
REM Надёжный способ: запустить и записать PID через wmic.

start "VHS-Streamer" cmd /k ".venv\Scripts\python.exe app.py"

REM Ждём, пока python займёт порт 8000
set /a WAIT=0
:waitloop
timeout /t 1 /nobreak >nul
netstat -ano | find ":8000 " | find "LISTENING" >nul
if %errorlevel% == 0 goto found
set /a WAIT+=1
if !WAIT! LSS 20 goto waitloop
echo [!] Сервер не поднялся за 20 секунд. Проверьте лог.
pause
exit /b 1

:found
REM Найдём PID питона, слушающего 8000
for /f "tokens=5" %%a in ('netstat -ano ^| find ":8000 " ^| find "LISTENING"') do (
    set SERVER_PID=%%a
    goto :savepid
)

:savepid
echo !SERVER_PID!> run.pid
echo [OK] Сервер запущен. PID=!SERVER_PID!
echo      Web UI:  http://localhost:8000
echo      Логи:    logs\vhs-streamer.log
echo      Стоп:    stop.bat
echo.
endlocal
exit /b 0