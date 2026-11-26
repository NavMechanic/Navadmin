@echo off
REM Project Athena - Standalone Launcher for Windows

echo.
echo ===========================================
echo PROJECT ATHENA - STANDALONE LAUNCHER
echo ===========================================
echo.

REM Check Python
echo Checking Python...
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo Error: Python not found. Please install Python 3.11+
    pause
    exit /b 1
)

for /f "tokens=2" %%i in ('python --version 2^>^&1') do set PYTHON_VERSION=%%i
echo   Using Python %PYTHON_VERSION%
echo.

REM Install requirements
echo Installing dependencies...
pip install -q -r requirements-standalone.txt
echo.

REM Run application
echo Starting Project Athena...
echo.
python athena_standalone.py

pause
