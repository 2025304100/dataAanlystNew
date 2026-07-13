@echo off
setlocal
cd /d "%~dp0"
set "PYTHON_EXE=C:\Python312\python.exe"
if not exist "%PYTHON_EXE%" set "PYTHON_EXE=python"
"%PYTHON_EXE%" scripts\dev_services.py status
exit /b %ERRORLEVEL%
