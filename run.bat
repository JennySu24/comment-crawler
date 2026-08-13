@echo off
cd /d "%~dp0"

REM Check if venv exists
if not exist "venv\Scripts\python.exe" (
    echo [1/3] Creating virtual environment...
    python -m venv venv
    if errorlevel 1 (
        echo ERROR: Cannot create venv. Please install Python first.
        pause
        exit /b 1
    )
)

echo [1/3] Installing dependencies...
venv\Scripts\python.exe -m pip install -r requirements.txt -q
if errorlevel 1 (
    echo ERROR: Failed to install dependencies.
    pause
    exit /b 1
)

echo [2/3] Installing Chromium browser...
venv\Scripts\python.exe -m playwright install chromium
if errorlevel 1 (
    echo WARNING: Chromium install failed. Some platforms may not work.
)

echo [3/3] Starting server...
echo.
echo Server started! Open: http://127.0.0.1:8765
echo.
start http://127.0.0.1:8765
venv\Scripts\python.exe app.py

pause
