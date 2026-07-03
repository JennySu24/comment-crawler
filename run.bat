@echo off
chcp 65001 >nul
title 评论区爬取工具
echo ============================================
echo          评论区爬取工具 v1.0
echo    支持 B站 / 小红书 / 抖音 评论区爬取
echo ============================================
echo.

cd /d "%~dp0"

REM Check if venv exists
if not exist "venv\Scripts\python.exe" (
    echo [1/3] 正在创建虚拟环境...
    python -m venv venv
    if errorlevel 1 (
        echo 错误: 无法创建虚拟环境，请确认 Python 已安装。
        pause
        exit /b 1
    )
)

echo [1/3] 正在安装依赖...
venv\Scripts\python.exe -m pip install -r requirements.txt -q
if errorlevel 1 (
    echo 错误: 依赖安装失败。
    pause
    exit /b 1
)

echo [2/3] 正在检查浏览器...
venv\Scripts\python.exe -m playwright install chromium
if errorlevel 1 (
    echo 警告: 浏览器安装失败，部分平台可能无法使用。
)

echo [3/3] 正在启动服务...
echo.
echo 服务启动后，请在浏览器中打开: http://127.0.0.1:8765
echo.
start http://127.0.0.1:8765
venv\Scripts\python.exe app.py

pause
