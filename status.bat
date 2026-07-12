@echo off
setlocal
cd /d "%~dp0"
python scripts\dev_services.py status
exit /b %ERRORLEVEL%
