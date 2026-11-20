@echo off
title Build Ha-Gemi Executable
cd /d "%~dp0"

echo ===================================================
echo   Building Ha-Gemi Desktop Pet Executable (.exe)
echo ===================================================

set "PY_EXE=python"
if exist ".venv\Scripts\python.exe" (
    set "PY_EXE=.venv\Scripts\python.exe"
) else if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" (
    set "PY_EXE=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
)

echo [INFO] Using compiler: %PY_EXE%
"%PY_EXE%" -m PyInstaller HaGemi.spec --noconfirm --distpath dist --workpath build
if errorlevel 1 (
    echo.
    echo [ERROR] PyInstaller build failed!
    pause
    exit /b 1
)

xcopy /E /I /Y "assets" "dist\Ha-Gemi\assets" >nul
copy /Y "config.json" "dist\Ha-Gemi\config.json" >nul
echo.
echo [SUCCESS] Build finished! Executable is at dist\Ha-Gemi\Ha-Gemi.exe
pause
