@echo off
chcp 65001 >nul
REM =====================================================================
REM  VHS-Streamer - сброс входа на дефолтный admin / admin
REM =====================================================================
setlocal
cd /d "%~dp0"

if not exist "VHS-Streamer.exe" (
    echo [!] Файл VHS-Streamer.exe не найден рядом с этим скриптом.
    pause
    exit /b 1
)

echo.
echo ============================================
echo   Сброс пароля на admin / admin
echo ============================================
echo.
echo [!] Сервер должен быть остановлен, иначе изменение не подхватится.
echo.
pause

VHS-Streamer.exe --reset-auth

echo.
echo Готово. Запускайте VHS-Streamer.exe и входите: admin / admin
echo.
pause
endlocal
