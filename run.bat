@echo off
REM Double-click this file to start Shorts Factory.
REM Bypasses PowerShell ExecutionPolicy.
chcp 65001 >nul
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0run.ps1"
echo.
echo ============================================================
echo  If you see "READY: http://localhost:8505" - open browser.
echo  You can close this window, Streamlit will keep running.
echo ============================================================
pause
