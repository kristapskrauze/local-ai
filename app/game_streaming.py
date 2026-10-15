"""Read-only health snapshot for the local Sunshine/Moonlight setup."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import psutil


_CACHE_LOCK = threading.Lock()
_CACHE = {"at": 0.0, "value": None}
_CACHE_SECONDS = 10.0
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

_SUNSHINE_ROOT = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Sunshine"
_TAILSCALE_EXE = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Tailscale" / "tailscale.exe"
_CLIENT_ALIASES = {
    "iphone184": "iPhone Air",
    "iphone-xs-max": "iPhone XS Max",
    # This M4 Mac inherited its hostname during restore; show the physical device.
    "macbook-pro": "MacBook Pro M4 · 14-inch",
}
_CLIENT_ORDER = {
    "iPhone Air": 0,
    "iPhone XS Max": 1,
    "MacBook Pro M4 · 14-inch": 2,
}


def _run(args: list[str], timeout: float = 4.0) -> tuple[bool, str]:
    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
            creationflags=_NO_WINDOW,
        )
        output = ((proc.stdout or "") + (proc.stderr or "")).strip()
        return proc.returncode == 0, output
    except Exception as exc:
        return False, str(exc)


def _sunshine_service() -> dict:
    out = {"installed": False, "running": False, "status": "missing", "start_type": None, "pid": None}
    try:
        svc = psutil.win_service_get("SunshineService").as_dict()
        status = str(svc.get("status") or "unknown").lower()
        out.update(
            installed=True,
            running=status == "running",
            status=status,
            start_type=svc.get("start_type"),
            pid=svc.get("pid"),
        )
    except Exception as exc:
        out["error"] = str(exc)
    return out


def _sunshine_server_info() -> dict:
    try:
        with urllib.request.urlopen("http://127.0.0.1:47989/serverinfo", timeout=1.5) as response:
            root = ET.fromstring(response.read())
        values = {child.tag: (child.text or "") for child in root}
        state = values.get("state") or "unknown"
        current_game = int(values.get("currentgame") or 0)
        return {
            "reachable": True,
            "hostname": values.get("hostname"),
            "version": values.get("appversion"),
            "state": state,
            "streaming": state != "SUNSHINE_SERVER_FREE" or current_game > 0,
            "current_game": current_game,
            "pair_status": int(values.get("PairStatus") or 0),
            "https_port": int(values.get("HttpsPort") or 47990),
        }
    except Exception as exc:
        return {"reachable": False, "streaming": False, "state": "unreachable", "error": str(exc)}


def _sunshine_config() -> dict:
    out = {"apps": [], "app_count": 0, "encoder": "NVENC", "upnp": False}
    apps_path = _SUNSHINE_ROOT / "config" / "apps.json"
    try:
        payload = json.loads(apps_path.read_text(encoding="utf-8"))
        apps = payload.get("apps") if isinstance(payload, dict) else []
        out["apps"] = [str(item.get("name")) for item in (apps or []) if item.get("name")]
        out["app_count"] = len(out["apps"])
    except Exception as exc:
        out["apps_error"] = str(exc)

    conf_path = _SUNSHINE_ROOT / "config" / "sunshine.conf"
    try:
        for raw_line in conf_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = [part.strip() for part in line.split("=", 1)]
            if key == "encoder":
                out["encoder"] = value.upper()
            elif key == "upnp":
                out["upnp"] = value.lower() in {"1", "true", "yes", "enabled"}
    except Exception:
        pass
    return out


def _tailscale_snapshot() -> tuple[dict, list[dict]]:
    exe = str(_TAILSCALE_EXE) if _TAILSCALE_EXE.exists() else shutil.which("tailscale")
    base = {"installed": bool(exe), "running": False, "state": "missing", "ip": None, "dns_name": None}
    if not exe:
        return base, []
    ok, raw = _run([exe, "status", "--json"])
    if not ok:
        base.update(state="unavailable", error=raw)
        return base, []
    try:
        payload = json.loads(raw)
    except Exception as exc:
        base.update(state="invalid response", error=str(exc))
        return base, []

    self_node = payload.get("Self") or {}
    ips = self_node.get("TailscaleIPs") or []
    base.update(
        running=payload.get("BackendState") == "Running",
        state=str(payload.get("BackendState") or "unknown"),
        ip=ips[0] if ips else None,
        dns_name=str(self_node.get("DNSName") or "").rstrip(".") or None,
    )

    clients = []
    peers = payload.get("Peer") or {}
    peer_values = peers.values() if isinstance(peers, dict) else peers
    for peer in peer_values:
        # iOS reports the generic HostName "localhost"; its stable tailnet DNS
        # name is the useful identity.
        host = str(peer.get("DNSName") or peer.get("HostName") or "unknown").split(".")[0].lower()
        if host not in _CLIENT_ALIASES:
            continue
        peer_ips = peer.get("TailscaleIPs") or []
        online = bool(peer.get("Online"))
        active = bool(peer.get("Active"))
        current_address = str(peer.get("CurAddr") or "")
        relay = str(peer.get("Relay") or "")
        if not online:
            connection = "offline"
        elif active and current_address:
            connection = "direct"
        elif active and relay:
            connection = "relay"
        elif active:
            connection = "active"
        else:
            connection = "idle"
        clients.append({
            "name": _CLIENT_ALIASES[host],
            "hostname": host,
            "ip": peer_ips[0] if peer_ips else None,
            "online": online,
            "active": active,
            "connection": connection,
            "direct_address": current_address or None,
            "relay": relay or None,
            "last_seen": peer.get("LastSeen"),
        })
    clients.sort(key=lambda item: _CLIENT_ORDER.get(item["name"], 99))
    return base, clients


def _lan_ip() -> str | None:
    candidates = []
    stats = psutil.net_if_stats()
    for adapter, addresses in psutil.net_if_addrs().items():
        if adapter.lower().startswith("tailscale") or not stats.get(adapter) or not stats[adapter].isup:
            continue
        for address in addresses:
            if address.family != socket.AF_INET:
                continue
            ip = address.address
            if ip.startswith(("127.", "169.254.", "100.")):
                continue
            candidates.append(ip)
    return candidates[0] if candidates else None


def _build_snapshot() -> dict:
    service = _sunshine_service()
    server = _sunshine_server_info()
    config = _sunshine_config()
    tailscale, clients = _tailscale_snapshot()
    sunshine = {**service, **server, **config}
    ready = bool(service.get("running") and server.get("reachable") and tailscale.get("running"))
    tailscale_dns = (tailscale.get("dns_name") or "").rstrip(".")
    sunshine_admin = (
        f"https://{tailscale_dns}:47991"
        if tailscale_dns
        else "https://localhost:47991"
    )
    return {
        "ok": True,
        "ready": ready,
        "streaming": bool(server.get("streaming")),
        "sunshine": sunshine,
        "tailscale": tailscale,
        "clients": clients,
        "addresses": {
            "moonlight_host": tailscale.get("ip"),
            "tailscale_dns": tailscale.get("dns_name"),
            "lan_ip": _lan_ip(),
            "sunshine_admin": sunshine_admin,
        },
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


def status(force: bool = False) -> dict:
    now = time.monotonic()
    with _CACHE_LOCK:
        if not force and _CACHE["value"] is not None and now - _CACHE["at"] < _CACHE_SECONDS:
            return _CACHE["value"]
        value = _build_snapshot()
        _CACHE.update(at=now, value=value)
        return value
