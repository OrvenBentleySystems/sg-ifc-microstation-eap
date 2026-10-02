@echo off
setlocal
title IFC+SG Checker - Uninstall

net session >nul 2>&1
if errorlevel 1 (
  echo.
  echo Uninstalling needs administrator rights.
  echo Right-click Uninstall.bat and choose Run as administrator.
  echo.
  pause
  exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\microstation\install_ifcsg_checker.ps1" -Uninstall
if errorlevel 1 (
  echo.
  echo Uninstall failed. Review the messages above.
  pause
  exit /b 1
)

echo.
echo IFC+SG Checker removed. Restart MicroStation.
echo.
pause
