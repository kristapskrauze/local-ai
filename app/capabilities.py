# -*- coding: utf-8 -*-
"""What Antons can ACTUALLY do right now, measured rather than claimed.

So every capability here is a PROBE against the live system: a config flag, a
reachable daemon, an importable module, a file that exists. Each answer
carries the reason it came out that way, because "images: false" is only
useful if it also says the vision model has no vision.

Two rules hold this together:

  * a probe never runs a model and never costs more than a few milliseconds
    (results are cached for CACHE_SECONDS), so the reply path can ask on every
    turn;
  * a probe FAILS CLOSED. Anything that errors, times out or is unsure reports
    False with the error as its reason. Claiming a capability you do not have
    is the failure mode this module exists to prevent.

`code_write` and `deploy` are hard False here on purpose. They are not
probes — they are permissions, and they are granted per request by the owner
in `selfinspect`, never inferred from the environment.
"""
import json
import logging
import os
import shutil
import socket
import threading
import time
import urllib.request
from pathlib import Path

LOGGER = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
CACHE_SECONDS = 60.0

_cache = {"at": 0.0, "data": None}
_lock = threading.Lock()


def _probe(fn, *args, **kwargs):
    """Run one probe. Any failure is a False capability, never an exception."""
    try:
        ok, why = fn(*args, **kwargs)
        return {"available": bool(ok), "reason": str(why)[:160]}
    except Exception as exc:                      # fail closed, always
        return {"available": False, "reason": "probe failed: %s" % str(exc)[:120]}


def _port_open(host, port, timeout=0.35):
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except Exception:
        return False


def _http_ok(url, timeout=0.8):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "antons-capabilities"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= int(resp.status) < 400
    except Exception:
        return False



def _cap_text(acfg):
    return bool(acfg.get("enabled") or acfg.get("assistant_enabled")), \
        "auto_reply enabled=%s assistant=%s" % (bool(acfg.get("enabled")),
                                                bool(acfg.get("assistant_enabled")))


def _cap_images(acfg):
    if not acfg.get("vision", True):
        return False, "auto_reply.vision is off"
    model = str(acfg.get("vision_model") or acfg.get("model") or "")
    if not model:
        return False, "no vision model configured"
    try:
        from app import messaging as M
        caps = M._model_capabilities(model, acfg.get("ollama_url"))
    except Exception as exc:
        return False, "could not read model capabilities: %s" % str(exc)[:80]
    if not caps:
        return False, "%s reports no capabilities" % model
    return ("vision" in caps), "%s capabilities: %s" % (model, ",".join(sorted(caps))[:80])


def _cap_incoming_audio(acfg):
    """Can a voice note become text? Nothing on this rig does that yet.

    Probed rather than assumed: an STT backend is either an importable package
    or a reachable daemon, and this looks for both. 139 voice notes have
    arrived and every one of them was silence to the pipeline.
    """
    daemon = str(acfg.get("stt_url") or "").strip()
    if daemon and _http_ok(daemon.rstrip("/") + "/health"):
        return True, "stt daemon answered at %s" % daemon
    try:
        from app import local_stt
        ok, reason = local_stt.available()
        if ok:
            return True, reason
    except Exception:
        pass
    for mod in ("faster_whisper", "whisper", "vosk", "sherpa_onnx"):
        try:
            __import__(mod)
            return True, "%s is importable" % mod
        except Exception:
            continue
    return False, "no STT package and no stt daemon (config auto_reply.stt_url)"


def _cap_outgoing_voice(acfg):
    if not acfg.get("voice_replies", False):
        return False, "auto_reply.voice_replies is off"
    if _http_ok("http://127.0.0.1:8123/health"):
        return True, "mms/kokoro daemon healthy on :8123"
    return False, "voice daemon not answering on :8123"



def _cap_web(acfg):
    if not acfg.get("web_access", True):
        return False, "auto_reply.web_access is off"
    url = str(acfg.get("searx_url") or "http://127.0.0.1:8888").rstrip("/")
    if not _http_ok(url + "/healthz") and not _http_ok(url):
        return False, "searx not answering at %s" % url
    return True, "searx reachable at %s" % url


def _cap_thread_search(acfg):
    p = ROOT / ".messaging_history" / "inbox.jsonl"
    return p.exists(), ("inbox.jsonl present" if p.exists() else "no inbox.jsonl")


def _cap_memory(acfg):
    try:
        from app import messaging as M
        path = getattr(M, "HERMES_PATH", None) or (ROOT / "AI" / "memory")
    except Exception:
        path = None
    if path and Path(str(path)).exists():
        return True, "memory store at %s" % str(path)[-60:]
    return bool(acfg.get("hermes_context")), "hermes_context=%s" % bool(acfg.get("hermes_context"))


def _cap_jellyfin(acfg):
    try:
        from app import rig_agent as R
        cfg = R.load_config()
    except Exception as exc:
        return False, "rig_agent config unreadable: %s" % str(exc)[:60]
    if not cfg.get("watch_enabled", True):
        return False, "rig_agent.watch_enabled is off"
    return True, "watch skill enabled"


def _cap_downloads(acfg):
    try:
        from app import rig_agent as R
        cfg = R.load_config()
    except Exception as exc:
        return False, "rig_agent config unreadable: %s" % str(exc)[:60]
    return bool(cfg.get("download_enabled", True)), \
        "rig_agent.download_enabled=%s" % bool(cfg.get("download_enabled", True))


def _cap_gpu_status(acfg):
    exe = shutil.which("nvidia-smi")
    return bool(exe), ("nvidia-smi at %s" % exe if exe else "nvidia-smi not on PATH")


def _cap_repo_read(acfg):
    try:
        from app import selfinspect
    except Exception as exc:
        return False, "selfinspect unavailable: %s" % str(exc)[:80]
    return selfinspect.READY, selfinspect.readiness()


def _cap_tests_run(acfg):
    try:
        from app import selfinspect
        names = selfinspect.allowlisted_tests()
    except Exception as exc:
        return False, "selfinspect unavailable: %s" % str(exc)[:80]
    return bool(names), "%d allowlisted suites" % len(names)


def _cap_agent_tools(acfg):
    try:
        from app import agent_tools
        ctx = agent_tools.AgentContext(actor="capability_probe",
                                       allow_private=True)
        tools = [t for t in agent_tools.tool_inventory(ctx)
                 if t.get("available")]
    except Exception as exc:
        return False, "agent tool registry unavailable: %s" % str(exc)[:80]
    return bool(tools), "%d model-selectable tools" % len(tools)


def _cap_never(_acfg, why):
    return False, why


def snapshot(acfg=None, force=False):
    """The registry. Cheap, cached, and true at the moment it was taken."""
    now = time.time()
    with _lock:
        if not force and _cache["data"] and (now - _cache["at"]) < CACHE_SECONDS:
            return _cache["data"]
    if acfg is None:
        try:
            from app import messaging as M
            acfg = (M.load_config().get("auto_reply") or {})
        except Exception:
            acfg = {}
    data = {
        "taken_at": now,
        "perception": {
            "text": _probe(_cap_text, acfg),
            "images": _probe(_cap_images, acfg),
            "incoming_audio": _probe(_cap_incoming_audio, acfg),
            "outgoing_voice": _probe(_cap_outgoing_voice, acfg),
        },
        "tools": {
            "web": _probe(_cap_web, acfg),
            "thread_search": _probe(_cap_thread_search, acfg),
            "memory": _probe(_cap_memory, acfg),
            "jellyfin": _probe(_cap_jellyfin, acfg),
            "downloads": _probe(_cap_downloads, acfg),
            "gpu_status": _probe(_cap_gpu_status, acfg),
            "repo_search": _probe(_cap_repo_read, acfg),
            "repo_read": _probe(_cap_repo_read, acfg),
            "logs_read": _probe(_cap_repo_read, acfg),
            "tests_run": _probe(_cap_tests_run, acfg),
            "agent_tools": _probe(_cap_agent_tools, acfg),
            # NOT probes. These are permissions the owner grants per request,
            # and inferring them from the environment is exactly the mistake
            # this module exists to stop.
            "code_write": _probe(_cap_never, acfg,
                                 "requires an explicit owner instruction"),
            "deploy": _probe(_cap_never, acfg,
                             "requires a separate explicit owner instruction"),
        },
    }
    with _lock:
        _cache["at"] = now
        _cache["data"] = data
    return data


def can(path, acfg=None):
    """can("perception.incoming_audio") -> bool. Unknown names are False."""
    node = snapshot(acfg)
    for part in str(path or "").split("."):
        if not isinstance(node, dict) or part not in node:
            return False
        node = node[part]
    return bool(isinstance(node, dict) and node.get("available"))


def why(path, acfg=None):
    node = snapshot(acfg)
    for part in str(path or "").split("."):
        if not isinstance(node, dict) or part not in node:
            return "unknown capability %r" % path
        node = node[part]
    return str((node or {}).get("reason") or "") if isinstance(node, dict) else ""


_PERMISSIONS = frozenset(("code_write", "deploy"))

_CALLABLE_AS = {
    "repo_read": "antons_read_code",
    "repo_search": "antons_read_code",
    "logs_read": "antons_investigate",
}


def prompt_lines(acfg=None):
    """The registry as a few lines the model can be told, truthfully.

    Only what it CANNOT do plus the tools it has — a full dump would be prompt
    bloat, and the failure being prevented is the bot claiming a sense it does
    not have.
    """
    snap = snapshot(acfg)
    missing = []
    for group in ("perception", "tools"):
        for name, node in (snap.get(group) or {}).items():
            if not node.get("available") and name not in _PERMISSIONS:
                missing.append(name)
    have = [_CALLABLE_AS.get(n, n)
            for n, node in (snap.get("tools") or {}).items()
            if node.get("available")]
    have = sorted(set(have))
    out = []
    if missing:
        out.append("Tu NEVARI: %s. Nekad neizliecies, ka vari." % ", ".join(sorted(missing)))
    if have:
        out.append("Tev ir pieejami: %s." % ", ".join(sorted(have)))
    return out


def as_json(acfg=None, force=False):
    return json.dumps(snapshot(acfg, force=force), ensure_ascii=False, indent=2)
