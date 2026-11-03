@echo off
powershell.exe -NoProfile -STA -ExecutionPolicy Bypass -File "%~dp0Convert-ToUtf8.ps1"
echo.
pause
