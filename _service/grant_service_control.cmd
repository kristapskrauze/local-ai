@echo off
setlocal EnableExtensions
rem Grants interactive desktop users enough rights to start/stop/restart the
rem SysMonitor service. Run once from an elevated shell.

set "SVC=SysMonitor"
set "SVC_SDDL=D:(A;;CCLCSWRPWPDTLOCRRC;;;SY)(A;;CCDCLCSWRPWPDTLOCRSDRCWDWO;;;BA)(A;;CCLCSWRPWPDTLOCRRC;;;IU)(A;;CCLCSWRPWPDTLOCRRC;;;SU)"

net session >nul 2>&1
if errorlevel 1 (
  echo ERROR: grant_service_control.cmd must be run from an elevated shell.
  echo Open Command Prompt or PowerShell as administrator, then run:
  echo   "%~f0"
  exit /b 5
)

echo Granting interactive users start/stop rights on %SVC%...
sc.exe sdset %SVC% %SVC_SDDL%
if errorlevel 1 (
  echo ERROR: failed to update the %SVC% service security descriptor.
  exit /b 6
)

echo OK: %SVC% can now be revived from the normal desktop session.
exit /b 0
