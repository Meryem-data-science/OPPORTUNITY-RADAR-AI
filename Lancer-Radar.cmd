@echo off
title Opportunity Radar AI
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\open-radar.ps1"
if errorlevel 1 (
    echo.
    echo Le lancement a echoue. Consultez les messages et les journaux dans data\runtime.
    pause
    exit /b 1
)
exit /b 0
