"""Dashboard command runner — run shell commands and long-running processes
(e.g. dev servers) on the rig from the browser, so the operator never has to
SSH into the box.

Two modes:
  * stream  — run a command, stream stdout/stderr, hard timeout. Good for
              builds, tests, one-shot commands, "did it work" checks.
  * start   — launch a DETACHED process (survives the request) and track it in
              a registry. Good for `python -m uvicorn ...`, `npm run dev`, etc.
              Output is captured to a per-process log; list/tail/stop manage it.

Security model: this is an RCE surface and the app has no auth, so it is only
reachable on the operator's Tailscale/LAN. Every call must pass confirm=true,
and the working directory must be inside an allow-listed root (the coding
project roots + the dobby home). Commands run in the service account; the env
is augmented with the dobby Python/Node/npm paths so `python`, `node`,
`opencode` resolve.
"""
import os
import re
import signal
import subprocess
import threading
import time
import uuid

from app import coder
from app import paths

DOBBY_HOME = paths.USER_HOME
LOG_DIR = os.path.join(coder.PROJECT_ROOT, ".terminal_logs")

_CREATE_NO_WINDOW = 0x08000000
_DETACHED_PROCESS = 0x00000008
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")

STREAM_TIMEOUT_DEFAULT = 120
STREAM_TIMEOUT_MAX = 1800

# id -> metadata for detached processes we launched.
_PROCS = {}
_LOCK = threading.Lock()


def _exec_env():
    env = dict(os.environ)
    env["USERPROFILE"] = DOBBY_HOME
    env["HOME"] = DOBBY_HOME
    env["HOMEDRIVE"] = "C:"
    env["HOMEPATH"] = r"\Users\dobby"
    env["APPDATA"] = os.path.join(DOBBY_HOME, "AppData", "Roaming")
    env["LOCALAPPDATA"] = os.path.join(DOBBY_HOME, "AppData", "Local")
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["NO_COLOR"] = "1"
    extra = [
        os.path.dirname(paths.SERVICE_PYTHON),
        os.path.join(os.path.dirname(paths.SERVICE_PYTHON), "Scripts"),
        paths.NPM_GLOBAL,
        os.path.join(paths.USER_HOME, "AppData", "Roaming", "npm"),
    ]
    have = env.get("PATH", "")
    env["PATH"] = ";".join([p for p in extra if os.path.isdir(p)] + [have])
    return env


def allowed_cwds():
    roots = list(coder.allowed_project_roots())
    if os.path.isdir(DOBBY_HOME):
        roots.append(os.path.abspath(DOBBY_HOME))
    seen, out = set(), []
    for r in roots:
        key = os.path.abspath(r).lower()
        if key not in seen:
            seen.add(key)
            out.append(os.path.abspath(r))
    return out


def cwd_allowed(cwd):
    try:
        requested = os.path.abspath(cwd or coder.PROJECT_ROOT)
        if not os.path.isdir(requested):
            return False
    except Exception:
        return False
    for root in allowed_cwds():
        # commonpath raises ValueError on cross-drive pairs (e.g. M: root vs a
        # C: cwd); that must skip THIS root, not veto every remaining one.
        try:
            if os.path.normcase(os.path.commonpath([root, requested])) == os.path.normcase(root):
                return True
        except Exception:
            continue
    return False


def _shell_args(command, shell):
    shell = (shell or "powershell").strip().lower()
    if shell == "cmd":
        return ["cmd", "/c", command], "cmd"
    if shell == "bash":
        bash = r"C:\Program Files\Git\bin\bash.exe"
        if os.path.isfile(bash):
            return [bash, "-lc", command], "bash"
        # fall through to powershell if git-bash is missing
    return ["powershell", "-NoProfile", "-NonInteractive", "-Command", command], "powershell"


def config():
    return {
        "ok": True,
        "shells": ["powershell", "cmd", "bash"],
        "default_shell": "powershell",
        "default_cwd": os.path.abspath(coder.PROJECT_ROOT),
        "cwds": allowed_cwds(),
        "stream_timeout_default": STREAM_TIMEOUT_DEFAULT,
        "stream_timeout_max": STREAM_TIMEOUT_MAX,
        "note": "Commands run in the service account on this rig. Reachable only over the LAN/Tailscale.",
    }



def run_stream(command, cwd, shell="powershell", timeout=STREAM_TIMEOUT_DEFAULT):
    """Generator of ndjson-ready event dicts for a one-shot command."""
    command = (command or "").strip()
    if not command:
        yield {"type": "error", "text": "command is required"}
        yield {"type": "done", "code": -1}
        return
    cwd = os.path.abspath(cwd or coder.PROJECT_ROOT)
    if not cwd_allowed(cwd):
        yield {"type": "error", "text": "working directory not allowed: %s" % cwd}
        yield {"type": "done", "code": -1}
        return
    try:
        timeout = max(1, min(int(timeout or STREAM_TIMEOUT_DEFAULT), STREAM_TIMEOUT_MAX))
    except Exception:
        timeout = STREAM_TIMEOUT_DEFAULT
    args, resolved_shell = _shell_args(command, shell)

    try:
        proc = subprocess.Popen(
            args, cwd=cwd, env=_exec_env(),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            text=True, bufsize=1, encoding="utf-8", errors="replace",
            creationflags=_CREATE_NO_WINDOW | _CREATE_NEW_PROCESS_GROUP,
        )
    except Exception as e:
        yield {"type": "error", "text": "launch failed: %s" % e}
        yield {"type": "done", "code": -1}
        return

    timed_out = {"v": False}

    def _watchdog():
        timed_out["v"] = True
        _kill_tree(proc.pid)

    timer = threading.Timer(timeout, _watchdog)
    timer.daemon = True
    timer.start()

    yield {"type": "start", "shell": resolved_shell, "cwd": cwd, "command": command, "pid": proc.pid}
    try:
        for line in proc.stdout:
            yield {"type": "out", "text": _ANSI.sub("", line).rstrip("\n")}
        proc.wait(timeout=10)
        if timed_out["v"]:
            yield {"type": "error", "text": "command exceeded %ds and was stopped" % timeout}
        yield {"type": "done", "code": proc.returncode}
    except GeneratorExit:
        _kill_tree(proc.pid)
        raise
    finally:
        timer.cancel()
        if proc.poll() is None:
            _kill_tree(proc.pid)



def start_detached(command, cwd, shell="powershell", label=""):
    command = (command or "").strip()
    if not command:
        return {"ok": False, "error": "command is required", "status_code": 400}
    cwd = os.path.abspath(cwd or coder.PROJECT_ROOT)
    if not cwd_allowed(cwd):
        return {"ok": False, "error": "working directory not allowed: %s" % cwd, "status_code": 400}
    args, resolved_shell = _shell_args(command, shell)
    os.makedirs(LOG_DIR, exist_ok=True)
    proc_id = uuid.uuid4().hex[:12]
    log_path = os.path.join(LOG_DIR, "proc_%s.log" % proc_id)
    try:
        logf = open(log_path, "ab")
        # CREATE_NO_WINDOW (hidden console), not DETACHED_PROCESS: console apps
        # like powershell/cmd exit immediately with no console at all when the
        # server itself runs in a console-less session, so detached launches
        # silently died. A hidden console keeps them alive and they still
        # survive the parent (no job object ties them to us).
        proc = subprocess.Popen(
            args, cwd=cwd, env=_exec_env(),
            stdout=logf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            creationflags=_CREATE_NO_WINDOW | _CREATE_NEW_PROCESS_GROUP, close_fds=True,
        )
    except Exception as e:
        return {"ok": False, "error": "launch failed: %s" % e, "status_code": 500}
    with _LOCK:
        _PROCS[proc_id] = {
            "id": proc_id, "pid": proc.pid, "command": command, "label": (label or command)[:80],
            "cwd": cwd, "shell": resolved_shell, "log_path": log_path,
            "started": time.time(), "proc": proc,
        }
    return {"ok": True, "id": proc_id, "pid": proc.pid, "log_path": log_path}


def _alive(pid):
    try:
        import psutil
        return psutil.pid_exists(pid) and psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except Exception:
        return False


def list_running():
    out = []
    with _LOCK:
        items = list(_PROCS.values())
    for m in items:
        alive = _alive(m["pid"])
        size = 0
        try:
            size = os.path.getsize(m["log_path"])
        except OSError:
            pass
        out.append({
            "id": m["id"], "pid": m["pid"], "command": m["command"], "label": m["label"],
            "cwd": m["cwd"], "shell": m["shell"], "started": m["started"],
            "uptime_s": round(time.time() - m["started"], 1), "running": alive, "log_bytes": size,
        })
    out.sort(key=lambda x: x["started"], reverse=True)
    return {"ok": True, "processes": out}


def tail(proc_id, offset=0, max_bytes=200000):
    with _LOCK:
        m = _PROCS.get(proc_id)
    if not m:
        return {"ok": False, "error": "unknown process id", "status_code": 404}
    path = m["log_path"]
    try:
        size = os.path.getsize(path)
        start = max(0, int(offset or 0))
        if size - start > max_bytes:
            start = size - max_bytes
        with open(path, "rb") as f:
            f.seek(start)
            data = f.read()
        text = _ANSI.sub("", data.decode("utf-8", errors="replace"))
        return {"ok": True, "id": proc_id, "offset": size, "running": _alive(m["pid"]), "text": text}
    except FileNotFoundError:
        return {"ok": True, "id": proc_id, "offset": 0, "running": _alive(m["pid"]), "text": ""}
    except Exception as e:
        return {"ok": False, "error": str(e), "status_code": 500}


def _kill_tree(pid):
    try:
        import psutil
        p = psutil.Process(pid)
        for ch in p.children(recursive=True):
            try:
                ch.kill()
            except Exception:
                pass
        p.kill()
        return True
    except Exception:
        return False


def stop(proc_id):
    with _LOCK:
        m = _PROCS.get(proc_id)
    if not m:
        return {"ok": False, "error": "unknown process id", "status_code": 404}
    killed = _kill_tree(m["pid"])
    return {"ok": True, "id": proc_id, "killed": killed, "running": _alive(m["pid"])}


def clear_finished():
    removed = []
    with _LOCK:
        for pid_id in list(_PROCS.keys()):
            if not _alive(_PROCS[pid_id]["pid"]):
                removed.append(pid_id)
                _PROCS.pop(pid_id, None)
    return {"ok": True, "removed": removed}
