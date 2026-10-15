# -*- coding: utf-8 -*-
"""Restart THIS service (SysMonitor), from one place."""
import logging
import os
import subprocess
from datetime import datetime, timedelta

LOGGER = logging.getLogger(__name__)

_CREATE_NO_WINDOW = 0x08000000
_NSSM = os.path.join(os.path.dirname(__file__), os.pardir, "_service", "nssm.exe")
TASK_NAME = "SysMonitorSelfRestart"


def _run(args, timeout=25):
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                           encoding="utf-8", errors="replace",
                           creationflags=_CREATE_NO_WINDOW)
        return r.returncode == 0, ((r.stdout or "") + (r.stderr or "")).strip()
    except FileNotFoundError as exc:
        return False, "not found: %s" % exc
    except subprocess.TimeoutExpired:
        return False, "timed out"
    except Exception as exc:
        return False, str(exc)


def request_self_restart(reason=""):
    """Schedule the restart. Returns (ok, detail).

    A detached child cannot do this itself: NSSM kills the service's whole
    process tree on stop, so any helper spawned from here dies before it can
    issue the start. Task Scheduler runs outside our process tree, so it can.
    """
    nssm = os.path.abspath(_NSSM)
    if not os.path.isfile(nssm):
        return False, "nssm.exe not found at %s" % nssm
    helper = os.path.join(os.path.dirname(nssm), "self_restart.cmd")
    if not os.path.isfile(helper):
        return False, "restart helper not found at %s" % helper
    run_at = (datetime.now() + timedelta(minutes=1)).strftime("%H:%M")
    ok, out = _run([
        "schtasks", "/create", "/f", "/tn", TASK_NAME,
        "/ru", "SYSTEM", "/rl", "HIGHEST", "/sc", "once", "/st", run_at,
        "/tr", 'cmd.exe /d /c "%s"' % helper,
    ], timeout=20)
    if not ok:
        return False, "could not create restart task: %s" % out
    ok, out = _run(["schtasks", "/run", "/tn", TASK_NAME], timeout=20)
    if not ok:
        return False, "could not run restart task: %s" % out
    LOGGER.info("self-restart scheduled (%s)", str(reason)[:120])
    return True, ("SysMonitor restart task is running; it will verify "
                  "/api/health before finishing.")
