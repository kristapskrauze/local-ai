@echo off
setlocal EnableExtensions
rem Safe wrapper. The real logic lives in revive.ps1 so cmd.exe cannot
rem mis-parse netstat/findstr patterns or labels.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0revive.ps1"
exit /b %ERRORLEVEL%
