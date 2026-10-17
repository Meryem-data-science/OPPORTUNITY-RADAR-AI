@echo off
setlocal
title Opportunity Radar AI - Connexion des alertes LinkedIn et Indeed
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\connect-linkedin-alerts.ps1"
set "taskExitCode=%errorlevel%"
echo.
if not "%taskExitCode%"=="0" echo La connexion a echoue. Consultez docs\connexion-linkedin.md.
pause
exit /b %taskExitCode%
