@echo off
title Gemini Desktop Pet - Ha-Gemi
cd /d "%~dp0"

set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

echo ===================================================
echo   Starting Gemini Desktop Pet (Ha-Gemi)
echo   Hotkeys:
echo     Ctrl+F1  : Start / stop voice input
echo     Ctrl+F9  : Instant Screen Critique
echo     Ctrl+F10 : Toggle Voice (Mute/Unmute)
echo     Ctrl+F11 : Cycle Avatar / Outfits
echo     Mouse Wheel / Middle Click on Pet to switch outfit!
echo ===================================================

if exist ".venv\Scripts\python.exe" goto USE_VENV
if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" goto USE_LOCAL
python main.py
goto END

:USE_VENV
echo [INFO] Starting pet in virtual environment (.venv)...
".venv\Scripts\python.exe" main.py
goto END

:USE_LOCAL
echo [INFO] Starting pet with Python 3.11...
"%LOCALAPPDATA%\Programs\Python\Python311\python.exe" main.py
goto END

:END
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [ERROR] Process exited with code: %ERRORLEVEL%
    pause
)
