"""User-session WSL OpenCode bridge for the SysMonitor dashboard.

SysMonitor runs as LocalSystem for hardware/fan access. LocalSystem cannot use
the operator's per-user WSL distro, so the dashboard writes request files here
and this user-session process runs WSL OpenCode.
"""
import json
import os
import queue
import re
import subprocess
import threading
import time
import uuid


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
BRIDGE_DIR = os.path.join(ROOT, ".wsl_bridge")
REQUEST_DIR = os.path.join(BRIDGE_DIR, "requests")
RUN_DIR = os.path.join(BRIDGE_DIR, "runs")
CANCEL_DIR = os.path.join(BRIDGE_DIR, "cancel")
HEARTBEAT = os.path.join(BRIDGE_DIR, "heartbeat.json")

WSL_EXE = r"C:\Windows\System32\wsl.exe"
WSL_OPENCODE = "/usr/local/bin/opencode"
_USER_HOME = os.environ.get("USER_HOME") or os.path.expanduser("~")
_drive, _rest = os.path.splitdrive(os.path.join(_USER_HOME, ".config"))
WSL_CONFIG_HOME = os.environ.get("WSL_CONFIG_HOME") or "/mnt/%s%s" % (
    _drive.rstrip(":").lower(), _rest.replace("\\", "/"))
WSL_HOME = os.environ.get("WSL_HOME") or "/home/" + (
    os.environ.get("WSL_USER") or os.path.basename(_USER_HOME))
MAX_SECONDS_DEFAULT = 1800
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def ensure_dirs():
    for path in (BRIDGE_DIR, REQUEST_DIR, RUN_DIR, CANCEL_DIR):
        os.makedirs(path, exist_ok=True)


def write_json_atomic(path, data):
    tmp = path + ".tmp.%s" % uuid.uuid4().hex
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=True)
    os.replace(tmp, path)


def append_event(log_path, event):
    with open(log_path, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(event, ensure_ascii=True) + "\n")


def sh_quote(value):
    return "'" + str(value).replace("'", "'\"'\"'") + "'"


def windows_to_wsl_path(path):
    abs_path = os.path.abspath(path)
    drive, tail = os.path.splitdrive(abs_path)
    if not drive:
        return abs_path.replace("\\", "/")
    mount = "/mnt/" + drive[:1].lower()
    tail = tail.replace("\\", "/").lstrip("/")
    return mount + ("/" + tail if tail else "")


def kill_process(proc):
    try:
        proc.kill()
    except Exception:
        pass


def run_request(request_path):
    try:
        with open(request_path, "r", encoding="utf-8") as f:
            req = json.load(f)
    except Exception:
        try:
            os.remove(request_path)
        except OSError:
            pass
        return

    run_id = str(req.get("id") or uuid.uuid4().hex)
    project_dir = os.path.abspath(req.get("project_dir") or ROOT)
    task = str(req.get("task") or "").strip()
    max_seconds = int(req.get("max_seconds") or MAX_SECONDS_DEFAULT)
    log_path = os.path.join(RUN_DIR, run_id + ".ndjson")
    done_path = os.path.join(RUN_DIR, run_id + ".done.json")
    cancel_path = os.path.join(CANCEL_DIR, run_id + ".cancel")

    try:
        os.remove(request_path)
    except OSError:
        pass

    if not task:
        append_event(log_path, {"type": "error", "text": "task is required"})
        append_event(log_path, {"type": "done", "code": -1})
        write_json_atomic(done_path, {"ok": False, "code": -1})
        return

    wsl_project = windows_to_wsl_path(project_dir)
    safe_project = sh_quote(wsl_project)
    safe_task = sh_quote(task)
    script = (
        "cd {project} && "
        "git config --global --add safe.directory {project} >/dev/null 2>&1 || true; "
        "XDG_CONFIG_HOME={config} HOME={home} NO_COLOR=1 CI=1 "
        "{opencode} run {task} --dir {project} --dangerously-skip-permissions"
    ).format(
        project=safe_project,
        config=sh_quote(WSL_CONFIG_HOME),
        home=sh_quote(WSL_HOME),
        opencode=sh_quote(WSL_OPENCODE),
        task=safe_task,
    )

    append_event(log_path, {"type": "start", "project": project_dir, "runner": "wsl_bridge", "task": task})
    append_event(log_path, {"type": "out", "text": "wsl bridge runner: live agent"})
    try:
        proc = subprocess.Popen(
            [WSL_EXE, "--", "bash", "-lc", script],
            cwd=project_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=0x08000000,
        )
    except Exception as exc:
        append_event(log_path, {"type": "error", "text": "WSL bridge launch failed: %s" % exc})
        append_event(log_path, {"type": "done", "code": -1})
        write_json_atomic(done_path, {"ok": False, "code": -1})
        return

    lines = queue.Queue()

    def reader():
        try:
            for line in proc.stdout:
                lines.put(line)
        except Exception:
            pass
        lines.put(None)

    threading.Thread(target=reader, daemon=True).start()
    start = time.time()
    next_tick = start + 5
    code = -1
    cancelled = False
    while True:
        if os.path.exists(cancel_path):
            cancelled = True
            append_event(log_path, {"type": "error", "text": "cancel requested; stopping WSL OpenCode"})
            kill_process(proc)
            break
        if time.time() - start > max_seconds:
            append_event(log_path, {"type": "error", "text": "WSL OpenCode exceeded %ds and was stopped" % max_seconds})
            kill_process(proc)
            break
        try:
            line = lines.get(timeout=0.5)
        except queue.Empty:
            if time.time() >= next_tick:
                append_event(log_path, {"type": "tick", "seconds": int(time.time() - start)})
                next_tick = time.time() + 5
            continue
        if line is None:
            proc.wait(timeout=10)
            code = int(proc.returncode or 0)
            break
        append_event(log_path, {"type": "out", "text": _ANSI.sub("", line).rstrip("\n")})

    if cancelled:
        code = -1
    append_event(log_path, {"type": "done", "code": code})
    write_json_atomic(done_path, {"ok": code == 0, "code": code})
    try:
        os.remove(cancel_path)
    except OSError:
        pass


def main():
    ensure_dirs()
    while True:
        write_json_atomic(HEARTBEAT, {"ok": True, "pid": os.getpid(), "time": time.time()})
        requests = sorted(
            os.path.join(REQUEST_DIR, name)
            for name in os.listdir(REQUEST_DIR)
            if name.endswith(".json")
        )
        if requests:
            run_request(requests[0])
        time.sleep(0.5)


if __name__ == "__main__":
    main()
