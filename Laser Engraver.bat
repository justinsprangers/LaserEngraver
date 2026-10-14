@echo off
rem Double-click to open the Laser Engraver app.
cd /d "%~dp0"
python -c "import PIL, cv2, numpy" 2>nul
if errorlevel 1 (
    echo Some required Python packages are missing. Installing them now...
    python -m pip install -r requirements.txt
    if errorlevel 1 (
        echo.
        echo Install failed. Make sure Python 3.8+ is installed and on PATH.
        pause
        exit /b 1
    )
)
start "" pythonw engrave_app.py
