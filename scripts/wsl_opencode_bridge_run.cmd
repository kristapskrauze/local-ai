@echo off
REM Keeps the user-session WSL OpenCode bridge alive for the dashboard's Code tab.
REM The service runs as LocalSystem and cannot use the per-user WSL distro, so this
REM loop must run in the user's interactive session (a logon scheduled task).
REM Restarts the bridge if it ever exits.
if not defined PYTHONW set "PYTHONW=pythonw.exe"
set "SCRIPT=%~dp0wsl_opencode_bridge.py"
:loop
"%PYTHONW%" "%SCRIPT%"
timeout /t 5 /nobreak >nul
goto loop
