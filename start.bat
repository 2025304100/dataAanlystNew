@echo off
chcp 65001 >nul 2>&1
echo ========================================
echo   Quant Workbench - 启动脚本
echo ========================================
echo.

cd /d "%~dp0"

echo [1/2] 启动后端服务 (FastAPI) ...
start "FastAPI-Server" cmd /k "python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload"

echo [2/2] 等待服务就绪...
timeout /t 3 /nobreak >nul

echo.
echo 服务已启动！
echo 前端地址: http://localhost:8000
echo API文档: http://localhost:8000/docs
echo.
echo 按 Ctrl+C 停止服务
echo.

:loop
timeout /t 60 /nobreak >nul
goto loop
