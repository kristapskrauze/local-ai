# -*- coding: utf-8 -*-
"""Render one URL in a real (off-screen) Chrome and read what a person would see.

So do what a person does: open the page in Chrome, let its JS run, click one
obvious "see prices / show schedule" control, and hand back the rendered text
plus every JSON response the page pulled from its own API. Headful, not
headless -- `--headless=new` is fingerprinted and Akamai/Cloudflare serve it
"Access Denied" on exactly the sites worth rendering; a real window at
-4000,-4000 is invisible and gets through (both measured).

`app/_browser_cdp.mjs` is the Node driver (no npm deps; Node >=22 has global
fetch + WebSocket). One Chrome at a time (`_LOCK`).
"""
import json
import os
import shutil
import subprocess
import threading
import time

from pathlib import Path

try:                                    # module-level logger if the app has one
    from app.logging_setup import get_logger  # type: ignore
    LOGGER = get_logger(__name__)
except Exception:                       # pragma: no cover
    import logging
    LOGGER = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = ROOT / "app" / "_browser_cdp.mjs"
_LOCK = threading.Lock()
_LAST_ERROR = {"ts": 0.0, "msg": ""}

_CHROME_CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
)


def _node_bin():
    for name in ("node", "node.exe"):
        p = shutil.which(name)
        if p:
            return p
    for guess in (r"C:\Program Files\nodejs\node.exe",
                  r"C:\Program Files\nodejs\node"):
        if os.path.exists(guess):
            return guess
    return None


def _chrome_bin(cfg=None):
    explicit = str((cfg or {}).get("chrome_path") or "").strip()
    if explicit and os.path.exists(explicit):
        return explicit
    for p in _CHROME_CANDIDATES:
        if os.path.exists(p):
            return p
    return None


def available(cfg=None):
    return bool(_node_bin() and _chrome_bin(cfg) and _SCRIPT.exists())


def render_page(url, cfg=None, click=None, wait_ms=7000, timeout=48):
    """Open `url` in Chrome, return a dict. Never raises.

    {ok, blocked, title, text, final_url, clicked, api: [{url, data}], error}
    `api` bodies are parsed to objects where possible, else left as strings.
    """
    node = _node_bin()
    chrome = _chrome_bin(cfg)
    if not (node and chrome and _SCRIPT.exists()):
        return {"ok": False, "text": "", "api": [],
                "error": "browser render unavailable (node=%s chrome=%s script=%s)"
                         % (bool(node), bool(chrome), _SCRIPT.exists())}
    args = [node, str(_SCRIPT), str(url), "--chrome", chrome,
            "--wait", str(int(max(1000, min(wait_ms, 15000))))]
    if click:
        args += ["--click", str(click)[:60]]

    if not _LOCK.acquire(timeout=timeout + 5):
        return {"ok": False, "text": "", "api": [],
                "error": "another render is in progress"}
    started = time.time()
    try:
        proc = subprocess.run(
            args, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        out = (proc.stdout or "").strip()
        data = json.loads(out[out.index("{"):out.rindex("}") + 1]) if "{" in out else {}
    except subprocess.TimeoutExpired:
        _kill_orphans()
        _LAST_ERROR.update(ts=time.time(), msg="timeout after %ds" % timeout)
        return {"ok": False, "text": "", "api": [], "error": "render timed out"}
    except Exception as exc:
        _LAST_ERROR.update(ts=time.time(), msg=str(exc)[:200])
        return {"ok": False, "text": "", "api": [],
                "error": "render failed: %s" % str(exc)[:200]}
    finally:
        _LOCK.release()

    api = []
    for item in (data.get("api") or [])[:12]:
        body = str(item.get("body") or "")
        try:
            parsed = json.loads(body)
            body = json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))
        except Exception:
            pass
        api.append({"url": str(item.get("url") or "")[:300], "data": body[:90000]})
    LOGGER.info("browser render %s: ok=%s clicked=%r api=%d %.1fs",
                url, data.get("ok"), data.get("clicked"), len(api),
                time.time() - started)
    links = [{"url": str(l.get("url") or "")[:400], "text": str(l.get("text") or "")[:80]}
             for l in (data.get("links") or [])[:120]
             if l.get("url")]
    return {
        "ok": bool(data.get("ok")),
        "blocked": bool(data.get("blocked")),
        "title": str(data.get("title") or "")[:240],
        "text": str(data.get("text") or "")[:40000],
        "final_url": str(data.get("final_url") or url),
        "clicked": str(data.get("clicked") or ""),
        "api": api,
        "links": links,
        "error": str(data.get("error") or ""),
    }


def _kill_orphans():
    """Kill any chrome/node holding one of our temp profiles."""
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe' OR Name='msedge.exe' OR Name='node.exe'\" "
             "| Where-Object { $_.CommandLine -like '*antons-cdp-*' } "
             "| ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"],
            capture_output=True, timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:
        pass
