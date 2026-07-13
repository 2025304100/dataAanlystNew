@echo off
setlocal
cd /d "%~dp0"
echo Stopping project services with administrator permission...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\dev_services_admin.ps1" -Action stop
set EXIT_CODE=%ERRORLEVEL%
if not "%EXIT_CODE%"=="0" pause
exit /b %EXIT_CODE%
