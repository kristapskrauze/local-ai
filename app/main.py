import base64
import ipaddress
import logging
import os
import shutil
import socket
import threading
import subprocess
import sys
import tempfile
import time
import html
import json
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from datetime import datetime, timedelta, timezone

import psutil
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app import (
    agent_tools,
    ai_stack,
    coder,
    fan_control,
    game_streaming,
    goals,
    hosts,
    messaging,
    qwen_agent,
    rgb,
    rig_agent,
    sidecar_health,
    terminal,
)
from app import paths

LOGGER = logging.getLogger(__name__)

app = FastAPI(title="System Monitoring")


_TRUSTED_NETS = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("100.64.0.0/10"),        # Tailscale CGNAT
]
for _cidr in str(os.environ.get("SYSMON_TRUSTED_CIDRS") or "").split(","):
    _cidr = _cidr.strip()
    if _cidr:
        try:
            _TRUSTED_NETS.append(ipaddress.ip_network(_cidr, strict=False))
        except ValueError:
            LOGGER.warning("ignoring bad SYSMON_TRUSTED_CIDRS entry: %s", _cidr)

# Anything that is not a GET changes something, so that is the line: the whole
# read-only dashboard keeps working from any LAN device, while every write goes
# through the trust check. Command execution and service control are guarded on
# GET too — those routes are RCE either way.
_ALWAYS_GUARDED = ("/api/exec/", "/api/service/")


def _client_trusted(request):
    host = (request.client.host if request.client else "") or ""
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return any(addr in net for net in _TRUSTED_NETS)


@app.middleware("http")
async def _restrict_mutations(request, call_next):
    path = request.url.path
    guarded = (path.startswith(_ALWAYS_GUARDED)
               or (path.startswith("/api/") and request.method != "GET"))
    if guarded and not _client_trusted(request):
        LOGGER.warning("blocked %s %s from untrusted %s", request.method, path,
                       request.client.host if request.client else "?")
        return JSONResponse(
            {"ok": False,
             "error": "this endpoint is restricted to localhost and Tailscale; "
                      "add the network to SYSMON_TRUSTED_CIDRS to allow it"},
            status_code=403)
    return await call_next(request)


@app.on_event("startup")
def _show_app_logs():
    """Let app.* INFO records reach service.err.log.

    Without this the root logger stays at WARNING and logging's handler of
    last resort drops everything below it, so the pipeline's own account of
    what it did — which request opened, which lane produced nothing, how long
    the answer took — existed only in the source.
    """
    logger = logging.getLogger("app")
    if any(getattr(h, "_sysmon", False) for h in logger.handlers):
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s", "%H:%M:%S"))
    handler._sysmon = True
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


@app.on_event("startup")
def _quiet_benign_connection_resets():
    """Stop asyncio spamming service.err.log with client-disconnect tracebacks.

    Every time the phone sleeps mid-poll, Windows' proactor transport raises
    ConnectionResetError (WinError 10054) from _call_connection_lost and asyncio
    logs a full traceback. That was 100% of service.err.log — 706 lines of it —
    which rotates real errors out of the file before anyone can read them.
    Anything that is not this specific benign case still goes to the default
    handler.
    """
    import asyncio

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    default_handler = loop.get_exception_handler()

    def handler(loop_, context):
        exc = context.get("exception")
        # A client vanishing mid-response is never actionable on the server side.
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError)):
            return
        if default_handler is not None:
            default_handler(loop_, context)
        else:
            loop_.default_exception_handler(context)

    loop.set_exception_handler(handler)


@app.on_event("startup")
def _sidecar_health_startup():
    def _run():
        time.sleep(25)          # let the daemons settle -- boot is CPU-contended
        try:
            sidecar_health.log_status(heal_degraded=True)
            sidecar_health.start_watch()
        except Exception:
            LOGGER.exception("sidecar health startup check failed")
    threading.Thread(target=_run, name="sidecar_health_boot", daemon=True).start()


@app.on_event("startup")
def _recover_pending_replies():
    """Answer messages that a restart interrupted mid-debounce."""
    try:
        result = messaging.recover_pending()
        if result.get("recovered"):
            LOGGER.warning("replayed %d chat batch(es) lost to a restart: %s",
                           result["recovered"], ", ".join(result.get("chats") or []))
    except Exception:
        LOGGER.exception("pending-reply recovery failed")


@app.on_event("startup")
def _recover_interrupted_rehearsals():
    """Put back files a proposal rehearsal left changed when a restart cut it off.

    Otherwise an unapproved change stays in the repo and this very start loads
    it. Anything edited since the rehearsal is left alone and named here.
    """
    try:
        from app import self_heal
        result = self_heal.recover_interrupted_rehearsals()
        if result.get("restored") or result.get("left"):
            LOGGER.warning("interrupted rehearsal: put back %s; left as found "
                           "(changed since): %s", result.get("restored"),
                           result.get("left"))
    except Exception:
        LOGGER.exception("rehearsal recovery failed")


@app.on_event("startup")
def _start_location_poller():
    """Start the background location poller in a daemon thread."""
    from app import location
    t = threading.Thread(target=location.poll_forever, name="location_poller", daemon=True)
    t.start()


@app.on_event("shutdown")
def _shutdown_release_fans():
    try:
        fan_control.release_all()
    except Exception:
        pass
    try:
        rgb.shutdown()
    except Exception:
        pass

HOSTNAME = socket.gethostname()
RIG_NAME = "AI Rig RTX3090"

start_time = time.time()

app.mount("/static", StaticFiles(directory="app/static"), name="static")

# Rolling state for rate calculations (bytes/sec etc.)
_PREV = {"net": None, "net_t": 0.0, "disk": None, "disk_t": 0.0}


def _num(s):
    """Parse an nvidia-smi style cell into float or None."""
    if s is None:
        return None
    s = str(s).strip()
    if not s or s.lower() in ("[n/a]", "n/a", "[not supported]", "not supported", "[unknown error]"):
        return None
    try:
        return float(s)
    except (ValueError, TypeError):
        return None



def get_core_temp_data():
    """Talk to the local Core Temp remote-server socket and parse per-core data."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(3)
        sock.connect(("127.0.0.1", 5200))
        sock.send(b"GET /\r\n")
        response = b""
        t0 = time.time()
        while time.time() - t0 < 2:
            try:
                chunk = sock.recv(2048)
                if not chunk:
                    break
                response += chunk
                if b"}" in response:
                    break
            except socket.timeout:
                break
            except Exception:
                break
        sock.close()

        text = response.decode("utf-8", errors="ignore")
        for line in text.strip().split("\n"):
            line = line.strip()
            if not (line.startswith("{") and line.endswith("}")):
                continue
            try:
                data = json.loads(line)
            except Exception:
                continue
            ci = data.get("CpuInfo")
            if not ci or "fTemp" not in ci:
                continue
            temps = [t for t in ci.get("fTemp", []) if t is not None]
            loads = [l for l in ci.get("fLoad", []) if l is not None]
            tjmax = ci.get("uiTjMax", [])
            if not temps:
                continue
            return {
                "temperature_available": True,
                "package_temperature_c": temps[0],
                "average_temperature_c": round(sum(temps) / len(temps), 1),
                "max_c": max(temps),
                "core_temperatures_c": [round(t, 1) for t in temps],
                "core_loads_percent": [round(l, 1) for l in loads] if loads else [],
                "tjmax_c": tjmax[0] if isinstance(tjmax, list) and tjmax else None,
                "cpu_model": ci.get("CPUName"),
                "vid_v": round(ci["fVID"], 4) if ci.get("fVID") else None,
                "multiplier": ci.get("fMultiplier"),
                "bus_speed_mhz": round(ci["fFSBSpeed"], 1) if ci.get("fFSBSpeed") else None,
            }
    except Exception:
        pass
    return {
        "temperature_available": False,
        "package_temperature_c": None,
        "average_temperature_c": None,
        "max_c": None,
        "core_temperatures_c": [],
        "core_loads_percent": [],
        "tjmax_c": None,
        "cpu_model": None,
        "vid_v": None,
        "multiplier": None,
        "bus_speed_mhz": None,
    }


def get_cpu_stats(per_core):
    freq = None
    try:
        freq = psutil.cpu_freq()
    except Exception:
        pass
    cpu_activity_pct = round(sum(per_core) / len(per_core), 1) if per_core else None
    core_temp = get_core_temp_data()

    # LHM is the source of truth for per-core temps and per-thread loads
    # (Core Temp's a user-session GUI, not always running). PawnIO driver
    # gives the service direct chip access. Core Temp data is used only
    # when LHM doesn't have something (model name, VID, multiplier).
    lhm = fan_control.get_cpu_telemetry()
    lhm_clock_mhz = None
    lhm_clock_min_mhz = None
    lhm_clock_max_mhz = None
    lhm_core_clocks_mhz = []
    if lhm is not None:
        cores = [t for t in (lhm.get("cores_c") or []) if t is not None]
        pkg = lhm.get("package_c")
        threads = [t for t in (lhm.get("threads_load") or []) if t is not None]
        lhm_clock_mhz = lhm.get("clock_mhz")
        lhm_clock_min_mhz = lhm.get("clock_min_mhz")
        lhm_clock_max_mhz = lhm.get("clock_max_mhz")
        lhm_core_clocks_mhz = lhm.get("clocks_mhz") or []
        if cores or pkg is not None:
            core_temp["temperature_available"] = True
            if pkg is not None:
                core_temp["package_temperature_c"] = pkg
            elif cores:
                core_temp["package_temperature_c"] = max(cores)
            if cores:
                core_temp["core_temperatures_c"] = cores
                core_temp["average_temperature_c"] = round(sum(cores) / len(cores), 1)
                core_temp["max_c"] = max(cores)
        if threads and not core_temp["core_loads_percent"]:
            core_temp["core_loads_percent"] = threads
        if lhm.get("bus_clock_mhz") is not None:
            core_temp["bus_speed_mhz"] = lhm.get("bus_clock_mhz")

    loadavg = None
    try:
        la = psutil.getloadavg()
        loadavg = [round(x, 2) for x in la]
    except Exception:
        pass

    times = {}
    try:
        tp = psutil.cpu_times_percent(interval=None)
        times = {
            "user": round(tp.user, 1),
            "system": round(tp.system, 1),
            "idle": round(tp.idle, 1),
        }
    except Exception:
        pass

    stats = {}
    try:
        cs = psutil.cpu_stats()
        stats = {
            "ctx_switches": cs.ctx_switches,
            "interrupts": cs.interrupts,
        }
    except Exception:
        pass

    raw_frequency_mhz = round(lhm_clock_mhz, 1) if lhm_clock_mhz is not None else (round(freq.current, 1) if freq else None)
    effective_frequency_mhz = None
    if raw_frequency_mhz is not None and cpu_activity_pct is not None:
        effective_frequency_mhz = round(raw_frequency_mhz * max(0.0, min(cpu_activity_pct, 100.0)) / 100.0, 1)

    out = {
        "name": HOSTNAME,
        "model": core_temp.get("cpu_model"),
        "temperature_available": core_temp["temperature_available"],
        "package_temperature_c": core_temp["package_temperature_c"],
        "average_temperature_c": core_temp["average_temperature_c"],
        "max_c": core_temp["max_c"],
        "tjmax_c": core_temp["tjmax_c"],
        "core_temperatures_c": core_temp["core_temperatures_c"],
        "core_loads_percent": core_temp["core_loads_percent"],
        "per_core_percent": [round(p, 1) for p in per_core] if per_core else [],
        "vid_v": core_temp["vid_v"],
        "multiplier": core_temp["multiplier"],
        "bus_speed_mhz": core_temp["bus_speed_mhz"],
        "frequency_mhz": raw_frequency_mhz,
        "frequency_effective_mhz": effective_frequency_mhz,
        "frequency_clock_mhz": raw_frequency_mhz,
        "frequency_max_mhz": round(freq.max, 1) if freq and freq.max else None,
        "frequency_min_mhz": round(lhm_clock_min_mhz, 1) if lhm_clock_min_mhz is not None else None,
        "frequency_peak_mhz": round(lhm_clock_max_mhz, 1) if lhm_clock_max_mhz is not None else None,
        "core_frequencies_mhz": lhm_core_clocks_mhz,
        "frequency_source": "LibreHardwareMonitor clock" if lhm_clock_mhz is not None else ("psutil" if freq else None),
        "thread_count": psutil.cpu_count(logical=True),
        "core_count": psutil.cpu_count(logical=False),
        "load_average": loadavg,
        "times_percent": times,
        "stats": stats,
    }
    return out



GPU_FIELDS = [
    "index", "name", "driver_version", "temperature.gpu",
    "utilization.gpu", "utilization.memory", "memory.used", "memory.total",
    "memory.free", "fan.speed", "power.draw", "power.limit",
    "clocks.current.graphics", "clocks.current.memory", "clocks.current.sm",
    "pstate",
]


def get_gpu_stats():
    result = {
        "available": False,
        "name": "N/A",
        "driver_version": None,
        "temperature_c": None,
        "fan_speed_percent": None,
        "power_draw_w": None,
        "power_limit_w": None,
        "clock_graphics_mhz": None,
        "clock_memory_mhz": None,
        "clock_sm_mhz": None,
        "pstate": None,
        "gpu_utilization_percent": 0,
        "memory_utilization_percent": 0,
        "memory_total_mb": 0,
        "memory_used_mb": 0,
        "memory_free_mb": 0,
        "processes": [],
    }
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=" + ",".join(GPU_FIELDS),
             "--format=csv,noheader,nounits"],
            stderr=subprocess.DEVNULL, timeout=5,
        ).decode("utf-8", errors="replace")
        line = next((l.strip() for l in out.splitlines() if l.strip()), None)
        if line:
            p = [c.strip() for c in line.split(",")]
            mem_used = _num(p[6]) or 0
            mem_total = _num(p[7]) or 0
            result.update({
                "available": True,
                "name": p[1] or "N/A",
                "driver_version": p[2] or None,
                "temperature_c": _num(p[3]),
                "gpu_utilization_percent": _num(p[4]) or 0,
                "memory_utilization_percent": (
                    round(mem_used / mem_total * 100, 1) if mem_total else 0
                ),
                "memory_used_mb": mem_used,
                "memory_total_mb": mem_total,
                "memory_free_mb": round(mem_total - mem_used, 1) if mem_total else 0,
                "fan_speed_percent": _num(p[9]),
                "power_draw_w": _num(p[10]),
                "power_limit_w": _num(p[11]),
                "clock_graphics_mhz": _num(p[12]),
                "clock_memory_mhz": _num(p[13]),
                "clock_sm_mhz": _num(p[14]),
                "pstate": p[15] or None,
            })
    except Exception:
        pass

    # WHICH processes hold VRAM changes when something is started or stopped,
    # not twice a second. This was the second nvidia-smi spawn of every poll:
    # 86k subprocesses a day, each one taking the NVML lock while the model
    # the messaging lane is running sits on the same card.
    cached = _GPU_APPS_CACHE
    if cached["out"] is not None and time.time() - cached["t"] < 10.0:
        result["processes"] = cached["out"]
        return result
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-compute-apps=pid,process_name,used_memory",
             "--format=csv,noheader,nounits"],
            stderr=subprocess.DEVNULL, timeout=5,
        ).decode("utf-8", errors="replace")
        procs = []
        for l in out.splitlines():
            l = l.strip()
            if not l or "[N/A]" in l:
                continue
            parts = [c.strip() for c in l.split(",")]
            if len(parts) >= 3:
                procs.append({
                    "pid": parts[0],
                    "name": parts[1],
                    "memory_mb": _num(parts[2]) or 0,
                })
        procs.sort(key=lambda x: x["memory_mb"], reverse=True)
        result["processes"] = procs[:8]
        _GPU_APPS_CACHE.update({"t": time.time(), "out": result["processes"]})
    except Exception:
        pass

    return result



def get_network_io():
    nets = psutil.net_io_counters(pernic=True)
    now = time.time()
    prev = _PREV["net"]
    dt = now - _PREV["net_t"] if _PREV["net_t"] else 0
    result = []
    for name, c in nets.items():
        lname = name.lower()
        if lname.startswith("lo") or lname == "loopback":
            continue
        rx_rate = tx_rate = 0.0
        if prev and name in prev and dt > 0:
            rx_rate = max(0.0, (c.bytes_recv - prev[name].bytes_recv) / dt / 1024)
            tx_rate = max(0.0, (c.bytes_sent - prev[name].bytes_sent) / dt / 1024)
        result.append({
            "name": name,
            "rx_kb": round(c.bytes_recv / 1024, 2),
            "tx_kb": round(c.bytes_sent / 1024, 2),
            "rx_rate_kbps": round(rx_rate, 1),
            "tx_rate_kbps": round(tx_rate, 1),
            "packets_sent": c.packets_sent,
            "packets_recv": c.packets_recv,
            "errors": c.errin + c.errout,
            "drops": c.dropin + c.dropout,
        })
    _PREV["net"] = nets
    _PREV["net_t"] = now
    return result if result else [{
        "name": "No network interfaces found", "rx_kb": 0, "tx_kb": 0,
        "rx_rate_kbps": 0, "tx_rate_kbps": 0, "packets_sent": 0,
        "packets_recv": 0, "errors": 0, "drops": 0,
    }]


def get_disk_io():
    try:
        c = psutil.disk_io_counters()
        if not c:
            return {}
        now = time.time()
        prev = _PREV["disk"]
        dt = now - _PREV["disk_t"] if _PREV["disk_t"] else 0
        read_rate = write_rate = 0.0
        if prev and dt > 0:
            read_rate = max(0.0, (c.read_bytes - prev.read_bytes) / dt / 1024 / 1024)
            write_rate = max(0.0, (c.write_bytes - prev.write_bytes) / dt / 1024 / 1024)
        _PREV["disk"] = c
        _PREV["disk_t"] = now
        return {
            "read_mb": round(c.read_bytes / 1024 / 1024, 1),
            "write_mb": round(c.write_bytes / 1024 / 1024, 1),
            "read_count": c.read_count,
            "write_count": c.write_count,
            "read_rate_mbps": round(read_rate, 2),
            "write_rate_mbps": round(write_rate, 2),
        }
    except Exception:
        return {}



_GPU_APPS_CACHE = {"t": 0.0, "out": None}
_PROC_CACHE = {"t": 0.0, "out": None}
_DISK_CACHE = {"t": 0.0, "out": None}


def _disk_usage():
    """Free space per volume, refreshed every 30 s.

    disk_usage() on M: is a round trip to a USB backup HDD: polling it every
    2 s kept the drive spinning for a number that moves in gigabytes per hour.
    """
    if _DISK_CACHE["out"] is not None and time.time() - _DISK_CACHE["t"] < 30.0:
        return _DISK_CACHE["out"]
    disks = []
    for part in psutil.disk_partitions(all=False):
        try:
            u = psutil.disk_usage(part.mountpoint)
            disks.append({
                "letter": part.mountpoint.replace("\\", ""),
                "fstype": part.fstype,
                "used_gb": round(u.used / (1024**3), 2),
                "free_gb": round(u.free / (1024**3), 2),
                "total_gb": round(u.total / (1024**3), 2),
                "percent": u.percent,
            })
        except (PermissionError, OSError):
            pass
    _DISK_CACHE.update({"t": time.time(), "out": disks})
    return disks


def get_top_processes():
    # psutil.process_iter walks ~350 processes and opens each one; at a 2 s
    # poll that is the most expensive thing in /api/stats after nvidia-smi,
    # and the top six by CPU do not turn over that fast.
    if (_PROC_CACHE["out"] is not None
            and time.time() - _PROC_CACHE["t"] < 6.0):
        return _PROC_CACHE["out"]
    procs = []
    try:
        for p in psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent"]):
            try:
                info = p.info
                procs.append({
                    "pid": info["pid"],
                    "name": info.get("name") or "?",
                    "cpu": round(info.get("cpu_percent") or 0.0, 1),
                    "mem": round(info.get("memory_percent") or 0.0, 1),
                })
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    except Exception:
        return {"by_cpu": [], "by_memory": []}
    by_cpu = sorted(procs, key=lambda x: x["cpu"], reverse=True)[:6]
    by_mem = sorted(procs, key=lambda x: x["mem"], reverse=True)[:6]
    out = {"by_cpu": by_cpu, "by_memory": by_mem, "total": len(procs)}
    _PROC_CACHE.update({"t": time.time(), "out": out})
    return out



REMOTE_CONSOLE = hosts.MEDIA_CONSOLE_URL
OLLAMA = "http://127.0.0.1:11434"
COMFY = "http://127.0.0.1:8188"

# ComfyUI portable install (COMFYUI_DIR; we only launch it and read its output).
COMFY_DIR = paths.COMFYUI_DIR
COMFY_PY = os.path.join(COMFY_DIR, "python_embeded", "python.exe")
COMFY_LOG = os.path.join(tempfile.gettempdir(), "comfyui_console.log")
COMFY_OUTPUT_ROOT = os.path.join(COMFY_DIR, "ComfyUI")
STUDIO_ARTIFACTS = os.path.abspath(os.path.join("artifacts", "studio"))
_COMFY_ARCHIVE_CACHE = {}

# docker.exe is not always on the service PATH; fall back to the install path.
DOCKER_EXE = (
    shutil.which("docker")
    or r"C:\Program Files\Docker\Docker\resources\bin\docker.exe"
)

# Windows process-creation flags (avoid console windows / detach long-runners).
_CREATE_NO_WINDOW = 0x08000000
_DETACHED_PROCESS = 0x00000008
_CREATE_NEW_PROCESS_GROUP = 0x00000200

DOCKER_CONTAINERS = {"open_webui": "open-webui", "searxng": "searxng"}
# Services the dashboard is allowed to control at all.
CONTROLLABLE = set(DOCKER_CONTAINERS) | {"comfyui"}

# Handle to a ComfyUI we launched ourselves (so stop is reliable even before
# the port scan); may be None if it was started outside this process.
_COMFY_PROC = None

# The remote box answers in ~1s; cache so dashboard polling can't hammer it.
_REMOTE_CACHE = {"t": 0.0, "out": None}

SERVICES = [
    {"id": "ollama",     "name": "Ollama",        "host": "127.0.0.1",    "port": 11434, "local": True,  "desc": "LLM runtime"},
    {"id": "open_webui", "name": "Open WebUI",    "host": "127.0.0.1",    "port": 3000,  "local": True,  "desc": "Chat UI (Docker)"},
    {"id": "comfyui",    "name": "ComfyUI",       "host": "127.0.0.1",    "port": 8188,  "local": True,  "desc": "Image / video gen"},
    {"id": "searxng",    "name": "SearXNG",       "host": "127.0.0.1",    "port": 8888,  "local": True,  "desc": "Private search (Docker)"},
    {"id": "sunshine",   "name": "Moonlight",      "host": "127.0.0.1",    "port": 47989, "local": True,  "desc": "Sunshine game-streaming host"},
    {"id": "media",      "name": "Media Console", "host": hosts.MEDIA_BOX_IP, "port": 8183, "local": False, "desc": "Jellyfin box dashboard"},
    {"id": "jellyfin",   "name": "Jellyfin",      "host": hosts.MEDIA_BOX_IP, "port": 8096, "local": False, "desc": "Media server"},
]


def _http_json(url, timeout=6, payload=None):
    data = None
    req = urllib.request.Request(url)
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, data=data, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", errors="replace"))


def _api_error(error, status_code=502):
    return JSONResponse(
        status_code=status_code,
        content={"ok": False, "error": str(error)},
    )


def _tcp_up(host, port, timeout=0.6):
    try:
        s = socket.create_connection((host, port), timeout=timeout)
        s.close()
        return True
    except Exception:
        return False


@app.get("/api/services")
def api_services():
    with ThreadPoolExecutor(max_workers=len(SERVICES)) as ex:
        ups = list(ex.map(lambda s: _tcp_up(s["host"], s["port"]), SERVICES))
    return {"services": [
        {"id": s["id"], "name": s["name"], "host": s["host"], "port": s["port"],
         "local": s["local"], "desc": s["desc"], "up": up,
         "control": s["id"] in CONTROLLABLE}
        for s, up in zip(SERVICES, ups)
    ]}


@app.get("/api/game-streaming")
def api_game_streaming():
    return game_streaming.status()


def _run_cmd(args, timeout=25):
    """Run a command quietly; return (ok, combined_output)."""
    try:
        r = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
            creationflags=_CREATE_NO_WINDOW,
        )
        out = ((r.stdout or "") + (r.stderr or "")).strip()
        return r.returncode == 0, out
    except FileNotFoundError as e:
        return False, "not found: %s" % e
    except subprocess.TimeoutExpired:
        return False, "timed out"
    except Exception as e:
        return False, str(e)


def _docker(action, container):
    verb = {"start": "start", "stop": "stop", "restart": "restart"}[action]
    return _run_cmd([DOCKER_EXE, verb, container], timeout=40)


def _comfy_running():
    return _tcp_up("127.0.0.1", 8188, timeout=0.5)


def _start_comfyui():
    global _COMFY_PROC
    if _comfy_running():
        return True, "already running"
    if not os.path.exists(COMFY_PY):
        return False, "ComfyUI python not found at %s" % COMFY_PY
    args = [
        COMFY_PY, "-s", os.path.join("ComfyUI", "main.py"),
        "--windows-standalone-build", "--listen", "0.0.0.0", "--port", "8188",
    ]
    try:
        logf = open(COMFY_LOG, "ab")
        _COMFY_PROC = subprocess.Popen(
            args, cwd=COMFY_DIR, stdout=logf, stderr=logf, stdin=subprocess.DEVNULL,
            creationflags=_DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP,
            close_fds=True,
        )
    except Exception as e:
        return False, "launch failed: %s" % e
    # Give it a moment to bind; it finishes warming up async.
    for _ in range(20):
        time.sleep(0.5)
        if _comfy_running():
            return True, "started (pid %s)" % _COMFY_PROC.pid
    return True, "launching (pid %s, not yet listening)" % _COMFY_PROC.pid


def _stop_comfyui():
    global _COMFY_PROC
    killed = []
    # Prefer the handle we started; fall back to whatever holds :8188.
    pids = set()
    if _COMFY_PROC is not None and _COMFY_PROC.poll() is None:
        pids.add(_COMFY_PROC.pid)
    try:
        for c in psutil.net_connections(kind="inet"):
            if c.laddr and c.laddr.port == 8188 and c.status == psutil.CONN_LISTEN and c.pid:
                pids.add(c.pid)
    except Exception:
        pass
    for pid in pids:
        try:
            p = psutil.Process(pid)
            for ch in p.children(recursive=True):
                try:
                    ch.terminate()
                except Exception:
                    pass
            p.terminate()
            killed.append(pid)
        except Exception:
            pass
    _COMFY_PROC = None
    if not pids:
        return False, "ComfyUI not running"
    return True, "stopped pid(s) %s" % ", ".join(str(p) for p in killed)


@app.post("/api/services/{svc_id}/{action}")
def api_service_action(svc_id: str, action: str):
    if action not in ("start", "stop", "restart"):
        return _api_error("invalid action '%s'" % action, status_code=400)
    if svc_id not in CONTROLLABLE:
        return _api_error("service '%s' is not controllable" % svc_id, status_code=400)

    if svc_id in DOCKER_CONTAINERS:
        ok, out = _docker(action, DOCKER_CONTAINERS[svc_id])
    elif svc_id == "comfyui":
        if action == "start":
            ok, out = _start_comfyui()
        elif action == "stop":
            ok, out = _stop_comfyui()
        else:  # restart
            _stop_comfyui()
            time.sleep(1.0)
            ok, out = _start_comfyui()
    else:  # pragma: no cover - guarded by CONTROLLABLE above
        ok, out = False, "unhandled service"

    if not ok:
        return _api_error(out or "action failed", status_code=502)
    return {"ok": True, "id": svc_id, "action": action, "output": out}


@app.get("/api/remote/status")
def api_remote_status():
    now = time.time()
    if _REMOTE_CACHE["out"] is not None and now - _REMOTE_CACHE["t"] < 5:
        out = _REMOTE_CACHE["out"]
        return out if out.get("ok") else _api_error(out.get("error", "remote console unavailable"))
    try:
        out = {"ok": True, "data": _http_json(REMOTE_CONSOLE + "/api/status", timeout=6)}
    except Exception as e:
        out = {"ok": False, "error": str(e)}
    _REMOTE_CACHE["out"] = out
    _REMOTE_CACHE["t"] = now
    return out if out.get("ok") else _api_error(out.get("error", "remote console unavailable"))


@app.get("/api/ollama/models")
def api_ollama_models():
    try:
        tags = _http_json(OLLAMA + "/api/tags", timeout=5)
    except Exception as e:
        return _api_error(e)
    loaded = []
    try:
        loaded = _http_json(OLLAMA + "/api/ps", timeout=5).get("models") or []
    except Exception:
        pass
    models = []
    for m in tags.get("models") or []:
        det = m.get("details") or {}
        models.append({
            "name": m.get("name"),
            "size_gb": round((m.get("size") or 0) / (1024 ** 3), 1),
            "modified": m.get("modified_at"),
            "family": det.get("family"),
            "params": det.get("parameter_size"),
            "quant": det.get("quantization_level"),
        })
    return {"ok": True, "models": models, "loaded": loaded}


class OllamaModelRequest(BaseModel):
    model: str


def _shared_ctx_options(model, options=None):
    """Same model, same num_ctx, everywhere — or Ollama reloads it."""
    out = dict(options or {})
    if out.get("num_ctx"):
        return out
    try:
        acfg = (messaging.load_config().get("auto_reply") or {})
        want = int(acfg.get("num_ctx") or 0)
        if want and str(model or "") in (str(acfg.get("model") or ""),
                                         str(acfg.get("assistant_model") or ""),
                                         str(acfg.get("deep_model") or ""),
                                         str(acfg.get("vision_model") or "")):
            out["num_ctx"] = want
    except Exception:
        pass
    return out


def _shared_keep_alive(model, default="30m"):
    """2 h for a model the reply pipeline is living on, 30 min for anything else.

    Ollama honours the keep_alive of the LAST request, so the dashboard's own
    "30m" was rescheduling the eviction of the model messaging had pinned for
    two hours — after which the next iMessage paid a 40 s cold load. A model
    the console alone is using still expires quickly; there is no reason to
    hold 17 GB of VRAM for a one-off question in the UI.
    """
    try:
        acfg = (messaging.load_config().get("auto_reply") or {})
        lanes = {str(acfg.get(k) or "") for k in
                 ("model", "assistant_model", "deep_model", "vision_model",
                  "chat_think_model", "pipeline_model", "translate_model")}
        return "2h" if str(model or "") in (lanes - {""}) else default
    except Exception:
        return default


@app.post("/api/ollama/load")
def api_ollama_load(body: OllamaModelRequest):
    # Empty prompt = Ollama loads the model into VRAM and returns.
    try:
        _http_json(OLLAMA + "/api/generate", timeout=180,
                   payload={"model": body.model,
                            "keep_alive": _shared_keep_alive(body.model)})
        return {"ok": True}
    except Exception as e:
        return _api_error(e)


@app.post("/api/ollama/unload")
def api_ollama_unload(body: OllamaModelRequest):
    try:
        _http_json(OLLAMA + "/api/generate", timeout=30,
                   payload={"model": body.model, "keep_alive": 0})
        return {"ok": True}
    except Exception as e:
        return _api_error(e)


class ChatRequest(BaseModel):
    model: str
    messages: list
    options: dict | None = None
    tools: list | None = None
    format: dict | str | None = None
    think: bool | None = None
    agent_tools: bool | None = None
    allow_downloads: bool = False
    allow_goal_actions: bool = False
    allow_external_actions: bool = False
    # The dashboard Assistant tab was a raw model passthrough with no persona at
    # all, so the same assistant had a character over iMessage and none in the
    # browser. Opt-in so nothing else calling this route changes behaviour.
    persona: bool = False


def _last_user_text(messages):
    for msg in reversed(messages or []):
        if not isinstance(msg, dict) or str(msg.get("role") or "") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict):
                    text = item.get("text") or item.get("content")
                    if isinstance(text, str):
                        parts.append(text)
            return " ".join(parts).strip()
        return str(content or "").strip()
    return ""


_AGENT_GOAL_WRITE_TOOLS = {
    "add_goal", "update_goal", "complete_goal", "defer_goal",
    "cancel_goal", "note_goal_surfaced",
}


def _agent_allowed_tools(allow_downloads: bool = False,
                         allow_goal_actions: bool = False):
    blocked = set()
    if not allow_downloads:
        blocked.add("download_file")
    if not allow_goal_actions:
        blocked.update(_AGENT_GOAL_WRITE_TOOLS)
    if not blocked:
        return None
    return tuple(t.name for t in agent_tools.default_registry().all()
                 if t.name not in blocked)


@app.post("/api/ollama/chat")
def api_ollama_chat(body: ChatRequest):
    if not body.model.strip():
        return _api_error("model is required", status_code=400)
    if not isinstance(body.messages, list):
        return _api_error("messages must be a list", status_code=400)

    messages = list(body.messages)
    if body.persona:
        # Read per request, like the messaging lane does, so editing the file
        # takes effect without a restart.
        try:
            ppath = messaging.ROOT / "config" / "assistant_persona.md"
            ptext = ppath.read_text(encoding="utf-8").strip()
        except Exception:
            ptext = ""
        if ptext:
            # in front of any memory-injection system message the client added
            messages = [{"role": "system", "content": ptext}] + messages

    _opts = _shared_ctx_options(body.model, body.options)
    try:
        acfg = (messaging.load_config().get("auto_reply") or {})
    except Exception:
        acfg = {}
    use_agent = body.agent_tools
    if use_agent is None:
        use_agent = bool(
            body.persona
            and not any(isinstance(m, dict) and m.get("images") for m in messages)
            and str(acfg.get("assistant_agent_tools") or "active").lower() == "active"
        )
    if use_agent:
        def gen_agent():
            rgb.note_activity("chat", True, ttl=600.0)
            try:
                audit_id = "chat-" + uuid.uuid4().hex[:16]
                downloads_allowed = bool(
                    body.allow_downloads or acfg.get("assistant_agent_downloads"))
                run = qwen_agent.run_agent_turn(
                    conversation=messages,
                    model=body.model,
                    ollama_url=OLLAMA,
                    options=_opts,
                    tool_policy=qwen_agent.ToolPolicy(
                        allowed_tools=_agent_allowed_tools(
                            downloads_allowed, body.allow_goal_actions),
                        allow_private=True,
                        allow_local_write=bool(downloads_allowed
                                               or body.allow_goal_actions),
                        allow_external_write=bool(body.allow_external_actions),
                    ),
                    interface_context=qwen_agent.InterfaceContext(
                        interface="dashboard_assistant",
                        actor="dashboard_assistant",
                        audit_id=audit_id,
                        turn_text=_last_user_text(messages),
                    ),
                )
                yield (json.dumps({
                    "message": {"role": "assistant", "content": run.answer},
                    "agent": run.metrics(),
                    "done": False,
                }, ensure_ascii=False) + "\n").encode("utf-8")
                final = {"done": True, "done_reason": "stop", "agent": run.metrics()}
                if isinstance(run.final_raw, dict):
                    for key in ("eval_count", "eval_duration", "prompt_eval_count",
                                "prompt_eval_duration", "load_duration"):
                        if key in run.final_raw:
                            final[key] = run.final_raw[key]
                yield (json.dumps(final, ensure_ascii=False) + "\n").encode("utf-8")
            except GeneratorExit:
                raise
            except Exception as exc:
                yield (json.dumps({
                    "error": str(exc),
                    "done": True,
                    "done_reason": "error",
                }, ensure_ascii=False) + "\n").encode("utf-8")
            finally:
                rgb.note_activity("chat", False)

        return StreamingResponse(gen_agent(), media_type="application/x-ndjson")

    payload = {"model": body.model, "messages": messages, "stream": True,
               "keep_alive": _shared_keep_alive(body.model),
               "think": bool(body.think) if body.think is not None else False}
    if _opts:
        payload["options"] = _opts
    if body.tools:
        payload["tools"] = body.tools
    if body.format is not None:
        payload["format"] = body.format
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(OLLAMA + "/api/chat", data=data)
    req.add_header("Content-Type", "application/json")

    def gen():
        # Keep the upstream handle so a client disconnect (user hits Stop)
        # can close it instead of leaking the Ollama generation.
        resp = None
        saw_done = False
        # Case lighting pulses while the model is generating (rgb "ai" mode).
        # The ttl means a stream that dies without reaching the finally block
        # still clears itself.
        rgb.note_activity("chat", True, ttl=600.0)
        try:
            resp = urllib.request.urlopen(req, timeout=300)
            while True:
                line = resp.readline()
                if not line:
                    break
                try:
                    item = json.loads(line.decode("utf-8", errors="replace"))
                    if item.get("done"):
                        saw_done = True
                        if not item.get("done_reason"):
                            item["done_reason"] = "stop"
                        line = (json.dumps(item) + "\n").encode("utf-8")
                except Exception:
                    pass
                yield line
            if not saw_done:
                yield (json.dumps({"done": True, "done_reason": "upstream_eof"}) + "\n").encode("utf-8")
        except GeneratorExit:
            # Client went away mid-stream; close upstream and stop quietly.
            raise
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                raw = e.read().decode("utf-8", errors="replace")
                parsed = json.loads(raw)
                detail = parsed.get("error") or raw
            except Exception:
                detail = str(e)
            yield (json.dumps({
                "error": detail or str(e),
                "done": True,
                "done_reason": "error",
            }) + "\n").encode("utf-8")
        except Exception as e:
            yield (json.dumps({
                "error": str(e),
                "done": True,
                "done_reason": "error",
            }) + "\n").encode("utf-8")
        finally:
            rgb.note_activity("chat", False)
            if resp is not None:
                try:
                    resp.close()
                except Exception:
                    pass

    return StreamingResponse(gen(), media_type="application/x-ndjson")


class TtsRequest(BaseModel):
    text: str
    pitch: float | None = None


class OpenAISpeechRequest(BaseModel):
    """OpenAI /v1/audio/speech shape, so Open WebUI can speak through the rig.

    Open WebUI's "OpenAI" TTS engine posts exactly this; pointing its Audio
    settings at http://host.docker.internal:8765/v1 gives its Call mode the
    same local voices the iMessage bot uses. Deliberately NOT under /api/ —
    that prefix blocks non-GET from anything outside loopback/Tailscale, and
    Open WebUI talks to us from the Docker bridge (172.x).
    """
    input: str
    model: str | None = None
    voice: str | None = None
    response_format: str | None = None
    speed: float | None = None


@app.get("/v1/models")
def api_openai_models():
    """OpenAI-shaped model list, so Open WebUI can enumerate through us."""
    try:
        with urllib.request.urlopen(OLLAMA + "/api/tags", timeout=15) as r:
            tags = json.loads(r.read().decode("utf-8"))
    except Exception as exc:
        return _api_error("ollama unreachable: %s" % exc, status_code=502)
    return {"object": "list", "data": [
        {"id": m.get("name"), "object": "model", "created": 0, "owned_by": "ollama"}
        for m in (tags.get("models") or [])]}


class OpenAIChatRequest(BaseModel):
    model: str
    messages: list
    stream: bool | None = None
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    tools: list | None = None
    tool_choice: dict | str | None = None
    agent_tools: bool | None = None
    allow_downloads: bool = False
    allow_goal_actions: bool = False
    allow_external_actions: bool = False


@app.post("/v1/chat/completions")
def api_openai_chat(body: OpenAIChatRequest):
    """OpenAI-compatible chat that FORCES think:false on the way to Ollama.

    Not under /api/ on purpose — that prefix rejects non-GET from outside
    loopback/Tailscale, and Open WebUI reaches us from the Docker bridge.
    """
    messages = list(body.messages or [])
    if str(body.model or "").lower().startswith("antons"):
        try:
            ptext = (messaging.ROOT / "config" / "assistant_persona.md").read_text(
                encoding="utf-8").strip()
        except Exception:
            ptext = ""
        if ptext:
            extra = [str(m.get("content") or "") for m in messages
                     if str(m.get("role")) == "system"]
            merged = ptext
            try:
                _last = ""
                for m in reversed(messages):
                    if str(m.get("role")) == "user":
                        _last = str(m.get("content") or "")[:300]
                        break
                if _last:
                    _q = _last
                    _names = ((messaging.load_config().get("auto_reply") or {})
                              .get("contact_names") or {})
                    _fq = messaging._fold_lv(_last).lower()
                    for _n in set(_names.values()):
                        _f = str(_n or "").split()[0] if _n else ""
                        if len(_f) >= 4 and messaging._fold_lv(_f)[:4].lower() in _fq:
                            _q += " " + _f
                    _mem = (ai_stack.memory_context(q=_q, limit=6) or {})
                    _mtext = str(_mem.get("text") or "").strip()
                    if _mtext:
                        merged += "\n\n" + _mtext
            except Exception:
                pass
            if any(x.strip() for x in extra):
                merged += "\n\n" + "\n\n".join(x for x in extra if x.strip())
            messages = ([{"role": "system", "content": merged}]
                        + [m for m in messages if str(m.get("role")) != "system"])
    made = int(time.time())
    cid = "chatcmpl-%s" % uuid.uuid4().hex[:24]
    # NO keep_alive here meant Ollama's own default of 5 MINUTES, and Ollama
    # applies the newest value to the resident runner: one request through
    # this proxy re-armed the messaging lane's 2 h pin to expire in five, and
    # the next iMessage after that paid a 40 s cold load for it.
    payload = {"model": body.model, "messages": messages,
               "keep_alive": _shared_keep_alive(body.model),
               "think": False, "stream": bool(body.stream)}
    options = {}
    if body.temperature is not None:
        options["temperature"] = body.temperature
    if body.top_p is not None:
        options["top_p"] = body.top_p
    if body.max_tokens is not None:
        options["num_predict"] = body.max_tokens
    options = _shared_ctx_options(body.model, options)
    if options:
        payload["options"] = options
    if body.agent_tools:
        try:
            run = qwen_agent.run_agent_turn(
                conversation=messages,
                model=body.model,
                ollama_url=OLLAMA,
                options=options,
                tool_policy=qwen_agent.ToolPolicy(
                    allowed_tools=_agent_allowed_tools(
                        body.allow_downloads, body.allow_goal_actions),
                    allow_private=str(body.model or "").lower().startswith("antons"),
                    allow_local_write=bool(body.allow_downloads
                                           or body.allow_goal_actions),
                    allow_external_write=bool(body.allow_external_actions),
                ),
                interface_context=qwen_agent.InterfaceContext(
                    interface="openai_proxy",
                    actor="openai_proxy",
                    audit_id="oai-" + uuid.uuid4().hex[:16],
                    turn_text=_last_user_text(messages),
                ),
            )
        except Exception as exc:
            return _api_error("agent chat failed: %s" % exc, status_code=502)
        if not body.stream:
            return {"id": cid, "object": "chat.completion", "created": made,
                    "model": body.model,
                    "choices": [{"index": 0, "finish_reason": "stop",
                                 "message": {"role": "assistant",
                                             "content": run.answer}}],
                    "agent": run.metrics()}

        def gen_agent_oai():
            yield ("data: " + json.dumps({
                "id": cid,
                "object": "chat.completion.chunk",
                "created": made,
                "model": body.model,
                "choices": [{"index": 0,
                             "delta": {"role": "assistant",
                                       "content": run.answer},
                             "finish_reason": None}],
                "agent": run.metrics(),
            }, ensure_ascii=False) + "\n\n").encode("utf-8")
            yield ("data: " + json.dumps({
                "id": cid,
                "object": "chat.completion.chunk",
                "created": made,
                "model": body.model,
                "choices": [{"index": 0, "delta": {},
                             "finish_reason": "stop"}],
                "agent": run.metrics(),
            }, ensure_ascii=False) + "\n\n").encode("utf-8")
            yield b"data: [DONE]\n\n"

        return StreamingResponse(gen_agent_oai(), media_type="text/event-stream")
    if body.tools:
        payload["tools"] = body.tools
    if body.tool_choice:
        payload["tool_choice"] = body.tool_choice
    req = urllib.request.Request(
        OLLAMA + "/api/chat", data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"})

    if not body.stream:
        try:
            with urllib.request.urlopen(req, timeout=900) as r:
                data = json.loads(r.read().decode("utf-8"))
        except Exception as exc:
            return _api_error("ollama chat failed: %s" % exc, status_code=502)
        msg = data.get("message") or {}
        out_msg = {"role": "assistant", "content": msg.get("content") or ""}
        if msg.get("tool_calls"):
            out_msg["tool_calls"] = msg.get("tool_calls")
        return {"id": cid, "object": "chat.completion", "created": made,
                "model": body.model,
                "choices": [{"index": 0,
                             "finish_reason": "tool_calls" if msg.get("tool_calls") else "stop",
                             "message": out_msg}]}

    def gen():
        resp = None
        try:
            resp = urllib.request.urlopen(req, timeout=900)
            first = True
            while True:
                line = resp.readline()
                if not line:
                    break
                try:
                    item = json.loads(line.decode("utf-8", errors="replace"))
                except Exception:
                    continue
                piece = (item.get("message") or {}).get("content") or ""
                delta = {"content": piece}
                if first:
                    delta["role"] = "assistant"
                    first = False
                tool_calls = (item.get("message") or {}).get("tool_calls")
                if tool_calls:
                    delta["tool_calls"] = tool_calls
                if piece or tool_calls:
                    yield ("data: " + json.dumps({
                        "id": cid, "object": "chat.completion.chunk",
                        "created": made, "model": body.model,
                        "choices": [{"index": 0, "delta": delta,
                                     "finish_reason": None}]}) + "\n\n").encode("utf-8")
                if item.get("done"):
                    yield ("data: " + json.dumps({
                        "id": cid, "object": "chat.completion.chunk",
                        "created": made, "model": body.model,
                        "choices": [{"index": 0, "delta": {},
                                     "finish_reason": "tool_calls" if tool_calls else "stop"}]}) + "\n\n").encode("utf-8")
                    break
            yield b"data: [DONE]\n\n"
        except GeneratorExit:
            raise
        except Exception as exc:
            yield ("data: " + json.dumps({"error": str(exc)}) + "\n\n").encode("utf-8")
            yield b"data: [DONE]\n\n"
        finally:
            try:
                if resp is not None:
                    resp.close()
            except Exception:
                pass

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.post("/v1/audio/speech")
def api_openai_speech(body: OpenAISpeechRequest):
    text = (body.input or "").strip()
    if not text:
        return _api_error("input is required", status_code=400)
    # "alloy" and friends are OpenAI's names — Open WebUI sends whatever is
    # typed in its voice box, so only pass through names Kokoro actually has.
    voice = (body.voice or "").strip() or None
    if voice and not re.match(r"^[abehij][fm]_[a-z]+$", voice):
        voice = None
    try:
        data, ext = messaging.synth_speech(text, 1.0, voice=voice)
    except Exception as exc:
        return _api_error("tts failed: %s" % exc, status_code=502)
    if not data:
        return _api_error("voice not ready yet — try again in a moment",
                          status_code=503)
    want = (body.response_format or "mp3").lower()
    if want == "mp3" and ext == "wav":
        conv = messaging.wav_to_mp3(data)
        if conv:
            data, ext = conv, "mp3"
    ctype = {"mp3": "audio/mpeg", "wav": "audio/wav",
             "m4a": "audio/mp4"}.get(ext, "application/octet-stream")
    return Response(content=data, media_type=ctype)


@app.post("/api/tts")
def api_tts(body: TtsRequest):
    text = (body.text or "").strip()
    if not text:
        return _api_error("text is required", status_code=400)
    try:
        data, ext = messaging.synth_speech(text, body.pitch if body.pitch else 1.0)
    except Exception as exc:  # never 500 the UI over a voice clip
        return _api_error("tts failed: %s" % exc, status_code=502)
    if not data:
        return _api_error("voice not ready yet — try again in a moment",
                          status_code=503)
    ctype = "audio/mp4" if ext == "m4a" else "audio/wav"
    return Response(content=data, media_type=ctype,
                    headers={"Cache-Control": "no-store"})



SDXL_CKPT = "sd_xl_base_1.0.safetensors"
SDXL_SAMPLERS = {"euler", "euler_ancestral", "dpmpp_2m", "dpmpp_2m_sde", "dpmpp_3m_sde", "dpmpp_sde"}
SDXL_SCHEDULERS = {"normal", "karras", "exponential", "simple", "sgm_uniform"}
LTX_CKPT = "ltxv-13b-0.9.8-dev-fp8.safetensors"
LTX_T5 = "t5xxl_fp8_e4m3fn_scaled.safetensors"
LTX_NEG = "worst quality, blurry, jittery, distorted"

# Z-Image (Tongyi, Apache-2.0) — the quality upgrade over sd_xl_base on 24 GB.
# Node graph + settings lifted from the official ComfyUI templates
# image_z_image.json / image_z_image_turbo.json (templates pkg 0.9.73).
COMFY_MODELS_DIR = os.path.join(COMFY_DIR, "ComfyUI", "models")
ZIMAGE_UNET = "z_image_bf16.safetensors"
ZIMAGE_TURBO_UNET = "z_image_turbo_bf16.safetensors"
ZIMAGE_CLIP = "qwen_3_4b.safetensors"
ZIMAGE_VAE = "z_image_ae.safetensors"
STUDIO_QUALITIES = {"fast", "good", "best"}


def _zimage_available(turbo):
    unet = ZIMAGE_TURBO_UNET if turbo else ZIMAGE_UNET
    return (os.path.isfile(os.path.join(COMFY_MODELS_DIR, "diffusion_models", unet))
            and os.path.isfile(os.path.join(COMFY_MODELS_DIR, "text_encoders", ZIMAGE_CLIP))
            and os.path.isfile(os.path.join(COMFY_MODELS_DIR, "vae", ZIMAGE_VAE)))


# Qwen-Image-Edit 2511 (instruction photo editing, multi-image: "put the person
# from image 2 into image 1"). Graph follows the official ComfyUI template
# image_qwen_image_edit_2511.json with the Lightning 4-step LoRA always on —
# 40-step base sampling on a 20B model is not practical on the 3090.
QWEN_EDIT_UNET = "qwen_image_edit_2511_fp8mixed.safetensors"
QWEN_EDIT_CLIP = "qwen_2.5_vl_7b_fp8_scaled.safetensors"
QWEN_EDIT_VAE = "qwen_image_vae.safetensors"
QWEN_EDIT_LORA = "Qwen-Image-Edit-2511-Lightning-4steps-V1.0-bf16.safetensors"
COMFY_INPUT_DIR = os.path.join(COMFY_DIR, "ComfyUI", "input")


def _qwen_edit_available():
    return (os.path.isfile(os.path.join(COMFY_MODELS_DIR, "diffusion_models", QWEN_EDIT_UNET))
            and os.path.isfile(os.path.join(COMFY_MODELS_DIR, "text_encoders", QWEN_EDIT_CLIP))
            and os.path.isfile(os.path.join(COMFY_MODELS_DIR, "vae", QWEN_EDIT_VAE))
            and os.path.isfile(os.path.join(COMFY_MODELS_DIR, "loras", QWEN_EDIT_LORA)))


def _safe_comfy_name(value, default):
    value = (value or "").strip().replace("\\", "/")
    if not value or value.startswith("/") or ".." in value or len(value) > 240:
        return default
    return value


def _sdxl_source_size(width, height):
    # SDXL renders ~1MP; anything bigger is rendered small and lanczos-upscaled
    if width * height <= 1100000 and width <= 1536 and height <= 1536:
        return width, height
    scale = min(1536.0 / max(width, height), (1050000.0 / (width * height)) ** 0.5)
    return max(512, int(width * scale) // 8 * 8), max(512, int(height * scale) // 8 * 8)


def _sdxl_graph(
    prompt,
    negative,
    seed,
    width=1024,
    height=1024,
    steps=25,
    cfg=7.0,
    checkpoint=SDXL_CKPT,
    sampler="dpmpp_2m_sde",
    scheduler="karras",
):
    checkpoint = _safe_comfy_name(checkpoint, SDXL_CKPT)
    sampler = sampler if sampler in SDXL_SAMPLERS else "dpmpp_2m_sde"
    scheduler = scheduler if scheduler in SDXL_SCHEDULERS else "karras"
    source_width, source_height = _sdxl_source_size(width, height)
    graph = {
        "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": checkpoint}},
        "5": {"class_type": "EmptyLatentImage", "inputs": {"width": source_width, "height": source_height, "batch_size": 1}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["4", 1]}},
        "7": {"class_type": "CLIPTextEncode", "inputs": {"text": negative, "clip": ["4", 1]}},
        "3": {"class_type": "KSampler", "inputs": {
            "seed": seed, "steps": steps, "cfg": cfg, "sampler_name": sampler,
            "scheduler": scheduler, "denoise": 1.0, "model": ["4", 0],
            "positive": ["6", 0], "negative": ["7", 0], "latent_image": ["5", 0]}},
        "8": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["4", 2]}},
        "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "console", "images": ["8", 0]}},
    }
    if source_width != width or source_height != height:
        graph["10"] = {
            "class_type": "ImageScale",
            "inputs": {
                "image": ["8", 0],
                "upscale_method": "lanczos",
                "width": width,
                "height": height,
                "crop": "disabled",
            },
        }
        graph["9"]["inputs"]["images"] = ["10", 0]
    return graph


def _zimage_source_size(width, height):
    # Z-Image renders well up to ~2.3MP at /16 dims; bigger wallpaper targets
    # (1440p, MacBook 3024x1964, iPhone 1260x2736) render at an aspect-matched
    # ~2.3MP source and get a modest lanczos scale to the exact target.
    if width * 9 == height * 16 and width >= 1920:
        return 2048, 1152  # exact 16:9 bucket for FHD/1440p
    if width % 16 == 0 and height % 16 == 0 and width * height <= 2360000:
        return width, height
    scale = min(1.0, 2048.0 / max(width, height), (2360000.0 / (width * height)) ** 0.5)
    src_w = int(width * scale / 16.0 + 0.5) * 16
    src_h = int(height * scale / 16.0 + 0.5) * 16
    return max(512, min(2048, src_w)), max(512, min(2048, src_h))


def _zimage_graph(prompt, negative, seed, width, height, steps, cfg, turbo):
    source_width, source_height = _zimage_source_size(width, height)
    graph = {
        "1": {"class_type": "UNETLoader", "inputs": {
            "unet_name": ZIMAGE_TURBO_UNET if turbo else ZIMAGE_UNET, "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {
            "clip_name": ZIMAGE_CLIP, "type": "lumina2", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": ZIMAGE_VAE}},
        "4": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["2", 0]}},
        "6": {"class_type": "EmptySD3LatentImage", "inputs": {
            "width": source_width, "height": source_height, "batch_size": 1}},
        "7": {"class_type": "ModelSamplingAuraFlow", "inputs": {"shift": 3, "model": ["1", 0]}},
        "8": {"class_type": "KSampler", "inputs": {
            "seed": seed, "steps": steps, "cfg": cfg, "sampler_name": "res_multistep",
            "scheduler": "simple", "denoise": 1.0, "model": ["7", 0],
            "positive": ["4", 0], "negative": ["5", 0], "latent_image": ["6", 0]}},
        "9": {"class_type": "VAEDecode", "inputs": {"samples": ["8", 0], "vae": ["3", 0]}},
        "11": {"class_type": "SaveImage", "inputs": {"filename_prefix": "console", "images": ["9", 0]}},
    }
    if turbo:
        # distilled model runs at cfg 1: negative prompt has no effect, zero it out
        graph["5"] = {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["4", 0]}}
    else:
        graph["5"] = {"class_type": "CLIPTextEncode", "inputs": {"text": negative, "clip": ["2", 0]}}
    if source_width != width or source_height != height:
        graph["10"] = {"class_type": "ImageScale", "inputs": {
            "image": ["9", 0], "upscale_method": "lanczos",
            "width": width, "height": height, "crop": "disabled"}}
        graph["11"]["inputs"]["images"] = ["10", 0]
    return graph


def _clamp_int(value, default, low, high, multiple=None):
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = default
    n = max(low, min(high, n))
    if multiple:
        n = max(low, (n // multiple) * multiple)
    return n


def _clamp_float(value, default, low, high):
    try:
        n = float(value)
    except (TypeError, ValueError):
        n = default
    return max(low, min(high, n))


def _ltx_graph(prompt, seconds=6.0, seed=None):
    length = max(25, min(257, ((round(seconds * 25) + 7) // 8) * 8 + 1))  # valid 8n+1
    if seed is None:
        seed = int(time.time()) % 100000
    return {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": LTX_CKPT}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": LTX_T5, "type": "ltxv"}},
        "3": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["2", 0]}},
        "4": {"class_type": "CLIPTextEncode", "inputs": {"text": LTX_NEG, "clip": ["2", 0]}},
        "5": {"class_type": "EmptyLTXVLatentVideo", "inputs": {"width": 1216, "height": 704, "length": length, "batch_size": 1}},
        "6": {"class_type": "LTXVConditioning", "inputs": {"positive": ["3", 0], "negative": ["4", 0], "frame_rate": 25.0}},
        "7": {"class_type": "ModelSamplingLTXV", "inputs": {"model": ["1", 0], "max_shift": 2.05, "base_shift": 0.95, "latent": ["5", 0]}},
        "8": {"class_type": "LTXVScheduler", "inputs": {"steps": 40, "max_shift": 2.05, "base_shift": 0.95, "stretch": True, "terminal": 0.1, "latent": ["5", 0]}},
        "9": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "euler"}},
        "10": {"class_type": "SamplerCustom", "inputs": {
            "model": ["7", 0], "add_noise": True, "noise_seed": seed, "cfg": 4.0,
            "positive": ["6", 0], "negative": ["6", 1], "sampler": ["9", 0],
            "sigmas": ["8", 0], "latent_image": ["5", 0]}},
        "11": {"class_type": "VAEDecode", "inputs": {"samples": ["10", 0], "vae": ["1", 2]}},
        "12": {"class_type": "SaveWEBM", "inputs": {"images": ["11", 0], "filename_prefix": "video", "codec": "vp9", "fps": 25.0, "crf": 18.0}},
    }


def _qwen_edit_graph(prompt, image1, image2, seed, out_width=None, out_height=None):
    graph = {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": QWEN_EDIT_UNET, "weight_dtype": "default"}},
        "2": {"class_type": "LoraLoaderModelOnly", "inputs": {
            "model": ["1", 0], "lora_name": QWEN_EDIT_LORA, "strength_model": 1.0}},
        "3": {"class_type": "ModelSamplingAuraFlow", "inputs": {"shift": 3.1, "model": ["2", 0]}},
        "4": {"class_type": "CFGNorm", "inputs": {"model": ["3", 0], "strength": 1.0}},
        "5": {"class_type": "CLIPLoader", "inputs": {
            "clip_name": QWEN_EDIT_CLIP, "type": "qwen_image", "device": "default"}},
        "6": {"class_type": "VAELoader", "inputs": {"vae_name": QWEN_EDIT_VAE}},
        "7": {"class_type": "LoadImage", "inputs": {"image": image1}},
        "8": {"class_type": "FluxKontextImageScale", "inputs": {"image": ["7", 0]}},
        "10": {"class_type": "VAEEncode", "inputs": {"pixels": ["8", 0], "vae": ["6", 0]}},
        "11": {"class_type": "TextEncodeQwenImageEditPlus", "inputs": {
            "clip": ["5", 0], "prompt": prompt, "vae": ["6", 0], "image1": ["8", 0]}},
        "12": {"class_type": "TextEncodeQwenImageEditPlus", "inputs": {
            "clip": ["5", 0], "prompt": "", "vae": ["6", 0], "image1": ["8", 0]}},
        "13": {"class_type": "FluxKontextMultiReferenceLatentMethod", "inputs": {
            "conditioning": ["11", 0], "reference_latents_method": "index_timestep_zero"}},
        "14": {"class_type": "FluxKontextMultiReferenceLatentMethod", "inputs": {
            "conditioning": ["12", 0], "reference_latents_method": "index_timestep_zero"}},
        "15": {"class_type": "KSampler", "inputs": {
            "seed": seed, "steps": 4, "cfg": 1.0, "sampler_name": "euler", "scheduler": "simple",
            "denoise": 1.0, "model": ["4", 0], "positive": ["13", 0], "negative": ["14", 0],
            "latent_image": ["10", 0]}},
        "16": {"class_type": "VAEDecode", "inputs": {"samples": ["15", 0], "vae": ["6", 0]}},
        "17": {"class_type": "SaveImage", "inputs": {"filename_prefix": "console-edit", "images": ["16", 0]}},
    }
    if image2:
        graph["9"] = {"class_type": "LoadImage", "inputs": {"image": image2}}
        graph["11"]["inputs"]["image2"] = ["9", 0]
        graph["12"]["inputs"]["image2"] = ["9", 0]
    if out_width and out_height:
        graph["18"] = {"class_type": "ImageScale", "inputs": {
            "image": ["16", 0], "upscale_method": "lanczos",
            "width": out_width, "height": out_height, "crop": "disabled"}}
        graph["17"]["inputs"]["images"] = ["18", 0]
    return graph


def _save_comfy_input(b64, name, tag):
    data = b64.strip()
    if data.startswith("data:") and "," in data[:100]:
        data = data.split(",", 1)[1]
    raw = base64.b64decode(data)
    if not raw:
        raise ValueError("empty image upload")
    if len(raw) > 25 * 1024 * 1024:
        raise ValueError("image too large (max 25 MB)")
    ext = ".png"
    lower = (name or "").lower()
    for cand in (".png", ".jpg", ".jpeg", ".webp"):
        if lower.endswith(cand):
            ext = cand
            break
    fname = "console-%s-%s%s" % (tag, datetime.now().strftime("%Y%m%d-%H%M%S-%f"), ext)
    os.makedirs(COMFY_INPUT_DIR, exist_ok=True)
    with open(os.path.join(COMFY_INPUT_DIR, fname), "wb") as f:
        f.write(raw)
    return fname


def _comfy_submit(graph):
    res = _http_json(COMFY + "/prompt", timeout=30,
                     payload={"prompt": graph, "client_id": "rig-console"})
    pid = res.get("prompt_id")
    if not pid:
        raise RuntimeError("ComfyUI rejected the prompt: %s" % json.dumps(res)[:400])
    return pid


class ComfyImageRequest(BaseModel):
    prompt: str
    negative: str | None = None
    quality: str | None = None  # fast | good | best — the simple-UI knob
    seed: int | None = None
    checkpoint: str | None = None  # legacy explicit override -> SDXL graph
    sampler: str | None = None
    scheduler: str | None = None
    width: int | None = None
    height: int | None = None
    steps: int | None = None
    cfg: float | None = None


class ComfyVideoRequest(BaseModel):
    prompt: str
    seconds: float | None = None
    seed: int | None = None


class ComfyEditRequest(BaseModel):
    prompt: str
    photo_b64: str            # the photo to edit (data-URL or raw base64)
    photo_name: str | None = None
    ref_b64: str | None = None   # optional reference (e.g. the person to insert)
    ref_name: str | None = None
    width: int | None = None
    height: int | None = None
    seed: int | None = None


def _archive_comfy_file(file_info):
    filename = os.path.basename(str(file_info.get("filename") or ""))
    if not filename:
        return file_info
    file_type = _safe_comfy_name(file_info.get("type") or "output", "output")
    subfolder = _safe_comfy_name(file_info.get("subfolder") or "", "")
    src = os.path.abspath(os.path.join(COMFY_OUTPUT_ROOT, file_type, subfolder, filename))
    root = os.path.abspath(COMFY_OUTPUT_ROOT)
    if not src.startswith(root + os.sep) or not os.path.isfile(src):
        return file_info
    key = "%s/%s/%s" % (file_type, subfolder, filename)
    cached = _COMFY_ARCHIVE_CACHE.get(key)
    if cached and os.path.isfile(os.path.join(STUDIO_ARTIFACTS, cached)):
        file_info["artifact_url"] = "/api/studio/artifact/%s" % urllib.parse.quote(cached)
        file_info["artifact_path"] = os.path.join(STUDIO_ARTIFACTS, cached)
        return file_info
    os.makedirs(STUDIO_ARTIFACTS, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", filename).strip("-") or "studio.png"
    dest_name = "%s-%s" % (stamp, safe_name)
    dest = os.path.join(STUDIO_ARTIFACTS, dest_name)
    shutil.copyfile(src, dest)
    _COMFY_ARCHIVE_CACHE[key] = dest_name
    file_info["artifact_url"] = "/api/studio/artifact/%s" % urllib.parse.quote(dest_name)
    file_info["artifact_path"] = dest
    return file_info


@app.get("/api/studio/artifact/{filename}")
def api_studio_artifact(filename: str):
    safe = os.path.basename(filename)
    path = os.path.abspath(os.path.join(STUDIO_ARTIFACTS, safe))
    if not path.startswith(STUDIO_ARTIFACTS + os.sep) or not os.path.isfile(path):
        return _api_error("artifact not found", status_code=404)
    return FileResponse(path)


@app.get("/api/comfy/checkpoints")
def api_comfy_checkpoints():
    if not _comfy_running():
        return {"ok": True, "models": [SDXL_CKPT], "default": SDXL_CKPT, "source": "fallback"}
    try:
        info = _http_json(COMFY + "/object_info/CheckpointLoaderSimple", timeout=10)
        node = info.get("CheckpointLoaderSimple", {})
        choices = (((node.get("input") or {}).get("required") or {}).get("ckpt_name") or [[]])[0]
        models = [m for m in choices if isinstance(m, str)]
    except Exception:
        models = []
    if not models:
        models = [SDXL_CKPT]
    default = SDXL_CKPT if SDXL_CKPT in models else models[0]
    return {"ok": True, "models": models, "default": default, "source": "comfyui"}


@app.get("/api/comfy/engines")
def api_comfy_engines():
    return {
        "ok": True,
        "zimage_turbo": _zimage_available(True),
        "zimage": _zimage_available(False),
        "qwen_edit": _qwen_edit_available(),
        "sdxl": os.path.isfile(os.path.join(COMFY_MODELS_DIR, "checkpoints", SDXL_CKPT)),
    }


# quality -> (steps, cfg) for the Z-Image base model; turbo is pinned at 8/1.0
_ZIMAGE_QUALITY = {"fast": (20, 4.0), "good": (25, 4.0), "best": (40, 4.0)}
# quality -> (steps, cfg, sampler, scheduler) for the SDXL fallback
_SDXL_QUALITY = {
    "fast": (18, 5.5, "euler", "normal"),
    "good": (30, 6.5, "dpmpp_2m_sde", "karras"),
    "best": (45, 7.0, "dpmpp_2m_sde", "karras"),
}


@app.post("/api/comfy/image")
def api_comfy_image(body: ComfyImageRequest):
    if not body.prompt.strip():
        return _api_error("prompt is required", status_code=400)
    if not _comfy_running():
        return _api_error("ComfyUI is not running", status_code=503)
    seed = body.seed if body.seed is not None else int(time.time() * 1000) % (2 ** 31)
    neg = body.negative or "text, watermark, low quality, blurry"
    quality = body.quality if body.quality in STUDIO_QUALITIES else "good"
    # final output dims (source render size is derived per engine; upscale is lanczos)
    width = _clamp_int(body.width, 1024, 512, 3072)
    height = _clamp_int(body.height, 1024, 512, 3072)

    # Pick the best engine we have unless the caller pinned a checkpoint (legacy API).
    engine = "sdxl"
    if body.checkpoint is None:
        if quality == "fast" and _zimage_available(True):
            engine = "z-image-turbo"
        elif _zimage_available(False):
            engine = "z-image"
        elif _zimage_available(True):
            engine = "z-image-turbo"

    if engine == "sdxl":
        q_steps, q_cfg, q_sampler, q_scheduler = _SDXL_QUALITY[quality]
        steps = _clamp_int(body.steps, q_steps, 10, 60)
        cfg = _clamp_float(body.cfg, q_cfg, 1.0, 12.0)
        checkpoint = _safe_comfy_name(body.checkpoint, SDXL_CKPT)
        sampler = body.sampler if body.sampler in SDXL_SAMPLERS else q_sampler
        scheduler = body.scheduler if body.scheduler in SDXL_SCHEDULERS else q_scheduler
        source_width, source_height = _sdxl_source_size(width, height)
        graph = _sdxl_graph(
            body.prompt, neg, seed,
            width=width, height=height, steps=steps, cfg=cfg,
            checkpoint=checkpoint, sampler=sampler, scheduler=scheduler,
        )
    else:
        turbo = engine == "z-image-turbo"
        if turbo:
            steps, cfg = 8, 1.0
        else:
            q_steps, q_cfg = _ZIMAGE_QUALITY[quality]
            steps = _clamp_int(body.steps, q_steps, 4, 60)
            cfg = _clamp_float(body.cfg, q_cfg, 1.0, 12.0)
        checkpoint = ZIMAGE_TURBO_UNET if turbo else ZIMAGE_UNET
        sampler, scheduler = "res_multistep", "simple"
        source_width, source_height = _zimage_source_size(width, height)
        graph = _zimage_graph(body.prompt, neg, seed, width, height, steps, cfg, turbo)

    try:
        pid = _comfy_submit(graph)
    except Exception as e:
        return _api_error(e)
    return {
        "ok": True,
        "prompt_id": pid,
        "kind": "image",
        "engine": engine,
        "quality": quality,
        "width": width,
        "height": height,
        "source_width": source_width,
        "source_height": source_height,
        "steps": steps,
        "cfg": cfg,
        "seed": seed,
        "checkpoint": checkpoint,
        "sampler": sampler,
        "scheduler": scheduler,
    }


@app.post("/api/comfy/video")
def api_comfy_video(body: ComfyVideoRequest):
    if not body.prompt.strip():
        return _api_error("prompt is required", status_code=400)
    if not _comfy_running():
        return _api_error("ComfyUI is not running", status_code=503)
    secs = body.seconds if body.seconds is not None else 6.0
    secs = max(1.0, min(10.0, secs))
    try:
        pid = _comfy_submit(_ltx_graph(body.prompt, seconds=secs, seed=body.seed))
    except Exception as e:
        return _api_error(e)
    return {"ok": True, "prompt_id": pid, "kind": "video", "seconds": secs}


@app.post("/api/comfy/edit")
def api_comfy_edit(body: ComfyEditRequest):
    if not body.prompt.strip():
        return _api_error("prompt is required", status_code=400)
    if not _comfy_running():
        return _api_error("ComfyUI is not running", status_code=503)
    if not _qwen_edit_available():
        return _api_error("edit model not installed (Qwen-Image-Edit 2511 files missing)", status_code=503)
    try:
        img1 = _save_comfy_input(body.photo_b64, body.photo_name, "photo")
        img2 = _save_comfy_input(body.ref_b64, body.ref_name, "ref") if body.ref_b64 else None
    except Exception as e:
        return _api_error(e, status_code=400)
    seed = body.seed if body.seed is not None else int(time.time() * 1000) % (2 ** 31)
    # scale the ~1MP edit result back to the original photo size (capped at 3072)
    out_w = out_h = None
    if body.width and body.height and body.width > 0 and body.height > 0:
        scale = min(1.0, 3072.0 / max(body.width, body.height))
        out_w = max(64, int(body.width * scale))
        out_h = max(64, int(body.height * scale))
    try:
        pid = _comfy_submit(_qwen_edit_graph(body.prompt, img1, img2, seed,
                                             out_width=out_w, out_height=out_h))
    except Exception as e:
        return _api_error(e)
    return {
        "ok": True,
        "prompt_id": pid,
        "kind": "edit",
        "engine": "qwen-edit-2511",
        "seed": seed,
        "width": out_w,
        "height": out_h,
        "steps": 4,
        "cfg": 1.0,
    }


@app.get("/api/comfy/result/{prompt_id}")
def api_comfy_result(prompt_id: str):
    try:
        hist = _http_json(COMFY + "/history/" + prompt_id, timeout=10)
    except Exception as e:
        return _api_error(e)
    entry = hist.get(prompt_id)
    if not entry:
        return {"ok": True, "status": "running", "prompt_id": prompt_id, "files": []}
    status = entry.get("status") or {}
    files = []
    for _node, out in (entry.get("outputs") or {}).items():
        for _key, val in out.items():
            if not isinstance(val, list):
                continue
            for item in val:
                if isinstance(item, dict) and item.get("filename"):
                    files.append(_archive_comfy_file({
                        "filename": item.get("filename"),
                        "subfolder": item.get("subfolder", ""),
                        "type": item.get("type", "output"),
                    }))
    if files:
        state = "done"
    elif status.get("status_str") == "error":
        state = "error"
    elif status.get("completed"):
        state = "done"
    else:
        state = "running"
    return {"ok": True, "status": state, "prompt_id": prompt_id, "files": files}


@app.get("/api/comfy/file")
def api_comfy_file(filename: str, subfolder: str = "", type: str = "output"):
    qs = urllib.parse.urlencode({"filename": filename, "subfolder": subfolder, "type": type})
    try:
        with urllib.request.urlopen(COMFY + "/view?" + qs, timeout=60) as r:
            content = r.read()
            ctype = r.headers.get("Content-Type", "application/octet-stream")
    except Exception as e:
        return _api_error(e)
    return Response(content=content, media_type=ctype)



class OpencodeRequest(BaseModel):
    project: str
    task: str
    mode: str | None = None
    runner: str | None = None
    confirm_run: bool | None = None


class MediaOpsRequest(BaseModel):
    message: str | None = None
    prompt: str | None = None
    task: str | None = None
    target: str | None = None
    ssh_password: str | None = None
    sudo_password: str | None = None
    deploy: bool | None = None


class MessagingSendRequest(BaseModel):
    provider: str | None = None
    target: str | None = None
    text: str | None = None
    metadata: dict | None = None
    real_send: bool | None = None


class MessagingInboundRequest(BaseModel):
    provider: str | None = None
    sender: str | None = None
    text: str | None = None
    raw: dict | None = None


class AutoReplyTestRequest(BaseModel):
    text: str | None = None
    sender: str | None = None
    chat_guid: str | None = None


class HermesMemoryRequest(BaseModel):
    kind: str | None = None
    title: str | None = None
    content: str | None = None
    source: str | None = None
    metadata: dict | None = None
    pinned: bool | None = None


class HermesToggleRequest(BaseModel):
    enabled: bool


class HermesUpdateRequest(BaseModel):
    kind: str | None = None
    title: str | None = None
    content: str | None = None
    pinned: bool | None = None
    disabled: bool | None = None


class AiReviewRequest(BaseModel):
    prompt: str | None = None
    subject: str | None = None
    provider: str | None = None
    allow_cloud: bool | None = None
    save_memory: bool | None = None


class ExecRequest(BaseModel):
    command: str | None = None
    cwd: str | None = None
    shell: str | None = None
    timeout: int | None = None
    label: str | None = None
    confirm: bool | None = None


class ExecIdRequest(BaseModel):
    id: str


class ConfirmRequest(BaseModel):
    confirm: bool | None = None


def _opencode_enabled():
    # Safe by default: the runner is an RCE surface (it runs opencode with
    # --dangerously-skip-permissions and the app has no auth). Arm it explicitly
    # with RIG_ENABLE_OPENCODE=1 on the SysMonitor service.
    return os.environ.get("RIG_ENABLE_OPENCODE", "").strip().lower() in ("1", "true", "yes", "on")


@app.get("/api/opencode/projects")
def api_opencode_projects():
    status = coder.runner_status(wsl_run_enabled=True)
    return {
        "ok": True,
        "enabled": True,
        "live_run_enabled": bool(status.get("auto_run_default")),
        "projects": coder.list_projects(),
        "modes": coder.code_mode_options(),
        "runners": status,
    }


@app.post("/api/opencode/run")
def api_opencode_run(body: OpencodeRequest):
    task = (body.task or "").strip()
    if not task:
        return _api_error("task is required", status_code=400)
    mode = coder.normalize_mode(body.mode)
    runner = coder.normalize_runner(body.runner)
    if body.project not in coder.list_projects():
        return _api_error("project not in allow-list", status_code=400)
    if runner == coder.RUNNER_WSL and not body.confirm_run:
        return _api_error("WSL OpenCode requires confirm_run=true.", status_code=400)
    def gen():
        research = []
        if coder.mode_uses_web(mode):
            yield (json.dumps({"type": "research", "text": "web search: " + task}) + "\n").encode("utf-8")
            result = coder.web_search(task)
            if result.get("ok"):
                research = result.get("results") or []
                for idx, item in enumerate(research, 1):
                    title = item.get("title") or item.get("url") or "untitled"
                    url = item.get("url") or ""
                    text = f"{idx}. {title}"
                    if url:
                        text += f" - {url}"
                    yield (json.dumps({"type": "research", "text": text}) + "\n").encode("utf-8")
            else:
                text = "web search unavailable: " + (result.get("error") or "no results")
                yield (json.dumps({"type": "research", "text": text}) + "\n").encode("utf-8")
        else:
            yield (json.dumps({"type": "research", "text": "local-only mode: web search disabled"}) + "\n").encode("utf-8")

        wrapped_task = coder.build_coding_task(task, mode, research)
        exit_code = None
        it = coder.run_opencode(
            body.project,
            wrapped_task,
            runner=runner,
            explicit_request=bool(body.confirm_run),
        )
        try:
            for evt in it:
                if evt.get("type") == "done":
                    exit_code = evt.get("code")
                yield (json.dumps(evt) + "\n").encode("utf-8")
        finally:
            it.close()

        # Rename tasks get a mechanical leftover check + one targeted second
        # pass: a 30B model reliably covers ~5 of 6 visible strings per pass.
        if exit_code == 0 and runner != coder.RUNNER_MANUAL:
            for evt in _rename_verify_events(body.project, task, runner):
                yield (json.dumps(evt) + "\n").encode("utf-8")

    return StreamingResponse(gen(), media_type="application/x-ndjson")


def _rename_verify_events(project_dir, task, runner, files=None):
    """Post-run leftover grep + targeted second pass for rename-shaped tasks.
    visible_only grep: identifier leftovers (luma-*, LUMA_*, file names) are
    expected after a branding rename and must not trigger follow-up passes."""
    rename = coder.rename_terms(task)
    if not rename:
        return
    old, new = rename
    hits = coder.grep_term(project_dir, old, visible_only=True, files=files)
    yield {"type": "verify",
           "text": "rename check: %d user-visible occurrence(s) of '%s' remain" % (len(hits), old)}
    if not hits:
        return
    for h in hits[:12]:
        yield {"type": "verify", "text": "  " + h}
    yield {"type": "verify", "text": "running a targeted follow-up pass on the leftovers..."}
    it2 = coder.run_opencode(
        project_dir,
        coder.build_rename_followup(old, new, hits),
        runner=runner,
        explicit_request=True,
    )
    try:
        for evt in it2:
            yield evt
    finally:
        it2.close()
    left = coder.grep_term(project_dir, old, visible_only=True, files=files)
    yield {"type": "verify",
           "text": "final rename check: %d user-visible occurrence(s) of '%s' remain" % (len(left), old)}



@app.get("/api/exec/config")
def api_exec_config():
    return terminal.config()


@app.post("/api/exec/stream")
def api_exec_stream(body: ExecRequest):
    if not body.confirm:
        return _api_error("exec requires confirm=true", status_code=400)

    def gen():
        try:
            for evt in terminal.run_stream(
                body.command, body.cwd, shell=body.shell,
                timeout=body.timeout or terminal.STREAM_TIMEOUT_DEFAULT,
            ):
                yield (json.dumps(evt) + "\n").encode("utf-8")
        except GeneratorExit:
            raise
        except Exception as e:
            yield (json.dumps({"type": "error", "text": str(e)}) + "\n").encode("utf-8")
            yield (json.dumps({"type": "done", "code": -1}) + "\n").encode("utf-8")

    return StreamingResponse(gen(), media_type="application/x-ndjson")


@app.post("/api/exec/start")
def api_exec_start(body: ExecRequest):
    if not body.confirm:
        return _api_error("exec requires confirm=true", status_code=400)
    result = terminal.start_detached(body.command, body.cwd, shell=body.shell, label=body.label)
    if not result.get("ok"):
        return _api_error(result.get("error", "launch failed"), status_code=result.get("status_code", 500))
    return result


@app.get("/api/exec/list")
def api_exec_list():
    return terminal.list_running()


@app.get("/api/exec/tail")
def api_exec_tail(id: str, offset: int = 0):
    result = terminal.tail(id, offset=offset)
    if not result.get("ok"):
        return _api_error(result.get("error", "tail failed"), status_code=result.get("status_code", 500))
    return result


@app.post("/api/exec/stop")
def api_exec_stop(body: ExecIdRequest):
    result = terminal.stop(body.id)
    if not result.get("ok"):
        return _api_error(result.get("error", "stop failed"), status_code=result.get("status_code", 500))
    return result


@app.post("/api/exec/clear")
def api_exec_clear():
    return terminal.clear_finished()



_NSSM = os.path.join(os.path.dirname(__file__), os.pardir, "_service", "nssm.exe")


@app.post("/api/service/restart")
def api_service_restart(body: ConfirmRequest):
    if not body.confirm:
        return _api_error("service restart requires confirm=true", status_code=400)
    # One implementation, in app/service_restart.py, so the messaging side can
    # trigger the identical restart without importing main (a cycle).
    from app import service_restart as _sr
    ok, detail = _sr.request_self_restart(reason="dashboard")
    if not ok:
        return _api_error(detail, status_code=500)
    return {"ok": True, "restarting": True,
            "note": "SysMonitor restart task is running; it will verify /api/health before finishing."}



class InvestigateRequest(BaseModel):
    question: str
    sender: str | None = None
    chat_guid: str | None = None
    steps: int | None = 5


@app.get("/api/messaging/capabilities")
def api_messaging_capabilities(refresh: bool = False):
    """What Antons can actually do right now — probed, not claimed.

    Every entry carries the reason it came out that way, so "images: false"
    also says which model has no vision. `code_write` and `deploy` are always
    false here: they are owner permissions, not properties of the machine.
    """
    from app import capabilities
    return capabilities.snapshot(force=bool(refresh))


@app.get("/api/messaging/chat-controls")
def api_messaging_chat_controls():
    """One row per configured chat: mode, which tools reach it, which risk
    tiers are structurally open there, and which persona/note source applies.
    Config-level view, not a per-message simulation — see
    messaging.chat_controls_summary's docstring for why.
    """
    acfg = (messaging.load_config().get("auto_reply") or {})
    return {"ok": True, "chats": messaging.chat_controls_summary(acfg)}


@app.post("/api/messaging/investigate")
def api_messaging_investigate(body: InvestigateRequest):
    """Owner-only: let Antons diagnose its own behaviour from its own logs.

    Read-only and bounded — see app/investigate.py. The sender must be a
    configured rig owner; anyone else is refused without a model call.
    """
    from app import investigate as _inv
    from app import rig_agent
    acfg = (messaging.load_config().get("auto_reply") or {})
    allowed = bool(rig_agent.owner_allowed(body.sender or "", acfg))
    res = _inv.investigate(body.question or "", acfg, allowed=allowed,
                           chat_guid=body.chat_guid or "",
                           max_steps=max(1, min(int(body.steps or 5), 8)),
                           # a step costs a call, so the call budget has to lead
                           # the step budget or the loop stops mid-investigation
                           max_model_calls=max(3, min(int(body.steps or 5), 8) + 3),
                           who=body.sender or "")
    return res


@app.post("/api/messaging/propose-fix")
def api_messaging_propose_fix(body: InvestigateRequest):
    """Owner-only: diagnose, then write down what it WOULD change.

    app/propose_patch.py was written, reviewed and then never called by
    anything — so the whole capability sat dead. It still writes no files:
    that module imports selfinspect, which has no write verb, and a proposal
    is a document the owner reads and decides on.
    """
    from app import propose_patch as _pp
    from app import rig_agent
    acfg = (messaging.load_config().get("auto_reply") or {})
    allowed = bool(rig_agent.owner_allowed(body.sender or "", acfg))
    steps = max(1, min(int(body.steps or 5), 8))
    return _pp.propose(body.question or "", acfg, allowed=allowed,
                       chat_guid=body.chat_guid or "", who=body.sender or "",
                       max_steps=steps, max_model_calls=steps + 3)


@app.post("/api/messaging/apply-fix")
def api_messaging_apply_fix(body: dict):
    """Owner-only: apply a proposal's edits, run its suites, revert unless green.

    The edits must be the ones a proposal produced -- each is re-checked against
    the file before anything is written, so text that does not match is refused
    rather than guessed at. Nothing restarts the service; picking the change up
    stays a deliberate act.
    """
    from app import rig_agent, self_heal
    acfg = (messaging.load_config().get("auto_reply") or {})
    sender = str((body or {}).get("sender") or "")
    if not rig_agent.owner_allowed(sender, acfg):
        return _api_error("applying a fix is owner-only", status_code=403)
    try:
        return self_heal.apply_edits((body or {}).get("edits") or [],
                                     suites=(body or {}).get("suites") or [],
                                     reason=(body or {}).get("reason") or "")
    except self_heal.HealError as exc:
        return _api_error(str(exc), status_code=400)


@app.get("/api/agent/tools")
def api_agent_tools(allow_downloads: bool = False,
                    allow_goal_actions: bool = False,
                    allow_external_actions: bool = False):
    ctx = agent_tools.AgentContext(
        actor="dashboard",
        allow_private=True,
        allow_local_write=bool(allow_downloads or allow_goal_actions),
        allow_external_write=bool(allow_external_actions),
    )
    tools = agent_tools.tool_inventory(ctx)
    for tool in tools:
        if tool.get("name") == "download_file" and not allow_downloads:
            tool["available"] = False
            tool["unavailable_reason"] = "download_file requires allow_downloads"
        if tool.get("name") in _AGENT_GOAL_WRITE_TOOLS and not allow_goal_actions:
            tool["available"] = False
            tool["unavailable_reason"] = "goal write tools require allow_goal_actions"
    return {"ok": True, "tools": tools}


@app.get("/api/search/stats")
def api_search_stats():
    """Rate-limit accounting for the web search backend.

    Every time search "stopped working" the first missing thing was a number:
    how many calls in the last minute, whether we are blocked, for how long.
    """
    from app import coder
    return {"ok": True, **coder.search_stats()}


@app.get("/api/agent/metrics")
def api_agent_metrics(limit: int = 5000):
    return {"ok": True, "metrics": agent_tools.trace_metrics(limit=limit)}


@app.get("/api/tailscale/shares")
def api_tailscale_shares():
    """Everything this machine exposes over Tailscale right now -- which URL,
    what it points at, and whether it is public (funnel) or tailnet-only."""
    from app import tailscale_shares
    return tailscale_shares.list_shares()


class TailscaleStopRequest(BaseModel):
    port: str
    confirm: bool = False


@app.post("/api/tailscale/stop")
def api_tailscale_stop(body: TailscaleStopRequest):
    """Cut one Tailscale share (serve + funnel) on an https port."""
    if not body.confirm:
        return _api_error("pass confirm=true to stop a share", status_code=400)
    from app import tailscale_shares
    res = tailscale_shares.stop_share(body.port)
    if not res.get("ok"):
        return JSONResponse(status_code=502, content=res)
    return res


@app.get("/api/pulse")
def api_pulse():
    """What the assistant is doing RIGHT NOW: idle / listening / thinking /
    reasoning / tool (detail = tool name) / speaking / building / polishing /
    built / error. Goes stale to 'idle' on its own. Used by the pet page so the
    creature's mood tracks the AI's activity."""
    try:
        return agent_tools.pulse_get()
    except Exception:
        return {"state": "idle", "detail": "", "age_seconds": 0}


class PulseSetRequest(BaseModel):
    state: str
    detail: str = ""


@app.post("/api/pulse")
def api_pulse_set(body: PulseSetRequest):
    """Manually nudge the activity pulse (for testing the pet, or a demo)."""
    try:
        agent_tools.pulse_set(body.state, body.detail)
        return {"ok": True, **agent_tools.pulse_get()}
    except Exception as exc:
        return _api_error(str(exc), status_code=500)


@app.get("/api/messaging/latency")
def api_messaging_latency(limit: int = 200):
    """How long the last `limit` turns took, and where the seconds went."""
    try:
        from app import telemetry
        return telemetry.status_payload(limit=max(1, min(5000, int(limit))))
    except Exception as e:
        return _api_error(e)


@app.get("/api/messaging/status")
def api_messaging_status():
    return messaging.status_payload()


@app.get("/api/messaging/providers")
def api_messaging_providers():
    return messaging.providers_payload()


@app.post("/api/messaging/send")
def api_messaging_send(body: MessagingSendRequest):
    try:
        return messaging.send_message(
            provider=body.provider,
            target=body.target,
            text=body.text,
            metadata=body.metadata,
            real_send=bool(body.real_send),
        )
    except messaging.MessagingError as exc:
        return _api_error(str(exc), status_code=exc.status_code)
    except Exception as exc:
        return _api_error(str(exc), status_code=500)


@app.get("/api/messaging/outbox")
def api_messaging_outbox(limit: int = 50):
    return {"ok": True, "history_path": str(messaging.OUTBOX_PATH), "items": messaging.outbox(limit=limit)}


@app.get("/api/messaging/send-queue")
def api_messaging_send_queue():
    """What is parked waiting for the Mac to come back, and for how long."""
    return messaging.send_queue_status()


@app.post("/api/messaging/send-queue/drain")
def api_messaging_send_queue_drain():
    return messaging.drain_send_queue()


@app.post("/api/messaging/inbound")
def api_messaging_inbound(body: dict):
    result = messaging.record_inbound(body)
    result["scheduled"] = messaging.dispatch_recorded_inbound(result)
    return result


@app.get("/api/messaging/inbox")
def api_messaging_inbox(limit: int = 50):
    return {"ok": True, "inbox_path": str(messaging.INBOX_PATH), "items": messaging.inbox(limit=limit)}


# NOTE: /api/messaging/auto-reply (+ /test-reply) and /api/messaging/autoreply
# (+ /autoreply/test) are aliases for the same messaging functions — a second
# set was added later without noticing the first. The dashboard calls the
# unhyphenated ones. Change behaviour in messaging.py, not in one route pair.

@app.get("/api/messaging/auto-reply")
def api_messaging_auto_reply():
    return messaging.auto_reply_status()


@app.post("/api/messaging/auto-reply")
def api_messaging_auto_reply_update(body: dict):
    try:
        return messaging.update_auto_reply(body if isinstance(body, dict) else {})
    except messaging.MessagingError as exc:
        return _api_error(str(exc), status_code=exc.status_code)


class MessagingTestReplyRequest(BaseModel):
    text: str
    sender: str | None = None
    chat_guid: str | None = None
    from_me: bool | None = False
    # Opt OUT of dry-run so the agent may really call write/privileged tools.
    # Still never SENDS to the chat. Off by default on purpose.
    live: bool | None = False


@app.post("/api/messaging/test-reply")
def api_messaging_test_reply(body: MessagingTestReplyRequest):
    """Full persona+history+Hermes pipeline, but never sends anything.

    With `live: true` the privileged tools actually execute instead of being
    refused as a preview -- the only way to exercise the propose/apply chain
    end to end without texting from the phone.
    """
    try:
        return messaging.generate_test_reply(text=body.text, sender=body.sender,
                                             chat_guid=body.chat_guid,
                                             from_me=bool(getattr(body, "from_me", False)),
                                             live=bool(getattr(body, "live", False)))
    except messaging.MessagingError as exc:
        return _api_error(str(exc), status_code=exc.status_code)
    except Exception as exc:
        return _api_error(str(exc), status_code=500)


@app.get("/api/messaging/digest")
def api_messaging_digest_status():
    status = messaging.daily_digest_status()
    try:
        with open(_DIGEST_MARKER, encoding="utf-8") as fh:
            status["last_sent"] = fh.read().strip()
    except Exception:
        status["last_sent"] = ""
    return status


@app.post("/api/messaging/digest")
def api_messaging_digest_run(body: dict | None = None):
    """Build (and send) today's news digest now. {"dry_run": true} returns text only."""
    dry = bool((body or {}).get("dry_run"))
    targets = (body or {}).get("targets")
    if targets is not None and not isinstance(targets, list):
        return _api_error("targets must be a list of chat guids", status_code=400)
    try:
        result = messaging.run_daily_digest(dry_run=dry, targets=targets)
    except messaging.MessagingError as exc:
        return _api_error(str(exc), status_code=exc.status_code)
    except Exception as exc:
        return _api_error(str(exc), status_code=500)
    # a targets override is a test send to a subset — it must not consume the
    # day's marker, or the real recipients silently lose their digest
    if not dry and targets is None and result.get("sent_real_message"):
        # a manual send counts as today's digest — don't double-send from the loop
        with open(_DIGEST_MARKER, "w", encoding="utf-8") as fh:
            fh.write(datetime.now().strftime("%Y-%m-%d"))
    return result


@app.post("/api/messaging/digest-config")
def api_messaging_digest_config(body: dict):
    try:
        return messaging.update_daily_digest(body if isinstance(body, dict) else {})
    except messaging.MessagingError as exc:
        return _api_error(str(exc), status_code=exc.status_code)


_DIGEST_MARKER = os.path.join(str(messaging.HISTORY_DIR), "last_digest.txt")
_DIGEST_MAX_FAILED_RUNS = 6
_DIGEST_FAILED = {"day": "", "runs": 0}


def _daily_digest_loop():
    """Send the news digest once per day after the configured hour."""
    while True:
        try:
            dcfg = (messaging.load_config().get("daily_digest") or {})
            if dcfg.get("enabled") and messaging._digest_recipients(dcfg):
                now = datetime.now()
                today = now.strftime("%Y-%m-%d")
                last = ""
                if os.path.isfile(_DIGEST_MARKER):
                    with open(_DIGEST_MARKER, encoding="utf-8") as fh:
                        last = fh.read().strip()
                if _DIGEST_FAILED["day"] != today:
                    _DIGEST_FAILED.update(day=today, runs=0)
                if (now.hour >= int(dcfg.get("hour") or 8) and last != today
                        and _DIGEST_FAILED["runs"] < _DIGEST_MAX_FAILED_RUNS):
                    try:
                        res = messaging.run_daily_digest()
                    except Exception as exc:
                        # was `pass` at the bottom: a failed morning left no trace
                        _DIGEST_FAILED["runs"] += 1
                        LOGGER.warning(
                            "daily digest run %d/%d failed: %s%s",
                            _DIGEST_FAILED["runs"], _DIGEST_MAX_FAILED_RUNS, exc,
                            "; no more tries today"
                            if _DIGEST_FAILED["runs"] >= _DIGEST_MAX_FAILED_RUNS else "")
                        raise
                    if (not isinstance(res, dict) or res.get("sent_real_message")
                            or res.get("queued")):
                        with open(_DIGEST_MARKER, "w", encoding="utf-8") as fh:
                            fh.write(today)
        except Exception:
            pass
        time.sleep(300)


@app.on_event("startup")
def _start_send_loops():
    threading.Thread(target=_daily_digest_loop, daemon=True).start()
    # Redelivers anything parked by a transport failure (see messaging.drain_send_queue)
    messaging.ensure_send_queue_worker(60)
    from app import reminders
    reminders.ensure_worker(20)


@app.on_event("startup")
def _start_inbound_catchup():
    """On startup, not at import: this loop stores messages and hands fresh ones
    to the reply pipeline, so a test or a script that imports this module must
    never become a second copy of the rig answering real chats.
    """
    messaging.ensure_inbound_catchup_worker()


@app.get("/api/messaging/catchup")
def api_messaging_catchup():
    return messaging.inbound_catchup_status()


@app.get("/api/messaging/transcript")
def api_messaging_transcript(chat_guid: str, limit: int = 40):
    try:
        return messaging.labeled_transcript(chat_guid, limit)
    except Exception as exc:
        return _api_error(str(exc), status_code=500)


@app.get("/api/messaging/history")
def api_messaging_history(chat_guid: str, limit: int = 20):
    """Debug view of the context the auto-reply would see for a chat."""
    cfg = messaging.load_config()
    acfg = cfg.get("auto_reply") or {}
    pcfg = (cfg.get("providers") or {}).get(str(acfg.get("provider") or "bluebubbles_http")) or {}
    try:
        items = messaging._bb_fetch_history(pcfg, chat_guid, limit)
        source = "bluebubbles"
    except Exception as exc:
        items = messaging._chat_history_messages(chat_guid, limit)
        source = "local (%s)" % exc
    return {"ok": True, "source": source, "count": len(items),
            "items": [{k: v for k, v in it.items() if k != "images"} for it in items]}


@app.get("/api/messaging/routing")
def api_messaging_routing(limit: int = 400):
    """Is the subject/source watcher actually deciding anything, and how well?

    "shadow" means it classifies every message and logs the verdict while the
    old regex gate still decides — so this endpoint is the only way to tell
    whether switching it to "active" would help or hurt.
    """
    import json as _json
    cfg = messaging.load_config().get("auto_reply") or {}
    mode = str(cfg.get("agentic_routing") or "off").lower()
    minimum = float(cfg.get("routing_watcher_min_confidence") or 0.80)
    rows = []
    try:
        with open(messaging.ROUTING_WATCHER_PATH, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        rows.append(_json.loads(line))
                    except Exception:
                        pass
    except Exception:
        rows = []
    rows = rows[-max(1, min(int(limit or 400), 5000)):]
    agrees = sum(1 for r in rows if not r.get("disagrees"))
    subjects, would_act, disagreements = {}, 0, []
    would_change = new_replies = new_silences = 0
    for r in rows:
        w = r.get("watcher") or {}
        subj = str(w.get("subject") or "unknown")
        subjects[subj] = subjects.get(subj, 0) + 1
        conf = float(w.get("confidence") or 0)
        # what active mode would actually override: confident and not unknown
        authoritative = subj != "unknown" and conf >= minimum
        if authoritative:
            would_act += 1
        if authoritative and r.get("disagrees"):
            would_change += 1
            if str(w.get("action")) == "reply":
                new_replies += 1
            else:
                new_silences += 1
        if r.get("disagrees"):
            disagreements.append({"legacy_reason": r.get("legacy_reason") or "",
                                  "action": w.get("action"), "subject": subj,
                                  "confidence": round(conf, 2)})
    return {"ok": True, "mode": mode, "min_confidence": minimum,
            "decisions": len(rows), "agrees": agrees,
            "disagrees": len(rows) - agrees,
            "would_override_in_active": would_act,
            "would_change_behaviour": would_change,
            "would_start_replying": new_replies,
            "would_go_silent": new_silences,
            "subjects": subjects, "recent_disagreements": disagreements[-10:]}


@app.get("/api/messaging/autoreply")
def api_messaging_autoreply():
    try:
        return messaging.auto_reply_status()
    except Exception as exc:
        return _api_error(str(exc), status_code=500)


@app.post("/api/messaging/autoreply")
def api_messaging_autoreply_update(body: dict):
    try:
        return messaging.update_auto_reply(body)
    except messaging.MessagingError as exc:
        return _api_error(str(exc), status_code=exc.status_code)
    except Exception as exc:
        return _api_error(str(exc), status_code=500)


@app.get("/api/calendar/anton.ics")
def api_calendar_ics():
    """Anton's dates as a subscribable calendar."""
    from app import date_plans
    return Response(content=date_plans.to_ics(),
                    media_type="text/calendar; charset=utf-8",
                    headers={"Content-Disposition":
                             "inline; filename=anton.ics"})


@app.get("/api/calendar/share/{token}.ics")
def api_calendar_share_ics(token: str):
    """The date plans as a subscribable feed for the other person (404 unless calendar_enabled).

    An .ics attachment sent over iMessage gives the receiving phone nothing to
    add, with METHOD:REQUEST, a bare VEVENT or a fresh UID alike; a calendar
    subscription works. The feed sits behind an unguessable token so the URL can
    be shared without exposing the rig: it serves only titles and times, never
    the notes or who requested what, and only this path needs to be reachable.
    """
    from app import date_plans
    _acfg = messaging.load_config().get("auto_reply") or {}
    if not _acfg.get("calendar_enabled", False):
        return _api_error("not found", status_code=404)
    want = str(_acfg.get("calendar_share_token") or "")
    if not want or token != want:
        return _api_error("not found", status_code=404)
    live = [p.get("id") for p in date_plans.list_plans()
            if p.get("status") not in ("cancelled", "declined")]
    body = date_plans.to_ics(only_ids=live, share=True) if live else \
        date_plans.to_ics(only_ids=["__none__"], share=True)
    return Response(content=body,
                    media_type="text/calendar; charset=utf-8",
                    headers={"Content-Disposition":
                             "inline; filename=kalendars.ics"})


@app.get("/api/calendar/plans")
def api_calendar_plans(include_past: bool = False):
    from app import date_plans
    return {"ok": True, "plans": date_plans.list_plans(include_past),
            "calendar_connected": date_plans.calendar_connected()}


@app.post("/api/calendar/plans/{plan_id}")
def api_calendar_plan_update(plan_id: str, body: dict):
    """Confirm, decline or annotate a plan: {"status": "confirmed"} etc."""
    from app import date_plans
    updated = date_plans.update_plan(plan_id, **(body or {}))
    if not updated:
        return _api_error("no such plan", status_code=404)
    return {"ok": True, "plan": updated}


@app.post("/api/messaging/voice")
def api_messaging_voice(body: dict):
    """Speak a message and send it as a voice note. {"target": guid, "text": ...}"""
    try:
        return messaging.send_voice_message((body or {}).get("target"),
                                            (body or {}).get("text"))
    except messaging.MessagingError as exc:
        return _api_error(str(exc), status_code=exc.status_code)
    except Exception as exc:
        return _api_error(str(exc), status_code=500)


@app.get("/api/messaging/chat-mode")
def api_messaging_chat_mode(guid: str):
    return {"ok": True, "chat_guid": guid, "mode": messaging.chat_mode(guid)}


@app.post("/api/messaging/chat-mode")
def api_messaging_chat_mode_set(body: dict):
    """Flip one chat between 'persona' (reply-as-me) and 'assistant' (stats)."""
    try:
        return messaging.set_chat_mode((body or {}).get("guid"),
                                       (body or {}).get("mode"))
    except messaging.MessagingError as exc:
        return _api_error(str(exc), status_code=exc.status_code)
    except Exception as exc:
        return _api_error(str(exc), status_code=500)


@app.post("/api/messaging/corrections/mine")
def api_messaging_mine_corrections(body: dict | None = None):
    """Rebuild the correction style-pairs from the message history.

    Also the only honest quality metric this bot has: what share of its replies
    the owner immediately typed over.
    """
    try:
        return messaging.mine_corrections(write=bool((body or {}).get("write", True)))
    except Exception as exc:
        return _api_error(str(exc), status_code=500)


@app.get("/api/messaging/corrections")
def api_messaging_corrections_status():
    try:
        return messaging.mine_corrections(write=False)
    except Exception as exc:
        return _api_error(str(exc), status_code=500)


@app.post("/api/messaging/autoreply/test")
def api_messaging_autoreply_test(body: AutoReplyTestRequest):
    try:
        return messaging.generate_test_reply(text=body.text, sender=body.sender, chat_guid=body.chat_guid)
    except messaging.MessagingError as exc:
        return _api_error(str(exc), status_code=exc.status_code)
    except Exception as exc:
        return _api_error(str(exc), status_code=500)



@app.get("/api/rig-agent")
def api_rig_agent_status():
    try:
        return rig_agent.status_payload()
    except Exception as exc:
        return _api_error(str(exc), status_code=500)


@app.post("/api/rig-agent")
def api_rig_agent_update(body: dict):
    try:
        rig_agent.save_config(body or {})
        return rig_agent.status_payload()
    except Exception as exc:
        return _api_error(str(exc), status_code=500)


@app.post("/api/rig-agent/ask")
def api_rig_agent_ask(body: dict):
    """Preview an answer. Unlike the messaging path this is not owner-gated —
    reaching it already means access to the dashboard on the local network."""
    text = str((body or {}).get("text") or "").strip()
    if not text:
        return _api_error("text is required", status_code=400)
    try:
        result = rig_agent.answer(text)
    except Exception as exc:
        return _api_error(str(exc), status_code=500)
    if not result:
        return {"ok": True, "matched": False,
                "text": "", "hint": "Not recognised as a rig question."}
    return {"ok": True, "matched": True, "skill": result["skill"],
            "lang": result["lang"], "text": result["text"]}



@app.get("/api/ai/status")
def api_ai_status():
    try:
        return ai_stack.status_payload()
    except Exception as exc:
        return _api_error(str(exc), status_code=500)


@app.post("/api/ai/review")
def api_ai_review(body: AiReviewRequest):
    result = ai_stack.run_review(
        provider=body.provider,
        prompt=body.prompt,
        subject=body.subject or "",
        allow_cloud=bool(body.allow_cloud),
        save_memory=body.save_memory if body.save_memory is not None else True,
    )
    if not result.get("ok"):
        return _api_error(result.get("error", "review failed"),
                          status_code=result.get("status_code", 502))
    return result


@app.post("/api/ai/review/stream")
def api_ai_review_stream(body: AiReviewRequest):
    """Token-streamed local review. Hermes-aware (injects relevant memories via
    ai_stack._review_prompt) and saves the result back to memory when done.
    Cloud providers stay on the non-streaming /api/ai/review endpoint."""
    prompt = (body.prompt or "").strip()
    if not prompt:
        return _api_error("review prompt is required", status_code=400)
    cfg = ai_stack.load_config()
    model = (cfg.get("models") or {}).get("local_reviewer") or "qwen3.8:latest"
    review_text = ai_stack._review_prompt(prompt, subject=(body.subject or ""), cloud=False)
    save_memory = body.save_memory if body.save_memory is not None else True
    payload = {"model": model, "prompt": review_text, "stream": True,
               "keep_alive": "30m", "options": {"temperature": 0.1}}
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(OLLAMA + "/api/generate", data=data)
    req.add_header("Content-Type", "application/json")

    def gen():
        resp = None
        acc = []
        try:
            yield (json.dumps({"type": "meta", "model": model}) + "\n").encode("utf-8")
            resp = urllib.request.urlopen(req, timeout=300)
            while True:
                line = resp.readline()
                if not line:
                    break
                try:
                    item = json.loads(line.decode("utf-8", errors="replace"))
                except Exception:
                    continue
                tok = item.get("response") or ""
                if tok:
                    acc.append(tok)
                    yield (json.dumps({"type": "token", "text": tok}) + "\n").encode("utf-8")
                if item.get("done"):
                    break
            output = "".join(acc).strip()
            mem_id = None
            if output and save_memory:
                saved = ai_stack.add_memory(
                    "review", prompt[:120], output, source="aistack:local",
                    metadata={"provider": "local", "model": model})
                mem_id = saved.get("id")
            yield (json.dumps({"type": "done", "memory_id": mem_id}) + "\n").encode("utf-8")
        except GeneratorExit:
            raise
        except Exception as e:
            yield (json.dumps({"type": "error", "error": str(e)}) + "\n").encode("utf-8")
        finally:
            if resp is not None:
                try:
                    resp.close()
                except Exception:
                    pass
    return StreamingResponse(gen(), media_type="application/x-ndjson")


@app.get("/api/hermes/context")
def api_hermes_context(q: str = "", limit: int = 6):
    """A formatted Hermes memory block for injecting into assistant chats.
    Uses word-overlap relevance search (a raw chat prompt as a LIKE pattern
    never matched anything, so injection was silently empty before)."""
    try:
        return ai_stack.memory_context(q=q, limit=max(1, min(int(limit or 6), 20)))
    except Exception as exc:
        return _api_error(str(exc), status_code=500)


@app.get("/api/hermes")
def api_hermes_list(limit: int = 50, q: str = "", kind: str = "",
                    include_disabled: bool = False):
    try:
        listing = ai_stack.list_memories(
            limit=limit, q=q, kind=kind or None, include_disabled=include_disabled)
        return {"ok": True, "items": listing.get("items", []),
                "stats": ai_stack.memory_stats()}
    except Exception as exc:
        return _api_error(str(exc), status_code=500)


@app.post("/api/hermes")
def api_hermes_add(body: HermesMemoryRequest):
    result = ai_stack.add_memory(
        kind=body.kind,
        title=body.title,
        content=body.content,
        source=(body.source or "dashboard"),
        metadata=body.metadata,
        pinned=bool(body.pinned),
    )
    if not result.get("ok"):
        return _api_error(result.get("error", "could not save memory"),
                          status_code=result.get("status_code", 400))
    return result


@app.patch("/api/hermes/{memory_id}")
def api_hermes_update(memory_id: int, body: HermesUpdateRequest):
    try:
        result = ai_stack.update_memory(
            memory_id,
            kind=body.kind,
            title=body.title,
            content=body.content,
            pinned=body.pinned,
            disabled=body.disabled,
        )
        if not result.get("ok"):
            return _api_error(result.get("error", "update failed"),
                              status_code=result.get("status_code", 400))
        return result
    except Exception as exc:
        return _api_error(str(exc), status_code=500)


@app.delete("/api/hermes/{memory_id}")
def api_hermes_delete(memory_id: int):
    try:
        return ai_stack.delete_memory(memory_id)
    except Exception as exc:
        return _api_error(str(exc), status_code=500)


@app.post("/api/hermes/toggle")
def api_hermes_toggle(body: HermesToggleRequest):
    try:
        return ai_stack.set_memory_enabled(bool(body.enabled))
    except Exception as exc:
        return _api_error(str(exc), status_code=500)


@app.post("/api/hermes/summarize")
def api_hermes_summarize():
    result = ai_stack.summarize_memory()
    if not result.get("ok"):
        return _api_error(result.get("error", "summarize failed"),
                          status_code=result.get("status_code", 502))
    return result


@app.post("/api/hermes/compact")
def api_hermes_compact(body: dict | None = None):
    """Manual trigger for the nightly memory compaction. {"dry_run": true} to preview."""
    dry = bool((body or {}).get("dry_run"))
    result = ai_stack.compact_memories(dry_run=dry)
    if not result.get("ok"):
        return _api_error(result.get("error", "compact failed"),
                          status_code=result.get("status_code", 502))
    return result


@app.post("/api/hermes/extract")
def api_hermes_extract():
    """Mine recent chats for per-contact facts into Hermes (also runs nightly)."""
    try:
        return messaging.hermes_extract_daily()
    except Exception as exc:
        return _api_error(str(exc), status_code=500)


_HERMES_COMPACT_MARKER = os.path.join(".ai_memory", "last_compact.txt")


def _hermes_compact_loop():
    """Nightly (after 04:00): extract fresh per-contact facts from the day's
    chats into Hermes, then compact/dedupe the store."""
    while True:
        try:
            now = datetime.now()
            today = now.strftime("%Y-%m-%d")
            last = ""
            if os.path.isfile(_HERMES_COMPACT_MARKER):
                with open(_HERMES_COMPACT_MARKER, encoding="utf-8") as fh:
                    last = fh.read().strip()
            if now.hour >= 4 and last != today:
                try:
                    messaging.hermes_extract_daily()
                except Exception:
                    pass
                try:
                    messaging.mine_corrections()
                except Exception:
                    pass
                ai_stack.compact_memories()
                os.makedirs(os.path.dirname(_HERMES_COMPACT_MARKER), exist_ok=True)
                with open(_HERMES_COMPACT_MARKER, "w", encoding="utf-8") as fh:
                    fh.write(today)
        except Exception:
            pass
        time.sleep(600)


@app.on_event("startup")
def _start_hermes_compact_loop():
    threading.Thread(target=_hermes_compact_loop, daemon=True).start()


@app.on_event("startup")
def _warm_sidecars():
    """Warm what the first reply after a restart would otherwise wait for."""
    from app import lv_morph as _lv_morph
    from app import rag as _rag_warm
    for warm in (messaging._ensure_mms_daemon, messaging._ensure_nmt_daemon,
                 _rag_warm.warm_embed, _lv_morph._ensure_daemon):
        try:
            threading.Thread(target=warm, daemon=True).start()
        except Exception:
            pass



MEDIA_HOST = os.environ.get("MEDIA_OPS_HOST", hosts.MEDIA_BOX_LAN_IP)
MEDIA_USER = os.environ.get("MEDIA_OPS_USER", "media")
MEDIA_HOME = hosts.MEDIA_BOX_HOME
MEDIA_AI_SH = MEDIA_HOME + "/media-ai.sh"
MEDIA_SSH_KEY = os.environ.get("MEDIA_OPS_SSH_KEY",
                               os.path.join(os.path.expanduser("~"), ".ssh", "media_ops_to_media"))
MEDIA_WORKSPACE = os.environ.get("MEDIA_OPS_WORKSPACE", r"M:\AI\media-server-workspace")
SERVICE_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# MEDIA_OPS_MODEL is unset, so this default is what actually reaches Ollama,
# not a stale tag -- it must be the one resident model (qwen3.8), not a second
# 18-20 GB model that would evict the lane already on the card.
MEDIA_MODEL = os.environ.get("MEDIA_OPS_MODEL", "qwen3.8:latest")
MEDIA_PASSWORD_FILE = os.environ.get(
    "MEDIA_OPS_PASSWORD_FILE",
    os.path.join(coder.PROJECT_ROOT, ".secrets", "media_ops_password.txt"),
)
JELLYFIN_PUBLIC_URL = os.environ.get("MEDIA_OPS_JELLYFIN_URL", hosts.JELLYFIN_URL)
JELLYFIN_SCREENSHOT_DIR = os.path.join(coder.PROJECT_ROOT, "artifacts", "jellyfin-screenshots")
JELLYFIN_SCREENSHOT_TOOL = os.path.join(
    coder.PROJECT_ROOT, "_tools", "jellyfin_screenshot", "capture-jellyfin.mjs"
)
JELLYFIN_STORAGE_STATE = os.path.join(
    coder.PROJECT_ROOT, ".jellyfin-auth", "storage-state.json"
)

JELLYFIN_SOURCE_FILES = [
    "apply-jellyfin-mobile-ui.py",
    "jellyfin-mobile-streaming-2026.css",
    "jellyfin-ios-player.js",
    "jellyfin-luma-home.js",
    "jellyfin-anton-icon-512.png",
]
JELLYFIN_JS_FILES = ["jellyfin-ios-player.js", "jellyfin-luma-home.js"]
JELLYFIN_CSS_FILE = "jellyfin-mobile-streaming-2026.css"
# Files reset/diff-saved on rollback: the deployed set plus repo files the AI
# tends to touch even though they never ship (kept clean so reverts aren't blocked).
JELLYFIN_RESET_FILES = JELLYFIN_SOURCE_FILES + ["jellyfin-theme-color.js"]
JELLYFIN_CSS_START = "/* ANTON MOBILE STREAMING UI START */"
JELLYFIN_CSS_END = "/* ANTON MOBILE STREAMING UI END */"

MEDIA_COMMANDS = {
    "status": [MEDIA_AI_SH, "status"],
    "verify": [MEDIA_AI_SH, "verify"],
    "preflight": [MEDIA_AI_SH, "preflight", "--strict-runtime-match"],
    "checkpoint": [MEDIA_AI_SH, "checkpoint"],
    "service-backup": [MEDIA_AI_SH, "backup"],
    "sync-big-rig": [MEDIA_AI_SH, "sync-big-rig"],
}

MEDIA_TARGETS = {
    "media-console": {
        "label": "Media console / dashboard",
        "files": ["media-console-server.py"],
        "deploy": [MEDIA_AI_SH, "deploy", "media-console"],
        "needs_sudo": True,
    },
    "jellyfin-ui": {
        "label": "Jellyfin UI",
        "files": [
            "media-ai.sh",
            "media-ai-safety/tools/backup_jellyfin_ui.py",
            "media-ai-safety/tools/restore_jellyfin_ui.py",
            "media-ai-safety/tools/preflight_media_stack.py",
            "apply-jellyfin-mobile-ui.py",
            "jellyfin-mobile-streaming-2026.css",
            "jellyfin-ios-player.js",
            "jellyfin-luma-home.js",
            "jellyfin-anton-icon-512.png",
        ],
        "deploy": [MEDIA_AI_SH, "deploy", "jellyfin-ui"],
        "needs_sudo": False,
    },
}


def _clip_text(text, limit=24000):
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit] + "\n\n[output truncated]"


def _run_local(args, cwd=None, timeout=120, input_text=None, env_extra=None):
    if args and args[0] == "git" and cwd:
        args = ["git", "-c", "safe.directory=" + str(cwd)] + list(args[1:])
    try:
        env = dict(os.environ)
        if env_extra:
            env.update({k: str(v) for k, v in env_extra.items() if v is not None})
        kwargs = {
            "cwd": cwd,
            "capture_output": True,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "timeout": timeout,
            "creationflags": _CREATE_NO_WINDOW,
            "env": env,
        }
        if input_text is None:
            kwargs["stdin"] = subprocess.DEVNULL
        else:
            kwargs["input"] = input_text
        r = subprocess.run(
            args,
            **kwargs,
        )
        out = ((r.stdout or "") + (r.stderr or "")).rstrip()
        return {"ok": r.returncode == 0, "code": r.returncode, "output": _clip_text(out)}
    except subprocess.TimeoutExpired:
        return {"ok": False, "code": -1, "output": "timed out"}
    except Exception as e:
        return {"ok": False, "code": -1, "output": str(e)}


def _ssh_available():
    return bool(shutil.which("ssh")) and _path_is_file(MEDIA_SSH_KEY)


def _default_media_password():
    value = os.environ.get("MEDIA_OPS_PASSWORD", "").strip()
    if value:
        return value
    try:
        with open(MEDIA_PASSWORD_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def _effective_media_password(value=None):
    return (value or "").strip() or _default_media_password()


def _request_media_password(body):
    if not body:
        return _default_media_password()
    return _effective_media_password(body.ssh_password or body.sudo_password)


def _ssh_password_available(ssh_password):
    return bool((ssh_password or "").strip())


def _media_password_client(ssh_password, attempts=3):
    ssh_password = _effective_media_password(ssh_password)
    if not _ssh_password_available(ssh_password):
        return None, "Media SSH password was not supplied."
    try:
        import paramiko
    except Exception as exc:
        return None, "paramiko is not installed: %s" % exc

    last_error = ""
    for attempt in range(max(1, attempts)):
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client.connect(
                MEDIA_HOST,
                username=MEDIA_USER,
                password=ssh_password,
                look_for_keys=False,
                allow_agent=False,
                timeout=8,
                banner_timeout=12,
                auth_timeout=12,
            )
            return client, ""
        except Exception as exc:
            last_error = str(exc)
            try:
                client.close()
            except Exception:
                pass
            if attempt + 1 < attempts:
                time.sleep(1 + attempt)
    return None, last_error


def _path_is_file(path):
    try:
        return os.path.isfile(path)
    except OSError:
        return False


def _path_is_dir(path):
    try:
        return os.path.isdir(path)
    except OSError:
        return False


def _sh_quote(value):
    return "'" + str(value).replace("'", "'\"'\"'") + "'"


def _run_media_password(args, timeout=120, ssh_password=None, input_text=None):
    client, err = _media_password_client(ssh_password)
    if err:
        return {"ok": False, "code": -1, "output": err}
    try:
        remote = " ".join(_sh_quote(a) for a in args)
        stdin, stdout, stderr = client.exec_command(remote, timeout=timeout)
        if input_text is not None:
            stdin.write(input_text)
            stdin.flush()
        out = (stdout.read() or b"").decode("utf-8", errors="replace")
        err = (stderr.read() or b"").decode("utf-8", errors="replace")
        code = stdout.channel.recv_exit_status()
        return {"ok": code == 0, "code": code, "output": _clip_text((out + err).strip())}
    except Exception as exc:
        return {"ok": False, "code": -1, "output": str(exc)}
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass


def _run_media(args, timeout=120, ssh_password=None):
    ssh_password = _effective_media_password(ssh_password)
    if not _ssh_available():
        if _ssh_password_available(ssh_password):
            return _run_media_password(args, timeout=timeout, ssh_password=ssh_password)
        return {
            "ok": False,
            "code": -1,
            "output": "Media SSH bridge is not configured for this console user.",
        }
    remote = " ".join(_sh_quote(a) for a in args)
    cmd = [
        "ssh", "-n", "-T", "-i", MEDIA_SSH_KEY,
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=no",
        "-o", "ConnectTimeout=8",
        "%s@%s" % (MEDIA_USER, MEDIA_HOST),
        remote,
    ]
    res = _run_local(cmd, timeout=timeout)
    if not res.get("ok") and _ssh_password_available(ssh_password):
        return _run_media_password(args, timeout=timeout, ssh_password=ssh_password)
    return res


def _run_media_sudo(args, sudo_password, timeout=360, ssh_password=None):
    ssh_password = _effective_media_password(ssh_password)
    if not _ssh_available():
        if _ssh_password_available(ssh_password):
            password = (sudo_password or ssh_password or "")
            if not password:
                return {"ok": False, "code": -1, "output": "sudo password is required for this deploy target"}
            remote = ["sudo", "-S", "-p", ""] + args
            return _run_media_password(remote, timeout=timeout, ssh_password=ssh_password, input_text=password + "\n")
        return {
            "ok": False,
            "code": -1,
            "output": "Media SSH bridge is not configured for this console user.",
        }
    password = (sudo_password or ssh_password or "")
    if not password:
        return {"ok": False, "code": -1, "output": "sudo password is required for this deploy target"}
    remote = "sudo -S -p '' " + " ".join(_sh_quote(a) for a in args)
    cmd = [
        "ssh", "-T", "-i", MEDIA_SSH_KEY,
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=no",
        "-o", "ConnectTimeout=8",
        "%s@%s" % (MEDIA_USER, MEDIA_HOST),
        remote,
    ]
    res = _run_local(cmd, timeout=timeout, input_text=password + "\n")
    if not res.get("ok") and _ssh_password_available(ssh_password):
        remote_args = ["sudo", "-S", "-p", ""] + args
        return _run_media_password(remote_args, timeout=timeout, ssh_password=ssh_password, input_text=password + "\n")
    return res


def _sftp_to_media(local_path, remote_name, timeout=90, ssh_password=None):
    return _sftp_many_to_media([(local_path, remote_name)], timeout=timeout, ssh_password=ssh_password)


def _sftp_many_to_media(file_pairs, timeout=90, ssh_password=None):
    ssh_password = _effective_media_password(ssh_password)
    client, err = _media_password_client(ssh_password)
    if err:
        return {"ok": False, "code": -1, "output": err}
    sftp = None
    try:
        sftp = client.open_sftp()
        for local_path, remote_name in file_pairs:
            remote_path = (MEDIA_HOME + "/%s") % remote_name.replace("\\", "/").lstrip("/")
            remote_dir = os.path.dirname(remote_path)
            cur = ""
            for part in [p for p in remote_dir.split("/") if p]:
                cur += "/" + part
                try:
                    sftp.stat(cur)
                except IOError:
                    sftp.mkdir(cur)
            sftp.put(local_path, remote_path)
        return {"ok": True, "code": 0, "output": ""}
    except Exception as exc:
        return {"ok": False, "code": -1, "output": str(exc)}
    finally:
        if sftp is not None:
            try:
                sftp.close()
            except Exception:
                pass
        if client is not None:
            try:
                client.close()
            except Exception:
                pass


def _sftp_from_media(remote_name, local_path, timeout=90, ssh_password=None):
    """Copy <media home>/<remote_name> FROM the media box to local_path, whole --
    _run_media_password clips its output, so `cat` cannot carry a page."""
    ssh_password = _effective_media_password(ssh_password)
    client, err = _media_password_client(ssh_password)
    if err:
        return {"ok": False, "code": -1, "output": err}
    sftp = None
    try:
        sftp = client.open_sftp()
        sftp.get((MEDIA_HOME + "/%s") % remote_name.replace("\\", "/").lstrip("/"), local_path)
        return {"ok": True, "code": 0, "output": ""}
    except Exception as exc:
        return {"ok": False, "code": -1, "output": str(exc)}
    finally:
        if sftp is not None:
            try:
                sftp.close()
            except Exception:
                pass
        if client is not None:
            try:
                client.close()
            except Exception:
                pass


def _scp_to_media(local_path, remote_name, timeout=90, ssh_password=None):
    ssh_password = _effective_media_password(ssh_password)
    if not _ssh_available():
        if _ssh_password_available(ssh_password):
            return _sftp_to_media(local_path, remote_name, timeout=timeout, ssh_password=ssh_password)
        return {
            "ok": False,
            "code": -1,
            "output": "Media SSH bridge is not configured for this console user.",
        }
    if not _path_is_file(local_path):
        return {"ok": False, "code": -1, "output": "missing source file: %s" % local_path}
    cmd = [
        "scp", "-i", MEDIA_SSH_KEY,
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=no",
        "-o", "ConnectTimeout=8",
        local_path,
        "%s@%s:%s/%s" % (MEDIA_USER, MEDIA_HOST, MEDIA_HOME, remote_name),
    ]
    res = _run_local(cmd, timeout=timeout)
    if not res.get("ok") and _ssh_password_available(ssh_password):
        return _sftp_to_media(local_path, remote_name, timeout=timeout, ssh_password=ssh_password)
    return res


def _git_status():
    if not _path_is_dir(SERVICE_REPO):
        return {"ok": False, "code": -1, "output": "repo not found: %s" % SERVICE_REPO}
    branch_res = _run_local(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=SERVICE_REPO, timeout=30)
    if not branch_res["ok"]:
        return {"ok": False, "code": branch_res["code"], "output": branch_res["output"]}
    branch = branch_res["output"].strip()
    status_res = _run_local(["git", "status", "--short"], cwd=SERVICE_REPO, timeout=30)
    if not status_res["ok"]:
        return {"ok": False, "code": status_res["code"], "output": status_res["output"]}
    files = []
    for line in status_res["output"].splitlines():
        if not line.strip():
            continue
        code = line[:2].strip()
        path = line[3:]
        files.append({"path": path, "status": code})
    return {"ok": True, "code": 0, "branch": branch, "files": files}


def _git_commit(message, files=None):
    msg = (message or "").strip()
    if not msg:
        return {"ok": False, "code": -1, "output": "commit message is required"}
    if not _path_is_dir(SERVICE_REPO):
        return {"ok": False, "code": -1, "output": "repo not found: %s" % SERVICE_REPO}
    outputs = []
    add_args = ["git", "add", "."]
    if files:
        add_args = ["git", "add", "--"] + list(files)
    for args in (
        add_args,
        ["git", "commit", "-m", msg],
        ["git", "status", "--short"],
        ["git", "log", "-3", "--oneline"],
    ):
        res = _run_local(args, cwd=SERVICE_REPO, timeout=60)
        outputs.append("> " + " ".join(args))
        if res["output"]:
            outputs.append(res["output"])
        if not res["ok"] and args[:2] != ["git", "commit"]:
            return {"ok": False, "code": res["code"], "output": _clip_text("\n".join(outputs))}
    return {"ok": True, "code": 0, "output": _clip_text("\n".join(outputs))}


def _node_exe():
    return shutil.which("node") or r"C:\Program Files\nodejs\node.exe"


def _target_files(target):
    key, cfg = _target_config(target)
    return key, list(cfg.get("files") or [])


def _git_dirty_for(files):
    res = _run_local(["git", "status", "--short", "--"] + files, cwd=MEDIA_WORKSPACE, timeout=30)
    return res["output"].strip() if res["ok"] else ""


@app.get("/api/git/status")
def api_git_status():
    res = _git_status()
    if not res.get("ok"):
        return JSONResponse(status_code=500, content={"ok": False, "error": res.get("output")})
    return {"branch": res.get("branch", ""), "files": res.get("files", [])}


@app.get("/api/git/diff")
def api_git_diff(path: str = ""):
    if not _path_is_dir(SERVICE_REPO):
        return JSONResponse(status_code=500, content={"ok": False, "error": "repo not found: %s" % SERVICE_REPO})
    args = ["git", "diff"]
    if path:
        args += ["--", path]
    res = _run_local(args, cwd=SERVICE_REPO, timeout=30)
    if not res["ok"]:
        return JSONResponse(status_code=500, content={"ok": False, "error": res.get("output")})
    return Response(content=res.get("output", ""), media_type="text/plain")


@app.post("/api/git/commit")
def api_git_commit(payload: dict):
    message = (payload.get("message") or "").strip()
    if not message:
        return JSONResponse(status_code=400, content={"ok": False, "error": "commit message is required"})
    res = _git_commit(message)
    if not res.get("ok"):
        return JSONResponse(status_code=500, content={"ok": False, "error": res.get("output")})
    return {"ok": True, "output": res.get("output", "")}


_GIT_TASK_JOBS = {}


def _run_git_task_propose(job_id, description):
    """Background worker: diagnose + write the exact edits as a proposal (nothing
    applied). Same work as antons_propose_fix. Stores the result in
    _GIT_TASK_JOBS[job_id]."""
    try:
        from app import propose_patch as _pp
        from app import messaging as _M
        acfg = _M.load_config().get("auto_reply") or {}
        res = _pp.propose(
            description, acfg, allowed=True,
            chat_guid="git-page", who="git-page",
            max_steps=14, max_model_calls=24,
            deadline=time.time() + 900.0)
        if isinstance(res, dict) and res.get("ok") and res.get("proposal"):
            try:
                summary = _pp.render(res, lang="lv")
            except Exception:
                summary = ""
            _GIT_TASK_JOBS[job_id] = {
                "status": "done",
                "proposal": res.get("proposal"),
                "summary": summary or "(proposal written)",
                "files": [f.get("path") for f in (res["proposal"].get("files") or [])
                          if isinstance(f, dict) and f.get("path")],
            }
        else:
            reason = (res or {}).get("reason") or "no proposal produced"
            _GIT_TASK_JOBS[job_id] = {"status": "failed", "error": reason}
    except Exception as exc:
        _GIT_TASK_JOBS[job_id] = {"status": "failed", "error": str(exc)[:500]}


@app.post("/api/git/task")
def api_git_task(payload: dict):
    description = (payload.get("description") or "").strip()
    if not description:
        return JSONResponse(status_code=400, content={"ok": False, "error": "task description is required"})
    job_id = uuid.uuid4().hex
    _GIT_TASK_JOBS[job_id] = {"status": "pending"}
    t = threading.Thread(target=_run_git_task_propose, args=(job_id, description), daemon=True)
    t.start()
    return {"ok": True, "job_id": job_id}


@app.get("/api/git/task/status")
def api_git_task_status(job_id: str = ""):
    job = _GIT_TASK_JOBS.get(job_id)
    if job is None:
        return JSONResponse(status_code=404, content={"ok": False, "error": "unknown job_id"})
    return {"ok": True, "status": job.get("status", "pending"), **{k: v for k, v in job.items() if k != "status"}}


@app.get("/api/git/task/{job_id}")
def api_git_task_poll(job_id: str):
    job = _GIT_TASK_JOBS.get(job_id)
    if job is None:
        return JSONResponse(status_code=404, content={"ok": False, "error": "unknown job_id"})
    return {"ok": True, "status": job.get("status", "pending"), **{k: v for k, v in job.items() if k != "status"}}
    return {"ok": True, "job_id": job_id, **job}


def _save_failed_jellyfin_diff(reason):
    failed_dir = os.path.join(MEDIA_WORKSPACE, "media-ai-safety", "failed-deploy-diffs")
    try:
        os.makedirs(failed_dir, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = os.path.join(failed_dir, "jellyfin-ui-%s.patch" % stamp)
        res = _run_local(["git", "diff", "--"] + JELLYFIN_RESET_FILES, cwd=MEDIA_WORKSPACE, timeout=30)
        body = "Reason: %s\n\n%s\n" % (reason, res.get("output") or "(no diff)")
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(body)
        return path
    except Exception:
        return ""


def _brand_prefix_check():
    """Catch half-renames (e.g. Luma->Luna in some CSS selectors while the JS
    still writes luma-* class names). Such edits pass node --check and
    git diff --check but break the UI, so validation must catch them
    mechanically: the brand prefix must be uniform across the JS/CSS bundle."""
    per_file = {}
    for name in JELLYFIN_JS_FILES + [JELLYFIN_CSS_FILE]:
        path = os.path.join(MEDIA_WORKSPACE, name)
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            continue
        found = {m.lower() for m in re.findall(r"\b(luma|luna)(?=-[a-z])", text, re.I)}
        if found:
            per_file[name] = found
    union = set()
    for prefixes in per_file.values():
        union |= prefixes
    if len(union) > 1:
        detail = "; ".join("%s uses %s" % (n, "+".join(sorted(p))) for n, p in sorted(per_file.items()))
        return False, ("mixed brand prefixes %s across the bundle (half-rename): %s"
                       % ("+".join(sorted(union)), detail))
    return True, "brand prefix uniform (%s)" % ("+".join(sorted(union)) or "none")


def _validate_jellyfin_ui_source(auto_reset=False):
    if not _path_is_dir(MEDIA_WORKSPACE):
        return {"ok": False, "code": -1, "output": "workspace not found: %s" % MEDIA_WORKSPACE}

    outputs = ["== Validate Jellyfin UI Source =="]
    failures = []

    def add_result(label, res):
        outputs.append("> " + label)
        if res.get("output"):
            outputs.append(res["output"])
        if not res.get("ok"):
            failures.append(label)

    res = _run_local(["git", "diff", "--check", "--"] + JELLYFIN_SOURCE_FILES, cwd=MEDIA_WORKSPACE, timeout=30)
    add_result("git diff --check", res)

    css_path = os.path.join(MEDIA_WORKSPACE, JELLYFIN_CSS_FILE)
    try:
        with open(css_path, "r", encoding="utf-8") as f:
            css = f.read().strip()
        if css.startswith(JELLYFIN_CSS_START) and css.endswith(JELLYFIN_CSS_END):
            outputs.append("PASS CSS deployment markers are intact")
        else:
            outputs.append("FAIL CSS deployment markers are missing or not at file boundaries")
            failures.append("css markers")
    except Exception as exc:
        outputs.append("FAIL CSS read failed: %s" % exc)
        failures.append("css read")

    brand_ok, brand_msg = _brand_prefix_check()
    if brand_ok:
        outputs.append("PASS " + brand_msg)
    else:
        outputs.append("FAIL " + brand_msg)
        failures.append("brand prefix consistency")

    node = _node_exe()
    if not node or not os.path.isfile(node):
        outputs.append("FAIL node.exe not found; cannot syntax-check Jellyfin JavaScript")
        failures.append("node missing")
    else:
        for name in JELLYFIN_JS_FILES:
            path = os.path.join(MEDIA_WORKSPACE, name)
            add_result("node --check " + name, _run_local([node, "--check", path], cwd=MEDIA_WORKSPACE, timeout=30))

    apply_path = os.path.join(MEDIA_WORKSPACE, "apply-jellyfin-mobile-ui.py")
    add_result(
        "py_compile apply-jellyfin-mobile-ui.py",
        _run_local([sys.executable, "-m", "py_compile", apply_path], cwd=MEDIA_WORKSPACE, timeout=30),
    )

    if not failures:
        outputs.append("OK Jellyfin UI source is deployable.")
        return {"ok": True, "code": 0, "output": _clip_text("\n".join(outputs))}

    outputs.append("FAILED checks: " + ", ".join(failures))
    if auto_reset:
        dirty = _git_dirty_for(JELLYFIN_RESET_FILES)
        if dirty:
            diff_path = _save_failed_jellyfin_diff(", ".join(failures))
            if diff_path:
                outputs.append("Saved failed diff: %s" % diff_path)
            outputs.append("Edits were preserved. Use Reset uncommitted only if you explicitly want to discard them.")
        else:
            outputs.append("No dirty Jellyfin UI files to preserve.")
    return {"ok": False, "code": 1, "output": _clip_text("\n".join(outputs))}


def _reset_jellyfin_source():
    if not _path_is_dir(MEDIA_WORKSPACE):
        return {"ok": False, "code": -1, "output": "workspace not found: %s" % MEDIA_WORKSPACE}
    outputs = ["== Reset Jellyfin UI Source To HEAD =="]
    dirty_before = _git_dirty_for(JELLYFIN_RESET_FILES)
    outputs.append(dirty_before or "(target files already clean)")
    res = _run_local(["git", "restore", "--worktree", "--"] + JELLYFIN_RESET_FILES, cwd=MEDIA_WORKSPACE, timeout=60)
    if res.get("output"):
        outputs.append(res["output"])
    status = _run_local(["git", "status", "--short", "--"] + JELLYFIN_RESET_FILES, cwd=MEDIA_WORKSPACE, timeout=30)
    outputs.append("== Target Status ==")
    outputs.append(status.get("output") or "(clean)")
    return {"ok": bool(res.get("ok")), "code": res.get("code", 0), "output": _clip_text("\n".join(outputs))}


def _revert_last_commit_and_deploy_jellyfin(ssh_password=None):
    if not _path_is_dir(MEDIA_WORKSPACE):
        return {"ok": False, "code": -1, "output": "workspace not found: %s" % MEDIA_WORKSPACE}
    dirty = _run_local(["git", "status", "--short"], cwd=MEDIA_WORKSPACE, timeout=30)
    if not dirty.get("ok"):
        return dirty
    if dirty.get("output", "").strip():
        return {
            "ok": False,
            "code": 1,
            "output": "Refusing to revert with a dirty workspace.\n\n" + dirty["output"],
        }
    outputs = ["== Git Revert HEAD =="]
    res = _run_local(["git", "revert", "--no-edit", "HEAD"], cwd=MEDIA_WORKSPACE, timeout=120)
    if res.get("output"):
        outputs.append(res["output"])
    if not res.get("ok"):
        return {"ok": False, "code": res.get("code", 1), "output": _clip_text("\n".join(outputs))}
    outputs.append("== Deploy Reverted Jellyfin UI ==")
    deploy = _deploy_target("jellyfin-ui", ssh_password=ssh_password)
    outputs.append(deploy["output"])
    return {"ok": deploy["ok"], "code": deploy["code"], "output": _clip_text("\n\n".join(outputs))}


def _restore_latest_jellyfin_backup(ssh_password=None):
    return _run_media([MEDIA_AI_SH, "rollback", "jellyfin-ui", "latest"], timeout=300, ssh_password=ssh_password)


def _jellyfin_screenshot():
    node = _node_exe()
    if not node or not os.path.isfile(node):
        return {"ok": False, "code": -1, "output": "node.exe not found"}
    if not _path_is_file(JELLYFIN_SCREENSHOT_TOOL):
        return {"ok": False, "code": -1, "output": "screenshot tool missing: %s" % JELLYFIN_SCREENSHOT_TOOL}
    env = {
        "JELLYFIN_URL": JELLYFIN_PUBLIC_URL,
        "JELLYFIN_STORAGE_STATE": JELLYFIN_STORAGE_STATE,
        "JELLYFIN_SCREENSHOT_OUT": JELLYFIN_SCREENSHOT_DIR,
    }
    res = _run_local([node, JELLYFIN_SCREENSHOT_TOOL], cwd=os.path.dirname(JELLYFIN_SCREENSHOT_TOOL), timeout=120, env_extra=env)
    return res


def _target_config(target):
    key = (target or "media-console").strip()
    if key not in MEDIA_TARGETS:
        raise ValueError("unknown target: %s" % key)
    return key, MEDIA_TARGETS[key]


def _push_target_sources(target, ssh_password=None):
    ssh_password = _effective_media_password(ssh_password)
    key, cfg = _target_config(target)
    outputs = ["== Push %s files to media server ==" % cfg["label"]]
    pairs = []
    for name in cfg["files"]:
        local_path = os.path.join(MEDIA_WORKSPACE, name)
        if not _path_is_file(local_path):
            return {
                "ok": False,
                "code": -1,
                "output": _clip_text("\n".join(outputs + ["missing source file: %s" % local_path])),
            }
        pairs.append((local_path, name))
        outputs.append("> %s" % name)

    if _ssh_password_available(ssh_password):
        res = _sftp_many_to_media(pairs, timeout=max(90, 20 * len(pairs)), ssh_password=ssh_password)
        if res["output"]:
            outputs.append(res["output"])
        if res["ok"]:
            return {"ok": True, "code": 0, "output": _clip_text("\n".join(outputs))}
        outputs.append("password SFTP failed; trying configured SSH key")

    for local_path, name in pairs:
        res = _scp_to_media(local_path, name, ssh_password=ssh_password)
        if res["output"]:
            outputs.append(res["output"])
        if not res["ok"]:
            if not _ssh_password_available(ssh_password):
                outputs.append(
                    "Media password was not supplied. If the SSH key is locked or the dashboard is not running as "
                    "SysMonitor, enter the media password in the Code tab and run again. Edits were preserved."
                )
            return {"ok": False, "code": res["code"], "output": _clip_text("\n".join(outputs))}
    return {"ok": True, "code": 0, "output": _clip_text("\n".join(outputs))}


def _deploy_target(target, sudo_password=None, message=None, ssh_password=None):
    ssh_password = _effective_media_password(ssh_password)
    sudo_password = (sudo_password or "").strip() or ssh_password
    key, cfg = _target_config(target)
    outputs = []

    if key == "jellyfin-ui":
        res = _validate_jellyfin_ui_source(auto_reset=True)
        outputs.append(res["output"])
        if not res["ok"]:
            return {"ok": False, "code": res["code"], "output": _clip_text("\n\n".join(outputs))}

    res = _push_target_sources(key, ssh_password=ssh_password)
    outputs.append(res["output"])
    if not res["ok"]:
        return {"ok": False, "code": res["code"], "output": _clip_text("\n\n".join(outputs))}

    outputs.append("== Deploy %s ==" % cfg["label"])
    if cfg["needs_sudo"]:
        res = _run_media_sudo(cfg["deploy"], sudo_password, timeout=420, ssh_password=ssh_password)
    else:
        res = _run_media(cfg["deploy"], timeout=420, ssh_password=ssh_password)
    outputs.append(res["output"])
    if not res["ok"]:
        return {"ok": False, "code": res["code"], "output": _clip_text("\n\n".join(outputs))}

    outputs.append("== Sync deployed state back to big rig ==")
    res = _run_media(MEDIA_COMMANDS["sync-big-rig"], timeout=240, ssh_password=ssh_password)
    outputs.append(res["output"])
    if not res["ok"]:
        return {"ok": False, "code": res["code"], "output": _clip_text("\n\n".join(outputs))}

    commit_msg = (message or "").strip()
    if commit_msg:
        outputs.append("== Commit on big rig ==")
        res = _git_commit(commit_msg, files=cfg.get("files"))
        outputs.append(res["output"])
        if not res["ok"]:
            return {"ok": False, "code": res["code"], "output": _clip_text("\n\n".join(outputs))}

    return {"ok": True, "code": 0, "output": _clip_text("\n\n".join(outputs))}


def _run_media_ai_fix(target, task, deploy_after=False, sudo_password=None, message=None, ssh_password=None):
    key, cfg = _target_config(target)
    task = (task or "").strip()
    if not task:
        return {"ok": False, "code": -1, "output": "task is required"}
    if not _path_is_dir(MEDIA_WORKSPACE):
        return {"ok": False, "code": -1, "output": "workspace not found: %s" % MEDIA_WORKSPACE}

    guarded_task = (
        "You are editing the media-server-workspace for target '%s'. "
        "Keep changes scoped to these files when possible: %s. "
        "Do not edit secrets, media libraries, or unrelated projects. "
        "Make the SMALLEST diff that satisfies the task. For branding/name/wordmark "
        "changes edit user-visible STRINGS only (displayed text, titles, labels); "
        "NEVER rename CSS classes, CSS custom properties (--luma-*), JS identifiers, "
        "or HTML classes/ids - the JS writes those class names and the CSS selects on "
        "them, so a partial rename breaks the whole UI. Do not reformat or re-indent "
        "code you are not changing. "
        "After edits, run focused syntax/preflight checks when available. "
        "User task: %s"
    ) % (key, ", ".join(cfg["files"]), task)

    # Media/Jellyfin edits must use WSL. Native Windows OpenCode repeatedly
    # used PowerShell-shaped commands and damaged source, so do not fall back.
    status = coder.runner_status(wsl_run_enabled=True)
    runner = status.get("recommended_runner") or coder.RUNNER_MANUAL
    if runner == coder.RUNNER_MANUAL:
        return {
            "ok": False, "code": -1,
            "output": ("No live AI runner is available: WSL is required for media/Jellyfin edits "
                       "and native Windows OpenCode is disabled."),
        }

    outputs = ["== Local AI edit on big rig ==", "> target: %s" % cfg["label"],
               "> runner: %s" % runner]
    code = 0
    try:
        it = coder.run_opencode(
            MEDIA_WORKSPACE,
            guarded_task,
            runner=runner,
            explicit_request=True,
        )
        try:
            for evt in it:
                et = evt.get("type")
                if et in ("out", "error", "diff"):
                    txt = evt.get("text") or ""
                    if txt:
                        outputs.append(txt)
                elif et == "done":
                    code = int(evt.get("code") or 0)
                    outputs.append("= AI edit finished (exit %s)" % code)
        finally:
            it.close()
    except Exception as e:
        return {"ok": False, "code": -1, "output": _clip_text("\n".join(outputs + [str(e)]))}

    if code != 0:
        return {"ok": False, "code": code, "output": _clip_text("\n".join(outputs))}

    changed = _git_dirty_for(cfg["files"])
    if not changed:
        outputs.append("AI edit made no source changes; deploy aborted.")
        return {"ok": False, "code": 2, "output": _clip_text("\n".join(outputs))}

    if deploy_after:
        deploy_res = _deploy_target(key, sudo_password=sudo_password, message=message, ssh_password=ssh_password)
        outputs.append(deploy_res["output"])
        return {
            "ok": deploy_res["ok"],
            "code": deploy_res["code"],
            "output": _clip_text("\n\n".join(outputs)),
        }

    return {"ok": True, "code": 0, "output": _clip_text("\n".join(outputs))}


def _ollama_review(prompt):
    prompt = (prompt or "").strip()
    if not prompt:
        return {"ok": False, "code": -1, "output": "review prompt is required"}
    payload = {
        "model": MEDIA_MODEL,
        "prompt": prompt,
        "stream": False,
        "keep_alive": "30m",
        "options": {"temperature": 0.1},
    }
    try:
        out = _http_json(OLLAMA + "/api/generate", timeout=300, payload=payload)
        return {"ok": True, "code": 0, "output": _clip_text(out.get("response") or "")}
    except Exception as e:
        return {"ok": False, "code": -1, "output": str(e)}
    finally:
        messaging._ollama_warm()


@app.get("/api/mediaops")
def api_mediaops_status():
    try:
        return _mediaops_status_payload()
    except Exception as exc:
        return {
            "ok": False,
            "error": str(exc),
            "media_host": MEDIA_HOST,
            "media_user": MEDIA_USER,
            "ssh_configured": False,
            "ssh_key": MEDIA_SSH_KEY,
            "workspace": MEDIA_WORKSPACE,
            "workspace_exists": False,
            "model": MEDIA_MODEL,
            "targets": {},
            "actions": [],
            "privileged_note": "Media actions can use the per-run Media password for SSH/SFTP fallback and sudo; it is not stored.",
        }


def _mediaops_status_payload():
    return {
        "ok": True,
        "media_host": MEDIA_HOST,
        "media_user": MEDIA_USER,
        "ssh_configured": _ssh_available(),
        "ssh_key": MEDIA_SSH_KEY,
        "workspace": MEDIA_WORKSPACE,
        "workspace_exists": _path_is_dir(MEDIA_WORKSPACE),
        "model": MEDIA_MODEL,
        "jellyfin_url": JELLYFIN_PUBLIC_URL,
        "screenshot_auth_ready": _path_is_file(JELLYFIN_STORAGE_STATE),
        "screenshot_tool_ready": _path_is_file(JELLYFIN_SCREENSHOT_TOOL),
        "media_password_default_ready": bool(_default_media_password()),
        "targets": {k: {"label": v["label"], "needs_sudo": v["needs_sudo"]} for k, v in MEDIA_TARGETS.items()},
        "actions": sorted(MEDIA_COMMANDS.keys()) + [
            "deploy-target", "safe-fix", "big-rig-status", "big-rig-commit",
            "local-review", "jellyfin-validate-source", "jellyfin-reset-source",
            "jellyfin-restore-latest", "jellyfin-revert-deploy", "jellyfin-screenshot",
        ],
        "privileged_note": "Media actions can use the per-run Media password for SSH/SFTP fallback and sudo; it is not stored.",
    }


# Streaming variant of safe-fix: the plain POST blocks for the whole AI edit +
# deploy (minutes) with zero feedback; this yields agent output live, heartbeat
# ticks during silence, and per-phase markers during the deploy.
_PHASE_POOL = ThreadPoolExecutor(max_workers=4)


def _phase_events(label, holder, fn, *args, **kwargs):
    """Run fn in a worker; yield a phase marker + ticks while it runs.
    fn must return the usual {'ok','code','output'} dict -> holder[0]."""
    yield {"type": "phase", "text": label}
    fut = _PHASE_POOL.submit(fn, *args, **kwargs)
    t0 = time.time()
    while True:
        try:
            holder[0] = fut.result(timeout=5)
            break
        except FuturesTimeoutError:
            yield {"type": "tick", "seconds": int(time.time() - t0), "text": label}
    res = holder[0]
    if res.get("output"):
        yield {"type": "out", "text": res["output"]}


@app.post("/api/mediaops/safe-fix/stream")
def api_mediaops_safe_fix_stream(body: MediaOpsRequest):
    try:
        key, cfg = _target_config(body.target)
    except ValueError as exc:
        return _api_error(str(exc), status_code=400)
    task = (body.task or "").strip()
    if not task:
        return _api_error("task is required", status_code=400)
    if not _path_is_dir(MEDIA_WORKSPACE):
        return _api_error("workspace not found: %s" % MEDIA_WORKSPACE, status_code=500)

    ssh_password = _request_media_password(body)
    deploy_after = bool(body.deploy)

    def _j(evt):
        return (json.dumps(evt) + "\n").encode("utf-8")

    def gen():
        status = coder.runner_status(wsl_run_enabled=True)
        runner = status.get("recommended_runner") or coder.RUNNER_MANUAL
        if runner == coder.RUNNER_MANUAL:
            yield _j({"type": "error",
                      "text": "No live AI runner is available: WSL is required for media/Jellyfin edits "
                              "and native Windows OpenCode is disabled."})
            yield _j({"type": "done", "code": -1, "ok": False})
            return
        if key == "jellyfin-ui":
            dirty_before = _git_dirty_for(JELLYFIN_RESET_FILES)
            if dirty_before:
                yield _j({"type": "error",
                          "text": "Jellyfin source is already dirty before the AI run; refusing to start so Stop cannot discard existing work.\n" + dirty_before})
                yield _j({"type": "done", "code": -1, "ok": False})
                return

        guarded_task = (
            "You are editing the media-server-workspace for target '%s'. "
            "Keep changes scoped to these files when possible: %s. "
            "Do not edit secrets, media libraries, or unrelated projects. "
            "Make the SMALLEST diff that satisfies the task. For branding/name/wordmark "
            "changes edit user-visible STRINGS only (displayed text, titles, labels); "
            "NEVER rename CSS classes, CSS custom properties (--luma-*), JS identifiers, "
            "or HTML classes/ids - the JS writes those class names and the CSS selects on "
            "them, so a partial rename breaks the whole UI. Do not reformat or re-indent "
            "code you are not changing. "
            "After edits, run focused syntax/preflight checks when available. "
            "User task: %s"
        ) % (key, ", ".join(cfg["files"]), task)

        yield _j({"type": "phase", "text": "AI edit (%s via %s)" % (cfg["label"], runner)})
        exit_code = None
        it = coder.run_opencode(MEDIA_WORKSPACE, guarded_task, runner=runner, explicit_request=True)
        aborted = False
        try:
            for evt in it:
                if evt.get("type") == "done":
                    exit_code = evt.get("code")
                yield _j(evt)
        except GeneratorExit:
            aborted = True
            raise
        finally:
            it.close()
            if aborted and key == "jellyfin-ui":
                _reset_jellyfin_source()
        if exit_code != 0:
            yield _j({"type": "error", "text": "AI edit failed (exit %s); nothing was deployed." % exit_code})
            return

        changed = _git_dirty_for(cfg["files"])
        if not changed:
            yield _j({"type": "error", "text": "AI edit made no source changes; deploy aborted. This usually means the model only wrote analysis instead of editing files."})
            yield _j({"type": "done", "code": 2, "ok": False})
            return

        for evt in _rename_verify_events(MEDIA_WORKSPACE, task, runner, files=cfg["files"]):
            yield _j(evt)

        if not deploy_after:
            yield _j({"type": "phase", "text": "done (no deploy requested)"})
            yield _j({"type": "done", "code": 0, "ok": True})
            return

        holder = [None]
        if key == "jellyfin-ui":
            for evt in _phase_events("validate Jellyfin UI source", holder,
                                     _validate_jellyfin_ui_source, auto_reset=True):
                yield _j(evt)
            if not holder[0].get("ok"):
                yield _j({"type": "error", "text": "validation failed; deploy aborted. Edits were preserved; use Reset only if you want to discard them."})
                yield _j({"type": "done", "code": 1, "ok": False})
                return

        for evt in _phase_events("push sources to media server", holder,
                                 _push_target_sources, key, ssh_password=ssh_password):
            yield _j(evt)
        if not holder[0].get("ok"):
            yield _j({"type": "done", "code": 1, "ok": False})
            return

        if cfg["needs_sudo"]:
            for evt in _phase_events("deploy " + cfg["label"], holder, _run_media_sudo,
                                     cfg["deploy"], body.sudo_password or ssh_password, timeout=420, ssh_password=ssh_password):
                yield _j(evt)
        else:
            for evt in _phase_events("deploy " + cfg["label"], holder, _run_media,
                                     cfg["deploy"], timeout=420, ssh_password=ssh_password):
                yield _j(evt)
        if not holder[0].get("ok"):
            yield _j({"type": "done", "code": 1, "ok": False})
            return

        for evt in _phase_events("sync deployed state back to big rig", holder,
                                 _run_media, MEDIA_COMMANDS["sync-big-rig"], timeout=240,
                                 ssh_password=ssh_password):
            yield _j(evt)

        commit_msg = (body.message or "").strip()
        if commit_msg:
            for evt in _phase_events("commit on big rig", holder, _git_commit, commit_msg, files=cfg.get("files")):
                yield _j(evt)

        yield _j({"type": "done", "code": 0, "ok": True})

    return StreamingResponse(gen(), media_type="application/x-ndjson")


@app.post("/api/mediaops/{action}")
def api_mediaops_action(action: str, body: MediaOpsRequest | None = None):
    try:
        body = body or MediaOpsRequest()
        ssh_password = _request_media_password(body)
        if action in MEDIA_COMMANDS:
            return _run_media(
                MEDIA_COMMANDS[action],
                timeout=180 if action in ("preflight", "sync-big-rig") else 90,
                ssh_password=ssh_password,
            )
        if action == "big-rig-status":
            return _git_status()
        if action == "big-rig-commit":
            return _git_commit(body.message)
        if action == "local-review":
            return _ollama_review(body.prompt)
        if action == "jellyfin-validate-source":
            return _validate_jellyfin_ui_source(auto_reset=False)
        if action == "jellyfin-reset-source":
            return _reset_jellyfin_source()
        if action == "jellyfin-restore-latest":
            return _restore_latest_jellyfin_backup(ssh_password=ssh_password)
        if action == "jellyfin-revert-deploy":
            return _revert_last_commit_and_deploy_jellyfin(ssh_password=ssh_password)
        if action == "jellyfin-screenshot":
            return _jellyfin_screenshot()
        if action == "deploy-target":
            return _deploy_target(body.target, sudo_password=body.sudo_password or ssh_password, message=body.message, ssh_password=ssh_password)
        if action == "safe-fix":
            return _run_media_ai_fix(
                body.target,
                body.task,
                deploy_after=bool(body.deploy),
                sudo_password=body.sudo_password or ssh_password,
                message=body.message,
                ssh_password=ssh_password,
            )
        return _api_error("unknown media op '%s'" % action, status_code=404)
    except ValueError as exc:
        return _api_error(str(exc), status_code=400)
    except Exception as exc:
        return _api_error(str(exc), status_code=500)



@app.get("/")
def index():
    return FileResponse("app/static/index.html", headers={
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        "Pragma": "no-cache",
        "Expires": "0",
    })


@app.get("/me")
def personal():
    # Personal app: assistant chat, goals, memory, messages. Same backend and
    # same app.js as the console at "/" — only the shell differs.
    return FileResponse("app/static/me.html", headers={
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        "Pragma": "no-cache",
        "Expires": "0",
    })


@app.get("/label")
def label_page():
    return FileResponse("app/static/label.html", headers={
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    })


_LABEL_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "tests", "fixtures", "label_sheet.json")


@app.get("/api/label/cases")
def api_label_cases():
    try:
        with open(_LABEL_PATH, encoding="utf-8") as fh:
            return {"ok": True, "cases": json.load(fh)}
    except Exception as exc:
        return _api_error(str(exc), status_code=500)


@app.post("/api/label/set")
def api_label_set(body: dict):
    try:
        with open(_LABEL_PATH, encoding="utf-8") as fh:
            cases = json.load(fh)
        n = int((body or {}).get("n") or 0)
        label = str((body or {}).get("label") or "").upper()[:1]
        for case in cases:
            if case.get("n") == n:
                case["label"] = label
                break
        with open(_LABEL_PATH, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(cases, fh, ensure_ascii=False, indent=1)
        done = sum(1 for c in cases if c.get("label"))
        return {"ok": True, "labelled": done, "total": len(cases)}
    except Exception as exc:
        return _api_error(str(exc), status_code=500)


@app.get("/mini")
def mini():
    # In-case iPhone 4S dashboard. Safari standalone PWAs can cling to old
    # HTML aggressively, so this route must never be cached.
    return FileResponse("app/static/mini.html", headers={
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        "Pragma": "no-cache",
        "Expires": "0",
    })


@app.get("/mini-v2")
def mini_v2():
    return mini()


@app.get("/pet")
def pet():
    # The "living creature" system monitor (app/static/pet.html), authored by
    # antons_build. Same no-cache headers as /mini -- a standalone iOS webview
    # clings to old HTML hard.
    return FileResponse("app/static/pet.html", headers={
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        "Pragma": "no-cache",
        "Expires": "0",
    })


@app.get("/api/status")
def api_status():
    return {
        "status": "running",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "uptime": round(time.time() - start_time, 1),
    }


@app.get("/api/hosts")
def api_hosts():
    """Links to the other machines, for the dashboard (addresses come from the environment)."""
    return {"media_console": REMOTE_CONSOLE, "jellyfin": JELLYFIN_PUBLIC_URL}


@app.get("/api/health")
def api_health():
    """Lightweight liveness probe (no hardware access)."""
    account = None
    try:
        account = psutil.Process().username()
    except Exception:
        pass
    return {
        "ok": True,
        "status": "running",
        "hostname": HOSTNAME,
        "rig_name": RIG_NAME,
        "uptime_seconds": round(time.time() - start_time, 1),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        # Fan control / chip access need the elevated SysMonitor service; a
        # manual dev uvicorn shows elevated=false and the UI explains itself.
        "elevated": fan_control.is_elevated(),
        "service_account": account,
        "sidecars": sidecar_health.last_snapshot(),
    }


@app.get("/api/sidecars")
def api_sidecars(deep: bool = False):
    """Fresh probe of the morph / NMT / F5 / MMS daemons -- does each one
    actually do its job, not just answer its socket. `?deep=1` makes F5
    really synthesise (slow, ~1-2 min cold)."""
    return sidecar_health.check(deep=deep)


class FanModeRequest(BaseModel):
    mode: str
    channel: int | None = None


@app.post("/api/fan/rescan")
def api_fan_rescan():
    """Retry fan-control discovery after a failed init (no restart needed
    unless the native layer wedged)."""
    return fan_control.rescan()


@app.get("/api/cpu/fan")
def api_cpu_fan_get():
    return fan_control.status()


@app.post("/api/cpu/fan")
def api_cpu_fan_set(body: FanModeRequest):
    result = fan_control.set_mode(body.mode, channel=body.channel)
    if not result.get("ok"):
        return JSONResponse(status_code=503, content=result)
    return result


@app.get("/api/gpu/fan")
def api_gpu_fan_get():
    return fan_control.gpu_status()


@app.post("/api/gpu/fan")
def api_gpu_fan_set(body: FanModeRequest):
    result = fan_control.set_gpu_mode(body.mode, channel=body.channel)
    if not result.get("ok"):
        return JSONResponse(status_code=503, content=result)
    return result



# The lighting engine needs vitals several times a second, but nvidia-smi is a
# subprocess. /api/stats already pays that cost for the dashboard, so it
# publishes here and the engine reuses the reading; we only sample directly when
# nobody has polled recently (i.e. no browser open).
_VITALS = {"t": 0.0, "gpu_temp": None, "cpu_temp": None, "gpu_util": None,
           "cpu_load": None, "model_loaded": False}
_OLLAMA_PS = {"t": 0.0, "loaded": False}
_GOAL_PROGRESS = {"t": 0.0, "v": None}


def _publish_vitals(gpu, cpu, cpu_percent):
    try:
        _VITALS.update({
            "t": time.time(),
            "gpu_temp": (gpu or {}).get("temperature_c"),
            "gpu_util": (gpu or {}).get("gpu_utilization_percent"),
            "cpu_temp": (cpu or {}).get("max_c"),
            "cpu_load": cpu_percent,
        })
    except Exception:
        pass


def _model_loaded():
    """Is anything resident in VRAM? Cached — /api/ps is a network round trip."""
    now = time.time()
    if now - _OLLAMA_PS["t"] < 15.0:
        return _OLLAMA_PS["loaded"]
    loaded = False
    try:
        loaded = bool((_http_json(OLLAMA + "/api/ps", timeout=3) or {}).get("models"))
    except Exception:
        pass
    _OLLAMA_PS.update({"t": now, "loaded": loaded})
    return loaded


def _rgb_state_provider():
    state = dict(_VITALS)
    if time.time() - _VITALS["t"] > 10.0:
        try:
            gpu = get_gpu_stats()
            state["gpu_temp"] = gpu.get("temperature_c")
            state["gpu_util"] = gpu.get("gpu_utilization_percent")
            state["cpu_load"] = psutil.cpu_percent()
            state["cpu_temp"] = (get_cpu_stats([]) or {}).get("max_c")
        except Exception:
            pass
    state["model_loaded"] = _model_loaded()
    # Goal progress drives the "goals" lighting mode. Cached — the board query
    # is far heavier than the engine's tick rate warrants.
    now = time.time()
    if now - _GOAL_PROGRESS["t"] > 30.0:
        _GOAL_PROGRESS.update({"t": now, "v": goals.overall_progress()})
    state["goal_progress"] = _GOAL_PROGRESS["v"]
    return state


rgb.set_state_provider(_rgb_state_provider)


def _rgb_boot():
    """Bring lighting up in the background — hardware probing must not block
    the app from serving requests."""
    try:
        rgb.apply_defaults_for_new_devices()
    except Exception:
        pass
    try:
        if rgb._CFG.get("enabled"):
            rgb.start_engine()
    except Exception:
        pass


@app.on_event("startup")
def _start_rgb_boot():
    threading.Thread(target=_rgb_boot, daemon=True).start()


def _ss_notify_boot():
    """Report new ss.com listings on a watched search. Late import: it pulls in
    messaging, which imports this module."""
    try:
        from app import ss_notify
        ss_notify.loop()
    except Exception:
        LOGGER.exception("ss_notify loop did not start")


@app.on_event("startup")
def _start_ss_notify():
    threading.Thread(target=_ss_notify_boot, daemon=True).start()


class RgbConfigRequest(BaseModel):
    enabled: bool | None = None
    mode: str | None = None
    color: str | None = None
    brightness: int | None = None
    speed: int | None = None
    interval_ms: int | None = None
    alert: bool | None = None
    alert_gpu_temp: float | None = None
    alert_cpu_temp: float | None = None
    jrgb: bool | None = None
    jrgb_invert: bool | None = None
    key_effect: str | None = None
    mystic: bool | None = None
    mystic_areas: list[str] | None = None
    # Fixed AIO colour; "" hands it back to the scene colour. Not `bool|None`
    # -- an empty string must survive exclude_none=True to be able to clear it.
    mystic_color: str | None = None
    keyboard: bool | None = None
    gpu_label: bool | None = None


class RgbDeviceRequest(BaseModel):
    name: str
    enabled: bool


class RgbPulseRequest(BaseModel):
    color: str = "#ffffff"
    ms: int = 450


class RgbActivityRequest(BaseModel):
    label: str
    active: bool = True
    ttl: float = 8.0


@app.get("/api/rgb")
def api_rgb_status():
    return rgb.status()


@app.post("/api/rgb/rescan")
def api_rgb_rescan():
    """Re-enumerate after starting OpenRGB or plugging something in."""
    return rgb.status(refresh=True)


@app.post("/api/rgb/config")
def api_rgb_config(body: RgbConfigRequest):
    result = rgb.set_config(**body.model_dump(exclude_none=True))
    if not result.get("ok"):
        return JSONResponse(status_code=result.get("status_code", 400), content=result)
    return rgb.status()


@app.post("/api/rgb/device")
def api_rgb_device(body: RgbDeviceRequest):
    result = rgb.set_device_enabled(body.name, body.enabled)
    if not result.get("ok"):
        return JSONResponse(status_code=result.get("status_code", 400), content=result)
    return result


@app.post("/api/rgb/identify")
def api_rgb_identify(body: RgbDeviceRequest):
    result = rgb.identify(body.name)
    if not result.get("ok"):
        return JSONResponse(status_code=result.get("status_code", 500), content=result)
    return result


@app.post("/api/rgb/test")
def api_rgb_test():
    """Red/green/blue/white sweep — proves what is physically on the header."""
    result = rgb.test_pattern()
    if not result.get("ok"):
        return JSONResponse(status_code=result.get("status_code", 503), content=result)
    return result


@app.post("/api/rgb/pulse")
def api_rgb_pulse(body: RgbPulseRequest):
    result = rgb.pulse(body.color, body.ms)
    if not result.get("ok"):
        return JSONResponse(status_code=result.get("status_code", 503), content=result)
    return result


@app.post("/api/rgb/activity")
def api_rgb_activity(body: RgbActivityRequest):
    """Let any other process flag AI work so 'ai' mode reacts to it."""
    rgb.note_activity(body.label, body.active, ttl=body.ttl)
    return {"ok": True, "activity": rgb._busy()}


@app.get("/api/rgb/diag")
def api_rgb_diag():
    """Read the diagnostic NCT RGB registers without changing hardware."""
    from app import msi_jrgb
    return msi_jrgb.dump(write_test=None)



class GoalCreateRequest(BaseModel):
    title: str
    horizon: str = "week"
    area: str = ""
    detail: str = ""
    parent_id: int | None = None
    target_date: str | None = None
    importance: str = "normal"
    contexts: list[str] | None = None
    resurface_mode: str = "contextual"
    visibility: str = "private_self"
    kind: str = ""
    done_definition: str = ""


class GoalUpdateRequest(BaseModel):
    title: str | None = None
    detail: str | None = None
    horizon: str | None = None
    area: str | None = None
    status: str | None = None
    progress: int | None = None
    target_date: str | None = None
    parent_id: int | None = None
    importance: str | None = None
    contexts: list[str] | None = None
    resurface_mode: str | None = None
    visibility: str | None = None
    kind: str | None = None
    done_definition: str | None = None
    snoozed_until: str | None = None


class GoalIdRequest(BaseModel):
    id: int
    note: str = ""


class GoalBreakdownRequest(BaseModel):
    id: int
    model: str = ""
    count: int = 4


@app.get("/api/goals")
def api_goals_board(weeks: int = 13):
    return goals.board(weeks=max(4, min(26, weeks)))


@app.post("/api/goals")
def api_goals_create(body: GoalCreateRequest):
    result = goals.create(body.title, horizon=body.horizon, area=body.area,
                          detail=body.detail, parent_id=body.parent_id,
                          target_date=body.target_date,
                          importance=body.importance,
                          contexts=body.contexts or [],
                          resurface_mode=body.resurface_mode,
                          visibility=body.visibility,
                          kind=body.kind,
                          done_definition=body.done_definition,
                          source="dashboard_api",
                          actor="dashboard_api")
    if not result.get("ok"):
        return JSONResponse(status_code=result.get("status_code", 400), content=result)
    return result


@app.patch("/api/goals/{goal_id}")
def api_goals_update(goal_id: int, body: GoalUpdateRequest):
    result = goals.update(goal_id, **body.model_dump(exclude_none=True))
    if not result.get("ok"):
        return JSONResponse(status_code=result.get("status_code", 400), content=result)
    return result


@app.delete("/api/goals/{goal_id}")
def api_goals_delete(goal_id: int):
    result = goals.delete(goal_id)
    if not result.get("ok"):
        return JSONResponse(status_code=result.get("status_code", 404), content=result)
    return result


@app.post("/api/goals/checkin")
def api_goals_checkin(body: GoalIdRequest):
    result = goals.check_in(body.id, body.note)
    if not result.get("ok"):
        return JSONResponse(status_code=result.get("status_code", 404), content=result)
    return result


@app.post("/api/goals/breakdown")
def api_goals_breakdown(body: GoalBreakdownRequest):
    """Ask the local model to split a big goal into concrete small ones. The
    goals are NOT saved here — they come back as suggestions for the operator
    to accept, so the model can never quietly write into the real board."""
    board = goals.board(weeks=1)
    target = next((g for g in board["goals"] if g["id"] == body.id), None)
    if not target:
        return _api_error("goal not found", status_code=404)

    # was hardcoded "chat:latest": clicking Breakdown in the console swapped a
    # 19.9 GB model onto a card holding the 17.7 GB chat model, so the click
    # cost ~50 s and the next iMessage paid the reload back. Ask whichever
    # model the reply pipeline is already running unless the caller names one.
    model = ((body.model or "").strip()
             or str((messaging.load_config().get("auto_reply") or {}).get("model")
                    or "chat:latest"))
    count = max(2, min(8, body.count))
    prompt = (
        "Break this goal into %d concrete, small next actions.\n\n"
        "Goal: %s\nHorizon: %s\nArea: %s\nDetail: %s\n\n"
        "Rules: each action must be doable in one sitting this week, start with a verb, "
        "and be specific enough to know when it is done. No preamble, no numbering, "
        "no markdown. Output exactly %d lines, one action per line."
        % (count, target["title"], target["horizon"], target["area"] or "-",
           target["detail"] or "-", count)
    )
    _body = {
        "model": model, "prompt": prompt, "stream": False,
        # same num_ctx as every other lane that shares this model, or the call
        # evicts the resident runner — see _shared_ctx_options
        "options": _shared_ctx_options(model, {"temperature": 0.6}),
        "keep_alive": _shared_keep_alive(model),
    }
    # chat:latest advertises `thinking`; this prompt wants a bare list of lines,
    # so the thinking phase is pure latency against the 180s timeout.
    if "thinking" in messaging._model_capabilities(model):
        _body["think"] = False
    payload = json.dumps(_body).encode("utf-8")
    req = urllib.request.Request(OLLAMA + "/api/generate", data=payload)
    req.add_header("Content-Type", "application/json")
    rgb.note_activity("goals-breakdown", True, ttl=300.0)
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception as e:
        return _api_error("breakdown failed: %s" % e, status_code=502)
    finally:
        rgb.note_activity("goals-breakdown", False)

    lines = []
    for raw in (data.get("response") or "").splitlines():
        text = raw.strip().lstrip("-*0123456789.) \t").strip()
        # models like to think out loud; drop anything that is not an action
        if len(text) < 4 or text.endswith(":"):
            continue
        lines.append(text[:160])
        if len(lines) >= count:
            break
    return {"ok": True, "goal_id": body.id, "model": model, "suggestions": lines}


@app.get("/api/stats")
def api_stats():
    per_core = []
    try:
        per_core = psutil.cpu_percent(interval=0.15, percpu=True)
    except Exception:
        pass
    cpu_pct = round(sum(per_core) / len(per_core), 1) if per_core else psutil.cpu_percent()

    mem = psutil.virtual_memory()
    swap = None
    try:
        sw = psutil.swap_memory()
        swap = {
            "total_gb": round(sw.total / (1024**3), 2),
            "used_gb": round(sw.used / (1024**3), 2),
            "percent": sw.percent,
        }
    except Exception:
        pass

    disks = _disk_usage()

    net = get_network_io()
    disk_io = get_disk_io()
    gpu = get_gpu_stats()
    cpu = get_cpu_stats(per_core)
    cpu["fan"] = fan_control.status()
    gpu["fan"] = fan_control.gpu_status()
    procs = get_top_processes()
    # Hand the freshly-read vitals to the lighting engine so it never has to
    # spawn its own nvidia-smi while the dashboard is being watched.
    _publish_vitals(gpu, cpu, cpu_pct)

    battery = None
    try:
        b = psutil.sensors_battery()
        if b is not None:
            battery = {"percent": round(b.percent, 1), "plugged": b.power_plugged}
    except Exception:
        pass

    try:
        boot = psutil.boot_time()
        system_uptime = round(time.time() - boot, 1)
    except Exception:
        system_uptime = None

    warnings = []
    if cpu_pct and cpu_pct > 90:
        warnings.append(f"CPU high: {cpu_pct}%")
    if mem.percent and mem.percent > 90:
        warnings.append(f"Memory high: {mem.percent}%")
    for d in disks:
        if d["percent"] and d["percent"] > 90:
            warnings.append(f"Disk {d['letter']}: {d['percent']}%")
    if gpu["memory_total_mb"] > 0 and gpu["memory_utilization_percent"] > 90:
        warnings.append(f"GPU VRAM high: {gpu['memory_utilization_percent']}%")
    if gpu.get("temperature_c") and gpu["temperature_c"] > 83:
        warnings.append(f"GPU hot: {gpu['temperature_c']}C")
    if cpu.get("max_c") and cpu["max_c"] > 90:
        warnings.append(f"CPU hot: {cpu['max_c']}C")

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "hostname": HOSTNAME,
        "rig_name": RIG_NAME,
        "uptime_seconds": round(time.time() - start_time, 1),
        "system_uptime_seconds": system_uptime,
        "cpu_percent": cpu_pct,
        "warnings": warnings,
        "gpu": gpu,
        "cpu": cpu,
        "memory": {
            "total_gb": round(mem.total / (1024**3), 2),
            "available_gb": round(mem.available / (1024**3), 2),
            "used_gb": round(mem.used / (1024**3), 2),
            "percent": mem.percent,
            "swap": swap,
        },
        "disks": disks,
        "disk_io": disk_io,
        "network": net,
        "processes": procs,
        "battery": battery,
    }
