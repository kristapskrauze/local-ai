@echo off
setlocal EnableExtensions
rem Explicit elevated repair. This script never self-elevates; run it from an
rem Administrator Command Prompt or PowerShell window.

set "SVC=SysMonitor"
set "PORT=8765"

net session >nul 2>&1
if errorlevel 1 (
  echo ERROR: revive-admin.cmd must be run from an elevated shell.
  echo It will not launch UAC by itself.
  echo.
  echo Open Command Prompt or PowerShell as administrator, then run:
  echo   "%~f0"
  exit /b 5
)

call "%~dp0grant_service_control.cmd"
if errorlevel 1 exit /b %errorlevel%

call "%~dp0revive.cmd"
exit /b %errorlevel%
