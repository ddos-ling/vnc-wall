@echo off
setlocal
cd /d "%~dp0"
echo Starting VNC Wall...
py -3 main.py 2>nul
if errorlevel 1 python main.py
if errorlevel 1 (
    echo.
    echo Failed to start. Make sure Python 3.12+ and PySide6 are installed.
    echo   pip install -r requirements.txt
    pause
)
endlocal
