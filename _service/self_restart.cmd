@echo off
setlocal EnableExtensions

rem Called by the SysMonitorSelfRestart scheduled task (created by the
rem dashboard's Restart-service button). Runs outside the service's process
rem tree so NSSM's stop can't kill it mid-restart.

set "SVC=SysMonitor"
set "PORT=8765"
set "NSSM=%~dp0nssm.exe"
set "LOG=%~dp0self_restart.log"
set "POWERSHELL=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"

if /I "%~1"=="/?" (
  echo Usage: self_restart.cmd
  echo Stops, starts, and health-checks the SysMonitor service.
  exit /b 0
)
if /I "%~1"=="--check" (
  echo self_restart.cmd syntax check OK
  exit /b 0
)

call :log === self_restart starting ===

if not exist "%NSSM%" (
  call :log ERROR: nssm.exe not found at "%NSSM%"
  exit /b 2
)

call :log Stopping %SVC% if it is running.
"%NSSM%" stop "%SVC%" >>"%LOG%" 2>&1
call :wait_stopped 25
if errorlevel 1 (
  call :log Service did not report STOPPED quickly; continuing with start attempt.
)

call :log Starting %SVC%.
"%NSSM%" start "%SVC%" >>"%LOG%" 2>&1
call :wait_health 45
if not errorlevel 1 goto healthy

call :log Health check did not pass after start; trying one NSSM restart.
"%NSSM%" restart "%SVC%" >>"%LOG%" 2>&1
call :wait_health 45
if not errorlevel 1 goto healthy

call :log ERROR: %SVC% did not become healthy on port %PORT%.
sc.exe query "%SVC%" >>"%LOG%" 2>&1
exit /b 7

:healthy
call :log %SVC% is healthy on http://127.0.0.1:%PORT%/api/health.
schtasks /delete /tn "SysMonitorSelfRestart" /f >nul 2>&1
if errorlevel 1 (
  call :log Cleanup note: could not delete SysMonitorSelfRestart task from this account; the next restart run overwrites it.
)
exit /b 0

:wait_stopped
for /L %%I in (1,1,%~1) do (
  sc.exe query "%SVC%" | find /I "STOPPED" >nul
  if not errorlevel 1 exit /b 0
  call :sleep_one
)
exit /b 1

:wait_health
for /L %%I in (1,1,%~1) do (
  "%POWERSHELL%" -NoProfile -ExecutionPolicy Bypass -Command "try { $h = Invoke-RestMethod -TimeoutSec 2 'http://127.0.0.1:%PORT%/api/health'; if ($h.ok -and $h.elevated) { exit 0 } } catch {}; exit 1" >nul 2>&1
  if not errorlevel 1 exit /b 0
  call :sleep_one
)
exit /b 1

:sleep_one
"%POWERSHELL%" -NoProfile -ExecutionPolicy Bypass -Command "Start-Sleep -Seconds 1" >nul 2>&1
exit /b 0

:log
>>"%LOG%" echo [%date% %time%] %*
exit /b 0
