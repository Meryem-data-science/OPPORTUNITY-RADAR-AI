@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\backup-github.ps1"
if errorlevel 1 (
  echo.
  echo La sauvegarde GitHub n'a pas termine. Lire le message ci-dessus.
)
pause
