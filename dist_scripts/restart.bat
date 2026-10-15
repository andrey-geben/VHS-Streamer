@echo off
cd /d "%~dp0"
call stop.bat
timeout /t 2 /nobreak >nul
call start.bat