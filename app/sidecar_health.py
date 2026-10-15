# -*- coding: utf-8 -*-
r"""Do the sidecar daemons actually WORK -- not just answer their socket.

    GET /api/health           -> adds a "sidecars" summary (light probes)
    startup                   -> logs a WARN line per degraded/down sidecar
                                 and heals the ones that are safe to restart
    _sidecar_watch_loop       -> re-checks every 15 min, logs state changes
"""
import json
import subprocess
import threading
import time
import urllib.request

try:
    from app.logging_setup import get_logger  # type: ignore
    LOGGER = get_logger(__name__)
except Exception:  # pragma: no cover
    import logging
    LOGGER = logging.getLogger(__name__)

_TIMEOUT = 3.0
_last = {}          # name -> status, for change detection in the watch loop


def _get(url, timeout=_TIMEOUT):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read()


def _post(url, obj, timeout=8.0):
    req = urllib.request.Request(
        url, data=json.dumps(obj).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()



def _probe_morph():
    body = _post("http://127.0.0.1:8126/parse", {"text": "Māja ir liela un balta."})
    toks = (json.loads(body) or {}).get("tokens") or []
    if len(toks) >= 4 and any(t.get("feats") for t in toks):
        return True, "parses (%d tokens)" % len(toks)
    return False, "answered but returned no parse (%s)" % (str(body)[:120])


def _probe_nmt():
    body = _post("http://127.0.0.1:8124/translate",
                 {"text": "the white house is on the hill", "dir": "en2lv"}, timeout=20)
    lv = (json.loads(body) or {}).get("lv") or ""
    if lv.strip() and lv.strip().lower() != "the white house is on the hill":
        return True, "translates (%r)" % lv[:48]
    return False, "answered but did not translate (%s)" % (str(body)[:120])


def _probe_f5(deep=False):
    h = json.loads(_get("http://127.0.0.1:8125/health") or b"{}")
    if h.get("last_error"):
        return False, "last_error: %s" % str(h["last_error"])[:140]
    if not deep:
        # a fresh daemon is loaded:false until the first synth -- not an error.
        # Only a recorded failure or a set last_error is a real signal here.
        if h.get("failed") and not h.get("spoken"):
            return False, "every synth so far failed (%s)" % json.dumps(h)[:120]
        return True, "health ok%s" % ("" if h.get("loaded") else ", model not yet loaded")
    # deep: actually synthesise. A COLD model load is 60-120s and a timeout
    # here is not proof of breakage -- retry once with room before judging.
    for attempt, tmo in enumerate((110, 60)):
        try:
            body = _post("http://127.0.0.1:8125/tts", {"text": "Labdien, tests."}, timeout=tmo)
            if body[:4] == b"RIFF" or len(body) > 2000:
                return True, "synthesised %d bytes of audio" % len(body)
            return False, "no audio from /tts (%s)" % (str(body[:120]))
        except Exception as exc:
            if attempt == 1:
                return False, "/tts failed: %s" % str(exc)[:120]
            time.sleep(2)
    return False, "/tts unreachable"


def _probe_mms():
    return (_get("http://127.0.0.1:8123/health").strip() == b"ok"), "health ok"


SIDECARS = (
    ("lv_morph", 8126, _probe_morph, "Latvian grammar/agreement checks + digest proofing"),
    ("nmt",      8124, _probe_nmt,   "EN-reasoning -> LV translation pipeline"),
    ("f5_voice", 8125, _probe_f5,    "Anton's voice notes (silently falls back to MMS)"),
    ("mms_voice", 8123, _probe_mms,  "voice fallback / STT-adjacent"),
)


def check(deep=False):
    """{ok, degraded:[names], sidecars:[{name, port, status, detail, matters}]}.

    status: 'up' (real probe works) | 'degraded' (socket answers, probe fails)
    | 'down' (socket refused/unreachable).
    """
    rows = []
    degraded = []
    for name, port, probe, matters in SIDECARS:
        status, detail = "down", ""
        reachable = False
        for _try in range(2):            # a boot-time timeout is not "down"
            try:
                _get("http://127.0.0.1:%d/health" % port, timeout=4.0)
                reachable = True
                break
            except Exception as exc:
                detail = str(exc)[:120]
                time.sleep(1.5)
        if reachable:
            try:
                ok, detail = (probe(deep=True) if (deep and name == "f5_voice")
                              else probe())
                status = "up" if ok else "degraded"
            except Exception as exc:
                status, detail = "degraded", "probe error: %s" % str(exc)[:120]
        if status != "up":
            degraded.append(name)
        rows.append({"name": name, "port": port, "status": status,
                     "detail": detail, "matters": matters})
    return {"ok": not degraded, "degraded": degraded, "sidecars": rows}


def _pids_on_port(port):
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "tcp"], capture_output=True,
                             text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except Exception:
        return set()
    pids = set()
    for ln in out.splitlines():
        if (":%d " % port) in ln and "LISTENING" in ln:
            p = ln.split()[-1]
            if p.isdigit() and p != "0":
                pids.add(p)
    return pids


def heal(names=None):
    """Kill any DEGRADED daemon -- one that answers /health 'ok' but fails its
    real-work probe (stale data paths, a model that never loaded). A fresh one
    with the fixed code then takes the port on next use. The old ones are
    detached and often run as LocalSystem; this runs inside the LocalSystem
    service, so it can.

    A 'down' daemon is NOT healed here -- it just is not running, and each
    caller's own _ensure_* starts it. Killing a port with nothing on it is a
    no-op; killing one that is merely slow to answer during boot is harmful.
    """
    st = check()
    targets = names if names is not None else st["degraded"]
    degraded_now = {r["name"] for r in st["sidecars"] if r["status"] == "degraded"}
    killed = []
    for name in targets:
        if name not in degraded_now:
            continue
        port = next((p for n, p, _, _ in SIDECARS if n == name), None)
        if not port:
            continue
        for pid in _pids_on_port(port):
            try:
                subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                killed.append("%s(pid %s)" % (name, pid))
            except Exception:
                pass
    if killed:
        LOGGER.warning("sidecar heal: killed stale %s -- will respawn on next use",
                       ", ".join(killed))
    return {"killed": killed}


def log_status(heal_degraded=True):
    st = check()
    for row in st["sidecars"]:
        if row["status"] == "up":
            LOGGER.info("sidecar %s :%d up -- %s", row["name"], row["port"], row["detail"])
        else:
            LOGGER.warning("sidecar %s :%d %s -- %s  (affects: %s)",
                           row["name"], row["port"], row["status"].upper(),
                           row["detail"], row["matters"])
    if heal_degraded and st["degraded"]:
        heal(st["degraded"])
    for row in st["sidecars"]:
        _last[row["name"]] = row["status"]
    return st


def _watch_loop():
    time.sleep(150)                      # let the daemons settle after a restart
    n = 0
    while True:
        try:
            # deep (real F5 synthesis) once an hour; light the rest of the time
            st = check(deep=(n % 4 == 0))
            for row in st["sidecars"]:
                was = _last.get(row["name"])
                if was and was != row["status"]:
                    lvl = LOGGER.warning if row["status"] != "up" else LOGGER.info
                    lvl("sidecar %s :%d changed %s -> %s (%s)", row["name"],
                        row["port"], was, row["status"], row["detail"])
                    if row["status"] == "degraded":
                        heal([row["name"]])
                _last[row["name"]] = row["status"]
        except Exception:
            LOGGER.debug("sidecar watch loop hiccup", exc_info=True)
        n += 1
        time.sleep(900)                  # 15 min


def start_watch():
    threading.Thread(target=_watch_loop, name="sidecar_watch", daemon=True).start()


def last_snapshot():
    """The most recent status per sidecar, from startup + the watch loop -- no
    fresh probing, safe to call from a hot path like /api/health."""
    matters = {n: m for n, _, _, m in SIDECARS}
    rows = [{"name": n, "status": _last.get(n, "unknown"), "matters": matters.get(n, "")}
            for n, _, _, _ in SIDECARS]
    bad = [r["name"] for r in rows if r["status"] not in ("up", "unknown")]
    return {"ok": not bad, "degraded": bad, "sidecars": rows}
