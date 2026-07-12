@echo off
setlocal
cd /d "%~dp0"
python scripts\dev_services.py stop
set EXIT_CODE=%ERRORLEVEL%
if not "%EXIT_CODE%"=="0" pause
exit /b %EXIT_CODE%
