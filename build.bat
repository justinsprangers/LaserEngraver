@echo off
rem Builds installer\LaserEngraver-Setup-<version>.exe from the current source.
rem Bump VERSION in engrave_core.py first if you want the installer to show a new number.
cd /d "%~dp0"
python packaging\build.py
if errorlevel 1 (
    echo.
    echo BUILD FAILED - see the messages above.
)
pause
