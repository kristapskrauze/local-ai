# -*- coding: utf-8 -*-
"""What this machine currently exposes over Tailscale.

The CLI talks to the local tailscaled over a named pipe; the SysMonitor service
runs as LocalSystem which can reach it.
"""
import json
import os
import subprocess
import time

_TS_CANDIDATES = [
    r"C:\Program Files\Tailscale\tailscale.exe",
    r"C:\Program Files (x86)\Tailscale\tailscale.exe",
    "tailscale",
]
_CREATE_NO_WINDOW = 0x08000000


def _tailscale_bin():
    for c in _TS_CANDIDATES:
        if c == "tailscale" or os.path.isfile(c):
            return c
    return "tailscale"


def _run(args, timeout=15):
    try:
        p = subprocess.run([_tailscale_bin()] + list(args),
                           capture_output=True, text=True, timeout=timeout,
                           creationflags=_CREATE_NO_WINDOW)
        return p.returncode, (p.stdout or ""), (p.stderr or "")
    except Exception as exc:
        return -1, "", str(exc)


def _self_dns_name():
    rc, out, _ = _run(["status", "--json"])
    if rc == 0:
        try:
            return str((json.loads(out).get("Self") or {}).get("DNSName") or "").rstrip(".")
        except Exception:
            pass
    return ""


def list_shares():
    """[{host, port, path, target, scope, url}], scope = 'public' | 'tailnet'."""
    rc, out, err = _run(["serve", "status", "--json"])
    if rc != 0:
        return {"ok": False, "error": (err or out or "tailscale serve status failed").strip()[:300],
                "shares": []}
    try:
        cfg = json.loads(out or "{}")
    except Exception as exc:
        return {"ok": False, "error": "unparseable serve status: %s" % exc, "shares": []}
    allow = cfg.get("AllowFunnel") or {}
    shares = []
    for hostport, web in (cfg.get("Web") or {}).items():
        host = hostport.rsplit(":", 1)[0]
        port = hostport.rsplit(":", 1)[1] if ":" in hostport else "443"
        public = bool(allow.get(hostport))
        for path, h in (web.get("Handlers") or {}).items():
            tgt = h.get("Proxy") or h.get("Path") or h.get("Text") or ""
            base = "https://%s%s" % (host, "" if port == "443" else ":" + port)
            url = base + (path if path != "/" else "/")
            shares.append({
                "host": host, "port": port, "path": path, "target": tgt,
                "scope": "public" if public else "tailnet",
                "url": url,
            })
    shares.sort(key=lambda s: (s["scope"] != "public", s["port"], s["path"]))
    return {"ok": True, "shares": shares, "dns_name": _self_dns_name(),
            "checked_at": time.time()}


def stop_share(port, funnel=None):
    """Turn off the serve/funnel on one https port. `port` like '8443'.
    Tries funnel-off first (that is what a public share needs), then serve-off."""
    port = str(port or "").strip().replace("https:", "").strip("/") or "443"
    if not port.isdigit():
        return {"ok": False, "error": "port must be a number, e.g. 8443"}
    before = {s["port"]: s for s in (list_shares().get("shares") or [])}
    tried = []
    # a public port: funnel off (also clears the serve handler on that port)
    rc1, o1, e1 = _run(["funnel", "--https=%s" % port, "off"])
    tried.append(("funnel off", rc1, (e1 or o1).strip()[:160]))
    rc2, o2, e2 = _run(["serve", "--https=%s" % port, "off"])
    tried.append(("serve off", rc2, (e2 or o2).strip()[:160]))
    after = list_shares()
    still = any(s["port"] == port for s in (after.get("shares") or []))
    return {"ok": (not still), "port": port, "still_shared": still,
            "attempts": tried, "shares": after.get("shares", [])}
