# -*- coding: utf-8 -*-
"""Start a sidecar daemon only when none is there.

A launch now needs both: the port is free -- a plain bind succeeds, which
opens no connection, so a test that blocks the network cannot fake it -- and
no process is already running that script, which covers a copy that is still
loading its model and has not bound the port yet.
"""
import os
import socket
import subprocess

# DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
DETACHED = 0x08000208


def port_taken(port, host="127.0.0.1"):
    """Something already holds host:port. Measured on the rig: a plain bind
    fails on every port a daemon listens on and succeeds on a free one."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind((host, int(port)))
        return False
    except OSError:
        return True
    finally:
        probe.close()


def _tail(path):
    """`folder\\file` of a path, case-folded: the daemons are all daemon.py in
    their own folder, and a copy started by hand may carry a relative path."""
    parts = os.path.normcase(os.path.normpath(str(path))).split(os.sep)
    return os.sep.join(parts[-2:])


def script_running(script):
    """A live process has `script` on its command line. A process whose
    command line cannot be read (another user's) is not counted; the port
    check covers that one once it is serving."""
    try:
        import psutil
    except Exception:
        return False
    want = _tail(script)
    for proc in psutil.process_iter(["cmdline"]):
        try:
            cmd = proc.info.get("cmdline") or []
        except Exception:
            continue
        if any(_tail(part) == want for part in cmd if str(part).lower().endswith(".py")):
            return True
    return False


def start_once(python, script, port):
    """Launch `python script` detached unless its port is held or a copy is
    already running. True only when a new process was started."""
    if port_taken(port) or script_running(script):
        return False
    subprocess.Popen([python, script], creationflags=DETACHED,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     close_fds=True)
    return True
