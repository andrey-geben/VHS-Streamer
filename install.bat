@echo off
REM =====================================================================
REM  VHS-Streamer — установка на новую машину
REM =====================================================================
chcp 65001 >nul
setlocal EnableDelayedExpansion
cd /d "%~dp0"

echo.
echo ============================================
echo   VHS-Streamer — установка
echo ============================================
echo.

REM ---------------------------------------------------------------
REM 1. Проверка Python
REM ---------------------------------------------------------------
echo [1/5] Проверяю Python...

where python >nul 2>&1
if errorlevel 1 (
    echo.
    echo [!] Python не найден в PATH.
    echo.
    echo     Скачайте Python 3.12+ с https://www.python.org/downloads/
    echo     При установке отметьте галочку "Add python.exe to PATH".
    echo.
    pause
    exit /b 1
)

REM Версия Python
for /f "tokens=2" %%v in ('python --version 2^>^&1') do set PYVER=%%v
echo     Найден Python !PYVER!

REM Простая проверка: 3.12 и выше
for /f "tokens=1,2 delims=." %%a in ("!PYVER!") do (
    set PYMAJ=%%a
    set PYMIN=%%b
)
if !PYMAJ! LSS 3 (
    echo [!] Требуется Python 3.12 или новее. Обнаружено: !PYVER!
    pause
    exit /b 1
)
if !PYMAJ! EQU 3 if !PYMIN! LSS 12 (
    echo [!] Требуется Python 3.12 или новее. Обнаружено: !PYVER!
    pause
    exit /b 1
)
echo     Версия подходит.

REM ---------------------------------------------------------------
REM 2. Виртуальное окружение
REM ---------------------------------------------------------------
echo.
echo [2/5] Виртуальное окружение .venv...

if exist ".venv\Scripts\python.exe" (
    echo     Уже существует — пропускаю создание.
) else (
    echo     Создаю .venv...
    python -m venv .venv
    if errorlevel 1 (
        echo [!] Не удалось создать виртуальное окружение.
        pause
        exit /b 1
    )
    echo     Готово.
)

REM Активируем venv на время скрипта
set VENV_PY=.venv\Scripts\python.exe
set VENV_PIP=.venv\Scripts\pip.exe

if not exist "!VENV_PY!" (
    echo [!] Не найден !VENV_PY!. Что-то пошло не так.
    pause
    exit /b 1
)

REM ---------------------------------------------------------------
REM 3. Зависимости
REM ---------------------------------------------------------------
echo.
echo [3/5] Устанавливаю зависимости...

if not exist "requirements.txt" (
    echo [!] Файл requirements.txt не найден.
    pause
    exit /b 1
)

"!VENV_PY!" -m pip install --upgrade pip >nul 2>&1
"!VENV_PY!" -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo [!] Ошибка установки зависимостей.
    echo     Проверьте интернет и повторите: install.bat
    pause
    exit /b 1
)
echo     Зависимости установлены.

REM ---------------------------------------------------------------
REM 4. Конфиг
REM ---------------------------------------------------------------
echo.
echo [4/5] Конфигурация config.yaml...

if exist "config.yaml" (
    echo     config.yaml уже существует — не трогаю.
    echo     Если нужно начать заново, удалите его и запустите install.bat снова.
) else (
    if exist "config.example.yaml" (
        copy /Y config.example.yaml config.yaml >nul
        echo     Создан config.yaml из config.example.yaml.
        echo.
        echo     ВАЖНО: откройте config.yaml и задайте:
        echo        - server.secret_key     (длинная случайная строка)
        echo        - security.users        (логины и пароли)
        echo        - security.api_token    (токен для API)
    ) else (
        echo [!] Не найден config.example.yaml.
        pause
        exit /b 1
    )
)

REM ---------------------------------------------------------------
REM 5. Проверка структуры
REM ---------------------------------------------------------------
echo.
echo [5/5] Проверяю структуру проекта...

set MISSING=0
if not exist "templates"      ( echo     [!] нет папки templates\      & set MISSING=1 )
if not exist "static"         ( echo     [!] нет папки static\         & set MISSING=1 )
if not exist "mediamtx.yml"   ( echo     [!] нет mediamtx.yml          & set MISSING=1 )
if not exist "icon.ico"       ( echo     [!] нет icon.ico              & set MISSING=1 )
if not exist "app.py"         ( echo     [!] нет app.py                & set MISSING=1 )

if !MISSING! == 1 (
    echo.
    echo [!] Некоторые файлы отсутствуют. Возможно, репозиторий склонирован не полностью.
    pause
    exit /b 1
)
echo     Структура в порядке.

REM ---------------------------------------------------------------
REM Готово
REM ---------------------------------------------------------------
echo.
echo ============================================
echo   Установка завершена
echo ============================================
echo.
echo   Виртуальное окружение:  .venv\
echo   Зависимости:            установлены
echo   Конфиг:                 config.yaml
echo   MediaMTX и FFmpeg:      скачаются автоматически при первом запуске
echo.
echo   Запуск:  start.bat
echo   Стоп:    stop.bat
echo.

set /p ANSWER="Запустить сервер сейчас? (y/n): "
if /I "!ANSWER!" == "y" (
    echo.
    echo [*] Запускаю start.bat...
    timeout /t 1 /nobreak >nul
    call start.bat
) else (
    echo.
    echo Хорошо. Когда будете готовы — запустите start.bat
    echo.
    pause
)

endlocal
exit /b 0