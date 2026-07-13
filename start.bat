@echo off
setlocal
cd /d "%~dp0"
echo Starting project services with administrator permission...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\dev_services_admin.ps1" -Action restart
set EXIT_CODE=%ERRORLEVEL%
if not "%EXIT_CODE%"=="0" (
  pause
  exit /b %EXIT_CODE%
)
set "PYTHON_EXE=C:\Python312\python.exe"
if not exist "%PYTHON_EXE%" set "PYTHON_EXE=python"
echo Starting project services in normal user mode...
"%PYTHON_EXE%" scripts\dev_services.py start
set EXIT_CODE=%ERRORLEVEL%
if not "%EXIT_CODE%"=="0" pause
exit /b %EXIT_CODE%
