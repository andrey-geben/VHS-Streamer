@echo off
REM =====================================================================
REM  VHS-Streamer — сборка .exe для Windows
REM =====================================================================
setlocal
cd /d "%~dp0"

echo.
echo ============================================
echo   VHS-Streamer — сборка .exe
echo ============================================
echo.

REM --- Проверка venv ---
if not exist ".venv\Scripts\python.exe" (
    echo [!] Не найдено .venv. Создайте:  python -m venv .venv
    pause
    exit /b 1
)

echo [*] Проверяю PyInstaller...
.venv\Scripts\python.exe -c "import PyInstaller" 2>nul
if errorlevel 1 (
    echo [*] PyInstaller не установлен — устанавливаю...
    .venv\Scripts\pip.exe install pyinstaller
)

echo [*] Очищаю предыдущую сборку...
if exist build rmdir /S /Q build
if exist dist rmdir /S /Q dist

echo [*] Собираю...
.venv\Scripts\python.exe -m PyInstaller app.spec --noconfirm

if errorlevel 1 (
    echo.
    echo [!] Сборка упала. Смотрите ошибки выше.
    pause
    exit /b 1
)

echo.
echo [*] Копирую внешние файлы рядом с .exe...

set OUTDIR=dist\VHS-Streamer

REM Шаблоны оставляем внутри .exe, но копируем и наружу —
REM так их можно править без пересборки
xcopy /E /I /Y templates "%OUTDIR%\templates" >nul

REM Конфиг — всегда внешний
if exist config.example.yaml (
    copy /Y config.example.yaml "%OUTDIR%\config.example.yaml" >nul
)
if exist config.yaml (
    copy /Y config.yaml "%OUTDIR%\config.yaml" >nul
) else (
    echo [i] config.yaml не найден — копирую из config.example.yaml
    copy /Y config.example.yaml "%OUTDIR%\config.yaml" >nul
)

REM MediaMTX-конфиг — внешний
if exist mediamtx.yml (
    copy /Y mediamtx.yml "%OUTDIR%\mediamtx.yml" >nul
)

REM bin/ — если уже скачан, копируем
if exist bin (
    xcopy /E /I /Y bin "%OUTDIR%\bin" >nul
) else (
    echo [i] bin\ не найден. MediaMTX и FFmpeg скачаются при первом запуске.
)

REM Скрипты запуска — из dist_scripts/ (версия для .exe)
copy /Y dist_scripts\start.bat   "%OUTDIR%\start.bat"   >nul 2>&1
copy /Y dist_scripts\stop.bat    "%OUTDIR%\stop.bat"    >nul 2>&1
copy /Y dist_scripts\status.bat  "%OUTDIR%\status.bat"  >nul 2>&1
copy /Y dist_scripts\restart.bat "%OUTDIR%\restart.bat" >nul 2>&1

echo.
echo ============================================
echo   Готово!
echo ============================================
echo.
echo Папка:     %OUTDIR%
echo Запуск:    %OUTDIR%\VHS-Streamer.exe
echo.
echo Для раздачи конечным пользователям:
echo   - Скопируйте всю папку "%OUTDIR%" на целевую машину
echo   - Установите Python НЕ требуется
echo   - Первый запуск скачает MediaMTX и FFmpeg в bin\
echo.
endlocal
pause