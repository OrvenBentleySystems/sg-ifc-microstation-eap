@echo off
setlocal
title IFC+SG Checker - Deploy

net session >nul 2>&1
if errorlevel 1 (
  echo.
  echo This installer needs administrator rights.
  echo Right-click Deploy.bat and choose Run as administrator.
  echo.
  pause
  exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\microstation\install_ifcsg_checker.ps1"
if errorlevel 1 (
  echo.
  echo Installation failed. Review the messages above.
  pause
  exit /b 1
)

echo.
echo Installation complete. Restart MicroStation.
echo Key-in: python load $(IFCSG_CHECKER_LAUNCHER)
echo.
pause
