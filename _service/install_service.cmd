@echo off
REM One-time elevated setup. Creates the auto-start Windows service for the
REM system monitoring app. Idempotent: safe to re-run. All output -> install_result.log
setlocal
set SVC=SysMonitor
for %%I in ("%~dp0..") do set "ROOT=%%~fI"
set NSSM=%ROOT%\_service\nssm.exe
if not defined SERVICE_PYTHON for /f "delims=" %%P in ('where python') do if not defined SERVICE_PYTHON set "SERVICE_PYTHON=%%P"
set "PY=%SERVICE_PYTHON%"
set LOG=%ROOT%\_service\install_result.log

(
  echo === install_service.cmd %DATE% %TIME% ===

  REM Remove any prior instance so this is repeatable
  "%NSSM%" stop %SVC%
  "%NSSM%" remove %SVC% confirm
  sc delete %SVC%

  REM Create the service
  "%NSSM%" install %SVC% "%PY%"
  "%NSSM%" set %SVC% AppParameters "-m uvicorn app.main:app --host 0.0.0.0 --port 8765"
  "%NSSM%" set %SVC% AppDirectory "%ROOT%"
  "%NSSM%" set %SVC% DisplayName "Dobby Console"
  "%NSSM%" set %SVC% Description "FastAPI system monitoring dashboard on port 8765, reachable over Tailscale."
  "%NSSM%" set %SVC% Start SERVICE_AUTO_START
  "%NSSM%" set %SVC% ObjectName LocalSystem
  REM LocalSystem has its own profile; point the app at the installing user's
  "%NSSM%" set %SVC% AppEnvironmentExtra USER_HOME=%USERPROFILE%
  call "%ROOT%\_service\grant_service_control.cmd"

  REM Resilience: auto-restart on crash
  "%NSSM%" set %SVC% AppExit Default Restart
  "%NSSM%" set %SVC% AppRestartDelay 3000
  "%NSSM%" set %SVC% AppThrottle 5000
  "%NSSM%" set %SVC% AppStopMethodConsole 5000

  REM Logging WITH rotation - never let logs grow unbounded
  "%NSSM%" set %SVC% AppStdout "%ROOT%\_service\service.out.log"
  "%NSSM%" set %SVC% AppStderr "%ROOT%\_service\service.err.log"
  "%NSSM%" set %SVC% AppRotateFiles 1
  "%NSSM%" set %SVC% AppRotateOnline 1
  "%NSSM%" set %SVC% AppRotateBytes 1048576

  REM Start it now
  "%NSSM%" start %SVC%
  timeout /t 3 /nobreak

  echo --- sc query ---
  sc query %SVC%
  echo --- sc qc ---
  sc qc %SVC%
  echo === done ===
) > "%LOG%" 2>&1

endlocal
