# -*- coding: utf-8 -*-
"""Central model-selectable tool registry for the local assistant.

This module is the boundary between "Qwen may ask for this capability" and
"the server decides whether that request is allowed".  Descriptions guide the
model, but risk checks, schema validation, timeouts and truncation live here.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass
from pathlib import Path
import base64
import hashlib
import json
import logging
import os
import re
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from app.people import OWNER, PARTNER, PARTNER_DAT
from app import hosts
from app import build_edit as _build_edit
from app import paths

LOGGER = logging.getLogger(__name__)

READ_ONLY = "READ_ONLY"
LOCAL_WRITE = "LOCAL_WRITE"
LOCAL_REVERSIBLE_WRITE = "LOCAL_REVERSIBLE_WRITE"
EXTERNAL_WRITE = "EXTERNAL_WRITE"
PRIVILEGED = "PRIVILEGED"
DANGEROUS = "DANGEROUS"

ROOT = Path(__file__).resolve().parent.parent
AI_ROOT = Path(paths.AI_HOME)
DOWNLOAD_ROOT = ROOT / ".agent_downloads"

_RUNNER = {"num_ctx": None, "read": False}


def _shared_num_ctx():
    if not _RUNNER["read"]:
        _RUNNER["read"] = True
        try:
            cfg = json.loads((ROOT / "config" / "messaging.json").read_text(
                encoding="utf-8"))
            _RUNNER["num_ctx"] = int((cfg.get("auto_reply") or {}).get("num_ctx") or 0) or None
        except Exception:
            _RUNNER["num_ctx"] = None
    return _RUNNER["num_ctx"]


def _runner_options(extra=None):
    """Options that keep this call on the runner everybody else is using."""
    out = dict(extra or {})
    ctx = _shared_num_ctx()
    if ctx:
        out.setdefault("num_ctx", ctx)
    return out


KEEP_ALIVE = "2h"      # same as the messaging lane; never shorten it here

# ---- live activity pulse: what the assistant is doing RIGHT NOW ----------
# Written from qwen_agent / _run_build as it works; read by /api/pulse so a
# front-end (the pet page) can show it. Plain dict + lock, no persistence.
_PULSE = {"state": "idle", "detail": "", "ts": 0.0}
_PULSE_LOCK = threading.Lock()


def pulse_set(state: str, detail: str = "") -> None:
    try:
        with _PULSE_LOCK:
            _PULSE["state"] = str(state or "idle")
            _PULSE["detail"] = str(detail or "")[:60]
            _PULSE["ts"] = time.time()
    except Exception:
        pass


_BUILD_SPECS_PATH = ROOT / ".messaging_history" / "build_specs.json"


def _build_specs_load() -> dict:
    try:
        return json.loads(_BUILD_SPECS_PATH.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def build_spec_for(conversation_id: str) -> dict:
    """The last thing antons_build made for this chat: {slug, goal, ios9, ts}."""
    return dict((_build_specs_load().get(str(conversation_id or "")) or {}))


def _build_spec_save(conversation_id: str, slug: str, goal: str, ios9: bool):
    cid = str(conversation_id or "")
    if not cid:
        return
    try:
        specs = _build_specs_load()
        specs[cid] = {"slug": slug, "goal": goal[:4000], "ios9": bool(ios9),
                      "ts": time.time()}
        _BUILD_SPECS_PATH.parent.mkdir(parents=True, exist_ok=True)
        _BUILD_SPECS_PATH.write_text(json.dumps(specs, ensure_ascii=False, indent=1),
                                     encoding="utf-8")
    except Exception:
        LOGGER.exception("could not save build spec")


def pulse_get(idle_after: float = 25.0) -> dict:
    with _PULSE_LOCK:
        st, dt, ts = _PULSE["state"], _PULSE["detail"], _PULSE["ts"]
    age = time.time() - ts if ts else 1e9
    if age > idle_after and st not in ("idle", "sleeping"):
        st, dt = "idle", ""
    return {"state": st, "detail": dt, "age_seconds": round(age, 1)}


_LEDGER_DIR = os.environ.get("ANTON_LEDGER_DIR")
if _LEDGER_DIR:
    _LEDGER_BASE = Path(_LEDGER_DIR)
else:
    _LEDGER_BASE = ROOT / ".messaging_history"
TRACE_PATH = _LEDGER_BASE / "agent_traces.jsonl"
ACTION_LEDGER_PATH = _LEDGER_BASE / "agent_actions.jsonl"

MAX_RESULT_CHARS = 6000
MAX_TOOL_ARGS_CHARS = 1200

_DENY_PARTS = (
    ".env", ".ssh", ".secrets", "secret", "credential", "token", "password",
    "passwd", "id_rsa", "id_ed25519", ".pem", ".key", ".pfx",
)


class ToolRefused(Exception):
    """A policy or validation boundary refused the request."""


@dataclass(frozen=True)
class AgentContext:
    actor: str = "assistant"
    allow_private: bool = False
    allow_local_write: bool = False
    allow_external_write: bool = False
    allow_privileged: bool = False
    audit_id: str = ""
    interface: str = ""
    conversation_id: str = ""
    turn_text: str = ""
    recent_text: str = ""
    now: str = ""
    sender: str = ""
    attachments: tuple = ()
    media_provider_config: object = None
    vision_model: str = ""
    media_transcriber: object = None
    media_image_inspector: object = None
    owner_authored: bool = False
    owner_recent_text: str = ""


def _owner_turn(ctx: "AgentContext", acfg: dict) -> bool:
    try:
        from app import rig_agent
        if rig_agent and rig_agent.owner_allowed(ctx.sender or "", acfg):
            return True
    except Exception:
        pass
    return bool(getattr(ctx, "owner_authored", False))


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    schema: dict
    handler: object
    risk: str = READ_ONLY
    timeout_seconds: float = 10.0
    private: bool = False
    migrated_from: str = ""
    requires_attachment_kind: str = ""

    def ollama_schema(self) -> dict:
        schema = self.schema
        if self.name == "antons_build":
            schema = dict(schema)
            props = dict(schema.get("properties") or {})
            files_prop = dict(props.get("files") or {})
            pages = sorted(
                p.stem for p in _BUILD_STATIC.glob("*.html") if p.is_file()
            )
            if pages:
                page_list = ", ".join(pages)
                files_prop["description"] = (
                    files_prop.get("description", "")
                    + " Existing pages you can edit: " + page_list
                    + " - use one of these names exactly when the request is about a page that already exists, and a new name only for something genuinely new."
                )
            props["files"] = files_prop
            schema["properties"] = props
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": schema,
            },
        }


class ToolRegistry:
    def __init__(self):
        self._tools = {}

    def register(self, tool: Tool) -> Tool:
        if not re.match(r"^[a-zA-Z][a-zA-Z0-9_]{1,63}$", tool.name):
            raise ValueError("bad tool name: %r" % tool.name)
        self._tools[tool.name] = tool
        return tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(str(name or "").strip())

    def all(self) -> list[Tool]:
        return [self._tools[k] for k in sorted(self._tools)]

    def available(self, ctx: AgentContext, include_unavailable: bool = False) -> list[Tool]:
        out = []
        for tool in self.all():
            ok, _why = authorize(tool, ctx)
            if ok or include_unavailable:
                out.append(tool)
        return out

    def ollama_tools(self, ctx: AgentContext, names: list[str] | None = None) -> list[dict]:
        allowed = []
        wanted = None if names is None else {str(n) for n in names}
        for tool in self.available(ctx):
            if wanted is not None and tool.name not in wanted:
                continue
            allowed.append(tool.ollama_schema())
        return allowed

    def execute(self, name: str, args: dict | None, ctx: AgentContext) -> dict:
        tool = self.get(name)
        if tool is None:
            return _failure(name, "unknown_tool", "unknown tool")
        ok, why = authorize(tool, ctx)
        if not ok:
            return _failure(
                name, "permission_denied", why,
                args=args or {}, **_action_failure_fields(tool, args or {}))
        try:
            clean_args = validate_args(tool.schema, args or {})
        except ToolRefused as exc:
            return _failure(
                name, "invalid_arguments", str(exc),
                args=args or {}, **_action_failure_fields(tool, args or {}))

        started = time.time()
        if dry_run_active() and tool.risk in (LOCAL_WRITE, EXTERNAL_WRITE, PRIVILEGED):
            return _failure(
                name, "dry_run",
                "this is a preview run: nothing that writes, sends or downloads "
                "is executed. Say what you WOULD do.",
                args=clean_args,
                **_action_failure_fields(tool, clean_args, state="DENIED"))
        executor = ThreadPoolExecutor(max_workers=1)
        fut = executor.submit(tool.handler, clean_args, ctx)
        try:
            result = fut.result(timeout=max(0.1, float(tool.timeout_seconds or 10.0)))
        except FutureTimeout:
            # cancel() cannot stop a call that has already started, so the work
            # is still going. Saying "it was delayed" invites a second identical
            # request; say what is actually true.
            fut.cancel()
            if tool.risk == READ_ONLY:
                message = (
                    "tool exceeded %.1fs and there is no result: the read did "
                    "not come back. Nothing is running in the background and "
                    "nothing will be reported later. You may retry in a "
                    "different way, or answer with what you already have and "
                    "state plainly what you could not verify."
                    % float(tool.timeout_seconds or 10.0)
                )
            else:
                message = (
                    "tool exceeded %.1fs and IS STILL RUNNING in the background. "
                    "Do NOT call it again for the same request. Tell the person it "
                    "was started and that you will report the result when it lands."
                    % float(tool.timeout_seconds or 10.0)
                )
            return _failure(
                name, "timeout",
                message,
                seconds=time.time() - started,
                args=clean_args,
                **_action_failure_fields(tool, clean_args, state="UNKNOWN"),
            )
        except ToolRefused as exc:
            return _failure(name, "refused", str(exc), seconds=time.time() - started,
                            args=clean_args,
                            **_action_failure_fields(tool, clean_args))
        except Exception as exc:
            return _failure(name, "exception", "tool failed: %s" % str(exc)[:180],
                            seconds=time.time() - started, args=clean_args,
                            **_action_failure_fields(tool, clean_args, state="UNKNOWN"))
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

        out = {
            "ok": True,
            "tool": name,
            "risk": tool.risk,
            "seconds": round(time.time() - started, 3),
            "args": _clip(clean_args, MAX_TOOL_ARGS_CHARS),
            "result": _limit_result(result),
        }
        if isinstance(result, dict):
            if result.get("risk"):
                out["risk"] = str(result.get("risk"))
            for key in ("action_id", "action_state", "idempotent_replay"):
                if key in result:
                    out[key] = result.get(key)
        if isinstance(result, dict) and result.get("ok") is False:
            out["ok"] = False
            out["error_type"] = str(result.get("error_type") or "tool_failed")
            out["message"] = str(result.get("message") or "")[:500]
        return out


def authorize(tool: Tool, ctx: AgentContext) -> tuple[bool, str]:
    if tool.private and not ctx.allow_private:
        return False, "private data tools require a private assistant context"
    if tool.requires_attachment_kind and not _ctx_has_attachment_kind(
            ctx, tool.requires_attachment_kind):
        return False, "%s requires an available %s attachment" % (
            tool.name, tool.requires_attachment_kind)
    if tool.risk == READ_ONLY:
        return True, ""
    if tool.risk in (LOCAL_WRITE, LOCAL_REVERSIBLE_WRITE):
        if ctx.allow_local_write:
            return True, ""
        return False, "local write tools require explicit server-side permission"
    if tool.risk == EXTERNAL_WRITE:
        if ctx.allow_external_write:
            return True, ""
        return False, "external write tools require explicit server-side permission"
    if tool.risk == PRIVILEGED:
        if ctx.allow_privileged:
            return True, ""
        return False, "privileged tools require explicit server-side permission"
    return False, "dangerous tools are never model-callable"


def validate_args(schema: dict, args: dict) -> dict:
    if not isinstance(args, dict):
        raise ToolRefused("arguments must be an object")
    props = schema.get("properties") if isinstance(schema, dict) else {}
    required = set(schema.get("required") or [])
    clean = {}
    for key in required:
        if key not in args:
            raise ToolRefused("missing required argument: %s" % key)
    for key, value in args.items():
        if key not in props:
            raise ToolRefused("unknown argument: %s" % key)
        clean[key] = _validate_value(key, props[key], value)
    return clean


def _validate_value(key: str, spec: dict, value):
    typ = spec.get("type")
    if isinstance(typ, list):
        if value is None and "null" in typ:
            return None
        typ = next((t for t in typ if t != "null"), typ[0])
    if "enum" in spec and value not in spec["enum"]:
        raise ToolRefused("%s must be one of: %s" % (key, ", ".join(map(str, spec["enum"]))))
    if typ == "string":
        if not isinstance(value, str):
            raise ToolRefused("%s must be a string" % key)
        out = value.strip()
        if spec.get("minLength") is not None and len(out) < int(spec["minLength"]):
            raise ToolRefused("%s is too short" % key)
        if spec.get("maxLength") is not None:
            out = out[:int(spec["maxLength"])]
        return out
    if typ == "integer":
        if isinstance(value, bool):
            raise ToolRefused("%s must be an integer" % key)
        try:
            out = int(value)
        except Exception:
            raise ToolRefused("%s must be an integer" % key)
        if spec.get("minimum") is not None:
            out = max(out, int(spec["minimum"]))
        if spec.get("maximum") is not None:
            out = min(out, int(spec["maximum"]))
        return out
    if typ == "number":
        try:
            out = float(value)
        except Exception:
            raise ToolRefused("%s must be a number" % key)
        if spec.get("minimum") is not None:
            out = max(out, float(spec["minimum"]))
        if spec.get("maximum") is not None:
            out = min(out, float(spec["maximum"]))
        return out
    if typ == "boolean":
        if not isinstance(value, bool):
            raise ToolRefused("%s must be a boolean" % key)
        return value
    if typ == "array":
        if not isinstance(value, list):
            raise ToolRefused("%s must be an array" % key)
        max_items = int(spec.get("maxItems") or 20)
        item_spec = spec.get("items") or {}
        return [_validate_value(key + "[]", item_spec, v) for v in value[:max_items]]
    if typ == "object":
        if not isinstance(value, dict):
            raise ToolRefused("%s must be an object" % key)
        return value
    return value


def _failure(name: str, error_type: str, message: str, seconds: float = 0.0,
             args: dict | None = None, risk: str = "",
             action_id: str = "", action_state: str = "") -> dict:
    out = {
        "ok": False,
        "tool": str(name or ""),
        "error_type": error_type,
        "message": str(message or "")[:500],
    }
    if risk:
        out["risk"] = str(risk)
    if action_id:
        out["action_id"] = str(action_id)
    if action_state:
        out["action_state"] = str(action_state)
    if seconds:
        out["seconds"] = round(seconds, 3)
    if args is not None:
        out["args"] = _clip(args, MAX_TOOL_ARGS_CHARS)
    return out


def _action_failure_fields(tool: Tool | None, args: dict | None,
                           state: str = "DENIED") -> dict:
    if tool is None or tool.risk not in (
            LOCAL_WRITE, LOCAL_REVERSIBLE_WRITE, EXTERNAL_WRITE, PRIVILEGED,
            DANGEROUS):
        return {}
    return {
        "risk": tool.risk,
        "action_id": _action_id(tool.name, args or {}),
        "action_state": state,
    }


def _clip(obj, limit=MAX_RESULT_CHARS):
    try:
        text = json.dumps(obj, ensure_ascii=False, default=str)
    except Exception:
        text = str(obj)
    if len(text) <= limit:
        return obj
    return {"truncated": True, "chars": len(text), "preview": text[:limit]}


def _limit_result(obj):
    return _clip(obj, MAX_RESULT_CHARS)


_INTERNAL_DEADLINE_FRACTION = 0.80


def _soft_deadline(tool_timeout_seconds: float) -> float:
    return time.time() + float(tool_timeout_seconds) * _INTERNAL_DEADLINE_FRACTION


def _fold(text: str) -> str:
    return " ".join(str(text or "").lower().split())


def _current_action_text(turn_text: str) -> str:
    text = str(turn_text or "")
    text = re.sub(r"(?s)```.*?```", " ", text)
    text = re.sub(r"(?m)^\s*>.*$", " ", text)
    text = re.sub(r"['\"“”‘’][^'\"“”‘’]{0,500}['\"“”‘’]", " ", text)
    text = re.sub(
        r"(?i)\b(earlier|previously|before|last time)\s+i\s+said\s+[^?!.]{0,180}",
        " ", text)
    return " ".join(text.split())


def _safe_filename(value: str, fallback: str = "download") -> str:
    base = os.path.basename(urllib.parse.urlsplit(value or "").path) or fallback
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("._")
    return (base or fallback)[:120]


def _normalize_url(url: str) -> str:
    value = str(url or "").strip()
    try:
        parts = urllib.parse.urlsplit(value)
        scheme = (parts.scheme or "https").lower()
        netloc = parts.netloc.lower()
        if netloc.endswith(":80") and scheme == "http":
            netloc = netloc[:-3]
        if netloc.endswith(":443") and scheme == "https":
            netloc = netloc[:-4]
        path = re.sub(r"/{2,}", "/", parts.path or "/")
        query = urllib.parse.urlencode(sorted(urllib.parse.parse_qsl(
            parts.query, keep_blank_values=True)))
        return urllib.parse.urlunsplit((scheme, netloc, path.rstrip("/") or "/",
                                        query, ""))
    except Exception:
        return " ".join(value.lower().split())


def _path_denied(path: Path) -> bool:
    low = str(path).lower()
    return any(part in low for part in _DENY_PARTS)


def _allowed_roots() -> list[Path]:
    roots = [ROOT]
    if AI_ROOT.exists():
        roots.append(AI_ROOT)
    if DOWNLOAD_ROOT.exists():
        roots.append(DOWNLOAD_ROOT)
    return roots


def _resolve_readable_path(raw: str) -> Path:
    value = str(raw or "").strip().strip("\"'")
    if not value:
        raise ToolRefused("path is required")
    p = Path(value)
    if not p.is_absolute():
        p = ROOT / p
    p = p.resolve()
    if _path_denied(p):
        raise ToolRefused("path names a protected file")
    for root in _allowed_roots():
        try:
            p.relative_to(root.resolve())
            return p
        except Exception:
            continue
    raise ToolRefused("path is outside readable roots")


def _attachment_kind_from_meta(att: dict) -> str:
    if not isinstance(att, dict):
        return "file"
    explicit = str(att.get("kind") or att.get("media_kind") or att.get("type") or "").lower()
    if explicit in ("audio", "voice"):
        return "audio"
    if explicit in ("image", "photo", "picture"):
        return "image"
    if explicit in ("video", "movie"):
        return "video"
    if explicit == "gif":
        return "gif"
    mime = str(att.get("mime") or att.get("mimeType") or "").lower()
    name = str(att.get("name") or att.get("transferName") or
               att.get("filename") or att.get("path") or "").lower()
    if mime == "image/gif" or name.endswith(".gif"):
        return "gif"
    if (mime.startswith("video/")
            or name.endswith((".mov", ".mp4", ".m4v", ".avi", ".webm", ".mkv"))):
        return "video"
    if (mime.startswith("audio/")
            or name.endswith((".m4a", ".mp3", ".wav", ".caf", ".aac",
                              ".ogg", ".opus", ".flac", ".amr"))):
        return "audio"
    if (mime.startswith("image/")
            or name.endswith((".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".bmp"))):
        return "image"
    return "file"


def _attachment_id(att: dict) -> str:
    if not isinstance(att, dict):
        return "att_unknown"
    for key in ("attachment_id", "id", "guid", "message_guid"):
        value = str(att.get(key) or "").strip()
        if value:
            if key == "attachment_id" and value.startswith("att_"):
                return value
            if re.match(r"^[A-Za-z0-9_.:-]{1,80}$", value) and key == "attachment_id":
                return value
            return "att_" + hashlib.sha1(value.encode("utf-8", "replace")).hexdigest()[:12]
    seed = "|".join(str(att.get(k) or "") for k in ("name", "mime", "mimeType", "path"))
    if not seed.strip("|"):
        seed = repr(sorted(att.keys()))
    return "att_" + hashlib.sha1(seed.encode("utf-8", "replace")).hexdigest()[:12]


def _attachment_matches_kind(att: dict, expected: str) -> bool:
    expected = str(expected or "").lower()
    kind = _attachment_kind_from_meta(att)
    if expected in ("audio", "voice"):
        return kind == "audio"
    if expected in ("image", "photo", "picture"):
        return kind == "image"
    if expected == "video":
        return kind == "video"
    if expected == "gif":
        return kind == "gif"
    if expected in ("file", "attachment"):
        return True
    return kind == expected


def _ctx_has_attachment_kind(ctx: AgentContext, expected: str) -> bool:
    return any(_attachment_matches_kind(att, expected)
               for att in (getattr(ctx, "attachments", ()) or ())
               if isinstance(att, dict))


def _resolve_attachment(ctx: AgentContext, attachment_id: str,
                        expected_kind: str = "") -> dict:
    wanted = str(attachment_id or "").strip()
    candidates = []
    for att in (getattr(ctx, "attachments", ()) or ()):
        if not isinstance(att, dict):
            continue
        if expected_kind and not _attachment_matches_kind(att, expected_kind):
            continue
        candidates.append(att)
        ids = {
            _attachment_id(att),
            str(att.get("attachment_id") or "").strip(),
            str(att.get("id") or "").strip(),
            str(att.get("guid") or "").strip(),
        }
        if wanted and wanted in ids:
            out = dict(att)
            out.setdefault("attachment_id", _attachment_id(att))
            out.setdefault("kind", _attachment_kind_from_meta(att))
            return out
    if not wanted and len(candidates) == 1:
        out = dict(candidates[0])
        out.setdefault("attachment_id", _attachment_id(out))
        out.setdefault("kind", _attachment_kind_from_meta(out))
        return out
    visible = [_attachment_id(att) for att in candidates[:8]]
    raise ToolRefused(
        "attachment %r is not available%s" % (
            wanted or "(missing id)",
            "; available: " + ", ".join(visible) if visible else ""))


def _attachment_transcript(att: dict) -> str:
    if not isinstance(att, dict):
        return ""
    keys = (
        "transcript", "voice_transcript", "audio_transcript",
        "speech_text", "stt_text", "caption_transcript",
    )
    for key in keys:
        value = str(att.get(key) or "").strip()
        if value:
            return value
    meta = att.get("metadata") if isinstance(att.get("metadata"), dict) else {}
    for key in keys + ("text", "caption"):
        value = str(meta.get(key) or "").strip()
        if value:
            return value
    return ""


def _attachment_bytes(att: dict, ctx: AgentContext, max_bytes: int) -> bytes:
    raw = att.get("bytes") if isinstance(att, dict) else None
    if isinstance(raw, bytes):
        if len(raw) > max_bytes:
            raise ToolRefused("attachment exceeds %d bytes" % max_bytes)
        return raw
    for key in ("data_b64", "base64", "content_b64"):
        value = str(att.get(key) or "").strip()
        if value:
            try:
                data = base64.b64decode(value, validate=False)
            except Exception:
                raise ToolRefused("attachment %s is not valid base64" % key)
            if len(data) > max_bytes:
                raise ToolRefused("attachment exceeds %d bytes" % max_bytes)
            return data
    if att.get("path"):
        path = _resolve_readable_path(str(att.get("path")))
        data = path.read_bytes()
        if len(data) > max_bytes:
            raise ToolRefused("attachment exceeds %d bytes" % max_bytes)
        return data
    guid = str(att.get("guid") or "").strip()
    pcfg = getattr(ctx, "media_provider_config", None)
    if guid and isinstance(pcfg, dict):
        from app import messaging
        data = messaging._bb_get(
            pcfg,
            "/api/v1/attachment/%s/download" % urllib.parse.quote(guid, safe=""),
            binary=True,
            timeout=60)
        if len(data) > max_bytes:
            raise ToolRefused("attachment exceeds %d bytes" % max_bytes)
        return data
    raise ToolRefused("attachment bytes are not available to the tool runtime")


def _attachment_image_b64(att: dict, ctx: AgentContext) -> str:
    for key in ("image_b64", "data_b64", "base64", "content_b64"):
        value = str(att.get(key) or "").strip()
        if value:
            return value
    guid = str(att.get("guid") or "").strip()
    pcfg = getattr(ctx, "media_provider_config", None)
    if guid and isinstance(pcfg, dict):
        from app import messaging
        value = messaging._bb_download_image(pcfg, guid)
        if value:
            return value
    raw = _attachment_bytes(att, ctx, 8 * 1024 * 1024)
    head = raw[:12]
    if not (head.startswith(b"\xff\xd8") or head.startswith(b"\x89PNG")
            or head.startswith(b"GIF8")
            or (head[:4] == b"RIFF" and head[8:12] == b"WEBP")):
        raise ToolRefused("attachment is not a supported model-readable image")
    return base64.b64encode(raw).decode("ascii")


def _attachment_public_meta(att: dict) -> dict:
    return {
        "attachment_id": _attachment_id(att),
        "kind": _attachment_kind_from_meta(att),
        "mime": str(att.get("mime") or att.get("mimeType") or "")[:120],
        "name": str(att.get("name") or att.get("transferName") or "")[:160],
    }


def _call_media_transcriber(backend, att: dict, ctx: AgentContext,
                            language: str) -> dict:
    raw = None
    for call in (
            lambda: backend(att, ctx=ctx, language=language),
            lambda: backend(att, language=language),
            lambda: backend(_attachment_bytes(att, ctx, 96 * 1024 * 1024),
                            language=language),
            lambda: backend(_attachment_bytes(att, ctx, 96 * 1024 * 1024))):
        try:
            value = call()
            if isinstance(value, dict):
                return dict(value)
            if str(value or "").strip():
                return {"transcript": str(value)}
            raw = value
        except TypeError:
            continue
    return {"transcript": str(raw or "")}


def _tool_transcribe_audio(args: dict, ctx: AgentContext) -> dict:
    att = _resolve_attachment(ctx, args.get("attachment_id") or "", "audio")
    transcript = _attachment_transcript(att)
    if transcript:
        return {
            **_attachment_public_meta(att),
            "transcript": transcript[:12000],
            "transcript_available": True,
            "source": "existing_transcript",
            "language": str(args.get("language") or "").strip(),
        }
    backend = getattr(ctx, "media_transcriber", None)
    if callable(backend):
        out = _call_media_transcriber(
            backend, att, ctx, str(args.get("language") or "").strip())
    else:
        try:
            from app import local_stt
            ok, reason = local_stt.available()
            if not ok:
                raise ToolRefused(
                    "audio transcript unavailable: no cached transcript and local STT is not ready: %s" % reason)
            out = local_stt.transcribe_attachment(
                att, ctx, str(args.get("language") or "").strip())
        except ToolRefused:
            raise
        except Exception as exc:
            raise ToolRefused(
                "audio transcript unavailable: local STT failed: %s" % str(exc)[:300])
    text = str(out.get("transcript") or out.get("text") or "").strip()
    if not text:
        raise ToolRefused("audio transcription produced no transcript")
    out.update(_attachment_public_meta(att))
    out["transcript"] = text[:12000]
    out["transcript_available"] = True
    out.setdefault("source", "live_stt")
    return out


def _tool_read_document(args: dict, ctx: AgentContext) -> dict:
    """Extract text from an available PDF attachment without cloud services."""
    att = _resolve_attachment(ctx, args.get("attachment_id") or "", "file")
    raw = _attachment_bytes(att, ctx, 20 * 1024 * 1024)
    mime = str(att.get("mime") or att.get("mimeType") or "").lower()
    name = str(att.get("name") or att.get("transferName") or "").lower()
    if not (raw.startswith(b"%PDF-") or "pdf" in mime or name.endswith(".pdf")):
        raise ToolRefused("attachment is not a PDF document")
    try:
        from app import messaging
        parsed = messaging._pdf_text_from_bytes(
            raw, max_chars=int(args.get("max_chars") or 12000))
    except ImportError as exc:
        raise ToolRefused("PDF reader dependency is unavailable: %s" % exc) from exc
    except Exception as exc:
        raise ToolRefused("PDF text extraction failed: %s" % str(exc)[:240]) from exc
    text = str(parsed.get("text") or "").strip()
    if not text:
        raise ToolRefused(
            "PDF contains no extractable text; it may be scanned or image-only")
    excerpt = _relevant_excerpt(
        text, args.get("query") or ctx.turn_text, max_chars=3400)
    return {
        **_attachment_public_meta(att),
        "title": str(parsed.get("title") or "")[:200],
        "text": excerpt,
        "extracted_chars": len(text),
        "excerpted": len(excerpt) < len(text),
        "page_count": int(parsed.get("page_count") or 0),
        "pages_read": int(parsed.get("pages_read") or 0),
        "source": "local_pypdf",
    }


def _tool_contact_lookup(args: dict, ctx: AgentContext) -> dict:
    from app import personal_assistant
    return personal_assistant.contact_lookup(args, ctx)


def _tool_calendar_search(args: dict, ctx: AgentContext) -> dict:
    from app import personal_assistant
    return personal_assistant.calendar_search(args, ctx)


def _tool_calendar_create(args: dict, ctx: AgentContext) -> dict:
    from app import personal_assistant
    return personal_assistant.calendar_create(args, ctx)


def _tool_send_message(args: dict, ctx: AgentContext) -> dict:
    from app import personal_assistant
    return personal_assistant.send_message(args, ctx)


def _his_turn(ctx: "AgentContext") -> bool:
    try:
        from app import messaging as _M
        acfg = _M.load_config().get("auto_reply") or {}
    except Exception:
        acfg = {}
    return _owner_turn(ctx, acfg)


def _tool_set_reminder(args: dict, ctx: AgentContext) -> dict:
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from app import reminders, temporal
    if not _his_turn(ctx):
        return {"ok": False, "error_type": "authorization_required",
                "action_state": "DENIED",
                "message": f"only {OWNER}' own messages can set reminders; this one "
                           "was written by someone else"}
    chat = str(ctx.conversation_id or "").strip()
    text = " ".join(str(args.get("text") or "").split())
    if not chat or not text:
        return {"ok": False, "error_type": "invalid_arguments", "action_state": "DENIED",
                "message": "a reminder needs the text to send and a chat to send it to"}
    tz_name, tz_source = reminders.local_timezone()
    now = datetime.now(ZoneInfo(tz_name))
    when = temporal.normalize_datetime(str(args.get("when") or ""), now=now,
                                       tz_name=tz_name, allow_past=False)
    if not when.ok:
        return {"ok": False, "error_type": when.error_type or "bad_time",
                "action_state": "DENIED",
                "message": (when.message or "could not read that time")
                + "; ask him when, or pass an exact time like 2026-10-01 09:00",
                "timezone": tz_name}
    due_local = when.dt.isoformat(timespec="minutes")
    for row in reminders.pending(chat):
        if row.get("text") == text and row.get("due_local") == due_local:
            return {"ok": True, "action_state": "REPLAYED", "idempotent_replay": True,
                    "reminder": row, "timezone": tz_name}
    row = reminders.add(chat, text, when.dt, tz_name, created_by=ctx.sender or "")
    return {"ok": True, "action_state": "EXECUTED", "risk": EXTERNAL_WRITE,
            "reminder": row, "timezone": tz_name,
            "timezone_source": tz_source,
            "note": "Antons will send '⏰ %s' to this chat at %s (%s)."
                    % (text[:80], when.dt.strftime("%Y-%m-%d %H:%M"), tz_name)}


def _tool_list_reminders(args: dict, ctx: AgentContext) -> dict:
    from app import reminders
    if not _his_turn(ctx):
        return {"ok": False, "error_type": "authorization_required",
                "message": f"only {OWNER} can see his reminders"}
    rows = reminders.pending(None if args.get("all_chats") else ctx.conversation_id)
    return {"ok": True, "count": len(rows), "reminders": rows}


def _allowed_sender(ctx: "AgentContext") -> bool:
    if not ctx.sender:
        return False
    try:
        from app import messaging as _M
        acfg = _M.load_config().get("auto_reply") or {}
        allowed = {_M._normalize_handle(h) for h in (acfg.get("allowed_senders") or [])}
        return _M._normalize_handle(ctx.sender) in allowed
    except Exception:
        return False


def _may_use_his_location(ctx: "AgentContext") -> bool:
    if _his_turn(ctx):
        return True
    try:
        from app import messaging as _M
        acfg = _M.load_config().get("auto_reply") or {}
        partners = {_M._normalize_handle(h) for h in (acfg.get("partner_handles") or [])}
        return bool(ctx.sender) and _M._normalize_handle(ctx.sender) in partners
    except Exception:
        return False


def _tool_plan_route(args: dict, ctx: AgentContext) -> dict:
    from app import route_planner
    dest = " ".join(str(args.get("destination") or "").split())
    chat = str(ctx.conversation_id or "").strip()
    if not dest or not chat:
        return {"ok": False, "error_type": "invalid_arguments",
                "message": "name the place to drive to"}
    via = [" ".join(str(v).split()) for v in (args.get("via") or []) if str(v).strip()]
    return route_planner.start(chat, dest,
                               origin_text=" ".join(str(args.get("origin") or "").split()),
                               via=via, sender=route_planner.send_to_chat,
                               may_use_his_location=_may_use_his_location(ctx))


def _tool_trip_map(args: dict, ctx: AgentContext) -> dict:
    from app import trip_log
    chat = str(ctx.conversation_id or "").strip()
    if not chat:
        return {"ok": False, "error_type": "invalid_arguments", "message": "no chat"}
    if not _may_use_his_location(ctx):
        return {"ok": False, "error_type": "authorization_required",
                "action_state": "DENIED",
                "message": f"his trip is shown only in his own chats and {PARTNER}'s"}
    style = str(args.get("style") or "light").strip().lower()
    return trip_log.start_map_job(
        chat, title=" ".join(str(args.get("title") or "").split())[:60],
        since=str(args.get("since") or "") or None,
        until=str(args.get("until") or "") or None,
        style=style if style in ("light", "dark") else "light")


def _tool_cancel_reminder(args: dict, ctx: AgentContext) -> dict:
    from app import reminders
    if not _his_turn(ctx):
        return {"ok": False, "error_type": "authorization_required",
                "action_state": "DENIED",
                "message": f"only {OWNER}' own messages can cancel reminders"}
    row = reminders.cancel(str(args.get("reminder_id") or ""), None)
    if not row:
        return {"ok": False, "error_type": "not_found", "action_state": "DENIED",
                "message": "no pending reminder with that id; list_reminders shows them"}
    return {"ok": True, "action_state": "EXECUTED", "reminder": row}


def _goal_action_failure(tool: str, error_type: str, message: str,
                         args: dict, ctx: AgentContext, action_id: str = "") -> dict:
    aid = action_id or _action_id(tool, args or {})
    out = {
        "ok": False,
        "tool": tool,
        "risk": LOCAL_WRITE,
        "action_id": aid,
        "action_state": "DENIED",
        "error_type": error_type,
        "message": str(message or "")[:500],
    }
    _append_action_ledger({
        "audit_id": ctx.audit_id,
        "interface": ctx.interface,
        "conversation_id": ctx.conversation_id,
        "action_id": aid,
        "tool": tool,
        "risk": LOCAL_WRITE,
        "status": "denied",
        "error_type": error_type,
    })
    return out


def _goal_action_success(tool: str, result: dict, ctx: AgentContext,
                         action_id: str, state: str = "EXECUTED") -> dict:
    out = dict(result or {})
    out.setdefault("ok", True)
    out["tool"] = tool
    out["risk"] = LOCAL_WRITE
    out["action_id"] = action_id
    out["action_state"] = state
    if state == "REPLAYED":
        out["idempotent_replay"] = True
    _append_action_ledger({
        "audit_id": ctx.audit_id,
        "interface": ctx.interface,
        "conversation_id": ctx.conversation_id,
        "action_id": action_id,
        "tool": tool,
        "risk": LOCAL_WRITE,
        "status": "replayed" if state == "REPLAYED" else "executed",
        "goal_id": out.get("id") or ((out.get("goal") or {}).get("id")),
    })
    return out


def _goal_write_allowed(ctx: "AgentContext") -> bool:
    return _his_turn(ctx)


_GOAL_NOT_HIS_TURN = (f"only {OWNER}' own messages can change his goals; this one "
                      "was written by someone else")


_EMPTY_GOAL_TITLE_RE = re.compile(
    r"^\W*(anton\W+)?(a\s+)?(new\s+goal|goal|jauns?\s+m[ēe]rķis?|jaunu\s+"
    r"m[ēe]rķi|m[ēe]rķis|untitled|tbd)\W*$", re.I)


def _tool_add_goal(args: dict, ctx: AgentContext) -> dict:
    from app import goals

    if _EMPTY_GOAL_TITLE_RE.match(str(args.get("title") or "").strip()):
        return _goal_action_failure(
            "add_goal", "goal_has_no_content",
            "that title is just the request; ask what the goal actually is, "
            "then save it with that as the title", args, ctx,
            _action_id("add_goal", {"title": args.get("title")}))
    action_id = _action_id("add_goal", {
        "title": args.get("title"),
        "parent_goal_id": args.get("parent_goal_id"),
        "contexts": args.get("contexts") or [],
    })
    if not _goal_write_allowed(ctx):
        return _goal_action_failure("add_goal", "authorization_required",
                                    _GOAL_NOT_HIS_TURN, args, ctx, action_id)
    result = goals.create(
        args.get("title") or "",
        horizon=args.get("horizon") or "week",
        area=args.get("area") or "",
        detail=args.get("description") or "",
        parent_id=args.get("parent_goal_id"),
        target_date=args.get("target_date") or None,
        importance=args.get("importance") or "normal",
        contexts=args.get("contexts") or [],
        resurface_mode=args.get("resurface_mode") or "contextual",
        visibility="private_self",
        source=ctx.interface or "qwen_agent",
        kind=args.get("kind") or "",
        done_definition=args.get("done_definition") or "",
        actor=ctx.actor,
        action_id=action_id,
        dedupe=True,
    )
    if not result.get("ok"):
        return _goal_action_failure(
            "add_goal", "goal_write_failed", result.get("error") or "goal could not be created",
            args, ctx, action_id)
    state = "REPLAYED" if result.get("duplicate") else "EXECUTED"
    return _goal_action_success("add_goal", result, ctx, action_id, state)


def _tool_search_goals(args: dict, ctx: AgentContext) -> dict:
    from app import goals

    return goals.search_goals(args.get("query") or "",
                              status=args.get("status") or "active",
                              limit=args.get("limit") or 8,
                              include_private=True)


def _tool_list_goals(args: dict, ctx: AgentContext) -> dict:
    from app import goals

    return goals.list_goals(status=args.get("status") or "active",
                            limit=args.get("limit") or 20,
                            include_private=True)


def _tool_update_goal(args: dict, ctx: AgentContext) -> dict:
    from app import goals

    action_id = _action_id("update_goal", {
        "goal_id": args.get("goal_id"),
        "progress": args.get("progress"),
        "progress_note": args.get("progress_note"),
        "title": args.get("title"),
    })
    if not _goal_write_allowed(ctx):
        return _goal_action_failure("update_goal", "authorization_required",
                                    _GOAL_NOT_HIS_TURN, args, ctx, action_id)
    fields = {}
    for src, dest in (
            ("title", "title"), ("description", "detail"),
            ("importance", "importance"), ("resurface_mode", "resurface_mode"),
            ("target_date", "target_date")):
        if args.get(src) not in (None, ""):
            fields[dest] = args.get(src)
    if args.get("contexts"):
        fields["contexts"] = args.get("contexts")
    result = None
    if fields:
        result = goals.update(
            args.get("goal_id"), actor=ctx.actor, action_id=action_id,
            event_type="updated", event_note=args.get("progress_note") or "",
            **fields)
        if not result.get("ok"):
            return _goal_action_failure("update_goal", "goal_write_failed",
                                        result.get("error") or "goal could not be updated",
                                        args, ctx, action_id)
    if args.get("progress") is not None or args.get("progress_note"):
        result = goals.progress(
            args.get("goal_id"), note=args.get("progress_note") or "",
            progress=args.get("progress"), actor=ctx.actor,
            source=ctx.interface or "qwen_agent", action_id=action_id)
    if not result:
        return _goal_action_failure("update_goal", "invalid_arguments",
                                    "nothing to update", args, ctx, action_id)
    if not result.get("ok"):
        return _goal_action_failure("update_goal", "goal_write_failed",
                                    result.get("error") or "goal could not be updated",
                                    args, ctx, action_id)
    return _goal_action_success("update_goal", result, ctx, action_id)


def _tool_complete_goal(args: dict, ctx: AgentContext) -> dict:
    from app import goals

    action_id = _action_id("complete_goal", {"goal_id": args.get("goal_id")})
    if not _goal_write_allowed(ctx):
        return _goal_action_failure("complete_goal", "authorization_required",
                                    _GOAL_NOT_HIS_TURN, args, ctx, action_id)
    result = goals.complete(args.get("goal_id"), note=args.get("note") or "",
                            actor=ctx.actor, action_id=action_id)
    if not result.get("ok"):
        return _goal_action_failure("complete_goal", "goal_write_failed",
                                    result.get("error") or "goal could not be completed",
                                    args, ctx, action_id)
    return _goal_action_success("complete_goal", result, ctx, action_id)


def _tool_defer_goal(args: dict, ctx: AgentContext) -> dict:
    from app import goals

    action_id = _action_id("defer_goal", {
        "goal_id": args.get("goal_id"), "until": args.get("until")})
    if not _goal_write_allowed(ctx):
        return _goal_action_failure("defer_goal", "authorization_required",
                                    _GOAL_NOT_HIS_TURN, args, ctx, action_id)
    result = goals.defer(args.get("goal_id"), until=args.get("until") or None,
                         note=args.get("note") or "", actor=ctx.actor,
                         action_id=action_id)
    if not result.get("ok"):
        return _goal_action_failure("defer_goal", "goal_write_failed",
                                    result.get("error") or "goal could not be deferred",
                                    args, ctx, action_id)
    return _goal_action_success("defer_goal", result, ctx, action_id)


def _tool_cancel_goal(args: dict, ctx: AgentContext) -> dict:
    from app import goals

    action_id = _action_id("cancel_goal", {"goal_id": args.get("goal_id")})
    if not _goal_write_allowed(ctx):
        return _goal_action_failure("cancel_goal", "authorization_required",
                                    _GOAL_NOT_HIS_TURN, args, ctx, action_id)
    result = goals.cancel(args.get("goal_id"), note=args.get("note") or "",
                          actor=ctx.actor, action_id=action_id)
    if not result.get("ok"):
        return _goal_action_failure("cancel_goal", "goal_write_failed",
                                    result.get("error") or "goal could not be cancelled",
                                    args, ctx, action_id)
    return _goal_action_success("cancel_goal", result, ctx, action_id)


def _tool_note_goal_surfaced(args: dict, ctx: AgentContext) -> dict:
    from app import goals

    action_id = _action_id("note_goal_surfaced", {"goal_id": args.get("goal_id")})
    result = goals.mark_surfaced(args.get("goal_id"), actor=ctx.actor,
                                 source=ctx.interface or "qwen_agent",
                                 action_id=action_id)
    if not result.get("ok"):
        return _goal_action_failure("note_goal_surfaced", "goal_write_failed",
                                    result.get("error") or "goal could not be marked surfaced",
                                    args, ctx, action_id)
    return _goal_action_success("note_goal_surfaced", result, ctx, action_id)


def _tool_inspect_audio(args: dict, ctx: AgentContext) -> dict:
    """Everything returned is measured (ffprobe + DSP) or transcribed
    (whisper.cpp); nothing is inferred from the filename. Each field carries
    its provenance so the model can tell ground truth from a heuristic.
    """
    from app import audio_probe
    att = _resolve_attachment(ctx, args.get("attachment_id") or "", "audio")
    meta = _attachment_public_meta(att)
    # Same byte source and the same content hashing the STT path uses, so the
    # two caches key on identical content and neither re-fetches for the other.
    import tempfile
    from pathlib import Path as _Path
    try:
        raw = _attachment_bytes(att, ctx, 96 * 1024 * 1024)
    except ToolRefused:
        raise
    except Exception as exc:
        return {**meta, "ok": False, "analysis_available": False,
                "reason": "audio bytes unavailable: %s" % str(exc)[:200],
                "transcript": _attachment_transcript(att)[:12000] or ""}
    name = str((att or {}).get("name") or (att or {}).get("transferName") or "")
    suffix = _Path(name).suffix or ".bin"
    with tempfile.TemporaryDirectory(prefix="audio-probe-") as tdir:
        src = _Path(tdir) / ("source" + suffix)
        src.write_bytes(raw)
        result = audio_probe.analyse(src)
    if not result.get("ok"):
        return {**meta, "ok": False, "analysis_available": False,
                "reason": result.get("error") or "analysis failed"}
    out = {
        **meta,
        "ok": True,
        "analysis_available": True,
        "type": result["type"],
        "type_confidence": result["type_confidence"],
        "evidence": result.get("why") or [],
        "duration_seconds": result.get("duration_seconds"),
        "codec": result.get("codec"),
        "channels": result.get("channels"),
        "sample_rate": result.get("sample_rate"),
        "features": result.get("features"),
        "cache_hit": bool(result.get("cache_hit")),
        "metadata_source": result.get("metadata_source"),
        "features_source": result.get("features_source"),
        "classification_source": result.get("classification_source"),
        # what this tool can NEVER tell you, stated in-band so the model does
        # not fill the gap with invention
        "not_available": [
            "song identity (no audio fingerprinting is installed)",
            "artist or title",
            "genre or instrumentation (no trained audio classifier)",
        ],
    }
    if result.get("possible_background_audio"):
        out["possible_background_audio"] = True

    # SPEECH GETS WORDS. Reuses the same cached whisper.cpp result the
    # transcribe_audio tool would produce, so asking both questions about one
    # clip never transcribes it twice.
    wants_words = result["type"] in (audio_probe.SPEECH, audio_probe.MIXED)
    existing = _attachment_transcript(att)
    if existing:
        out["transcript"] = existing[:12000]
        out["transcript_source"] = "existing_transcript"
    elif wants_words:
        try:
            from app import local_stt
            ok, reason = local_stt.available()
            if ok:
                stt = local_stt.transcribe_attachment(att, ctx, "")
                text = str(stt.get("transcript") or stt.get("text") or "").strip()
                if text:
                    out["transcript"] = text[:12000]
                    out["transcript_source"] = "whisper_cpp"
                    out["transcript_cache_hit"] = bool(stt.get("cache_hit"))
                    if stt.get("language"):
                        out["language"] = stt.get("language")
            else:
                out["transcript_unavailable"] = reason
        except Exception as exc:
            out["transcript_unavailable"] = str(exc)[:200]
    if result["type"] == audio_probe.MUSIC:
        # Whisper on sung vocals invents words. It is not run here, and the
        # tool says why rather than leaving a suggestive silence.
        out["lyrics_note"] = (
            "Speech recognition is not run on music: on singing it produces "
            "plausible but wrong words. Ask explicitly if an uncertain "
            "attempt is wanted.")
    return out


def _vision_model_name(ctx: AgentContext) -> str:
    return str(getattr(ctx, "vision_model", "") or "qwen3.8:latest").strip()


def _describe_image_b64(image_b64: str, question: str, ctx: AgentContext,
                        num_predict: int = 220) -> str:
    """Send one image to the local vision model and return what it says.

    Factored out of _tool_inspect_image so street imagery reuses the vision
    pipeline that already exists rather than standing up a second one. Same
    model, same endpoint, same keep-alive -- the only difference is where the
    pixels came from.
    """
    payload = {
        "model": _vision_model_name(ctx),
        "messages": [{"role": "user", "content": question, "images": [image_b64]}],
        "stream": False,
        "think": False,
        "keep_alive": KEEP_ALIVE,
        "options": _runner_options({"temperature": 0, "num_predict": num_predict}),
    }
    req = urllib.request.Request(
        "http://127.0.0.1:11434/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        raw = json.loads(resp.read().decode("utf-8", "replace"))
    return str(((raw.get("message") or {}).get("content") or "")).strip()


def _tool_inspect_image(args: dict, ctx: AgentContext) -> dict:
    att = _resolve_attachment(ctx, args.get("attachment_id") or "", "image")
    question = str(args.get("question") or "Describe the image and note any visible text.").strip()
    inspector = getattr(ctx, "media_image_inspector", None)
    if callable(inspector):
        value = inspector(att, question=question)
        if isinstance(value, dict):
            out = dict(value)
        else:
            out = {"description": str(value or "").strip()}
        out.update(_attachment_public_meta(att))
        out.setdefault("question", question)
        return out
    image_b64 = _attachment_image_b64(att, ctx)
    model = _vision_model_name(ctx)
    description = _describe_image_b64(image_b64, question, ctx)
    if not description:
        raise ToolRefused("vision model returned no image description")
    return {
        **_attachment_public_meta(att),
        "question": question,
        "model": model,
        "description": description[:6000],
    }


def _tool_inspect_video(args: dict, ctx: AgentContext) -> dict:
    att = _resolve_attachment(ctx, args.get("attachment_id") or "", "video")
    return {
        **_attachment_public_meta(att),
        "video_inspection_available": False,
        "frames_available": False,
        "audio_transcript_available": bool(_attachment_transcript(att)),
        "transcript": _attachment_transcript(att)[:12000],
        "message": (
            "Video frame extraction is not configured in this local agent runtime. "
            "Only attachment metadata and any supplied transcript are available."
        ),
    }


_LV_LETTERS = "\u0101\u010d\u0113\u0123\u012b\u0137\u013c\u0146\u0161\u016b\u017e"


def _english_twin(query, ctx):
    """The same query in English, or "" if it already is English."""
    text = str(query or "")
    if not any(ch in _LV_LETTERS for ch in text.lower()):
        return ""
    try:
        from app import messaging
        cfg = dict(messaging.load_config().get("auto_reply") or {})
        out = messaging._ollama_chat(
            cfg,
            [{"role": "system",
              "content": ("Translate this web-search query to English. Keep it "
                          "2-5 keywords, no sentence, no punctuation. Proper "
                          "names stay as they are. Answer with the query only.")},
             {"role": "user", "content": text[:120]}],
            options={"temperature": 0, "num_predict": 24})
    except Exception:
        return ""
    twin = " ".join(str(out or "").split())[:80]
    return twin if twin and twin.lower() != text.lower() else ""


_STREET_VISION_PROMPT = (
    "This is a street-level photograph taken from a vehicle or on foot. "
    "Describe only what is actually visible, in one short paragraph: the kind "
    "of road or place, buildings and their entrances, parking, pavements, "
    "junctions or traffic lights, and any legible signs. If something is "
    "unclear or cut off, say so instead of guessing."
)


def _street_geocode(place: str):
    """Address to coordinates, through the geocoder the project already has."""
    from app import date_flow
    try:
        return date_flow.geocode(str(place or "").strip())
    except Exception:
        return None


def _street_resolve_point(args: dict):
    """Where the caller means. Coordinates win; a place name is geocoded.

    Nothing here invents a position: if neither a valid pair of coordinates
    nor a resolvable place is given, the caller is told so and no imagery is
    fetched for a guessed point.
    """
    from app import street_imagery as si
    coords = si.valid_coords(args.get("latitude"), args.get("longitude"))
    if coords:
        return coords, ""
    place = str(args.get("place") or "").strip()
    if place:
        found = _street_geocode(place)
        if found:
            return (float(found[0]), float(found[1])), place
        return None, place
    return None, ""


def _street_no_point(place: str) -> dict:
    if place:
        return {"ok": False, "error_type": "place_not_found",
                "message": "could not turn that place into coordinates; give "
                           "latitude and longitude, or a fuller address",
                "place": place[:120], "images": []}
    return {"ok": False, "error_type": "no_location",
            "message": "street imagery needs a position: pass latitude and "
                       "longitude, or a place/address to look up",
            "images": []}


def _street_public(rec: dict) -> dict:
    """One image as the model should see it -- provenance kept, internals dropped."""
    return {k: rec.get(k) for k in
            ("id", "provider", "latitude", "longitude", "distance_m", "heading",
             "captured_at", "sequence_id", "is_panorama", "thumbnail",
             "image_reference", "attribution")}


def _tool_street_imagery(args: dict, ctx: AgentContext) -> dict:
    from app import street_imagery as si

    action = str(args.get("action") or "inspect_location").strip().lower()

    if action == "get_sequence":
        image_id = str(args.get("image_id") or "").strip()
        if not image_id:
            return {"ok": False, "error_type": "missing_image_id",
                    "message": "get_sequence needs the image_id of a frame "
                               "returned by an earlier search", "images": []}
        out = si.get_sequence(image_id, provider=args.get("provider"),
                              before=int(args.get("before") or 2),
                              after=int(args.get("after") or 2))
        if not out.get("ok"):
            return {"ok": False, "error_type": out.get("error") or "no_sequence",
                    "message": out.get("message") or "no surrounding frames",
                    "images": []}
        return {"ok": True, "action": action,
                "anchor": _street_public(out.get("anchor") or {}),
                "images": [_street_public(r) for r in out.get("images") or []],
                "attribution": out.get("attribution") or []}

    point, place = _street_resolve_point(args)
    if not point:
        return _street_no_point(place)
    lat, lon = point

    radius = int(args.get("radius_m") or si.DEFAULT_RADIUS_M)
    if action == "search_nearby":
        limit = int(args.get("limit") or 8)
        found = si.search_nearby(lat, lon, radius, limit)
        images = [_street_public(r) for r in found.get("images") or []]
        return {"ok": bool(images), "action": action,
                "location": found.get("location"),
                "resolved_from": place or None,
                "radius_m": found.get("radius_m"),
                "images": images,
                "providers": found.get("providers") or {},
                "attribution": found.get("attribution") or [],
                "message": "" if images else
                           "no street-level imagery is available around that point"}

    # inspect_location (the default) and get_image both end at the vision model.
    if action == "get_image":
        image_id = str(args.get("image_id") or "").strip()
        found = si.search_nearby(lat, lon, max(radius, 50), 25)
        chosen = [r for r in (found.get("images") or []) if r["id"] == image_id][:1]
        providers = found.get("providers") or {}
        if not chosen:
            return {"ok": False, "error_type": "unknown_image",
                    "message": "that image is not among the nearby results",
                    "images": []}
    else:
        max_images = max(1, min(int(args.get("max_images") or 4), 6))
        found = si.search_nearby(lat, lon, radius, 40)
        providers = found.get("providers") or {}
        chosen = si.select_best(found.get("images") or [], lat, lon, max_images,
                                radius_m=radius)

    if not chosen:
        failed = {k: v.get("error") for k, v in (providers or {}).items()
                  if not v.get("ok") and v.get("error")}
        return {"ok": False, "action": action, "error_type": "no_coverage",
                "location": {"latitude": lat, "longitude": lon},
                "resolved_from": place or None,
                "images": [], "providers": providers,
                "message": ("no street-level imagery is available around that "
                            "point" if not failed else
                            "no imagery available (providers unavailable: %s)"
                            % ", ".join(sorted(failed)))}

    question = str(args.get("question") or "").strip() or _STREET_VISION_PROMPT
    observations, seen_years, vlm_started = [], set(), time.time()
    for rec in chosen:
        entry = _street_public(rec)
        # Thumbnails, deliberately. A vision model reads these, it does not
        # print them: the KartaView thumbnail is ~11 KB against ~3 MB for the
        # full frame. A larger image is worth fetching only when something
        # specific -- a sign, a house number -- cannot be resolved without it.
        url = rec.get("thumbnail") or rec.get("image_reference")
        if args.get("detail"):
            url = rec.get("image_reference") or url
        try:
            blob = si.fetch_image_bytes(url)
            entry["description"] = _describe_image_b64(
                base64.b64encode(blob).decode("ascii"), question, ctx)
            si.cache_describe(rec["provider"], rec["id"], entry["description"])
        except si.ProviderError as exc:
            entry["description"] = ""
            entry["error"] = str(exc)
        except Exception:
            entry["description"] = ""
            entry["error"] = "vision_failed"
        if entry.get("captured_at"):
            seen_years.add(entry["captured_at"][:4])
        observations.append(entry)

    described = [o for o in observations if o.get("description")]
    LOGGER.info("tool=street_imagery action=%s lat=%.5f lon=%.5f radius=%d "
                "results=%d described=%d vlm_latency=%.2f",
                action, lat, lon, radius, len(chosen), len(described),
                time.time() - vlm_started)
    years = sorted(y for y in seen_years if y)
    return {
        "ok": bool(described),
        "action": action,
        "location": {"latitude": lat, "longitude": lon},
        "resolved_from": place or None,
        "radius_m": found.get("radius_m"),
        "images": observations,
        "providers": providers,
        "attribution": found.get("attribution") or [],
        "imagery_age_note": (
            "This imagery was captured in %s and shows the location as it was "
            "then, not as it is now." % ", ".join(years) if years else
            "Capture dates are unknown; do not describe this as current."),
        "message": "" if described else "imagery was found but could not be read",
    }


def _tool_geocode_place(args: dict, ctx: AgentContext) -> dict:
    place = str(args.get("place") or "").strip()
    if not place:
        return {"ok": False, "error_type": "missing_place",
                "message": "give a place or address to look up"}
    found = _street_geocode(place)
    if not found:
        return {"ok": False, "error_type": "not_found", "query": place,
                "message": "no coordinates found for that place"}
    return {"ok": True, "query": place,
            "latitude": round(float(found[0]), 7),
            "longitude": round(float(found[1]), 7),
            "source": "OpenStreetMap Nominatim"}


def _tool_web_search(args: dict, ctx: AgentContext) -> dict:
    from app import coder
    query = args["query"]
    limit = args.get("limit") or 5
    data = coder.web_search(query, limit=limit)
    rows = list(data.get("results") or [])
    twin = _english_twin(query, ctx)
    if twin:
        try:
            extra = coder.web_search(twin, limit=limit) or {}
        except Exception:
            extra = {}
        seen = {str(r.get("url") or "") for r in rows}
        for item in (extra.get("results") or []):
            if str(item.get("url") or "") not in seen:
                rows.append(item)
        data = dict(data, results=rows, english_query=twin)
    results = []
    for item in rows[:max(limit, 8)]:
        results.append({
            "title": str(item.get("title") or "")[:240],
            "url": str(item.get("url") or "")[:600],
            "snippet": str(item.get("snippet") or item.get("content") or "")[:700],
        })
    for item in results[:1]:
        url = str(item.get("url") or "")
        if not url:
            continue
        try:
            from app import messaging as _m
            info = _m.read_link(url, max_chars=2000, timeout=12) or {}
            page = {"text": info.get("text") or "",
                    "title": info.get("title") or "",
                    "links": _ranked_links(info.get("links") or [],
                                           ctx.turn_text)}
        except Exception:
            continue
        body = str(page.get("text") or "").strip()
        if body:
            item["page_text"] = body[:2000]
            item["page_title"] = page.get("title") or ""
            if page.get("links"):
                item["page_links"] = page["links"][:10]
                item["linked_pages"] = _read_linked(page["links"], ctx)
    out = {"query": query, "results": results,
           "backend_error": data.get("error") or ""}
    if data.get("english_query"):
        out["also_searched_in_english"] = data["english_query"]
    return out


def _relevant_excerpt(text: str, query: str, max_chars: int = 3400) -> str:
    """Return the most query-relevant bounded windows in source order."""
    body = " ".join(str(text or "").split())
    if len(body) <= max_chars:
        return body
    # Score full-size overlapping windows. A table often straddles several
    # short chunks; returning independently ranked fragments cut the Swedbank
    # limits row after its first number even though the rest was extracted.
    step = max(400, max_chars // 4)
    windows = [(i, body[i:i + max_chars])
               for i in range(0, len(body), step)]
    words = [w for w in re.findall(r"[0-9a-zāčēģīķļņšūž]+", _fold(query))
             if len(w) >= 4]
    roots = {w[:6] for w in words}
    scored = []
    for index, chunk in windows:
        folded = _fold(chunk)
        score = sum(2 * folded.count(root) for root in roots)
        scored.append((score, -index, chunk))
    if not scored:
        return body[:max_chars]
    return max(scored, key=lambda row: (row[0], row[1]))[2][:max_chars]


def _tool_web_read(args: dict, ctx: AgentContext) -> dict:
    from app import messaging
    url = args["url"]
    if not messaging._link_is_safe(url):
        raise ToolRefused("refusing to fetch non-public URL")
    max_chars = args.get("max_chars") or 1800
    render = bool(args.get("render"))
    click = args.get("click")
    # Network bytes are already downloaded in full; keeping more extracted text
    # is cheap and lets query selection reach a table below a long introduction.
    fetch_chars = max(int(max_chars), 60000)
    info = messaging.read_link(url, max_chars=fetch_chars, timeout=50,
                               render=render, click=click) or {}
    full_text = str(info.get("text") or "")
    is_json = info.get("note") == "json response"
    if is_json:
        text = full_text[:16000]
    else:
        text = _relevant_excerpt(
            full_text, args.get("query") or ctx.turn_text, max_chars=3800)
    out = {
        "url": info.get("url") or url,
        "title": str(info.get("title") or "")[:240],
        "text": text,
        "fetched_chars": len(full_text),
        "excerpted": len(text) < len(full_text),
        "note": str(info.get("note") or "")[:240],
        "links": _ranked_links(info.get("links") or [],
                               args.get("query") or ctx.turn_text),
    }
    # A JavaScript app renders almost no text server-side. read_link re-opens
    # such a page in a real browser; `api_responses` is the JSON the page loaded
    # from its own API (the actual data -- prices, schedules, search results).
    api = info.get("api_responses") or []
    if api:
        # one page's data endpoint can be large (a year of daily prices); keep
        # the biggest few whole enough to use, drop tiny/likely-irrelevant ones
        api = sorted(api, key=lambda a: -len(str(a.get("data") or "")))
        out["api_responses"] = [
            {"url": a.get("url"), "data": str(a.get("data") or "")[:45000]}
            for a in api[:5] if len(str(a.get("data") or "")) > 30]
        out["hint"] = ("the page's data is in api_responses (JSON the page "
                       "fetched). Read the answer from there -- the text above "
                       "is page chrome. A response cut at 45 KB may not parse as "
                       "JSON; scan it for the fields you need. If what you want "
                       "still is not there, call web_read again with `click` "
                       "naming a control to press (e.g. \"See prices\").")
    elif len(full_text) < 300 and not is_json:
        out["hint"] = ("this page rendered almost nothing. Retry once with "
                       "render:true, or click:\"<button text>\" if a control "
                       "must be pressed to load the data.")
    return out


def _read_linked(links, ctx, budget=2, chars=1200, depth=2):
    """Read the best few links off a page, so a fact one hop down is in hand."""
    from app import messaging as _m
    out = []
    for link in (links or [])[:budget]:
        url = str(link.get("url") or "")
        if not url:
            continue
        try:
            info = _m.read_link(url, max_chars=chars, timeout=12) or {}
        except Exception:
            continue
        body = " ".join(str(info.get("text") or "").split())
        if not body:
            continue
        entry = {"url": url,
                 "title": str(info.get("title") or "")[:120],
                 "text": body[:chars],
                 "links": _ranked_links(info.get("links") or [],
                                        ctx.turn_text)[:6]}
        if depth > 1 and entry["links"]:
            entry["linked_pages"] = _read_linked(
                entry["links"], ctx, budget=1, chars=chars, depth=depth - 1)
        out.append(entry)
    return out


def _ranked_links(links, query=None, limit=20):
    """The page's navigation, best first, for the model or the auto-walk.

    Two signals, because either alone misranks:

      * SHAPE. Across sites, schedules, price lists and opening hours live on
        section pages: short path, short human label. Articles are deep or
        carry no anchor text.
      * RARE words from the question. Counting every shared word is useless --
        on a radio station's site "radio" and "latvijas" match every URL. A
        term counts only when few of this page's links contain it, so "lr9"
        decides and "radio" does not.
    """
    from urllib.parse import urlparse
    links = [l for l in (links or []) if str(l.get("url") or "")]
    if not links:
        return []
    hays = [(str(l.get("text") or "") + " " + str(l.get("url") or "")).lower()
            for l in links]
    terms = {w for w in re.findall(r"\w+", str(query or "").lower())
             if len(w) > 2}
    rare = set()
    for w in terms:
        hits = sum(1 for h in hays if w in h)
        if 0 < hits <= max(1, len(links) * 0.4):
            rare.add(w)

    ranked, seen = [], set()
    for i, (link, hay) in enumerate(zip(links, hays)):
        url = str(link.get("url") or "")
        label = " ".join(str(link.get("text") or "").split())
        if url in seen:
            continue
        seen.add(url)
        try:
            depth = len([p for p in urlparse(url).path.split("/") if p])
        except Exception:
            depth = 9
        unlabelled = 6 if not label else 0
        wordy = 2 if len(label.split()) > 4 else 0
        tracking = 6 if "?" in url else 0
        relevance = sum(1 for w in rare if w in hay)
        ranked.append((depth + unlabelled + wordy + tracking - relevance * 4,
                       i, link))
    ranked.sort()
    return [link for _, _, link in ranked[:limit]]


def _tool_research(args: dict, ctx: AgentContext) -> dict:
    question = args["question"]
    limit = args.get("limit") or 3
    search = _tool_web_search({"query": question, "limit": limit}, ctx)
    pages = []
    for item in (search.get("results") or [])[:2]:
        url = item.get("url") or ""
        if not url:
            continue
        try:
            pages.append(_tool_web_read({"url": url, "max_chars": 1400}, ctx))
        except Exception as exc:
            pages.append({"url": url, "error": str(exc)[:160]})
    pages.extend(_follow_one_level(pages, question, ctx))
    return {"question": question, "search": search.get("results") or [], "pages": pages}


def _follow_one_level(pages, question, ctx, budget=3):
    """Read a few of the section pages the first level linked to.

    A front page names the station; the schedule, the price list and the
    opening hours live one click below it, and the top search hit is very
    often that front page. Which links get followed is decided by the same
    shape ranking web_read uses -- no reading of the question, no site
    knowledge -- so this behaves the same on any site.
    """
    seen = {str(p.get("url") or "") for p in pages}
    out = []
    for page in list(pages):
        for link in (page.get("links") or [])[:4]:
            url = str(link.get("url") or "")
            if not url or url in seen:
                continue
            seen.add(url)
            try:
                out.append(_tool_web_read(
                    {"url": url, "max_chars": 1200, "query": question}, ctx))
            except Exception:
                continue
            if len(out) >= budget:
                return out
    return out


def _tool_search_personal_memory(args: dict, ctx: AgentContext) -> dict:
    from app import ai_stack
    query = args["query"]
    limit = args.get("limit") or 6
    items = []
    for m in ai_stack.search_memories(query, limit=limit):
        items.append({
            "id": m.get("id"),
            "kind": m.get("kind"),
            "title": str(m.get("title") or "")[:180],
            "content": str(m.get("content") or "")[:700],
            "source": str(m.get("source") or "")[:180],
            "pinned": bool(m.get("pinned")),
        })
    return {"query": query, "items": items, "count": len(items)}


def _tool_search_chat_history(args: dict, ctx: AgentContext) -> dict:
    from app import messaging
    query = _fold(args["query"])
    person = _fold(args.get("person") or "")
    scope = str(args.get("scope") or "").strip().lower()
    limit = args.get("limit") or 8
    terms = [w for w in re.findall(r"\w{3,}", query)][:12]
    current_chat = str(ctx.conversation_id or "").strip()
    if not scope:
        scope = _default_chat_history_scope(ctx)
    since_days = args.get("since_days") or (
        3650 if scope == "current_conversation" else 45)
    cutoff = time.time() - (since_days * 86400)
    _bot_records_cache = {}

    def _bot_records_for(chat):
        if chat not in _bot_records_cache:
            try:
                _bot_records_cache[chat] = messaging._bot_outbox_records(chat)
            except Exception:
                _bot_records_cache[chat] = []
        return _bot_records_cache[chat]

    rows = []
    seen_bot_sends = set()   # (chat_guid, bot record id) already emitted once

    def _all_for_chat(path, guid):
        out = []
        try:
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    if not line.strip() or guid not in line:
                        continue
                    try:
                        d = json.loads(line)
                    except Exception:
                        continue
                    if str(d.get("chat_guid") or d.get("target") or "") == guid:
                        out.append(d)
        except Exception:
            return []
        return out

    # OUTBOX FIRST. It has to populate seen_bot_sends before the inbox pass
    # can dedupe against it -- reversed, every bot send's inbox echo would
    # be checked against an empty set and never dropped.
    if scope == "current_conversation" and current_chat:
        _out_items = _all_for_chat(messaging.OUTBOX_PATH, current_chat)
        _in_items = _all_for_chat(messaging.INBOX_PATH, current_chat)
    else:
        _out_items = messaging.outbox(2000)
        _in_items = messaging.inbox(2000)
    for source, items in (("outbox", _out_items), ("inbox", _in_items)):
        for item in items:
            text = str(item.get("text") or item.get("content") or "")
            hay = _fold(" ".join([
                text, str(item.get("sender") or ""), str(item.get("target") or ""),
                str(item.get("chat_guid") or ""),
            ]))
            if person and person not in hay:
                continue
            item_chat = str(item.get("chat_guid") or item.get("target") or "")
            if scope == "current_conversation" and current_chat and item_chat != current_chat:
                continue
            ts = float(item.get("ts") or 0)
            if not ts and item.get("timestamp"):
                try:
                    from datetime import datetime
                    ts = datetime.fromisoformat(str(item["timestamp"]).replace("Z", "+00:00")).timestamp()
                except Exception:
                    ts = 0
            if ts and ts < cutoff:
                continue
            score = float(sum(1 for t in terms if t in hay))
            if terms and score <= 0:
                continue
            _tl = text.lstrip()
            if _tl[:2] in ("☀", "☀️") or _tl.startswith(
                    ("☀️ Labrīt", "☀ Labrīt")) or (
                    "Labrīt" in text and "Vārdadienu" in text):
                score -= 5.0
            # a short line from the other person is far more likely to be the
            # specific fact being hunted (a game name, a place, a preference)
            if source == "inbox" and not item.get("is_from_me") and len(text) < 60:
                score += 0.75
            from_me = bool(item.get("is_from_me"))
            meta = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
            if source == "outbox":
                is_bot = bool(meta.get("auto_reply") or meta.get("digest"))
                speaker = "assistant" if is_bot else "owner"
                if is_bot and item.get("id"):
                    seen_bot_sends.add((item_chat, str(item["id"])))
            elif from_me:
                match = messaging._match_bot_send(_bot_records_for(item_chat), text, ts,
                                                  item.get("temp_guid"),
                                                  item.get("attachments"))
                if match and match.get("id"):
                    if (item_chat, str(match["id"])) in seen_bot_sends:
                        continue
                    seen_bot_sends.add((item_chat, str(match["id"])))
                    speaker = "assistant"
                else:
                    speaker = "owner"
            else:
                speaker = "contact"
            rows.append({
                "source": source,
                "score": score,
                "when": item.get("timestamp") or item.get("ts") or "",
                "chat_guid": item_chat,
                "sender": item.get("sender") or "",
                "speaker": speaker,
                "is_from_me": from_me or source == "outbox",
                "text": text[:500],
            })
    rows.sort(key=lambda r: (r["score"], str(r["when"])), reverse=True)
    return {"query": args["query"], "person": args.get("person") or "",
            "scope": scope or "all",
            "conversation_id": current_chat if scope == "current_conversation" else "",
            "matches": rows[:limit], "count": min(len(rows), limit)}


def _default_chat_history_scope(ctx: AgentContext) -> str:
    conv = str(getattr(ctx, "conversation_id", "") or "")
    if str(getattr(ctx, "interface", "") or "").startswith("bluebubbles") and conv:
        try:
            from app import messaging
            acfg = (messaging.load_config().get("auto_reply") or {})
            if conv not in set(acfg.get("assistant_chats") or []):
                return "current_conversation"
        except Exception:
            return "current_conversation"
    return "all"


def _tool_search_knowledge_base(args: dict, ctx: AgentContext) -> dict:
    from app import rag
    query = args["query"]
    corpora = args.get("corpora") or ["facts", "bible_lv", "bible_kjv"]
    limit = args.get("limit") or 5
    hits = []
    for h in rag.search(query, corpora=corpora, k=limit, min_score=0.30):
        hits.append({
            "corpus": h.get("corpus"),
            "score": h.get("score"),
            "title": str(h.get("title") or h.get("ref") or "")[:180],
            "text": str(h.get("text") or h.get("content") or "")[:800],
            "metadata": {k: v for k, v in h.items()
                         if k not in ("text", "content") and k not in ("embedding",)},
        })
    return {"query": query, "corpora": corpora, "hits": hits, "count": len(hits)}


def _tool_search_latvian_grammar(args: dict, ctx: AgentContext) -> dict:
    from app import lv_engine, lv_grammar
    text = args.get("text") or args.get("query") or ""
    out = {"coverage": lv_grammar.coverage_summary()}
    if text:
        out["analysis"] = lv_engine.analyze_reply(text, latest_message=args.get("context") or "")
    return out


def _tool_search_files(args: dict, ctx: AgentContext) -> dict:
    query = _fold(args["query"])
    root_key = args.get("root") or "all"
    limit = args.get("limit") or 20
    roots = []
    if root_key in ("project", "all"):
        roots.append(ROOT)
    if root_key in ("ai", "all") and AI_ROOT.exists():
        roots.append(AI_ROOT)
    if root_key in ("downloads", "all") and DOWNLOAD_ROOT.exists():
        roots.append(DOWNLOAD_ROOT)
    terms = [w for w in re.findall(r"[a-z0-9_.-]{2,}", query)][:8]
    matches = []
    started = time.time()
    for root in roots:
        for path in root.rglob("*"):
            if time.time() - started > 8:
                return {"query": args["query"], "matches": matches,
                        "truncated": True, "note": "file search time budget exhausted"}
            if len(matches) >= limit:
                return {"query": args["query"], "matches": matches, "truncated": True}
            if _path_denied(path):
                continue
            name = _fold(path.name)
            if terms and not all(t in name for t in terms):
                if not any(t in name for t in terms):
                    continue
            try:
                st = path.stat()
            except Exception:
                continue
            matches.append({
                "path": str(path),
                "name": path.name,
                "is_dir": path.is_dir(),
                "size_bytes": st.st_size if path.is_file() else None,
                "modified": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime)),
            })
    return {"query": args["query"], "matches": matches, "truncated": False}


def _tool_inspect_file(args: dict, ctx: AgentContext) -> dict:
    path = _resolve_readable_path(args["path"])
    if not path.exists():
        return {"path": str(path), "exists": False}
    st = path.stat()
    out = {
        "path": str(path),
        "exists": True,
        "is_dir": path.is_dir(),
        "size_bytes": st.st_size,
        "modified": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime)),
    }
    if args.get("include_preview") and path.is_file() and st.st_size <= 512000:
        try:
            out["preview"] = path.read_text(encoding="utf-8", errors="replace")[:3000]
        except Exception:
            out["preview"] = ""
    return out


RIG_LOOKUP_SKILLS = ("gpu", "cpu", "memory", "disks", "temps", "power", "models",
                     "services", "status", "uptime", "calendar", "kino", "watch",
                     "voicenote")


def _tool_rig_lookup(args: dict, ctx: AgentContext) -> dict:
    """Live data from the rig and the home services, by name."""
    from app import rig_agent
    from app import messaging as _M
    skill = str(args.get("skill") or "").strip().lower()
    if skill not in RIG_LOOKUP_SKILLS:
        raise ToolRefused("skill must be one of: %s" % ", ".join(RIG_LOOKUP_SKILLS))
    acfg = _M.load_config().get("auto_reply") or {}
    out = rig_agent.answer_skill(skill, str(ctx.turn_text or ""), ctx.sender or "",
                                 acfg, chat_guid=ctx.conversation_id or None)
    if not out:
        return {"ok": False, "skill": skill, "error_type": "not_available",
                "message": ("no answer from this lookup here: not permitted for "
                            "this person, or nothing to report")}
    return {"ok": True, "skill": skill, "text": str(out.get("text") or ""),
            "lang": str(out.get("lang") or "")}


def _tool_system_status(args: dict, ctx: AgentContext) -> dict:
    import psutil
    out = {
        "cpu_percent": psutil.cpu_percent(interval=0.1),
        "ram_percent": psutil.virtual_memory().percent,
        "disk_percent": psutil.disk_usage(str(ROOT.anchor or "C:\\")).percent,
        "ollama_models": [],
    }
    try:
        req = urllib.request.Request("http://127.0.0.1:11434/api/ps")
        with urllib.request.urlopen(req, timeout=3) as r:
            models = json.loads(r.read().decode("utf-8", "replace")).get("models") or []
        out["ollama_models"] = [{"name": m.get("name"), "vram": m.get("size_vram"),
                                 "expires_at": m.get("expires_at")} for m in models]
    except Exception as exc:
        out["ollama_error"] = str(exc)[:160]
    try:
        res = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,temperature.gpu,memory.used,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=3, encoding="utf-8", errors="replace",
            creationflags=0x08000000,
        )
        if res.returncode == 0:
            out["gpu"] = (res.stdout or "").strip()
    except Exception:
        pass
    return out


def _tool_disk_report(args: dict, ctx: AgentContext) -> dict:
    from app import disk_report
    return disk_report.build_report()


def _action_id(tool: str, args: dict) -> str:
    safe = _clip(args or {}, MAX_TOOL_ARGS_CHARS)
    payload = json.dumps({"tool": tool, "args": safe}, sort_keys=True,
                         ensure_ascii=False, default=str)
    return "%s_%s" % (
        re.sub(r"[^A-Za-z0-9_]+", "_", str(tool or "action")).strip("_") or "action",
        hashlib.sha256(payload.encode("utf-8", "replace")).hexdigest()[:16])


def _append_action_ledger(row: dict) -> None:
    try:
        ACTION_LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
        item = dict(row or {})
        item.setdefault("ts", time.time())
        with open(ACTION_LEDGER_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(item, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass


def _audit_url(url: str) -> dict:
    try:
        parts = urllib.parse.urlsplit(str(url or ""))
        return {
            "scheme": parts.scheme,
            "host": parts.hostname or "",
            "path": (parts.path or "")[:240],
            "has_query": bool(parts.query),
        }
    except Exception:
        return {"host": ""}


_DOWNLOAD_ACTION_RE = re.compile(
    r"\b(download|save|store|fetch)\b.{0,160}\b(https?://|file|image|video|"
    r"audio|page|link|this|it)\b|"
    r"\b(lejupielade|lejupielādē|saglaba|saglabā)\b",
    re.I)
_DOWNLOAD_UNCERTAIN_RE = re.compile(
    r"\b(maybe|perhaps|thinking about|what should i|should i|could i|"
    r"draft|not sure|probably|don't download|dont download|preview only|"
    r"varb[ūu]t|laikam|dom[āa]ju par|vajadz[ēe]tu|b[ūu]tu v[ēe]rts|"
    r"nevajag|nevelc|nenoka[čc][āa]|neka[čc][āa])\b",
    re.I)


def _download_authorized(turn_text: str) -> tuple[bool, str]:
    text = _current_action_text(turn_text)
    if not text:
        return False, "no current user turn is available for download authorization"
    if _DOWNLOAD_UNCERTAIN_RE.search(text):
        return False, "current turn discusses a download but does not clearly authorize it"
    if _DOWNLOAD_ACTION_RE.search(text):
        return True, ""
    return False, "current turn does not explicitly authorize saving a downloaded file"


def _tool_download_file(args: dict, ctx: AgentContext) -> dict:
    from app import messaging
    url = args["url"]
    ok, reason = _download_authorized(ctx.turn_text)
    if not ok:
        raise ToolRefused(reason)
    if not messaging._link_is_safe(url):
        raise ToolRefused("refusing to download non-public URL")
    filename = _safe_filename(args.get("filename") or url)
    max_mb = args.get("max_mb") or 512
    DOWNLOAD_ROOT.mkdir(parents=True, exist_ok=True)
    dest = (DOWNLOAD_ROOT / filename).resolve()
    try:
        dest.relative_to(DOWNLOAD_ROOT.resolve())
    except Exception:
        raise ToolRefused("download destination escaped the download directory")
    h = hashlib.sha256()
    size = 0
    limit = max_mb * 1024 * 1024
    action_id = _action_id("download_file", {
        "url": _normalize_url(url),
        "filename": filename,
        "max_mb": max_mb,
    })
    audit_base = {
        "audit_id": ctx.audit_id,
        "interface": ctx.interface,
        "conversation_id": ctx.conversation_id,
        "action_id": action_id,
        "tool": "download_file",
        "risk": LOCAL_REVERSIBLE_WRITE,
        "url": _audit_url(url),
        "path": str(dest),
    }
    if dest.exists() and dest.is_file():
        try:
            with open(dest, "rb") as fh:
                while True:
                    chunk = fh.read(1024 * 256)
                    if not chunk:
                        break
                    size += len(chunk)
                    h.update(chunk)
        except Exception:
            size = 0
            h = hashlib.sha256()
        sha = h.hexdigest()
        _append_action_ledger(dict(audit_base, status="already_exists",
                                   size_bytes=size, sha256=sha))
        return {
            "path": str(dest),
            "size_bytes": size,
            "sha256": sha,
            "already_exists": True,
            "idempotent_replay": True,
            "action_id": action_id,
        }
    try:
        with messaging._safe_urlopen(
                url, timeout=20,
                headers={"User-Agent": "Mozilla/5.0 AgentDownload/1.0"}) as resp:
            with open(dest, "wb") as fh:
                while True:
                    chunk = resp.read(1024 * 256)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > limit:
                        raise ToolRefused("download exceeded %d MB" % max_mb)
                    h.update(chunk)
                    fh.write(chunk)
    except Exception:
        try:
            dest.unlink()
        except Exception:
            pass
        _append_action_ledger(dict(audit_base, status="failed",
                                   size_bytes=size))
        raise
    sha = h.hexdigest()
    _append_action_ledger(dict(audit_base, status="executed",
                               size_bytes=size, sha256=sha))
    return {
        "path": str(dest),
        "size_bytes": size,
        "sha256": sha,
        "already_exists": False,
        "idempotent_replay": False,
        "action_id": action_id,
    }


def _ss_query_from_args(args: dict) -> dict:
    """The model's words -> ss_search kwargs. No sentence parsing here."""
    args = args or {}
    out = {
        "kind": str(args.get("kind") or "flats").strip().lower(),
        "area": " ".join(str(args.get("area") or "").split()),
        "deal": str(args.get("deal") or "").strip().lower(),
        "make": " ".join(str(args.get("make") or "").split()),
        # a list is kept as a list: "cooper un cooper s" is two ss.com pages
        "model": ([" ".join(str(m).split()) for m in args["model"] if str(m).strip()][:4]
                  if isinstance(args.get("model"), list)
                  else " ".join(str(args.get("model") or "").split())),
    }
    for field in ("max_price", "min_price", "min_year", "max_year",
                  "min_area", "max_area", "min_floor", "max_floor"):
        try:
            out[field] = int(args.get(field)) if args.get(field) else None
        except (TypeError, ValueError):
            out[field] = None
    picks = args.get("options")
    if isinstance(picks, str):
        picks = [picks]
    out["options"] = [str(p).strip() for p in (picks or []) if str(p).strip()][:5]
    out["text"] = str(args.get("text") or "").strip()[:80]
    out["path"] = str(args.get("path") or "").strip()[:120]
    return {k: v for k, v in out.items() if v not in (None, "", [])}


# 6+ characters of mixed letters and digits, or a plate-like group: what a VIN,
# a registration or a serial looks like once the words are stripped away.
_ID_TOKEN_RE = re.compile(r"\b(?=[A-Z0-9-]*[0-9])(?=[A-Z0-9-]*[A-Z])[A-Z0-9][A-Z0-9-]{4,20}\b")


def _identifiers(text):
    """Identifier-looking tokens, spaced or not.

    A VIN is often written in groups — "WMW RC3 104 0TH 281 12" — so the run
    with spaces squeezed out is checked as well, or the one identifier that
    matters most goes unverified.
    """
    body = str(text or "").upper()
    found = {t.replace("-", "") for t in _ID_TOKEN_RE.findall(body)}
    for run in re.findall(r"(?:\b[A-Z0-9]{2,6}\b[ -]){2,}\b[A-Z0-9]{2,6}\b", body):
        squeezed = re.sub(r"[ -]", "", run)
        if len(squeezed) >= 8 and re.search(r"[A-Z]", squeezed) and re.search(r"[0-9]", squeezed):
            found.add(squeezed)
    return found



def _ask_the_gallery(photos, text, question, model, timeout=300, batch=9):
    urls = list(photos or [])[:18]
    images = []
    for url in urls:
        try:
            req = urllib.request.Request(str(url), headers={
                "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                               "AppleWebKit/537.36 (KHTML, like Gecko) "
                               "Chrome/128.0 Safari/537.36")})
            with urllib.request.urlopen(req, timeout=25) as resp:
                blob = resp.read(4_000_000)
            if blob:
                images.append(base64.b64encode(blob).decode("ascii"))
        except Exception:
            continue
    if not images:
        return {"photos": 0, "seen": ""}
    prompt = ("Šīs ir viena sludinājuma bildes. %s\n\n"
              "Nenorādi bilžu numurus — pasaki, ko redzi un kurā telpā. "
              "Ja jālasa teksts, numurs vai kods, nolasi to TIKAI tad, ja tas "
              "bildē ir skaidri salasāms; ja tas ir izplūdis vai par mazu, "
              "saki NEREDZ. Neuzmini burtus un ciparus. "
              "Ja šajās bildēs to nevar redzēt, saki tikai NEREDZ — nemini un "
              "nepieņem. Sludinājuma teksts: %s"
              % (question, str(text or "")[:600]))
    answers, last = [], ""
    for start in range(0, len(images), batch):
        chunk = images[start:start + batch]
        payload = {
            "model": model, "stream": False, "think": False,
            "keep_alive": KEEP_ALIVE,
            "options": _runner_options({"temperature": 0, "num_predict": 200}),
            "messages": [{"role": "user", "content": prompt, "images": chunk}],
        }
        req = urllib.request.Request(
            "http://127.0.0.1:11434/api/chat",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = json.loads(resp.read().decode("utf-8", "replace"))
        said = str(((raw.get("message") or {}).get("content") or "")).strip()
        if said:
            answers.append("bildes %d-%d: %s" % (start + 1, start + len(chunk), said))
            last = said
        # a batch that actually saw the thing ends the search; NEREDZ means
        # keep looking in the rest of the gallery
        if said and "NEREDZ" not in said.upper():
            break
    seen = (" | ".join(answers))[:900] or last[:700]
    # A CODE MUST READ THE SAME TWICE. Photos reversed so the model cannot
    # simply repeat itself; a real document survives, a blurred plate does not.
    claimed = _identifiers(seen)
    if claimed and images:
        check = {
            "model": model, "stream": False, "think": False,
            "keep_alive": KEEP_ALIVE,
            "options": _runner_options({"temperature": 0, "num_predict": 120}),
            "messages": [{"role": "user", "content": prompt,
                          "images": list(reversed(images))[:batch]}],
        }
        try:
            req = urllib.request.Request(
                "http://127.0.0.1:11434/api/chat",
                data=json.dumps(check).encode("utf-8"),
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                again = json.loads(resp.read().decode("utf-8", "replace"))
            second = _identifiers(str(((again.get("message") or {}).get("content") or "")))
        except Exception:
            second = set()
        agreed = claimed & second
        if not agreed:
            seen += (" | PĀRBAUDE: otrreiz nolasot, kods sanāca citāds — "
                     "neuzticams, nevar droši salasīt")
        else:
            seen += " | PĀRBAUDE: nolasīts divreiz vienādi (%s)" % ", ".join(sorted(agreed))
    return {"photos": len(images), "batches": len(answers), "seen": seen}


def _answer_from_photos(rows, ctx, question, limit=6, per_ad=18):
    """Ask one question of each advert: its whole gallery and its own text."""
    from app import ss_search

    model = str(getattr(ctx, "vision_model", "") or "qwen3.8:latest").strip()
    out = []
    for row in list(rows or [])[:limit]:
        item = dict(row)
        try:
            ad = ss_search.listing(str(row.get("url") or ""))
        except Exception as exc:
            item["photo_check"] = {"error": str(exc)[:120]}
            out.append(item)
            continue
        item["advert_says"] = str(ad.get("description") or "")[:600]
        try:
            item["photo_check"] = _ask_the_gallery(
                list(ad.get("photos") or [])[:per_ad], ad.get("description"),
                question, model)
        except Exception as exc:
            item["photo_check"] = {"error": str(exc)[:120]}
        out.append(item)
    out.extend(dict(r) for r in list(rows or [])[limit:])
    return out


# The last listing block built in this thread, for the pipeline to fall back on
# when the model answers without the links it was handed.
# A dry run (the test endpoint / benches) may LOOK at anything and CHANGE
# nothing. Thread-local because one preview must not mute a real turn running
# beside it.
_DRY_RUN = threading.local()


def set_dry_run(on):
    try:
        _DRY_RUN.on = bool(on)
    except Exception:
        pass


def dry_run_active():
    return bool(getattr(_DRY_RUN, "on", False))


_LAST_READY = threading.local()


def last_listing_block():
    return str(getattr(_LAST_READY, "block", "") or "")


def _remember_block(block):
    try:
        _LAST_READY.block = str(block or "")
    except Exception:
        pass


def clear_listing_block():
    _remember_block("")


_AD_ANSWER_SCHEMA = {
    "type": "object",
    "properties": {"atbildes": {"type": "array", "items": {
        "type": "object",
        "properties": {"nr": {"type": "integer"},
                       "atbilde": {"type": "string"}},
        "required": ["nr", "atbilde"]}}},
    "required": ["atbildes"],
}


def _answer_from_adverts(rows, question, model, limit=8, timeout=180):
    """Ask one question of the adverts' own text. One call for all of them.

    For everything ss.com cannot filter on — 4x4, a tow bar, a sunroof, "no
    Šveices" — the answer is in the seller's description or the attribute
    table, and reading eight descriptions is one short model call.
    """
    from app import ss_search

    opened = []
    for row in list(rows or [])[:limit]:
        try:
            ad = ss_search.listing(str(row.get("url") or ""))
        except Exception:
            continue
        attrs = "; ".join("%s: %s" % (k, v) for k, v in (ad.get("attributes") or {}).items())
        gear = ", ".join(ad.get("equipment") or [])
        opened.append((row, "%s\nAprīkojums: %s\n%s"
                       % (attrs[:400], gear[:600] or "(nav norādīts)",
                          str(ad.get("description") or "")[:600])))
    if not opened:
        return rows
    listing_text = "\n\n".join(
        "--- %d ---\n%s" % (i + 1, text) for i, (_row, text) in enumerate(opened))
    prompt = ("Zemāk ir sludinājumu teksti un to raksturlielumi.\n\n%s\n\n"
              "Jautājums par katru: %s\n"
              "Atbildi par KATRU numuru. Ja sludinājumā par to nav ne vārda, "
              "raksti \"nav minēts\" — nemini un nepieņem.\n"
              "Atbildi TIKAI ar JSON: {\"atbildes\": [{\"nr\": 1, "
              "\"atbilde\": \"...\"}]}" % (listing_text[:8000], question))
    payload = {"model": model, "stream": False, "think": False,
               "keep_alive": KEEP_ALIVE, "format": _AD_ANSWER_SCHEMA,
               "options": _runner_options({"temperature": 0, "num_predict": 400}),
               "messages": [{"role": "user", "content": prompt}]}
    try:
        req = urllib.request.Request(
            "http://127.0.0.1:11434/api/chat",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = json.loads(resp.read().decode("utf-8", "replace"))
        said = json.loads(re.search(r"\{.*\}", str(
            ((raw.get("message") or {}).get("content") or "")), re.S).group(0))
        answers = {int(a.get("nr") or 0): str(a.get("atbilde") or "")
                   for a in (said.get("atbildes") or [])}
    except Exception as exc:
        answers = {}
    out = []
    for i, (row, _text) in enumerate(opened, 1):
        item = dict(row)
        if answers.get(i):
            item["advert_answer"] = answers[i][:300]
        out.append(item)
    out.extend(dict(r) for r in list(rows or [])[len(opened):])
    return out


def _ready_to_send(rows, kind=""):
    """The result rows already written out for a message, one per line.

    Built from the row data itself, so a price, a mileage or a link can never
    be a transcription error. Empty fields are simply left out rather than
    described.
    """
    lines = []
    for i, row in enumerate(rows or [], 1):
        bits = []
        for key in ("model", "year", "engine", "mileage", "rooms", "area_m2",
                    "space", "floor"):
            val = str(row.get(key) or "").strip()
            if val:
                bits.append(val + (" ist." if key == "rooms" else ""))
        price = str(row.get("price_text") or row.get("price") or "").strip()
        if price:
            bits.append(price)
        url = str(row.get("url") or "").strip()
        title = " ".join(str(row.get("title") or "").split())[:60]
        if not bits and title:
            bits.append(title)
        line = "%d. %s" % (i, ", ".join(b for b in bits if b))
        if url:
            line += "\n%s" % url
        lines.append(line)
    return "\n".join(lines)


def _available_filters(path):
    """The filter groups ss.com publishes on that page, as words."""
    from app import ss_search
    try:
        groups = {}
        for field, display, _name, _value in ss_search.filter_options(path):
            groups.setdefault(field, []).append(display)
        nums = ss_search.numeric_fields(path)
        if nums:
            groups["_ranges"] = sorted(nums)
        return {k: v[:20] for k, v in groups.items()}
    except Exception:
        return {}


def _tool_classifieds_search(args: dict, ctx: AgentContext) -> dict:
    from app import ss_search

    query = _ss_query_from_args(args)
    if query.get("kind") == "cars":
        meant = ss_search.make_he_meant(getattr(ctx, "turn_text", "") or "")
        if meant and str(query.get("make") or "").strip().lower() != meant.lower():
            query["make"] = meant
            query.pop("model", None)
    try:
        limit = max(1, min(int(args.get("limit") or 5), 10))
    except (TypeError, ValueError):
        limit = 5
    words, where = query.pop("text", ""), query.pop("path", "")
    if words:
        # a Mac mini, a guitar, a fridge: things that live under neither a make
        # nor a district, so ss.com's own words-search is the only way in
        try:
            found = ss_search.site_search(words, path=where, limit=limit)
        except ss_search.SSError as exc:
            return {"ok": False, "error_type": "classifieds_unavailable",
                    "message": str(exc)[:200]}
        return {"searched": words, "in": where or "the whole site",
                "url": found["url"], "total": found["count"],
                "results": found["results"],
                "sections_found": found.get("sections") or [],
                "hint": ("A whole-site search puts repair shops and wanted ads "
                         "first. If these are the wrong sort, call this tool "
                         "again with `path` set to one of `sections_found` — the "
                         "one whose category names the thing itself rather than "
                         "a service — and search there.")}
    need = "" if (query.get("options") or query.get("min_price")
                  or query.get("max_price") or query.get("min_year")
                  or query.get("max_year")) else ss_search.missing_choice(
        query.get("kind"), area=query.get("area"), make=query.get("make"))
    if need:
        # There is no all-cars page and no all-Riga page. Rather than let the
        # index parse to zero and read as an empty market, hand back the site's
        # own list so the model can pick the candidates that actually fit the
        # request and search each one.
        try:
            names = ss_search.choice_names(query.get("kind"))
        except ss_search.SSError as exc:
            return {"ok": False, "error_type": "classifieds_unavailable",
                    "message": str(exc)[:200]}
        return {"needs": need, "choices": names[:60], "query": query,
                "message": (
                    "ss.com files cars under a make, so there is no all-cars page "
                    "and no way to search by price alone. Decide which makes and "
                    "models fit what was asked for, then call this tool once per "
                    "candidate with make (and model) filled in."
                    if need == "make" else
                    "ss.com files property by Riga district. Name the district "
                    "that was asked for, or pick the likely ones and search each.")}
    # SEVERAL MODELS, ONE CALL. Each is a separate ss.com page; searching them
    # here is the only way "cooper un cooper s" cannot quietly become "cooper".
    _models = query.get("model")
    if isinstance(_models, list) and len(_models) > 1:
        merged, seen_urls, per_model, errors = [], set(), {}, []
        for name in _models:
            one = dict(query, model=name)
            try:
                got = ss_search.search(limit=limit, **one)
            except ss_search.SSChoice as exc:
                return {"needs": exc.needs, "choices": exc.choices[:40],
                        "query": one, "message": "%s. Pick the ones that fit "
                        "and search each." % str(exc)[:150]}
            except ss_search.SSError as exc:
                errors.append("%s: %s" % (name, str(exc)[:80]))
                continue
            rows = got.get("results") or []
            per_model[name] = len(rows)
            for row in rows:
                url = str(row.get("url") or "")
                if url and url in seen_urls:
                    continue
                seen_urls.add(url)
                merged.append(dict(row, searched_as=name))
        if not merged and errors:
            return {"ok": False, "error_type": "classifieds_unavailable",
                    "message": "; ".join(errors)[:200]}
        ss_search.remember_urls(getattr(ctx, "conversation_id", ""),
                                [r.get("url") for r in merged], rows=merged)
        _block = _ready_to_send(merged, query.get("kind"))
        _remember_block(_block)
        return {"ready_to_send": _block,
                "how_to_use": (
                    "PASTE `ready_to_send` AS IT IS. Every model asked for was "
                    "searched; `found_per_model` says how many each has, and a "
                    "zero there is a real answer — say so rather than leaving "
                    "the model out."),
                "query": query, "searched_models": _models,
                "found_per_model": per_model,
                "results": merged, "total": len(merged),
                "errors": errors or None}
    if isinstance(_models, list):
        query["model"] = _models[0] if _models else ""
    try:
        found = ss_search.search(limit=limit, **query)
    except ss_search.SSChoice as exc:
        # not a failure: ss.com has several pages for that name and the model
        # is the one that knows which of them the request meant
        return {"needs": exc.needs, "choices": exc.choices[:40], "query": query,
                "message": "%s. Pick the ones that fit and search each."
                           % str(exc)[:150]}
    except ss_search.SSError as exc:
        return {"ok": False, "error_type": "classifieds_unavailable",
                "message": str(exc)[:200]}
    _photo_q = str(args.get("photo_question") or "").strip()
    if _photo_q and isinstance(found, dict) and found.get("results"):
        found = dict(found)
        found["results"] = _answer_from_photos(
            found["results"], ctx, _photo_q,
            limit=max(1, min(int(args.get("inspect_limit") or 6), 8)))
        found["photo_question"] = _photo_q
        found["hint"] = ("`seen` is what the vision model reported from that "
                         "advert's own photos, and `advert_says` is the "
                         "seller's own text. Report which listings answer the "
                         "question and which do not, with the link for each; "
                         "say plainly when the photos do not show it rather "
                         "than guessing.")
    shown = found["results"]
    prices = [r["price"] for r in shown if r.get("price")]
    partial = ""
    if (query.get("max_price") or query.get("min_price")) and not found.get("filtered"):
        partial = ("ss.com would not apply the price filter, so these came from "
                   "reading the %d newest pages -- there are very likely more "
                   "further back. Do NOT say this is all there is; say the "
                   "filter did not hold." % int(found.get("scanned_pages") or 0))
    ss_search.remember_urls(getattr(ctx, "conversation_id", ""),
                            [r.get("url") for r in shown], rows=shown)
    _need = args.get("equipment_required")
    if isinstance(_need, str):
        _need = [_need]
    _need = [str(n).strip() for n in (_need or []) if str(n).strip()][:4]
    if _need:
        # GO INTO THE ADS AND CHECK. ss.com cannot filter on equipment, but
        # every advert lists it, so the rows are opened and kept or dropped on
        # what the seller actually ticked — no prose, no guessing.
        kept, dropped = [], []
        for row in shown[:max(1, min(int(args.get("inspect_limit") or 8), 10))]:
            try:
                ad = ss_search.listing(str(row.get("url") or ""))
            except Exception:
                continue
            have = [g.lower() for g in (ad.get("equipment") or [])]
            missing = [n for n in _need
                       if not any(n.lower() in g for g in have)]
            if missing:
                dropped.append({"url": row.get("url"), "missing": missing,
                                "equipment": ad.get("equipment") or []})
            else:
                kept.append(dict(row, equipment=ad.get("equipment") or []))
        shown = kept
        _equipment_report = {"required": _need, "kept": len(kept),
                             "dropped": dropped[:6]}
    else:
        _equipment_report = None
    _ad_q = str(args.get("ad_question") or "").strip()
    if _ad_q:
        # what ss.com cannot filter on, the adverts themselves can answer
        shown = _answer_from_adverts(
            shown, _ad_q, str(getattr(ctx, "vision_model", "") or "qwen3.8:latest"),
            limit=max(1, min(int(args.get("inspect_limit") or 8), 8)))
    _block = _ready_to_send(shown, query.get("kind"))
    _remember_block(_block)
    return {"ready_to_send": _block,
            "equipment_check": _equipment_report,
            "available_filters": _available_filters(
                str(found.get("url") or "").replace(ss_search.BASE, "")),
            "how_to_use": (
                "PASTE `ready_to_send` INTO THE MESSAGE AS IT IS. Do not retype "
                "the rows: retyping is where a price turns into a sentence and "
                "two cars end up sharing one link. Write your own words BEFORE "
                "or AFTER that block — which one you would take and why, what "
                "to watch out for — never inside it."),
            "query": query, "url": found["url"], "total": found["count"],
            "filter_applied": bool(found.get("filter_applied")
                                   or found.get("filtered")),
            "filter_means": found.get("filter_means") or "",
            "ordering": "newest first",
            "showing": "%d of %d, the newest — NOT the cheapest"
                       % (len(shown), found["count"]),
            "cheapest_shown": min(prices) if prices else None,
            "note": partial or ("To answer about price, search again with "
                                "max_price set rather than picking the lowest "
                                "of these." if found["count"] > len(shown) else ""),
            "results": shown}


def _tool_classifieds_watch(args: dict, ctx: AgentContext) -> dict:
    from app import ss_search

    from app import rig_agent

    action = str(args.get("action") or "add").strip().lower()
    chat = str(ctx.conversation_id or "")
    owner = rig_agent.owner_allowed(str(ctx.sender or ""))

    def _mine(wid):
        for w in ss_search.watch_list():
            if w["id"] == str(wid):
                return w if (owner or w.get("chat_guid") == chat) else None
        return None

    if action == "list":
        return {"watches": [
            {"id": w["id"], "label": w.get("label"), "url": w.get("url"),
             "checked": w.get("checked"), "reports_into": w.get("chat_guid"),
             "reports_here": w.get("chat_guid") == chat}
            for w in ss_search.watch_list("" if owner else chat)]}
    if action == "move":
        wid = str(args.get("watch_id") or "").strip()
        if not wid:
            return {"ok": False, "error_type": "invalid_arguments",
                    "message": "which watch? call action=list first"}
        if not chat:
            return {"ok": False, "error_type": "no_conversation",
                    "message": "a watch needs a live chat to report back into"}
        if _mine(wid) is None:
            return {"ok": False, "error_type": "not_authorized",
                    "message": "that watch belongs to another chat"}
        moved = ss_search.watch_move(wid, chat, str(ctx.sender or ""))
        if not moved:
            return {"ok": False, "error_type": "not_found", "watch_id": wid}
        return {"moved": True, "watch_id": wid, "label": moved.get("label"),
                "reports_into": chat, "note": "new listings arrive in this chat"}
    if action == "remove":
        wid = str(args.get("watch_id") or "").strip()
        if not wid:
            return {"ok": False, "error_type": "invalid_arguments",
                    "message": "which watch? call action=list first"}
        if _mine(wid) is None:
            return {"ok": False, "error_type": "not_authorized",
                    "message": "that watch belongs to another chat"}
        return {"removed": ss_search.watch_remove(wid), "watch_id": wid}
    if not chat:
        return {"ok": False, "error_type": "no_conversation",
                "message": "a watch needs a live chat to report back into"}
    query = _ss_query_from_args(args)
    if not query:
        return {"ok": False, "error_type": "invalid_arguments",
                "message": "say what to watch: a kind and a place or make, or text"}
    try:
        made = ss_search.watch_add(chat, str(ctx.sender or ""),
                                   str(args.get("label") or "").strip(), **query)
    except ss_search.SSChoice as exc:
        return {"needs": exc.needs, "choices": exc.choices[:40], "query": query,
                "message": "%s. Say which one to watch." % str(exc)[:150]}
    except ss_search.SSError as exc:
        return {"ok": False, "error_type": "classifieds_unavailable",
                "message": str(exc)[:200]}
    return dict(made, watching=query, note="only new listings are reported")


def _tool_classifieds_browse(args: dict, ctx: AgentContext) -> dict:
    """What ss.com publishes under a path, so the model can find where a thing
    lives instead of guessing a URL. /lv/ is the top of the tree."""
    from app import ss_search

    where = str(args.get("path") or "/lv/").strip() or "/lv/"
    try:
        found = ss_search.sections(where)
    except ss_search.SSError as exc:
        return {"ok": False, "error_type": "classifieds_unavailable",
                "message": str(exc)[:200]}
    return {"path": where,
            "categories": [{"name": name, "path": path} for name, path in found[:60]]}


_PHOTO_PROMPT = (
    "This is a photo from a used-car advert. Describe only what you can SEE of "
    "the car's condition: rust or bubbling (arches, sills, door bottoms, "
    "tailgate), dents, mismatched paint or panel gaps, cracked bumpers, kerbed "
    "wheels, tyre wear, interior wear on seats and steering wheel, warning "
    "lights or the odometer if the dashboard is visible, and anything that "
    "looks recently repaired. If the photo is too small or dark to judge, say "
    "so. Do not guess at mechanical condition you cannot see."
)


def _inspect_listing_photos(urls, ctx, question=""):
    """Run the vision model over an advert's own photos."""
    model = str(getattr(ctx, "vision_model", "") or "qwen3.8:latest").strip()
    asked = str(question or "").strip() or _PHOTO_PROMPT
    seen = []
    for url in list(urls or [])[:3]:
        try:
            req = urllib.request.Request(str(url), headers={
                "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                               "AppleWebKit/537.36 (KHTML, like Gecko) "
                               "Chrome/128.0 Safari/537.36")})
            with urllib.request.urlopen(req, timeout=25) as resp:
                blob = resp.read(4_000_000)
            if not blob:
                continue
            payload = {
                "model": model,
                "messages": [{"role": "user", "content": asked,
                              "images": [base64.b64encode(blob).decode("ascii")]}],
                "stream": False, "think": False, "keep_alive": KEEP_ALIVE,
                "options": _runner_options({"temperature": 0, "num_predict": 200}),
            }
            vreq = urllib.request.Request(
                "http://127.0.0.1:11434/api/chat",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(vreq, timeout=180) as vresp:
                raw = json.loads(vresp.read().decode("utf-8", "replace"))
            text = str(((raw.get("message") or {}).get("content") or "")).strip()
            if text:
                seen.append({"photo": str(url), "seen": text[:700]})
        except Exception as exc:
            seen.append({"photo": str(url), "error": str(exc)[:100]})
    return seen


def _tool_classifieds_listing(args: dict, ctx: AgentContext) -> dict:
    """One advert in full, plus what tirgusdati remembers about its price."""
    from app import ss_search

    url = str(args.get("url") or "").strip()
    if "ss.com" not in url:
        return {"ok": False, "error_type": "invalid_arguments",
                "message": "give the ss.com advert link from a search result"}
    try:
        ad = ss_search.listing(url)
    except ss_search.SSError as exc:
        return {"ok": False, "error_type": "classifieds_unavailable",
                "message": str(exc)[:200]}
    out = dict(ad)
    ss_search.remember_urls(getattr(ctx, "conversation_id", ""), [ad.get("url")],
                            rows=[ad])
    if args.get("price_history"):
        try:
            out["price_history"] = ss_search.price_history(ad["url"])
        except ss_search.SSError as exc:
            out["price_history"] = {"unavailable": str(exc)[:120]}
    if args.get("inspect_photos") and ad.get("photos"):
        out["photo_report"] = _inspect_listing_photos(
            ad["photos"], ctx, question=str(args.get("photo_question") or ""))
    return out


def _tool_media_library_search(args: dict, ctx: AgentContext) -> dict:
    """What is ALREADY on the media server. The model could not check before."""
    from app import media_agent

    query = " ".join(str(args.get("title") or "").split()).lower()
    try:
        owned = media_agent.owned_movies()
        try:
            series = media_agent.owned_series()
        except Exception:
            series = None
    except Exception as exc:
        return {"ok": False, "error_type": "library_unavailable",
                "message": "the media library could not be read: %s" % str(exc)[:120]}
    if owned is None:
        # NOT "you own nothing" — say so, or the model will happily conclude the
        # server is empty.
        return {"ok": False, "error_type": "library_unavailable",
                "message": ("the media library could not be read, so what is "
                            "already on the server is UNKNOWN — do not assume "
                            "it is empty")}
    rows = []
    for kind, items in (("movie", owned), ("series", series or [])):
        for name, year in items:
            if query and query not in str(name or "").lower():
                continue
            rows.append({"title": name, "year": year, "kind": kind})
    rows.sort(key=lambda r: str(r.get("title") or ""))
    out = {"query": args.get("title") or "",
           "owned_count": len(owned) + len(series or []),
           "owned_movies": len(owned), "owned_series": len(series or []),
           "matches": rows[:60], "match_count": len(rows)}
    if series is None:
        # Say it, or "no match" reads as "not on the server" for every show.
        out["series_unknown"] = ("the SERIES half of the library could not be "
                                 "read; a show missing from these matches may "
                                 "still be on the server")
    return out


def _tool_antons_run_tests(args: dict, ctx: AgentContext) -> dict:
    """Owner-only, READ-ONLY: run one existing test suite and report pass/fail."""
    from app import self_heal, rig_agent
    from app import messaging as _M
    acfg = _M.load_config().get("auto_reply") or {}
    if not (rig_agent and rig_agent.owner_allowed(ctx.sender or "", acfg)):
        raise ToolRefused("running tests is owner-only")
    suite = str(args.get("suite") or "").strip()
    if not suite:
        raise ToolRefused("suite is required, e.g. tests/rig_lookup_cases.py")
    try:
        res = self_heal.run_suite(suite)
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:300]}
    return _limit_result({"ok": True, "result": res})


def _tool_antons_investigate(args: dict, ctx: AgentContext) -> dict:
    """Owner-only. Same diagnosis loop the dashboard's propose-fix already
    runs (app/investigate.py) — just reachable as a tool call now, so
    self-chat can ask "why did you do that" mid-conversation instead of only
    through a direct HTTP POST. Never writes anything.
    """
    from app import investigate as _inv, rig_agent
    from app import messaging as _M
    acfg = _M.load_config().get("auto_reply") or {}
    if not (rig_agent and rig_agent.owner_allowed(ctx.sender or "", acfg)):
        raise ToolRefused("investigate is owner-only")
    res = _inv.investigate(
        args.get("question") or "", acfg, allowed=True,
        chat_guid=ctx.conversation_id or "", who=ctx.sender or "",
        max_steps=5, max_model_calls=8,
        # 45.0 must match this tool's OWN registered timeout_seconds below
        deadline=_soft_deadline(45.0))
    return _limit_result(res)


def _tool_antons_read_code(args: dict, ctx: AgentContext) -> dict:
    """Owner-only, READ-ONLY: look at its own source."""
    from app import selfinspect as _si, rig_agent
    from app import messaging as _M
    acfg = _M.load_config().get("auto_reply") or {}
    allowed = bool(rig_agent and rig_agent.owner_allowed(ctx.sender or "", acfg))
    action = str(args.get("action") or "symbols").strip()
    _map = {"symbols": "repo_symbols", "read": "repo_read",
            "refs": "repo_refs", "search": "code_search",
            "grep": "repo_search", "text": "repo_search"}
    if action not in _map:
        raise ToolRefused("action must be one of: %s" % ", ".join(sorted(_map)))
    passthrough = ("name", "path", "kind", "query", "start", "end", "limit")
    call_args = {k: args[k] for k in passthrough if args.get(k) not in (None, "")}
    out = _si.call(_map[action], call_args, allowed=allowed, who=ctx.sender or "")
    if action == "symbols" and isinstance(out, dict) and out.get("ok"):
        syms = ((out.get("result") or {}).get("symbols") or [])[:2]
        for sym in syms:
            try:
                start = max(1, int(sym.get("line") or 1))
                body = _si.call("repo_read",
                                {"path": sym.get("path"), "start": start,
                                 "end": start + 80},
                                allowed=allowed, who=ctx.sender or "")
                if isinstance(body, dict) and body.get("ok"):
                    sym["source"] = body.get("result")
            except Exception:
                pass
    if (action == "search" and isinstance(out, dict) and out.get("ok")
            and isinstance(out.get("result"), dict)):
        res = out["result"]
        try:
            top = max([float(h.get("score") or 0)
                       for h in (res.get("results") or [])] or [0.0])
        except Exception:
            top = 0.0
        try:
            lit = _si.call("repo_search", {"query": args.get("query") or ""},
                           allowed=allowed, who=ctx.sender or "")
        except Exception:
            lit = None
        matches = []
        if isinstance(lit, dict) and lit.get("ok") and isinstance(lit.get("result"), dict):
            lres = lit["result"]
            matches = (lres.get("matches") or lres.get("results") or [])[:10]
        if matches:
            res["literal_matches"] = matches
        if top < 0.62:
            res["hint"] = (
                "Semantic scores are low, so these symbol hits may all be "
                "unrelated to the question. For a UI label, a message, or any "
                "exact string, call this tool again with action='grep' and the "
                "literal text -- that searches the file contents, including "
                "the dashboard JavaScript, instead of ranking symbol names.")
    return _limit_result(out)


def _tool_antons_set_auto_accept(args: dict, ctx: AgentContext) -> dict:
    """Owner-only, PRIVILEGED: turn auto-accept on or off."""
    from app import rig_agent
    from app import messaging as _M
    acfg = _M.load_config().get("auto_reply") or {}
    if not (rig_agent and rig_agent.owner_allowed(ctx.sender or "", acfg)):
        raise ToolRefused("auto-accept is owner-only")
    enabled = bool(args.get("enabled"))
    state = _M.set_auto_accept(enabled, args.get("minutes"))
    if enabled:
        msg = (
            "Auto-akcepts ir IESLĒGTS līdz %s (%d min). Kamēr tas ir ieslēgts: "
            "koda labojumus es piemēroju uzreiz, neprasot tavu 'jā', un servisa "
            "restartu veicu pats. Testi joprojām skrien, un ja tie krīt, "
            "izmaiņa tiek atritināta atpakaļ. Tas, kas jau gaidīja apstiprinājumu "
            "PIRMS šī brīža, netiek automātiski piemērots — to tu vēl apstiprini "
            "parastajā kārtībā. Izslēgt var jebkurā brīdī."
            % (state["until_local"], state["minutes"]))
    else:
        msg = ("Auto-akcepts ir IZSLĒGTS. Turpmāk pirms katra koda labojuma un "
               "pirms restarta es gaidu tavu apstiprinājumu.")
    return _limit_result({"ok": True, "auto_accept": state, "message": msg})


def _tool_antons_find_message(args: dict, ctx: AgentContext) -> dict:
    """Owner-only, READ-ONLY: search further back in THIS chat than the
    default window reaches.
    """
    from app import messaging as _M, rig_agent
    acfg = _M.load_config().get("auto_reply") or {}
    if not (rig_agent and rig_agent.owner_allowed(ctx.sender or "", acfg)):
        raise ToolRefused("reading chat history is owner-only")
    query = str(args.get("query") or "").strip()
    if not query:
        raise ToolRefused("query is required")
    limit = args.get("limit")
    try:
        limit = max(1, min(20, int(limit))) if limit is not None else 8
    except Exception:
        limit = 8
    out = _M.find_chat_messages(ctx.conversation_id or "", query, limit=limit)
    return _limit_result(out)


def _tool_antons_propose_fix(args: dict, ctx: AgentContext) -> dict:
    from app import propose_patch as _pp, rig_agent
    from app import messaging as _M
    acfg = _M.load_config().get("auto_reply") or {}
    if not (rig_agent and rig_agent.owner_allowed(ctx.sender or "", acfg)):
        raise ToolRefused("proposing a fix is owner-only")
    res = _pp.propose(
        args.get("goal") or "", acfg, allowed=True,
        chat_guid=ctx.conversation_id or "", who=ctx.sender or "",
        max_steps=14, max_model_calls=24,
        # 900.0 must match this tool's OWN registered timeout_seconds below
        deadline=_soft_deadline(900.0))
    if isinstance(res, dict) and res.get("ok") and res.get("proposal"):
        try:
            _M.remember_last_proposal(ctx.conversation_id or "", res)
        except Exception:
            pass
    if isinstance(res, dict) and res.get("proposal"):
        try:
            summary = _pp.render(res, lang="lv")
        except Exception:
            summary = ""
        files = [f.get("path") for f in (res["proposal"].get("files") or [])
                 if isinstance(f, dict) and f.get("path")]
        return _limit_result({
            "ok": True,
            "state": res.get("state"),
            "summary": summary or "(proposal written)",
            "files": files,
            "rehearsal": ("passed: the change was applied for a moment, every "
                          "check and its test passed, and the files were put "
                          "back -- applying it should land on the first try"
                          if res["proposal"].get("rehearsal") == "passed" else
                          "not tried: it could not be tried before asking, so "
                          "its test runs for the first time when it is applied"),
            "confirmed_cause": res.get("confirmed_cause"),
            "next": "the edits are held for this chat -- call antons_apply_fix "
                    "only after he says to go ahead",
        })
    return _limit_result({"ok": False,
                          "state": (res or {}).get("state"),
                          "reason": (res or {}).get("reason")
                          or ((res or {}).get("attempts") or [None])[-1]
                          or "no proposal produced"})


# chat -> (when, {"files", "outcome"}) for the last apply that LANDED, so a
# second call in the same stretch can say what actually happened.
_LAST_APPLIED: dict = {}
_LAST_APPLIED_TTL = 1800


def _tool_antons_apply_fix(args: dict, ctx: AgentContext) -> dict:
    from app import self_heal, rig_agent
    from app import messaging as _M
    acfg = _M.load_config().get("auto_reply") or {}
    if not (rig_agent and rig_agent.owner_allowed(ctx.sender or "", acfg)):
        raise ToolRefused("applying a fix is owner-only")
    pending = _M._get_last_proposal(ctx.conversation_id or "")
    proposal = (pending or {}).get("proposal") if pending else None
    edits = (proposal or {}).get("edits") or []
    if not edits:
        done = _LAST_APPLIED.get(ctx.conversation_id or "")
        if done and time.time() - done[0] <= _LAST_APPLIED_TTL:
            when = time.strftime("%H:%M:%S", time.localtime(done[0]))
            return {"ok": True, "applied": True, "already_applied": True,
                    "applied_at": when, "files": done[1].get("files") or [],
                    "outcome": done[1].get("outcome") or "",
                    "note": ("nothing new to apply: the proposal he approved "
                             "was ALREADY APPLIED at %s and is on disk. Tell him "
                             "it is applied; do not tell him nothing was "
                             "applied." % when)}
        return {"ok": False, "reason":
                "no pending proposal in this chat (or it is older than 30 "
                "min) -- call antons_propose_fix first"}
    chat = ctx.conversation_id or ""
    auto_accept_on, _auto_until = _M._auto_accept_state(acfg)
    if not auto_accept_on and not _M.pending_proposal_approved(chat):
        return {"ok": False, "applied": False, "reason": (
            "he has not said yes to THIS proposal -- it was written after his "
            "go-ahead, or he never gave one. Show him what it changes and wait "
            "for his yes before applying.")}
    test = (proposal or {}).get("test") or {}
    suite = str(test.get("suite") or "").strip()
    try:
        res = self_heal.apply_edits(
            edits, suites=([suite] if suite else []),
            reason="antons_apply_fix via %s" % (ctx.conversation_id or ""))
    except self_heal.HealError as exc:
        _M.clear_proposal_approval(chat)
        return {"ok": False, "reason": str(exc)}
    _M.clear_proposal_approval(chat)
    told = _apply_result_for_model(res)
    if isinstance(res, dict) and res.get("applied"):
        try:
            _M._LAST_PROPOSAL.pop(str(ctx.conversation_id or ""), None)
        except Exception:
            pass
        _LAST_APPLIED[chat] = (time.time(), {
            "files": [f.get("path") for f in ((proposal or {}).get("files") or [])
                      if isinstance(f, dict) and f.get("path")],
            "outcome": (told or {}).get("outcome") if isinstance(told, dict) else ""})
        published = _publish_applied_pages(res.get("files") or [])
        if published and isinstance(told, dict):
            told["published"] = published
    return _limit_result(told)


def _publish_applied_pages(files) -> list:
    out = []
    for f in files or ():
        path = str((f or {}).get("path") if isinstance(f, dict) else f or "").replace("\\", "/")
        m = re.match(r"^(?:.*/)?app/static/([A-Za-z0-9_-]+)\.html$", path)
        if not m:
            continue
        slug = m.group(1)
        try:
            on_box = any(v.get("version") == "box:current" for v in _page_versions_box(slug))
        except Exception:
            on_box = False
        if not on_box:
            continue
        pub = _publish_slug_to_media(slug)
        out.append({"slug": slug, "ok": bool(pub.get("ok")),
                    "url_owner": pub.get("url_owner"), "url_partner": pub.get("url_partner"),
                    "reason": pub.get("reason")})
    return out


def _apply_result_for_model(res):
    """What the model needs from an apply, in the order it needs it."""
    if not isinstance(res, dict):
        return res
    checks = res.get("checks")
    if not isinstance(checks, dict):
        checks = {}
    failures = []
    for stage in (checks.get("stages") or []):
        if not isinstance(stage, dict) or stage.get("ok"):
            continue
        for item in (stage.get("findings") or [])[:5]:
            if isinstance(item, dict):
                failures.append({
                    "stage": stage.get("stage"),
                    "where": "%s:%s" % (item.get("path"), item.get("line")),
                    "code": item.get("code"),
                    "message": str(item.get("message") or "")[:200],
                    "note": item.get("baseline_note") or item.get("note"),
                })
        if stage.get("error"):
            failures.append({"stage": stage.get("stage"),
                             "message": str(stage.get("error"))[:200]})
    suites = [s for s in (res.get("suites") or []) if isinstance(s, dict)]
    failed_suites = [{"suite": s.get("suite"), "exit_code": s.get("exit_code"),
                      "output": str(s.get("output") or "")[-1500:]}
                     for s in suites if not s.get("ok")]
    failed_stage = str(checks.get("failed_stage") or "")
    if not failed_stage and failed_suites:
        failed_stage = "tests"
    return {
        "applied": bool(res.get("applied")),
        "reverted": bool(res.get("reverted")),
        "outcome": str(res.get("note") or "")[:400],
        "failed_stage": failed_stage,
        "failures": failures,
        "failed_suites": failed_suites,
        "passed_suites": [s.get("suite") for s in suites if s.get("ok")],
        "files": list(res.get("files") or []),
        "diff": str(res.get("diff") or "")[:1500],
    }


_RESTART_ASK_RE = re.compile(
    r"(?i)(\brestart\w*|\breboot\w*|p[aā]rstart\w*|restart[eē]\w*|"
    r"p[aā]rl[aā]d[eē]\w*|palaid\w*\s+no\s+jauna|no\s+jauna\s+palai\w*)")


def _restart_requested(turn_text: str, owner_recent_text: str = "") -> bool:
    if _RESTART_ASK_RE.search(_current_action_text(turn_text) or ""):
        return True
    return bool(_RESTART_ASK_RE.search(_current_action_text(owner_recent_text or "")))


def _tool_antons_restart_service(args: dict, ctx: AgentContext) -> dict:
    """Owner-only, PRIVILEGED. Restart SysMonitor so applied edits take effect."""
    from app import service_restart as _sr, rig_agent
    from app import messaging as _M
    acfg = _M.load_config().get("auto_reply") or {}
    if not (rig_agent and rig_agent.owner_allowed(ctx.sender or "", acfg)):
        raise ToolRefused("restarting the service is owner-only")
    if not _restart_requested(ctx.turn_text, getattr(ctx, "owner_recent_text", "") or ""):
        raise ToolRefused(
            "he has not asked for a restart -- applied code waits on disk until "
            "he says so; an apply, or a 'yes' to a proposal, is not a restart "
            "request. Tell him it is applied and ask if he wants a restart.")
    ok, detail = _sr.request_self_restart(reason="self-chat: %s" % (ctx.sender or ""))
    return {"ok": bool(ok), "restarting": bool(ok), "detail": detail}


def _tool_antons_shares(args: dict, ctx: AgentContext) -> dict:
    from app import rig_agent, tailscale_shares
    from app import messaging as _M
    acfg = _M.load_config().get("auto_reply") or {}
    if not (rig_agent and rig_agent.owner_allowed(ctx.sender or "", acfg)):
        raise ToolRefused("Tailscale shares are owner-only")
    res = tailscale_shares.list_shares()
    return _limit_result(res)


def _tool_antons_unshare(args: dict, ctx: AgentContext) -> dict:
    from app import rig_agent, tailscale_shares
    from app import messaging as _M
    acfg = _M.load_config().get("auto_reply") or {}
    if not (rig_agent and rig_agent.owner_allowed(ctx.sender or "", acfg)):
        raise ToolRefused("changing Tailscale shares is owner-only")
    port = str(args.get("port") or "").strip()
    if not port:
        return {"ok": False, "reason": "which share? pass the `port` from "
                                       "antons_shares (e.g. 8443)"}
    if port in ("443", "47991"):
        return {"ok": False, "reason": "port %s is one of his own standing "
                "services, not a game share -- refusing to touch it" % port}
    res = tailscale_shares.stop_share(port)
    return _limit_result(res)


_MEDIA_HOST = hosts.MEDIA_BOX_HOST
_MEDIA_IP_OWNER = hosts.MEDIA_BOX_IP
_MEDIA_IP_PARTNER = hosts.MEDIA_BOX_IP_PARTNER
_MEDIA_PAGES_URL = "http://" + _MEDIA_IP_OWNER + ":8183/%s.html"
_MEDIA_PAGES_URL_PARTNER = "http://" + _MEDIA_IP_PARTNER + ":8183/%s.html"
_MEDIA_PAGES_URL_HOST = "http://" + _MEDIA_HOST + ":8183/%s.html"


def _publish_slug_to_media(slug: str) -> dict:
    """Copy app/static/<slug>.html to the media box <media home>/pages/, timestamped
    backup first, verify it serves 200 on :8183, return {ok, url, ...}. TAILNET
    ONLY -- :8183 is the tailscale-only static server; never a funnel. Shared by
    antons_publish and the auto-publish after a good build."""
    slug = re.sub(r"[^a-z0-9_-]", "", str(slug or "").lower())
    if not slug or slug in _BUILD_RESERVED:
        return {"ok": False, "reason": "no slug to publish"}
    src = _BUILD_STATIC / (slug + ".html")
    if not src.exists():
        return {"ok": False, "reason": "no local file app/static/%s.html" % slug}
    html = src.read_text(encoding="utf-8", errors="replace")
    if not _html_looks_complete(html):
        return {"ok": False, "reason": "%s.html is not a complete page" % slug}
    syn = _js_syntax_error(html)
    if syn:
        return {"ok": False, "reason": "%s.html has a JS error: %s" % (slug, syn[:180])}
    try:
        from app import main as _main
    except Exception as exc:
        return {"ok": False, "reason": "media bridge unavailable: %s" % exc}
    stamp = time.strftime("%Y%m%dT%H%M%S")
    _main._run_media_password(
        ["sh", "-c", "cd " + hosts.MEDIA_BOX_HOME + "/pages && test -f %s.html && cp %s.html "
         "%s.html.bak-%s || true" % (slug, slug, slug, stamp)], timeout=30)
    put = _main._sftp_to_media(str(src), "pages/%s.html" % slug, timeout=120)
    if not put.get("ok"):
        return {"ok": False, "reason": "copy to the media box failed: %s"
                % (put.get("output") or "unknown")}
    chk = _main._run_media_password(
        ["sh", "-c", "curl -s -o /dev/null -w '%%{http_code}:%%{size_download}' "
         "http://127.0.0.1:8183/%s.html" % slug], timeout=20)
    served = (chk.get("output") or "").strip()
    want = len(html.encode("utf-8"))
    ok_served = served.startswith("200")
    url = _MEDIA_PAGES_URL % slug
    url_partner = _MEDIA_PAGES_URL_PARTNER % slug
    url_host = _MEDIA_PAGES_URL_HOST % slug       # MagicDNS -- either tailnet
    _append_action_ledger({"tool": "antons_publish", "slug": slug, "bytes": want,
                           "served": served, "ok": ok_served})
    if not ok_served:
        return {"ok": False, "url": url, "served": served,
                "reason": "copied the file but it did not serve a 200 (%s)" % served}
    return {"ok": True, "url": url, "url_owner": url, "url_partner": url_partner,
            "url_host": url_host, "bytes": want, "served": served,
            "backup": (hosts.MEDIA_BOX_HOME + "/pages/%s.html.bak-%s") % (slug, stamp)}


def _tool_antons_publish(args: dict, ctx: AgentContext) -> dict:
    from app import messaging as _M
    acfg = _M.load_config().get("auto_reply") or {}
    if not _owner_turn(ctx, acfg):
        raise ToolRefused("publishing to the media box is owner-only")
    slug = str(args.get("slug") or "").strip()
    if not slug:
        specs = sorted(_build_specs_load().values(),
                       key=lambda s: s.get("ts") or 0, reverse=True)
        slug = specs[0].get("slug") if specs else ""
    res = _publish_slug_to_media(slug)
    if res.get("ok"):
        res["note"] = (
            "Live on the tailnet (not public). TWO links, one per tailnet -- "
            "the same box has a different IP on each:\n"
            f"  Tev ({OWNER}):  %s\n"
            f"  {PARTNER_DAT}:          %s\n"
            f"Give {PARTNER} the .6 one; the .7 one only works on your own devices. "
            "(%s works on either if an IP ever changes.)"
            % (res.get("url_owner"), res.get("url_partner"), res.get("url_host")))
    elif "which page" not in str(res.get("reason", "")) and not slug:
        res["reason"] = ("which page? pass `slug` -- nothing built to publish")
    return res


_BOX_BACKUP_RE = re.compile(r"^bak-(\d{8})T(\d{6})$")


def _page_title(data: bytes) -> str:
    m = re.search(r"<title>([^<]{0,120})", data.decode("utf-8", errors="replace"), re.I)
    return " ".join(m.group(1).split()) if m else ""


def _page_versions_rig(slug: str) -> list:
    """The page here, its last browser-verified copy, loose snapshots."""
    out = []
    for p in sorted(_BUILD_STATIC.glob(slug + ".html*")):
        if not p.is_file():
            continue
        rest = p.name[len(slug + ".html"):].lstrip(".")
        data = p.read_bytes()
        out.append({"version": ("current" if not rest else
                                "lastgood" if rest == "lastgood" else "rig:" + rest),
                    "where": "rig", "bytes": len(data), "title": _page_title(data),
                    "saved": time.strftime("%Y-%m-%d %H:%M",
                                           time.localtime(p.stat().st_mtime)),
                    "sha256": hashlib.sha256(data).hexdigest()})
    return out


def _page_versions_box(slug: str) -> list:
    """What the games box serves now, and each copy a publish replaced --
    `live_until` is when that copy stopped being the one people opened."""
    from app import main as _main
    cmd = ("cd " + hosts.MEDIA_BOX_HOME + "/pages || exit 0; for f in %s.html $(ls -1t %s.html.bak-* "
           "2>/dev/null | head -n 30); do [ -f \"$f\" ] || continue; "
           "printf '%%s\\t%%s\\t%%s\\t%%s\\n' \"$f\" \"$(wc -c < \"$f\")\" "
           "\"$(sha256sum \"$f\" | cut -c1-64)\" "
           "\"$(grep -o '<title>[^<]*' \"$f\" | head -n 1 | cut -c8-)\"; done"
           % (slug, slug))
    r = _main._run_media_password(["sh", "-c", cmd], timeout=40)
    if not r.get("ok"):
        raise RuntimeError("games box: %s" % str(r.get("output") or "no answer")[:200])
    out = []
    for line in str(r.get("output") or "").splitlines():
        parts = line.split("\t")
        if len(parts) < 3 or not parts[0].startswith(slug + ".html"):
            continue
        rest = parts[0][len(slug + ".html"):].lstrip(".")
        m = _BOX_BACKUP_RE.match(rest)
        if rest and not m:
            continue
        row = {"version": "box:" + (rest or "current"), "where": "games box",
               "bytes": int(parts[1]) if parts[1].strip().isdigit() else None,
               "sha256": parts[2].strip(),
               "title": " ".join(parts[3].split()) if len(parts) > 3 else ""}
        if m:
            d, t = m.groups()
            row["live_until"] = "%s-%s-%s %s:%s" % (d[:4], d[4:6], d[6:], t[:2], t[2:4])
        else:
            row["live_until"] = "live now"
        out.append(row)
    return out


def _media_box_read(remote_name: str) -> bytes:
    """One file from <media home>/<remote_name> on the games box, whole."""
    import tempfile
    from app import main as _main
    fd, tmp = tempfile.mkstemp(suffix=".html")
    os.close(fd)
    try:
        r = _main._sftp_from_media(remote_name, tmp)
        if not r.get("ok"):
            raise RuntimeError(str(r.get("output") or "copy failed")[:200])
        return Path(tmp).read_bytes()
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _tool_antons_page_versions(args: dict, ctx: AgentContext) -> dict:
    """Owner-only. The copies of one page that still exist, or the pages."""
    from app import messaging as _M
    acfg = _M.load_config().get("auto_reply") or {}
    if not _owner_turn(ctx, acfg):
        raise ToolRefused("page history is owner-only")
    raw = str(args.get("slug") or "").strip()
    if not raw:
        pages = []
        for p in sorted(_BUILD_STATIC.glob("*.html")):
            if p.is_file() and p.stem not in _BUILD_RESERVED:
                data = p.read_bytes()
                pages.append({"slug": p.stem, "title": _page_title(data),
                              "bytes": len(data)})
        return {"ok": True, "pages": pages}
    slug = _slugify_build_name(raw, "")
    rig = _page_versions_rig(slug)
    box, box_error = [], ""
    try:
        box = _page_versions_box(slug)
    except Exception as exc:
        box_error = str(exc)[:200]
    if not rig and not box:
        return {"ok": False, "slug": slug, "reason": "no page called %s" % slug}
    main = [v for v in rig if v["version"] in ("current", "lastgood")]
    loose = sorted((v for v in rig if v["version"] not in ("current", "lastgood")),
                   key=lambda v: v.get("saved") or "", reverse=True)
    versions = [{k: val for k, val in v.items() if k != "sha256"}
                for v in box + main + loose[:6]]
    res = {"ok": True, "slug": slug, "versions": versions,
           "note": ("`version` is what antons_restore_page takes. A games-box "
                    "copy's `live_until` is when a publish replaced it.")}
    if len(loose) > 6:
        res["older_rig_snapshots_not_shown"] = len(loose) - 6
    if box_error:
        res["box_error"] = box_error
    return res


def _tool_antons_restore_page(args: dict, ctx: AgentContext) -> dict:
    """Owner-only, PRIVILEGED. Put one listed copy of a page back (or under a
    new name), then publish it -- the games box backs up what it replaces."""
    from app import messaging as _M
    acfg = _M.load_config().get("auto_reply") or {}
    if not _owner_turn(ctx, acfg):
        raise ToolRefused("restoring a page is owner-only")
    slug = _slugify_build_name(str(args.get("slug") or ""), "")
    version = str(args.get("version") or "").strip()
    to_slug = (_slugify_build_name(str(args.get("to_slug")), "")
               if str(args.get("to_slug") or "").strip() else slug)
    if not str(args.get("slug") or "").strip() or not version:
        return {"ok": False, "reason": "pass `slug` and a `version` from antons_page_versions"}
    try:
        if version.startswith("box:"):
            rest = version[4:]
            if rest != "current" and not _BOX_BACKUP_RE.match(rest):
                return {"ok": False, "reason": "unknown version %r" % version}
            data = _media_box_read("pages/%s.html%s" % (
                slug, "" if rest == "current" else "." + rest))
        else:
            rest = ("" if version == "current" else
                    "lastgood" if version == "lastgood" else
                    version[4:] if version.startswith("rig:") else None)
            p = (_BUILD_STATIC / ("%s.html%s" % (slug, "." + rest if rest else ""))
                 if rest is not None and "/" not in rest and "\\" not in rest else None)
            if p is None or not p.is_file():
                return {"ok": False, "reason": "unknown version %r" % version}
            data = p.read_bytes()
    except Exception as exc:
        return {"ok": False, "reason": "could not read %s: %s" % (version, str(exc)[:200])}
    html = data.decode("utf-8", errors="replace")
    if not _html_looks_complete(html):
        return {"ok": False, "reason": "%s is not a complete page" % version}
    syn = _js_syntax_error(html)
    if syn:
        return {"ok": False, "reason": "%s has a JS error: %s" % (version, syn[:180])}
    target = _BUILD_STATIC / (to_slug + ".html")
    if target.exists() and target.read_bytes() != data:
        # never lose what is there now: it must already be kept somewhere
        cur = hashlib.sha256(target.read_bytes()).hexdigest()
        kept = {v.get("sha256") for v in _page_versions_rig(to_slug)
                if v.get("version") != "current"}
        try:
            kept |= {v.get("sha256") for v in _page_versions_box(to_slug)}
        except Exception:
            pass
        if cur not in kept:
            return {"ok": False, "reason": (
                "the current %s.html is not kept anywhere else -- restore it "
                "under a new name with `to_slug`, or publish it first" % to_slug)}
    target.write_bytes(data)
    _append_action_ledger({"tool": "antons_restore_page", "slug": to_slug,
                           "from_slug": slug, "version": version,
                           "bytes": len(data), "title": _page_title(data)})
    pub = _publish_slug_to_media(to_slug)
    return {"ok": bool(pub.get("ok")), "slug": to_slug, "restored": version,
            "from_slug": slug, "title": _page_title(data), "bytes": len(data),
            "url_owner": pub.get("url_owner"), "url_partner": pub.get("url_partner"),
            "published": bool(pub.get("ok")),
            "reason": pub.get("reason") if not pub.get("ok") else None,
            "note": (f"Back on the rig and on the games box. Links: {OWNER} .7, "
                     f"{PARTNER} .6.") if pub.get("ok") else
                    ("Restored on the rig, but publishing failed -- run "
                     "antons_publish for %s." % to_slug)}


def _tool_antons_fix_page_text(args: dict, ctx: AgentContext) -> dict:
    from app import messaging as _M
    from app import page_texts as _pt
    acfg = _M.load_config().get("auto_reply") or {}
    if not _owner_turn(ctx, acfg):
        raise ToolRefused("changing a page is owner-only")
    raw = str(args.get("slug") or "").strip()
    if not raw:
        return {"ok": False, "reason": "pass `slug` -- the page whose text to fix"}
    slug = _slugify_build_name(raw, "")
    path = _BUILD_STATIC / (slug + ".html")
    if not path.is_file():
        return {"ok": False, "reason": "no page called %s" % slug}
    before = path.read_text(encoding="utf-8", errors="replace")
    provider = str(acfg.get("translator") or "")
    try:
        after, changes = _pt.retext(
            before, translate=lambda en: _M._nmt_lv(en, provider=provider, acfg=acfg))
    except Exception as exc:
        return {"ok": False, "slug": slug, "reason": "text pass failed: %s" % str(exc)[:200]}
    if not changes or after == before:
        return {"ok": True, "slug": slug, "changed": 0, "note": "no visible text to change"}
    syn = _js_syntax_error(after)
    if syn:
        return {"ok": False, "slug": slug, "reason": "the new text broke the JS: %s" % syn[:180]}
    test_line, new_fails = "", []
    if _selftest_enabled():
        try:
            from app import build_selftest as _bs
            if _bs.available():
                url = "http://127.0.0.1:8765/static/%s.html" % slug
                was = _selftest_failure_keys(_bs.run_selftest(url))
                path.write_text(after, encoding="utf-8", newline="\n")
                res = _bs.run_selftest(url)
                new_fails = [f for f in (res.get("failures") or [])
                             if _selftest_failure_key(f) not in was]
                test_line = _bs.summary_line(res)
        except Exception:
            LOGGER.exception("antons_fix_page_text: selftest could not run")
    if new_fails:
        path.write_text(before, encoding="utf-8", newline="\n")
        _append_action_ledger({"tool": "antons_fix_page_text", "slug": slug,
                               "changed": len(changes), "kept": False,
                               "new_failures": new_fails[:4]})
        return {"ok": False, "slug": slug, "reverted": True, "new_failures": new_fails[:4],
                "reason": "with the new text the page failed checks it passed "
                          "before -- it is left as it was"}
    path.write_text(after, encoding="utf-8", newline="\n")
    _append_action_ledger({"tool": "antons_fix_page_text", "slug": slug,
                           "changed": len(changes), "kept": True, "test": test_line})
    res = {"ok": True, "slug": slug, "changed": len(changes), "changes": changes[:60],
           "test": test_line, "published": False,
           "note": ("Show him the old and new text side by side -- he judges "
                    "the Latvian, not you.")}
    try:
        on_box = any(v.get("version") == "box:current" for v in _page_versions_box(slug))
    except Exception:
        on_box = False
    if on_box:
        pub = _publish_slug_to_media(slug)
        res.update({"published": bool(pub.get("ok")), "url_owner": pub.get("url_owner"),
                    "url_partner": pub.get("url_partner")})
    return res


_BUILD_STATIC = ROOT / "app" / "static"
_BUILD_RESERVED = {"index", "me", "mini", "label", "app", "app.candidate",
                   "app.js", "styles", "app.js.lastgood", "favicon"}

_BUILD_SYS_BASE = (
    "You write ONE complete, self-contained HTML file and nothing else. Output "
    "ONLY the file: start with `<!doctype html>` and end with `</html>`. No "
    "prose before or after, no markdown fence, no explanation. If the file is "
    "long and you reach the length limit, just STOP mid-line -- you will be "
    "asked to continue from exactly there. Never restart the file, never "
    "summarise, never write '... rest of code ...'.\n"
    "Rules:\n"
    "- One file. All CSS in a single <style>, all JS in a single <script>. No "
    "build step, no imports, no npm, no external URL, no CDN, no framework, no "
    "web fonts, no external images -- inline anything you need (SVG, a data: "
    "URI). Plain HTML/CSS/JS only.\n"
    "- Served at http://127.0.0.1:8765/static/<name>.html.\n"
    "- Fills the screen, NO page scroll, works portrait and landscape, on a "
    "phone. Fixed non-scrolling body. Handle touch as well as mouse.\n"
    "- Never throw on missing data; guard every access.\n"
)
_BUILD_SYS_DATA = (
    "\nLIVE RIG DATA (same origin):\n"
    "- GET /api/stats every 2s. Fields (numeric unless noted): "
    "gpu.temperature_c, gpu.gpu_utilization_percent, gpu.memory_used_mb, "
    "gpu.memory_total_mb, gpu.power_draw_w, gpu.power_limit_w, "
    "gpu.fan_speed_percent, gpu.name (text); cpu.per_core_percent (array), "
    "cpu.frequency_mhz, cpu.package_temperature_c (often null), cpu_percent; "
    "memory.percent, memory.used_gb, memory.total_gb, memory.swap.percent; "
    "disks (array of {letter, percent, free_gb}); warnings (array of strings); "
    "uptime_seconds. `warnings` almost always has a chronic 'Disk M:: 95%' "
    "line and VRAM sits ~85% whenever a model is loaded -- neither is a real "
    "problem; do NOT alarm on disk-space or memory/VRAM warnings.\n"
    "- GET /api/ollama/models returns {loaded:[{name,size_vram,expires_at}]}. "
    "A model whose name starts 'qwen' with size_vram>0 = the assistant is "
    "loaded/ready; only 'embed' or nothing = idle/asleep.\n"
)
_BUILD_SYS_SELFTEST = (
    "\nSELF-TEST BLOCK (required). Just before </body>, add:\n"
    "  <script type=\"application/json\" id=\"selftest\">[ ... ]</script>\n"
    "a JSON array of 4-7 checks, each {\"name\": \"...\", \"js\": \"...\"}. `js` is "
    "the BODY of an async function (no wrapper; `await` works) that acts on the "
    "page and RETURNS true when the check passes, false otherwise. Each check "
    "runs on its OWN freshly loaded page, so a check must set up everything it "
    "needs itself: a check that upgrades or sells something builds that thing "
    "first, in the same `js`. Keep each `js` one line, no backticks, no comments.\n"
    "TOUCH THE PAGE THE WAY A PERSON DOES -- each of these is a REAL finger or "
    "key and lands on whatever is on top there: `await __tap('#startBtn')`, "
    "`await __tapAt(x, y)`, `await __key('ArrowLeft')`, "
    "`await __swipe('#board', -120, 0)`, `await __wait(400)`. NEVER call your "
    "own game functions or `.click()` / `dispatchEvent` in a check -- that "
    "passes on a game nobody can play. Reading state to assert on is fine.\n"
    "Every check must EXERCISE the game and assert the RESULT, not read the "
    "opening state. 'score starts at 0' / 'there are 16 cells' are worthless -- "
    "they pass on a dead game:\n"
    "- The MAIN MOVE is one check: tap the start button, make the real move "
    "(tap a cell, press an arrow, swipe, tap the canvas), assert something "
    "changed -- not just 'no error'.\n"
    "- If there is a SCORE: make a scoring move, assert the score NUMBER went up "
    "from its previous value (a '+N' popup is not the score).\n"
    "- A win / game-over state is reachable. No control is dead (reset, "
    "powerups, direction buttons). Never assert on exact wording.\n"
    "  {\"name\":\"tapping a free cell selects it\",\"js\":\"await __tap('.cell:"
    "not(.locked)');return document.querySelectorAll('.cell.sel').length===1;\"}\n"
)
_BUILD_SYS_IOS9 = (
    "\nTARGET IS AN iPHONE 4S -- iOS 9 Safari, a 2015 WebKit. STRICT.\n"
    "USE: ES5 only (var, function; NO arrow functions, NO let/const, NO "
    "template literals, NO async/await); XMLHttpRequest, NOT fetch(); "
    "float/position for layout, NOT flexbox or grid; hex or rgb()/rgba() "
    "colours (convert any HSL to rgb in JS -- `hsl(H S% L%)` space syntax does "
    "NOT parse); `-webkit-` prefixes on transform/transition/animation and "
    "DUPLICATE every @keyframes as @-webkit-keyframes; px and % sizing (compute "
    "pixel sizes in JS from window.innerWidth/innerHeight, do not trust "
    "vw/vh/vmin).\n"
    "DO NOT USE: CSS custom properties (var(--x)), calc(), env(), clamp(), "
    "CSS min()/max(); CSS `transform` on SVG child elements (animate an SVG "
    "shape by writing geometry attributes -- ry/cx/d -- with setAttribute, or "
    "the SVG `transform` ATTRIBUTE; never CSS transform on <g>/<path>).\n"
    "`app/static/mini.html` in this repo is a known-good page under exactly "
    "these constraints.\n"
    "No scroll on 320x480 AND rotated 480x320; keep a band clear top and "
    "bottom for the status bar and any text of your own, and make bottom text "
    "wrap or shrink so it is never clipped on the narrow portrait width.\n"
)


_BUILD_RULES_MD = ROOT / "app" / "build_rules.md"


def _read_build_rules_md() -> dict:
    """Read app/build_rules.md and return {section_name: text}.

    Sections are `## <name>` headings.  Returns {} on any error so the
    caller can fall back to the hardcoded constants.
    """
    try:
        text = _BUILD_RULES_MD.read_text(encoding="utf-8")
    except Exception:
        return {}
    sections: dict = {}
    current = None
    buf = []
    for line in text.splitlines(keepends=True):
        m = re.match(r"^##\s+(\S+)", line)
        if m:
            if current is not None:
                sections[current] = "".join(buf).strip()
            current = m.group(1)
            buf = []
        elif current is not None:
            buf.append(line)
    if current is not None:
        sections[current] = "".join(buf).strip()
    return sections


_BUILD_DATA_RE = re.compile(
    r"(?i)(?:/api/(?:stats\b)?|\brig\b|\bgpu\b|\bcpu\b|\bvram\b|\bmonitor\w*|"
    r"telemetr|temperatur|system stat|\bollama\b|\bcreature\b|\bpet\b|"
    r"tamagotchi)")

_BUILD_GAME_RE = re.compile(
    r"(?i)(\bgame\b|\bspēl\w*|\bplay\b|\btile\b|\bboard\b|\bgrid\b|\bpuzzle\b|"
    r"\bmatch\b|\bmerge\b|2048|snake|tetris|mahjong|sudoku|minesweeper|"
    r"solitaire|\blevel\w*|\bscore\b|\bmove\w*)")


def _build_sys(goal: str, ios9: bool) -> str:
    md = _read_build_rules_md()
    sys = md.get("base") or _BUILD_SYS_BASE
    if _BUILD_DATA_RE.search(str(goal or "")):
        sys += md.get("data") or _BUILD_SYS_DATA
    if _BUILD_GAME_RE.search(str(goal or "")) and md.get("game"):
        sys += "\n" + md["game"]
    sys += md.get("selftest") or _BUILD_SYS_SELFTEST
    if ios9:
        sys += md.get("ios9") or _BUILD_SYS_IOS9
    return sys


def _slugify_build_name(raw: str, goal: str) -> str:
    raw = re.sub(r"\.html?$", "", str(raw or "").strip(), flags=re.I)
    base = re.sub(r"[^a-z0-9]+", "-", raw.lower()).strip("-")
    if not base:
        g = str(goal or "").lower()
        for key, name in (("tamagotchi", "pet"), ("radīj", "pet"),
                          ("dzīv", "pet"), ("crea", "pet"), ("pet", "pet"),
                          ("game", "game"), ("spēl", "game"), ("play", "game"),
                          ("dashboard", "board"), ("panel", "board")):
            if key in g:
                base = name
                break
    if not base:
        # a plain ASCII word from the goal, at least 4 letters, not a stopword
        for word in re.findall(r"[a-z]{4,}", str(goal or "").lower()):
            if word not in ("this", "that", "with", "make", "build", "page",
                            "create", "show", "shows", "which", "what", "your",
                            "into", "from", "have", "will", "look", "like"):
                base = word
                break
    base = (base or "page").split(".")[0][:40].strip("-") or "page"
    if not raw and (_BUILD_STATIC / (base + ".html")).exists():
        for _i in range(2, 60):
            if not (_BUILD_STATIC / ("%s-%d.html" % (base, _i))).exists():
                base = "%s-%d" % (base, _i)
                break
    if base in _BUILD_RESERVED:
        base = base + "-2"
    return base


def _extract_html(text: str) -> str:
    s = str(text or "").strip()
    # strip a wrapping ```html ... ``` fence, but ONLY if the doc really is
    # fenced (starts with a fence) -- a stray ``` mid-file must not truncate it.
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*\n?", "", s)
        s = re.sub(r"\n?```\s*$", "", s)
    lo = s.lower()
    i = lo.find("<!doctype")
    if i < 0:
        i = lo.find("<html")
    j = lo.rfind("</html>")
    if i >= 0 and j > i:
        return s[i:j + 7].strip()
    if i >= 0:               # opened but never closed -- return what we have
        return s[i:].strip()
    return s.strip()


def _html_looks_complete(html: str, needs_stats: bool = False) -> bool:
    lo = str(html or "").lower()
    ok = (len(html) > 500 and "<html" in lo and "</html>" in lo
          and "<script" in lo)
    if needs_stats:
        ok = ok and "/api/stats" in html
    return ok


_SCRIPT_TAG_RE = re.compile(
    r"<script\b(?![^>]*\btype\s*=\s*[\"']?(?:application/json|text/template))"
    r"[^>]*>(.*?)</script>", re.S | re.I)


def _js_syntax_error(html: str) -> str:
    """Parse every executable <script> with `node --check`. Returns "" when the
    JS is syntactically valid (or node is unavailable), else the first error.
    """
    try:
        from app import browser_render as _br
        node = _br._node_bin()
    except Exception:
        node = None
    if not node:
        return ""
    import tempfile
    for m in _SCRIPT_TAG_RE.finditer(str(html or "")):
        body = (m.group(1) or "").strip()
        if len(body) < 30 or body.lstrip().startswith(("{", "[", "<")):
            continue
        if re.search(r"\bsrc\s*=", str(html or "")[max(0, m.start() - 200):m.start()]):
            continue
        tf = None
        try:
            with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                             encoding="utf-8") as fh:
                fh.write(body)
                tf = fh.name
            r = subprocess.run([node, "--check", tf], capture_output=True,
                               text=True, timeout=20,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if r.returncode != 0:
                err = (r.stderr or r.stdout or "").strip().splitlines()
                # node prints: <file>:<line>\n<code>\n<caret>\n\nSyntaxError: ...
                msg = next((l for l in err if "Error:" in l), err[-1] if err else "syntax error")
                loc = next((re.search(r":(\d+)\s*$", l) for l in err
                            if re.search(r":\d+\s*$", l)), None)
                return ("JS %s%s" % (msg.strip(),
                                     (" (line %s)" % loc.group(1)) if loc else ""))[:240]
        except subprocess.TimeoutExpired:
            return ""
        except Exception:
            LOGGER.debug("js syntax check hiccup", exc_info=True)
            return ""
        finally:
            if tf:
                try:
                    os.unlink(tf)
                except Exception:
                    pass
    return ""


_IOS9_BREAKERS = (
    (r"[^A-Za-z_.]fetch\s*\(", "fetch() -- use XMLHttpRequest"),
    (r"=>", "arrow functions -- use function(){}"),
    (r"\bconst\s+\w+\s*=", "const -- use var"),
    (r"\blet\s+\w+\s*=", "let -- use var"),
    (r"`", "template literals -- use string concatenation"),
    (r"var\(--", "CSS custom properties -- compute values in JS"),
    (r"[\s:(]calc\(", "CSS calc()"),
    (r"\benv\(\s*safe-area", "CSS env()"),
    (r"[\s:(,]clamp\(\s*[\d.]", "CSS clamp()"),
)


def _ios9_violations(html: str) -> list[str]:
    # strip HTML comments so a "don't use calc()/env()" note doesn't self-trip,
    # and the self-test block (its JS runs in the modern test browser, never on
    # the iPhone, so `=>`/`const` in there are not device violations)
    src = re.sub(r"<!--.*?-->", "", str(html or ""), flags=re.S)
    src = re.sub(r'<script[^>]*id=["\']selftest["\'][^>]*>.*?</script>', "",
                 src, flags=re.S | re.I)
    hits = []
    for pat, why in _IOS9_BREAKERS:
        if re.search(pat, src):
            hits.append(why)
    # space-separated hsl(): "hsl(200 60% 50%)" / "hsl(200 60% 50% / .5)"
    if re.search(r"hsl\(\s*[\d.]+\s+[\d.]+%", src):
        hits.append("space-separated hsl() syntax -- convert HSL to rgb() in JS")
    return hits


def _ollama_cfg():
    try:
        cfg = json.loads((ROOT / "config" / "messaging.json").read_text(
            encoding="utf-8"))
    except Exception:
        cfg = {}
    acfg = (cfg.get("auto_reply") or {}) if isinstance(cfg, dict) else {}
    base = str(acfg.get("ollama_url") or "http://127.0.0.1:11434").rstrip("/")
    model = str(acfg.get("assistant_model") or acfg.get("model")
                or "qwen3.8:latest")
    nctx = int(acfg.get("num_ctx") or 32768)
    return base, model, nctx


def _chat_once(messages: list[dict], num_predict: int,
               think: bool = False, temperature: float = 0.35) -> tuple[str, str]:
    """One /api/chat call -> (content, done_reason)."""
    base, model, nctx = _ollama_cfg()
    payload = {
        "model": model, "messages": messages, "stream": False,
        "think": bool(think), "keep_alive": "2h",
        "options": {"temperature": float(temperature), "top_p": 0.9,
                    "num_predict": int(num_predict), "num_ctx": nctx},
    }
    req = urllib.request.Request(
        base + "/api/chat", data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=900) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    msg = (body or {}).get("message") or {}
    content = str(msg.get("content") or "")
    # some Ollama builds inline the reasoning as <think>...</think> in content
    content = re.sub(r"<think>.*?</think>\s*", "", content, flags=re.S)
    return content, str((body or {}).get("done_reason") or "")


def _build_chat(messages: list[dict], num_predict: int = 8000,
                think: bool = True) -> str:
    """Generate a whole HTML file, CONTINUING across the length limit.

    The FIRST call reasons (`think`); continuations do not -- the plan is
    already made, they just finish the file, and re-thinking each chunk wastes
    the token budget and can drift the design.
    """
    content, reason = _chat_once(messages, num_predict, think=think, temperature=0.0)
    rounds = 0
    while (rounds < 10 and reason == "length"
           and "</html>" not in content.lower()
           and len(content) < 120000):
        rounds += 1
        tail = content[-1800:]
        convo = list(messages) + [
            {"role": "assistant", "content": content},
            {"role": "user", "content":
             "The file is not finished -- you stopped at the length limit. "
             "Continue from EXACTLY where the text above ends. Output ONLY the "
             "continuation (do not repeat any earlier line, do not restart the "
             "file, no ``` fence, no commentary). Keep going until the file is "
             "complete and ends with </html>.\n\nThe last lines you wrote "
             "were:\n" + tail},
        ]
        more, reason = _chat_once(convo, num_predict, think=False, temperature=0.0)
        # drop an accidental re-emitted overlap with the tail
        more = _dedupe_overlap(content, more)
        if not more.strip():
            break
        content += more
    return content


def _dedupe_overlap(prev: str, cont: str) -> str:
    """If `cont` starts by repeating the end of `prev`, drop the repeat."""
    p = prev[-600:]
    hi = min(len(p), len(cont))
    for n in range(hi, 11, -1):
        if cont[:n] == p[-n:]:
            return cont[n:]
    return cont


def _change_is_expansive(change: str) -> bool:
    ch = str(change or "").strip()
    if not ch:
        return False
    try:
        out, _ = _chat_once([
            {"role": "system", "content":
             "Classify a change request for an existing HTML page or game. "
             "Reply with ONE word only.\n"
             "SURGICAL: a specific small edit -- fix a bug, change a number or "
             "some text, tweak one colour or size, rename something, add one "
             "small control.\n"
             "EXPANSIVE: wants it meaningfully bigger or better -- add features/"
             "levels/content, richer visuals, more animation or detail, a "
             "redesign, \"make it nicer / more interesting / more polished\".\n"
             "Reply SURGICAL or EXPANSIVE."},
            {"role": "user", "content": "CHANGE REQUEST:\n" + ch[:900]},
        ], 16)
        return "EXPANS" in str(out or "").upper()
    except Exception:
        return False


def _edit_is_noop(base: str, edited: str) -> bool:
    """True when `edited` is the working file back again with only cosmetic
    churn -- comments stripped, a few values nudged. Guards the EXPANSIVE edit
    path: a request to enrich the page that returns a ~identical file failed."""
    import difflib
    def _n(s):
        s = re.sub(r"/\*.*?\*/", "", str(s or ""), flags=re.S)
        s = re.sub(r"<!--.*?-->", "", s, flags=re.S)
        return re.sub(r"\s+", " ", s).strip()
    a, b = _n(base), _n(edited)
    if len(a) < 200 or len(b) < 200:
        return False
    if abs(len(b) - len(a)) / max(len(a), 1) > 0.15:
        return False
    return difflib.SequenceMatcher(None, a, b).ratio() > 0.93


def _one_build_call(goal: str, retry_note: str = "", ios9: bool = False) -> str:
    """One direct qwen /api/chat call that returns the whole HTML file."""
    user = "BUILD THIS:\n" + str(goal or "")
    if retry_note:
        user += "\n\n" + retry_note
    return _build_chat([{"role": "system", "content": _build_sys(goal, ios9)},
                        {"role": "user", "content": user}])


def _polish_build_call(goal: str, current_html: str, ios9: bool,
                       change: str = "", expansive: bool = False) -> str:
    """One rewrite pass over an existing file.

    Default (no `change`): critique against the spec and output an improved
    version. With `change`: apply that change and keep everything else. A
    SURGICAL change keeps everything else byte-for-byte; an EXPANSIVE one
    ("make it nicer / add more") is real new work and the file should grow.
    Either way, output the WHOLE file."""
    if change and expansive:
        sysmsg = (_build_sys(goal, ios9)
                  + "\nYou are IMPROVING an existing, working file. The person "
                  "wants it meaningfully better, as described below. This is "
                  "real new work: actually add the visual detail, animation, "
                  "content or behaviour they ask for. The file SHOULD grow. Do "
                  "not just nudge a few CSS values, restyle one button and call "
                  "it done.\n"
                  "But the WORKING LOGIC IS NOT YOURS TO REWRITE: keep the "
                  "event handlers, input handling, game loop, layout math and "
                  "state code EXACTLY as they are (copy them verbatim) and add "
                  "your visuals around them. Rewriting a tap/click handler or a "
                  "loop is how these edits break -- e.g. a `for (var i...)` "
                  "loop whose new listener closes over the shared `i`. If you "
                  "must touch that code, keep its exact structure.\n"
                  "Output the COMPLETE file, from <!doctype html> to </html>, "
                  "nothing else.")
        user = ("WHAT TO IMPROVE:\n" + str(change)
                + "\n\nTHE CURRENT FILE (build on it):\n"
                + str(current_html or "")[:60000])
    elif change:
        sysmsg = (_build_sys(goal, ios9)
                  + "\nYou are EDITING an existing, working file. Apply ONLY "
                  "the requested change. Keep everything else exactly as it is "
                  "-- same look, same structure, same working code. Do not "
                  "rewrite unrelated parts, do not 'improve' things nobody "
                  "asked about. Output the COMPLETE edited file, from "
                  "<!doctype html> to </html>, and nothing else.")
        user = ("THE CHANGE TO MAKE:\n" + str(change)
                + "\n\nTHE CURRENT FILE (edit it):\n"
                + str(current_html or "")[:60000])
    else:
        sysmsg = (_build_sys(goal, ios9)
                  + "\nYou are IMPROVING an existing draft. First, silently "
                  "find everything not good enough for someone to actually use "
                  "and enjoy: broken/unfair gameplay, ugly or cramped layout, "
                  "missing win/lose/restart states, no feedback on a move, "
                  "things that will not work on a phone, placeholder art. THEN "
                  "output the COMPLETE rewritten file. Output only the file.")
        user = ("SPEC:\n" + str(goal or "")
                + "\n\nCURRENT DRAFT (improve it, keep what already works):\n"
                + str(current_html or "")[:60000])
    return _build_chat([{"role": "system", "content": sysmsg},
                        {"role": "user", "content": user}])


def _selftest_enabled() -> bool:
    try:
        from app import messaging as _M
        v = (_M.load_config().get("auto_reply") or {}).get("build_selftest", True)
        return str(v).lower() not in ("0", "false", "off", "no")
    except Exception:
        return True


def _read_text_or_empty(p) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""
    except Exception:
        return ""


def _write_lastgood(slug: str, html: str) -> None:
    if not html or "</html>" not in html.lower():
        return
    try:
        (_BUILD_STATIC / (slug + ".html.lastgood")).write_text(
            html, encoding="utf-8", newline="\n")
    except Exception:
        LOGGER.debug("antons_build: could not update %s.html.lastgood", slug)


def _selftest_failure_key(line: str) -> str:
    s = " ".join(str(line or "").split())
    m = re.match(r"check FAILED:\s*(.+?)(?:\s+--\s+.*)?$", s, re.I)
    if m:
        # a model-authored check: key on its NAME only. The stale ones drifted
        # (e.g. "five distinct levels") and fail identically on base + edit.
        return "check:" + re.sub(r"[^a-z0-9]+", " ", m.group(1).lower()).strip()
    return s.lower()[:160]


def _selftest_failure_keys(result) -> set:
    return {_selftest_failure_key(f)
            for f in ((result or {}).get("failures") or []) if f}


def _repair_hint(fails) -> str:
    blob = " \n ".join(str(f or "").lower() for f in (fails or []))
    hints = []

    _slide = ("could not play the game" in blob
              or "does not actually progress" in blob
              or bool(re.search(r"moves tiles (right|left|up|down)", blob)))
    if _slide and "wrong way" not in blob:
        hints.append(
            "A move that only SLIDES tiles into empty space (no merge) is being "
            "dropped as a no-op -- only merges register, so the game stalls once "
            "no merge is available. In move(): you are almost certainly reading "
            "the board AFTER shifting the tiles (before==after always), or only "
            "setting your `moved` flag inside the merge branch. Snapshot each "
            "tile's row/col (or the whole board) BEFORE moving anything; the "
            "move counts -- spawn a tile and re-render -- if ANY tile changed "
            "cell OR any merge happened.")

    if "wrong way" in blob:
        hints.append(
            "The key->direction or direction->axis mapping is transposed. "
            "ArrowLeft must DECREASE a tile's column; ArrowUp must DECREASE its "
            "row. Horizontal moves iterate rows (one line per row); vertical "
            "moves iterate columns. Write all four directions explicitly and "
            "trace each -- they are not one code path with a flipped sign.")

    if ("not laid out correctly" in blob or "0x0" in blob
            or "at the board corner" in blob or "render at 0" in blob):
        hints.append(
            "Tiles land at 0,0 / the corner because a spawn or pop @keyframes "
            "animates `transform` on the SAME element that `transform: "
            "translate(x,y)` positions -- the keyframe wins and drops the "
            "translate. Move the scale/pop onto a CHILD element, or bake it in "
            "as `translate(x,y) scale(s)`. Guard the cell-size math against <=0.")

    if "does not disappear" in blob or "matched pair" in blob:
        hints.append(
            "A matched/cleared pair is staying visible. Remove it from the model "
            "AND the DOM node on match; check nothing has `animation-fill-mode: "
            "forwards` pinning the old tile on screen.")

    if ("none of your own checks ran" in blob
            or 'script id="selftest"' in blob or "script id=selftest" in blob):
        hints.append(
            "The <script id=\"selftest\"> block is not valid JSON. Re-emit the "
            "WHOLE block from scratch as ONE valid JSON array of {\"name\",\"js\"} "
            "objects. Check every object: a missing closing quote on the last "
            "js string, a missing closing ] on the array, a trailing comma, or "
            "an unescaped \" inside a js string are the usual culprits. Each js "
            "is one line, no comments, no backticks.")

    if "does not respond to any input" in blob:
        hints.append(
            "Nothing is wired to move the board: the arrow keys, WASD, the "
            "on-screen buttons and swipe must ALL call the same move(). Put the "
            "keydown listener on window or document; make sure an early return "
            "or a stuck lock flag is not swallowing every call.")

    return "\n".join("- " + h for h in hints) if hints else ""


_BUILD_LOCKS: dict = {}
_BUILD_LOCKS_GUARD = threading.Lock()
_BUILDS_IN_FLIGHT: dict = {}
_BUILD_IN_FLIGHT_MAX_SECONDS = 3 * 3600   # a build that never cleared is stale


def _build_lock_for(slug: str) -> "threading.Lock":
    with _BUILD_LOCKS_GUARD:
        lk = _BUILD_LOCKS.get(slug)
        if lk is None:
            lk = threading.Lock()
            _BUILD_LOCKS[slug] = lk
        return lk


def _page_tests_chat(messages: list[dict], num_predict: int) -> str:
    """The build model, reasoning first, for app/page_tests.py: it writes the
    page's Playwright tests and judges the ones that fail."""
    return _chat_once(messages, num_predict, think=True, temperature=0.2)[0]


def _selftest_and_repair(slug, path, html, goal, ios9, base_html, complete_fn,
                         change=""):
    """Load the just-written page in a real browser and use it. One repair pass
    if it fails. Revert to `base_html` if the page is hard-broken after that.
    The page's Playwright tests (app/page_tests.py) run next to the self-test
    and their failures go into the same repairs; `change` is the edit just
    made, so its tests cover it too.

    Returns (html, test_line, reverted, test_ok):
      test_ok  True  = the file that is now on disk passed the browser test
               False = it is still broken and there was nothing good to revert to
               None  = the test could not run (harness unavailable / crashed)
    `test_line` is a short "N/M" summary; the caller decides who sees it.
    """
    if not _selftest_enabled():
        return html, "", False, None
    try:
        from app import build_selftest as _bs
    except Exception:
        return html, "", False, None
    if not _bs.available():
        return html, "", False, None
    url = "http://127.0.0.1:8765/static/%s.html" % slug

    _shots = _BUILD_STATIC / ("_selftest_shots_" + slug)
    _base_keys_box: dict = {}

    def _base_fail_keys() -> set:
        if "v" in _base_keys_box:
            return _base_keys_box["v"]
        keys: set = set()
        b = str(base_html or "")
        if b and b != html and complete_fn(b) and not _js_syntax_error(b):
            try:
                path.write_text(b, encoding="utf-8", newline="\n")
                _bres = _bs.run_selftest(url, shots_dir=str(_shots))
                keys = {k for k in _selftest_failure_keys(_bres)
                        if k.startswith("check:")}
                if keys:
                    LOGGER.info("antons_build: %s -- %d selftest check(s) already "
                                "red on the base file: %s", slug, len(keys),
                                "; ".join(sorted(keys))[:240])
            except Exception:
                LOGGER.exception("antons_build: base selftest crashed")
            finally:
                try:
                    path.write_text(html, encoding="utf-8", newline="\n")
                except Exception:
                    LOGGER.exception("antons_build: restore after base test failed")
        _base_keys_box["v"] = keys
        return keys

    def _verified_revert(reason_line, tolerate=frozenset()):
        seen = set()
        for cand in (base_html,
                     _read_text_or_empty(_BUILD_STATIC / (slug + ".html.lastgood"))):
            cand = str(cand or "")
            if not cand or cand in seen:
                continue
            seen.add(cand)
            if not complete_fn(cand) or _js_syntax_error(cand):
                continue
            try:
                path.write_text(cand, encoding="utf-8", newline="\n")
            except Exception:
                LOGGER.exception("antons_build: revert write failed")
                continue
            try:
                rv = _bs.run_selftest(url, shots_dir=str(_shots))
            except Exception:
                LOGGER.exception("antons_build: revert selftest crashed")
                continue
            rv_new = [f for f in ((rv or {}).get("failures") or [])
                      if _selftest_failure_key(f) not in tolerate]
            if rv and rv.get("total") and not rv_new:
                _append_action_ledger({"tool": "antons_build", "phase": "selftest",
                                       "slug": slug, "passed": rv.get("passed"),
                                       "total": rv.get("total"), "ok": True,
                                       "revert": True,
                                       "tolerated_stale": sorted(tolerate)})
                if not rv.get("failures"):
                    _write_lastgood(slug, cand)
                return cand, (_bs.summary_line(rv) or reason_line), True, True
        return None

    # STEP 0: parse the JS before spending 30 s on a browser. A syntax error
    # means the whole page is dead on arrival, so go straight to a repair pass
    # with the exact error.
    _syn = _js_syntax_error(html)
    if _syn:
        LOGGER.info("antons_build: JS syntax error in %s: %s", slug, _syn)
        _append_action_ledger({"tool": "antons_build", "phase": "selftest",
                               "slug": slug, "ok": False, "failures": [_syn]})
        try:
            pulse_set("repairing", slug)
            fx = _extract_html(_polish_build_call(
                goal, html, ios9,
                change=("The JavaScript does not parse: " + _syn
                        + "\nFix that syntax error. Output the COMPLETE file, "
                        "<!doctype html> to </html>, nothing else.")))
            if complete_fn(fx) and not _js_syntax_error(fx) and len(fx) > len(html) * 0.6:
                path.write_text(fx, encoding="utf-8", newline="\n")
                html = fx
        except Exception:
            LOGGER.exception("antons_build: JS-syntax repair failed")
        # still doesn't parse -> a browser test on a dead page tells us nothing;
        # go straight to a VERIFIED revert.
        if _js_syntax_error(html):
            done = _verified_revert("testi: JS nesalasījās, atgriezu veco",
                                    tolerate=_base_fail_keys())
            if done:
                return done
            return html, "testi: JS nesalasījās, nav laba varianta", False, False

    _pt = None
    _pw_spec = None
    try:
        from app import page_tests as _pt_mod
        _pt = _pt_mod
        if _pt.available():
            pulse_set("testing", slug + " (Playwright)")
            _pw_spec = _pt.prepare(slug, goal, html, _page_tests_chat,
                                   url=url, change=change)
            _append_action_ledger({
                "tool": "antons_build", "phase": "playwright_spec", "slug": slug,
                "ok": bool(_pw_spec),
                "spec": (_pw_spec or {}).get("path"),
                "tests": len((_pw_spec or {}).get("titles") or []),
                "vacuous": (_pw_spec or {}).get("vacuous"),
                "static": (_pw_spec or {}).get("static"),
                "weak": (_pw_spec or {}).get("weak"),
                "attempts": (_pw_spec or {}).get("attempts")})
    except Exception:
        LOGGER.exception("antons_build: Playwright tests could not be written")
        _pw_spec = None

    def _with_playwright(r, tag):
        """The self-test verdict with this page's Playwright run folded in."""
        if not _pw_spec or _pt is None or r.get("infra_failed"):
            return r
        try:
            pw = _pt.run(_pw_spec, url, first=(tag == "initial"),
                         chat=_page_tests_chat, html_path=path)
        except Exception:
            LOGGER.exception("antons_build: Playwright run crashed")
            return r
        _append_action_ledger({
            "tool": "antons_build", "phase": "playwright", "slug": slug,
            "tag": tag, "passed": pw.get("passed"), "total": pw.get("total"),
            "ok": not pw.get("failures") and not pw.get("infra"),
            "failures": pw.get("failures"), "rewritten": pw.get("rewritten"),
            "error": pw.get("error")})
        return _pt.merge(r, pw)

    def _selftest_retrying(tag):
        r = _bs.run_selftest(url, shots_dir=str(_shots))
        if r.get("infra_failed"):
            LOGGER.warning("antons_build: %s selftest looks like an infra "
                           "failure (%s) -- retrying once", slug, tag)
            try:
                from app import browser_render as _brx
                _brx._kill_orphans()
            except Exception:
                pass
            time.sleep(3)
            try:
                r = _bs.run_selftest(url, shots_dir=str(_shots))
            except Exception:
                LOGGER.exception("antons_build selftest retry crashed")
        return _with_playwright(r, tag)

    try:
        pulse_set("testing", slug)
        res = _selftest_retrying("initial")
    except Exception:
        LOGGER.exception("antons_build selftest crashed")
        return html, "", False, None
    if res.get("infra_failed") or (res.get("error") and not res.get("total")):
        return html, "", False, None          # harness itself failed; don't judge
    _pl = res.get("play") or {}
    if not res.get("failures"):
        _append_action_ledger({"tool": "antons_build", "phase": "selftest",
                               "slug": slug, "passed": res.get("passed"),
                               "total": res.get("total"), "ok": True,
                               "play_moves": _pl.get("moves"),
                               "play_removed": _pl.get("removed"),
                               "shots": res.get("screenshots")})
        _write_lastgood(slug, html)
        return html, _bs.summary_line(res), False, True

    _base_keys = _base_fail_keys()
    _new_fails = [f for f in res["failures"]
                  if _selftest_failure_key(f) not in _base_keys]
    _stale_fails = [f for f in res["failures"]
                    if _selftest_failure_key(f) in _base_keys]
    if _base_keys and not _new_fails:
        LOGGER.info("antons_build selftest for %s: %s/%s, %d check(s) already red "
                    "on the base, none added by this edit -- shipping", slug,
                    res.get("passed"), res.get("total"), len(_stale_fails))
        _append_action_ledger({"tool": "antons_build", "phase": "selftest",
                               "slug": slug, "passed": res.get("passed"),
                               "total": res.get("total"), "ok": True,
                               "kept_over_stale": True,
                               "stale_failures": _stale_fails})
        return html, ("testi: %s/%s, mans labojums neko nesalauza (%d vecs "
                      "pārbaudes punkts krita jau iepriekš)"
                      % (res.get("passed"), res.get("total"),
                         len(_stale_fails))), False, True

    # up to three repair passes: each time tell the model exactly what a person
    # hit, fix it, re-test in the browser, and feed the NEW failure text forward
    # so the next attempt sees what is still broken.
    fails = _new_fails or list(res["failures"])
    LOGGER.info("antons_build selftest FAILED for %s: %s", slug, " | ".join(fails))
    _append_action_ledger({"tool": "antons_build", "phase": "selftest",
                           "slug": slug, "passed": res.get("passed"),
                           "total": res.get("total"), "ok": False,
                           "failures": fails, "stale_ignored": _stale_fails})
    _best_html, _best_fails = html, list(fails)
    for _attempt in range(3):
        try:
            pulse_set("repairing", slug)
            _diag = _repair_hint(fails)
            _pw_ctx = (_pt.repair_context(_pw_spec)
                       if (_pw_spec and _pt is not None) else "")
            note = ("A real browser just tried to USE this page and hit these "
                    "problems:\n- " + "\n- ".join(fails)
                    + ("\n\nMOST LIKELY CAUSE:\n" + _diag + "\n" if _diag else "")
                    + "\nFix EXACTLY these and nothing else. Change only the "
                    "function(s) responsible -- do not rewrite the file, do not "
                    "restyle or refactor working parts. If a previous fix made "
                    "MORE checks fail, you rewrote too much; go back to changing "
                    "just the broken function."
                    + ("" if _diag else
                       " Common causes: a closure over a `for (var i)` loop "
                       "variable (bind per element); a new tile object built "
                       "without carrying its `id` so the renderer cannot find "
                       "its element; an `animating`/lock flag never reset after "
                       "an exception in the move callback.")
                    + (("\n\n" + _pw_ctx + "\n") if _pw_ctx else "")
                    + " Output the COMPLETE file, <!doctype html> to </html>.")
            fixed = _build_edit.surgical(goal, html, note) or _extract_html(_polish_build_call(goal, html, ios9, change=note))
            if (complete_fn(fixed) and len(fixed) > len(html) * 0.6
                    and not _js_syntax_error(fixed)):
                path.write_text(fixed, encoding="utf-8", newline="\n")
                html = fixed
                res2 = _selftest_retrying("repair %d" % (_attempt + 1))
                if res2.get("infra_failed"):
                    # couldn't get a clean read twice -- don't judge this pass
                    # on garbage failures, keep the real diagnosis, try again
                    LOGGER.warning("antons_build: %s repair %d -- infra failure "
                                   "twice, not scoring this pass", slug, _attempt + 1)
                    continue
                _r2_new = [f for f in (res2.get("failures") or [])
                           if _selftest_failure_key(f) not in _base_keys]
                _append_action_ledger({"tool": "antons_build", "phase": "selftest",
                                       "slug": slug, "passed": res2.get("passed"),
                                       "total": res2.get("total"),
                                       "ok": not _r2_new,
                                       "failures": res2.get("failures"), "repair": True,
                                       "attempt": _attempt + 1})
                if res2.get("total") and not _r2_new:
                    if not res2.get("failures"):
                        _write_lastgood(slug, html)
                    return html, _bs.summary_line(res2), False, True
                if res2.get("total") and len(_r2_new) < len(_best_fails):
                    _best_html, _best_fails = html, list(_r2_new)
                res = res2
                fails = _r2_new or list(res2.get("failures") or [])
        except Exception:
            LOGGER.exception("antons_build selftest repair pass %d failed", _attempt + 1)

    # None of the repair passes came back clean. Put the LEAST-broken version we
    # saw on disk before deciding whether a revert beats it.
    if _best_html != html:
        try:
            path.write_text(_best_html, encoding="utf-8", newline="\n")
            html = _best_html
            res = _selftest_retrying("best")
        except Exception:
            LOGGER.exception("antons_build: restoring best repair version failed")

    done = _verified_revert(_bs.summary_line(res), tolerate=_base_keys)
    if done:
        return done
    # nothing we have actually works. Leave the pre-build version on disk if it
    # is at least complete, and tell the caller plainly: NOT a success, no link.
    _final = base_html if (base_html and complete_fn(base_html)) else html
    try:
        if _final != html:
            path.write_text(_final, encoding="utf-8", newline="\n")
    except Exception:
        LOGGER.exception("antons_build: final fallback write failed")
    return _final, _bs.summary_line(res), False, False


def _run_build(conversation_id: str, goal: str, slug: str, dispatch_ts: float,
               ios9: bool = False, polish: int = 0, change: str = "",
               base_html: str = "") -> None:
    lock = _build_lock_for(slug)
    try:
        with lock:
            _run_build_inner(conversation_id, goal, slug, dispatch_ts,
                             ios9, polish, change, base_html)
    finally:
        with _BUILD_LOCKS_GUARD:
            if (_BUILDS_IN_FLIGHT.get(slug) or (None, None))[1] == dispatch_ts:
                _BUILDS_IN_FLIGHT.pop(slug, None)


def _run_build_inner(conversation_id: str, goal: str, slug: str, dispatch_ts: float,
                     ios9: bool = False, polish: int = 0, change: str = "",
                     base_html: str = "") -> None:
    """Generate an HTML file with qwen, write it under app/static/, then
    message the chat with the URL. Daemon thread; the 'started' reply has
    already gone out. `polish` (0-3) = extra rewrite passes. With `change` +
    `base_html`, EDIT the existing file instead of building from scratch."""
    from app import messaging as _M
    path = _BUILD_STATIC / (slug + ".html")
    _disk_before = ""
    try:
        if path.exists():
            _disk_before = path.read_text(encoding="utf-8")
    except Exception:
        _disk_before = ""
    needs_stats = _BUILD_SYS_DATA[:20] in _build_sys(goal, ios9)
    _complete = lambda h: _html_looks_complete(h, needs_stats)
    html = ""
    err = ""
    passes = 1
    _editing = bool(change and base_html and _complete(base_html))
    try:
        if _editing:
            # EDIT the working file: apply the change, keep the rest.
            _exp = _change_is_expansive(change)
            pulse_set("building", slug + (" (improve)" if _exp else " (edit)"))
            _surgical_html = _build_edit.surgical(goal, base_html, change)
            html = _surgical_html or _extract_html(_polish_build_call(
                goal, base_html, ios9, change=change, expansive=_exp))
            if _surgical_html is None:
                # a redesign/"make it nicer" ask that comes back as the same
                # file (comments stripped, a value nudged) DID NOT do the work.
                _noop = _exp and _complete(html) and _edit_is_noop(base_html, html)
                if not _complete(html) or len(html) < len(base_html) * 0.6 or _noop:
                    _note = (change + (
                        " -- your last version barely changed the file; the person "
                        "will see almost no difference. Do the real work this time: "
                        "substantially richer visuals and animation, and let the "
                        "file grow." if _noop else
                        " -- output the WHOLE file, complete, ending with </html>."
                        + ("" if _exp else " Keep it about the same length.")))
                    html2 = _extract_html(_polish_build_call(
                        goal, base_html, ios9, change=_note, expansive=_exp))
                    passes += 1
                    if _complete(html2) and len(html2) >= len(base_html) * 0.6 and (
                            not _noop or not _edit_is_noop(base_html, html2)):
                        html = html2
                for _pi in range(min(3, max(int(polish or 0), 2 if _exp else 0))):
                    if not _complete(html):
                        break
                    pulse_set("polishing", slug)
                    cand = _extract_html(_polish_build_call(goal, html, ios9))
                    passes += 1
                    if _complete(cand) and len(cand) > len(base_html) * 0.6 and (
                            not ios9 or not _ios9_violations(cand)):
                        html = cand
            _append_action_ledger({"tool": "antons_build", "phase": "edit_done",
                                   "slug": slug, "passes": passes,
                                   "bytes": len(html), "complete": _complete(html)})
            raise StopIteration
        pulse_set("building", slug)
        html = _extract_html(_one_build_call(goal, ios9=ios9))
        bad = _ios9_violations(html) if (ios9 and _complete(html)) else []
        if not _complete(html) or bad:
            note = ("Your previous output was CUT OFF before </html> -- it ran "
                    "past the length limit. Write the SAME thing but far more "
                    "COMPACT: terser code, shorter names, fewer comments, "
                    "smaller inline SVG, no repetition -- and make sure the "
                    "file is COMPLETE, ending with </html>. Output only the "
                    "file, from <!doctype html>.")
            if bad:
                note = ("Your previous file used things an iPhone 4S (iOS 9 "
                        "Safari) CANNOT run, so it renders as a black screen: "
                        + "; ".join(bad) + ". Rewrite the WHOLE file avoiding "
                        "ALL of those -- ES5 only, XMLHttpRequest, rgb()/hex "
                        "colours, float layout, -webkit- prefixes, px sizing. "
                        "Output only the file.")
            html2 = _extract_html(_one_build_call(goal, note, ios9=ios9))
            passes += 1
            if _complete(html2) and (
                    not ios9 or len(_ios9_violations(html2)) < len(bad or [1])):
                html = html2
        # review-and-rewrite passes: only when we already have a usable file
        for _pi in range(max(0, min(3, int(polish or 0)))):
            if not _complete(html):
                break
            pulse_set("polishing", "%s %d/%d" % (slug, _pi + 1, polish))
            try:
                cand = _extract_html(_polish_build_call(goal, html, ios9))
            except Exception:
                LOGGER.exception("antons_build polish pass failed")
                break
            passes += 1
            if _complete(cand) and len(cand) > 400 and (
                    not ios9 or not _ios9_violations(cand)):
                html = cand
        _append_action_ledger({"tool": "antons_build", "phase": "progress",
                               "slug": slug, "passes": passes,
                               "bytes": len(html), "complete": _complete(html)})
    except StopIteration:
        pass                       # the edit path finished above
    except Exception as exc:
        err = str(exc)[:300]
        LOGGER.exception("antons_build generation failed")
    ok = bool(html) and _complete(html)
    test_line = ""
    reverted = False
    test_ok = None
    if ok:
        try:
            _BUILD_STATIC.mkdir(parents=True, exist_ok=True)
            path.write_text(html, encoding="utf-8", newline="\n")
        except Exception as exc:
            ok = False
            err = "write failed: " + str(exc)[:200]
        if ok:
            _fallback = base_html if (_editing and base_html) else _disk_before
            html, test_line, reverted, test_ok = _selftest_and_repair(
                slug, path, html, goal, ios9, _fallback, _complete,
                change=(change if _editing else ""))
            # a page that loads but fails the browser test is NOT a success
            if test_ok is False:
                ok = False
                err = err or ("browser test failed: " + test_line)
    elif html:
        # keep the reject for inspection rather than silently dropping it
        try:
            (_BUILD_STATIC / (slug + ".REJECT.html")).write_text(
                html, encoding="utf-8", newline="\n")
        except Exception:
            pass
        lo = html.lower()
        err = err or ("rejected: len=%d html=%s /html=%s script=%s%s" % (
            len(html), "<html" in lo, "</html>" in lo, "<script" in lo,
            (" needs_stats but no /api/stats" if needs_stats and "/api/stats" not in html else "")))
    _append_action_ledger({"tool": "antons_build", "phase": "finished",
                           "slug": slug, "ok": ok, "bytes": len(html),
                           "passes": passes, "error": err, "test_ok": test_ok,
                           "selftest": test_line, "reverted": reverted})
    pulse_set("built" if ok else "error", slug)

    _dev_tool = bool(re.search(
        r"/api/(opencode/run|messaging/(apply-fix|propose-fix)|git/|"
        r"service/restart|self[_-]?heal|exec|shell|deploy)", html or "", re.I))
    _pub_media = None
    if ok and not reverted and not _dev_tool:
        try:
            _pub_media = _publish_slug_to_media(slug)
            _append_action_ledger({"tool": "antons_build", "phase": "published",
                                   "slug": slug, "ok": bool(_pub_media.get("ok")),
                                   "url": _pub_media.get("url"),
                                   "served": _pub_media.get("served"),
                                   "reason": _pub_media.get("reason")})
        except Exception:
            LOGGER.exception("antons_build: auto-publish to media box failed")

    if not conversation_id:
        return
    pub = "http://127.0.0.1:8765/static/%s.html" % slug
    if _pub_media and _pub_media.get("ok") and _pub_media.get("url"):
        pub = _pub_media["url"]
    else:
        try:
            from app import tailscale_shares
            for sh in (tailscale_shares.list_shares().get("shares") or []):
                if slug in str(sh.get("target") or "") or slug in str(sh.get("path") or ""):
                    pub = sh.get("url") or pub
                    break
        except Exception:
            pass
    try:
        _acfg = _M.load_config().get("auto_reply") or {}
    except Exception:
        _acfg = {}
    _owner_chat = str(conversation_id) in (_acfg.get("assistant_chats") or [])
    _detail = ("\n(%s)" % test_line) if (_owner_chat and test_line) else ""
    _on_tailnet = bool(_pub_media and _pub_media.get("ok"))
    _url_k = (_pub_media or {}).get("url_owner") or pub
    _url_m = (_pub_media or {}).get("url_partner") or pub
    if reverted:
        body = ("Nesanāca kā gribēju, tāpēc atstāju spēli tādu, kāda bija, "
                "strādājošu. Pasaki citādāk, ko mainīt.\nAtver: %s%s" % (pub, _detail))
    elif ok:
        what = "Atjaunināts" if _editing else "Uzbūvēts"
        if _on_tailnet and _editing and _links_sent_recently(conversation_id, slug):
            body = ("%s: %s.html, strādā. Saite tā pati, kas iepriekš.%s"
                    % (what, slug, _detail))
        elif _on_tailnet:
            body = ("%s: %s.html, strādā un ir tīklā.\n"
                    "  Tev:    %s\n"
                    f"  {PARTNER_DAT}: %s\n"
                    f"{PARTNER} atver .6, tu .7: viena kaste, bet katram tīklam cits "
                    "IP. Publiskā internetā nelieku.%s"
                    % (what, slug, _url_k, _url_m, _detail))
        elif _dev_tool:
            body = ("%s: %s.html, strādā uz rīga.\nAtver: %s\n"
                    f"Uz media boxa nelieku: tas ir iekšējs rīks, ne {PARTNER_DAT}.%s"
                    % (what, slug, pub, _detail))
        else:
            body = ("%s: %s.html, strādā lokāli.\nAtver: %s\n"
                    f"Uz media serveri ({PARTNER_DAT}) pagaidām netiku, mēģini "
                    "'publicē to' vēlreiz.%s" % (what, slug, pub, _detail))
    else:
        # broken and nothing to fall back to -- do NOT hand out a link.
        # Not "Mēģināšu vēlreiz": nothing retries a build on its own, so that
        # promised a second attempt that never came (tower-defense, 09-15).
        body = ("Pagaidām nesanāca strādājoša versija (%s). Ja pateiksi, "
                "mēģināšu vēlreiz, vai apraksti citādāk.%s" % (slug, _detail))
    try:
        _M.send_message(provider="bluebubbles_http", target=conversation_id,
                        text=body, real_send=True,
                        metadata={"auto_reply": True, "origin": "bot",
                                  "kind": "antons_build_report", "slug": slug})
    except Exception:
        LOGGER.exception("antons_build: completion message not delivered")


def _links_sent_recently(conversation_id, slug, hours=24) -> bool:
    """This page's links already went to this chat in the last `hours`: a
    sent build report for the same page, to the same chat, carrying a link."""
    try:
        cutoff = time.time() - hours * 3600
        for row in _M_outbox(400):
            md = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
            if (md.get("kind") == "antons_build_report" and md.get("slug") == slug
                    and str(row.get("target") or "") == str(conversation_id or "")
                    and row.get("status") == "sent"
                    and "http" in str(row.get("text") or "")
                    and _M_epoch(row.get("timestamp")) >= cutoff):
                return True
    except Exception:
        LOGGER.debug("build report lookup failed", exc_info=True)
    return False


def _M_outbox(limit):
    from app import messaging as _M
    return _M.outbox(limit=limit)


def _M_epoch(value):
    from app import messaging as _M
    return _M._epoch_seconds(value)


def _tool_antons_build(args: dict, ctx: AgentContext) -> dict:
    from app import messaging as _M
    acfg = _M.load_config().get("auto_reply") or {}
    cid = str(ctx.conversation_id or "")
    is_owner = _owner_turn(ctx, acfg)
    goal = " ".join(str(args.get("goal") or "").split())
    change = ""
    base_html = ""
    args = dict(args)

    _all_specs = _build_specs_load()
    _existing_pages = {
        p.stem for p in _BUILD_STATIC.glob("*.html") if p.is_file()
    }
    _named = str(args.get("slug") or args.get("files") or "").strip()
    _named_slug = _slugify_build_name(_named, "") if _named else ""
    _pslug = _named_slug if _named_slug in _existing_pages else None
    _wants_existing = bool(_pslug)
    if not is_owner:
        try:
            ok_partner = bool(_M._assistant_external_write_allowed(
                acfg, cid, ctx.sender or "")) or (
                _M._normalize_handle(ctx.sender or "") in {
                    _M._normalize_handle(h) for h in (acfg.get("partner_handles") or [])})
        except Exception:
            ok_partner = False
        if not ok_partner:
            raise ToolRefused(f"only {OWNER} or {PARTNER} can ask for a page or "
                              "a game to be built")

    if _wants_existing:
        args["files"] = _pslug
        change = goal or " ".join(str(ctx.turn_text or "").split())
        _chosen_spec = next(
            (s for s in _all_specs.values() if s.get("slug") == _pslug),
            {},
        )
        goal = _chosen_spec.get("goal") or change
        args["target"] = "ios9" if _chosen_spec.get("ios9") else "modern"
        if "polish" not in args:
            args["polish"] = 1
        base_html = ""
        for _p in (_BUILD_STATIC / (_pslug + ".html"),
                   _BUILD_STATIC / (_pslug + ".html.lastgood")):
            try:
                if _p.exists():
                    base_html = _p.read_text(encoding="utf-8", errors="replace")
                    if _html_looks_complete(base_html):
                        break
            except Exception:
                LOGGER.exception("antons_build: could not read base %s", _p)
        if not base_html:
            LOGGER.warning("antons_build: EDIT wanted for %s but no readable "
                           "base -- proceeding as a fresh build", _pslug)

    if len(goal) < 8 and not change:
        return {"ok": False,
                "reason": "goal must describe what to build in a sentence or "
                          "short paragraph -- what it looks like, how it "
                          "behaves, device/layout constraints"}
    target = str(args.get("target") or "modern").strip().lower()
    ios9 = target in ("ios9", "iphone4s", "iphone 4s", "old", "in-case",
                      "incase")
    # "make it perfect / take your time" -> extra review-and-rewrite passes.
    polish = 0
    pv = args.get("polish")
    if isinstance(pv, bool):
        polish = 2 if pv else 0
    elif isinstance(pv, (int, float)):
        polish = max(0, min(3, int(pv)))
    elif isinstance(pv, str) and pv.strip().lower() in ("1", "true", "yes",
                                                        "on", "high", "max"):
        polish = 3 if pv.strip().lower() in ("max", "high") else 2
    slug = _slugify_build_name(args.get("slug") or args.get("files") or "", goal)
    existing = _BUILD_STATIC / (slug + ".html")
    prior = existing.exists()
    dispatch_ts = time.time()
    with _BUILD_LOCKS_GUARD:
        _running = _BUILDS_IN_FLIGHT.get(slug)
        if _running and dispatch_ts - _running[1] > _BUILD_IN_FLIGHT_MAX_SECONDS:
            _running = None
        if not _running:
            _BUILDS_IN_FLIGHT[slug] = (cid, dispatch_ts)
    if _running:
        return {"ok": True, "slug": slug, "started": False,
                "already_building": True,
                "note": ("%s.html is ALREADY being built (started %s); its link "
                         "comes into the chat when it is done. Nothing new was "
                         "started. Tell him it is still on its way -- do not "
                         "call antons_build for it again."
                         % (slug, time.strftime("%H:%M",
                                                time.localtime(_running[1]))))}
    _editing = bool(change and base_html)
    # only record a spec for a genuinely NEW build in a chat that had none --
    # never let a modify (or a bad modify-detection) overwrite the original.
    try:
        if not _editing and not _pslug:
            _build_spec_save(cid, slug, goal, ios9)
        _append_action_ledger({"tool": "antons_build",
                               "phase": "edit" if _editing else "dispatched",
                               "slug": slug, "goal": goal[:200],
                               "change": change[:200], "overwrite": prior,
                               "ios9": ios9, "polish": polish, "by_owner": is_owner,
                               "conversation_id": cid})
        threading.Thread(
            target=_run_build,
            args=(str(ctx.conversation_id or ""), goal, slug, dispatch_ts,
                  ios9, polish, change, base_html),
            name="antons_build_%s" % slug, daemon=True).start()
    except Exception:
        with _BUILD_LOCKS_GUARD:   # nothing started: do not block the next try
            if (_BUILDS_IN_FLIGHT.get(slug) or (None, None))[1] == dispatch_ts:
                _BUILDS_IN_FLIGHT.pop(slug, None)
        raise
    if _editing:
        mins = "a minute or two"
    else:
        mins = "a minute or two" if not polish else ("several minutes (it "
            "reviews and improves its own work %d times)" % polish)
    return {"ok": True, "slug": slug, "started": True, "polish_passes": polish,
            "editing": _editing,
            "target": "app/static/%s.html" % slug,
            "url": "http://127.0.0.1:8765/static/%s.html" % slug,
            "note": ("Building %s.html now in the background -- %s. A message "
                     "with the URL comes into THIS chat when it is done%s. "
                     "Tell him it has started and that you will report back; "
                     "do NOT call antons_build again for the same thing."
                     % (slug, mins,
                        " (this overwrites the current version)" if prior
                        else ""))}


def _tool_browser_check(args: dict, ctx: AgentContext) -> dict:
    """Open a URL in a real browser, exercise it, and report what is on screen.

    Runs the same machinery antons_build uses to check a page after it builds
    it (build_selftest.run_selftest -> a real off-screen Chrome via
    browser_render): loads the page, taps the interactive things, plays a game
    if it is one, screenshots, collects JS errors. Then the screenshot goes
    through the vision model so the description says what is actually visible.
    """
    url = " ".join(str((args or {}).get("url") or "").split())
    if not url:
        return {"ok": False, "error": "no url given"}
    if "://" not in url:
        url = "https://" + url
    try:
        from app import build_selftest as _bs
    except Exception as exc:
        return {"ok": False, "error": "browser check unavailable: %s" % str(exc)[:150]}
    import base64 as _b64, tempfile as _tf
    shots_dir = _tf.mkdtemp(prefix="browser_check_")
    try:
        res = _bs.run_selftest(url, shots_dir=shots_dir)
    except Exception as exc:
        return {"ok": False, "url": url, "error": "check crashed: %s" % str(exc)[:200]}
    shots = [s for s in (res.get("screenshots") or []) if s]
    shot = shots[-1] if shots else None
    visible = ""
    if shot:
        try:
            with open(shot, "rb") as _fh:
                _img = _b64.b64encode(_fh.read()).decode("ascii")
            visible = _describe_image_b64(
                _img,
                "Screenshot of a web page or game. Describe what is on screen "
                "and say whether it looks right or broken -- empty area, content "
                "cut off or overflowing, error text, nothing to interact with.",
                ctx) or ""
        except Exception:
            LOGGER.debug("browser_check: vision describe failed", exc_info=True)
    js_errors = list(res.get("load_errors") or [])
    if res.get("error"):
        js_errors.append(str(res.get("error")))
    play = res.get("play") or {}
    loaded = bool(res.get("total")) or bool(visible) or not (
        res.get("error") and not res.get("total"))
    return {
        "ok": True,
        "url": url,
        "loaded": loaded,
        "visible": visible[:4000],
        "problems": list(res.get("failures") or [])[:8],
        "js_errors": js_errors[:6],
        "checks_passed": res.get("passed"),
        "checks_total": res.get("total"),
        "played_moves": play.get("moves"),
        "screenshot": shot,
    }


def _tool_calculate(args: dict, ctx: AgentContext) -> dict:
    expr = str(args.get("expression") or "").strip()
    if not expr:
        raise ToolRefused("empty expression")
    if not re.fullmatch(r"[0-9+\-*/(). :]+", expr):
        raise ToolRefused("expression contains disallowed characters")
    time_pattern = re.compile(r"\b(\d{1,2}):(\d{2})\b")
    times = time_pattern.findall(expr)
    if times:
        def _to_minutes(m):
            h, mi = int(m.group(1)), int(m.group(2))
            if h > 23 or mi > 59:
                raise ToolRefused(f"invalid time {m.group(0)}")
            return str(h * 60 + mi)
        expr = time_pattern.sub(_to_minutes, expr)
    tokens = re.findall(r"\d+\.?\d*|[+\-*/()]", expr)
    if not tokens:
        raise ToolRefused("no tokens")
    pos = [0]
    def peek():
        return tokens[pos[0]] if pos[0] < len(tokens) else None
    def take():
        t = tokens[pos[0]]
        pos[0] += 1
        return t
    def parse_expr():
        val = parse_term()
        while peek() in ("+", "-"):
            op = take()
            rhs = parse_term()
            val = val + rhs if op == "+" else val - rhs
        return val
    def parse_term():
        val = parse_factor()
        while peek() in ("*", "/"):
            op = take()
            rhs = parse_factor()
            if op == "/":
                if rhs == 0:
                    raise ToolRefused("division by zero")
                val = val / rhs
            else:
                val = val * rhs
        return val
    def parse_factor():
        t = peek()
        if t == "(":
            take()
            val = parse_expr()
            if peek() != ")":
                raise ToolRefused("missing closing parenthesis")
            take()
            return val
        if t == "-":
            take()
            return -parse_factor()
        if t is None:
            raise ToolRefused("unexpected end of expression")
        take()
        return float(t)
    result = parse_expr()
    if pos[0] != len(tokens):
        raise ToolRefused("trailing tokens")
    if times:
        total_min = int(round(result))
        sign = "-" if total_min < 0 else ""
        abs_min = abs(total_min)
        h = abs_min // 60
        mi = abs_min % 60
        return {"result": f"{sign}{h}:{mi:02d}", "minutes": total_min}
    if result == int(result) and abs(result) < 1e15:
        return {"result": str(int(result))}
    # Round to 10 decimal places to kill float noise like 0.30000000000000004
    rounded = round(result, 10)
    if rounded == int(rounded) and abs(rounded) < 1e15:
        return {"result": str(int(rounded))}
    s = str(rounded)
    return {"result": s}


def _tool_find_my_device(args: dict, ctx: AgentContext) -> dict:
    from app import location as _loc
    from app import messaging as _msg
    from app import rig_agent as _rig
    if not _rig.owner_allowed(ctx.sender, (_msg.load_config().get("auto_reply") or {})):
        raise ToolRefused("find_my_device is owner-only")
    try:
        devices = _loc.find_devices(geocode=_loc.nominatim_geocode)
    except _loc.SessionExpired as exc:
        return {"devices": [], "login_needed": True, "note": str(exc)}
    return {"devices": devices}


def _icloud_reminder_handler(args, ctx):
    if not _his_turn(ctx) and not _allowed_sender(ctx):
        return {"ok": False, "error_type": "authorization_required",
                "action_state": "DENIED",
                "message": f"only {OWNER}' own messages or an allowed contact can "
                           "create an iCloud reminder; this one was written by "
                           "someone else"}
    from datetime import datetime
    from app import location as loc_mod

    title = args.get("title", "")
    if not title:
        return {"ok": False, "error": "title is required"}
    from app import messaging as _msg
    _cfg = _msg.load_config()
    _sender = ctx.sender or ""
    if _his_turn(ctx):
        _name = OWNER
    else:
        _name = _msg._contact_name(_cfg.get("auto_reply") or {}, _sender) or _sender
    desc = "Lūdzis: " + _name if _name else ""
    due = args.get("due")
    proximity = args.get("proximity")
    list_title = args.get("list", "Reminders")

    d = ".secrets/icloud"
    acct_path = os.path.join(d, "account.json")
    if not os.path.exists(acct_path):
        return {"ok": False, "error": "no iCloud account configured"}
    with open(acct_path, encoding="utf-8") as f:
        acct = json.load(f)
    apple_id = acct["apple_id"]
    client_id = acct["client_id"]

    try:
        svc = loc_mod._default_factory(apple_id, client_id, d)
    except Exception as exc:
        return {"ok": False, "error": "iCloud session failed: %s" % str(exc)[:200]}

    if getattr(svc, "requires_2fa", False):
        return {"ok": False, "error": "iCloud session requires a new login"}

    try:
        lists = svc.reminders.lists()
    except Exception as exc:
        return {"ok": False, "error": "could not list reminders: %s" % str(exc)[:200]}

    target = None
    for lst in lists:
        if lst.title == list_title:
            target = lst
            break
    if target is None:
        return {"ok": False, "error": "reminder list '%s' not found" % list_title}

    due_date = None
    tz_name = None
    if due:
        try:
            due_date = datetime.fromisoformat(due)
        except ValueError:
            return {"ok": False, "error": "invalid due date, use ISO format"}
        if due_date.tzinfo is None:
            from app import reminders as _rem
            from zoneinfo import ZoneInfo
            tz_name, _ = _rem.local_timezone()
            due_date = due_date.replace(tzinfo=ZoneInfo(tz_name))

    try:
        reminder = svc.reminders.create(
            target.id, title, desc=desc, due_date=due_date, time_zone=tz_name
        )
    except Exception as exc:
        return {"ok": False, "error": "create failed: %s" % str(exc)[:200]}

    if proximity:
        from pyicloud.services.reminders.models.domain import Proximity
        prox = Proximity.ARRIVING if proximity == "arrive" else Proximity.LEAVING
        try:
            fix = loc_mod.fetch_icloud(session_dir=d)
        except Exception as exc:
            return {"ok": True, "reminder_id": reminder.id, "location": False, "note": "reminder created, could not get location: %s" % str(exc)[:200]}
        if fix is None:
            return {"ok": True, "reminder_id": reminder.id, "location": False, "note": "reminder created, no Find My location available"}
        try:
            svc.reminders.add_location_trigger(
                reminder,
                title="",
                address="",
                latitude=fix["lat"],
                longitude=fix["lon"],
                radius=100.0,
                proximity=prox,
            )
        except Exception as exc:
            return {"ok": True, "reminder_id": reminder.id, "location": False, "note": "reminder created, location trigger failed: %s" % str(exc)[:200]}

    return {"ok": True, "reminder_id": reminder.id}


def build_default_registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(Tool(
        "icloud_reminder",
        f"Create a reminder in {OWNER}' iCloud Reminders. Supports time-based and location-based (arrive/leave) reminders. Use this tool ONLY when the reminder is for {OWNER} (the owner) himself — it sends a notification to his iPhone, because a chat message from the assistant does not notify him (it is sent in his own name). When the reminder is for someone else in the chat, use the regular chat reminder instead, because a chat message DOES notify them.",
        {"type": "object", "properties": {
            "title": {"type": "string", "description": "Reminder title"},
            "due": {"type": "string", "description": f"ISO datetime. Pulksteņa laiku (piem. 18:00) raksti tā, kā {OWNER} to domā valstī, kurā viņš ir, BEZ laika joslas nobīdes (piem. 2026-10-12T18:00:00) — rīks pats pieliks viņa laika joslu. Ja laiks ir relatīvs ('pēc N minūtēm/stundām'), aprēķini precīzo brīdi no pašreizējā pulksteņa un raksti AR nobīdi no tā pulksteņa, ko izmantoji."},
            "proximity": {"type": "string", "enum": ["arrive", "leave"], "description": "Location trigger type"},
            "list": {"type": "string", "description": "Reminder list name (default Reminders)"}
        }, "required": ["title"]},
        _icloud_reminder_handler, EXTERNAL_WRITE, 30.0, True,
        "location._default_factory + pyicloud reminders"),
    )
    reg.register(Tool(
        "find_my_device",
        "List the owner's devices from Find My. Read-only. Battery and 'online' are what Apple last heard from the device — for a sleeping or offline Mac that can be weeks old; 'online' stays on while the Mac sleeps and does NOT mean it is awake. place, fix_age_min and map_link are where Find My sees the device right now. If those are empty, last_seen_place, last_seen_age_min and last_seen_map_link say where and how many minutes ago the rig last saw it; if those are also empty, the rig has never seen it. Use when the owner asks where one of his devices is.",
        {"type": "object", "properties": {}},
        _tool_find_my_device, READ_ONLY, 30.0, True,
        "location.find_devices + location.nominatim_geocode"),
    )
    reg.register(Tool(
        "calculate",
        "Compute arithmetic or clock-time differences. Every number in an answer that comes from a calculation (time remaining, differences, sums, percentages) must be computed with this tool, not in the head. Accepts numbers, + - * /, parentheses, and HH:MM clock times. Example: '18:22 - 16:57' returns 1:25 (85 minutes).",
        {"type": "object", "properties": {
            "expression": {"type": "string", "minLength": 1, "maxLength": 200},
        }, "required": ["expression"]},
        _tool_calculate, READ_ONLY, 5.0, False,
        "local arithmetic parser (no eval)"),
    )
    reg.register(Tool(
        "browser_check",
        "Open a URL in a real browser and report what is actually on screen. Runs the same check antons_build does after building a page: loads it, taps the interactive parts, plays a game if it is one, screenshots it, collects JS errors, and describes what is visible. Use whenever you are asked to check, test, look at or \"pa\u010deko\" a page or a game -- your own (127.0.0.1:8765/static/... or this machine's tailnet name) or any URL. Never say you cannot open a link; open it with this. Returns loaded, a description of what is visible, problems found, JS errors, and the screenshot path.",
        {"type": "object", "properties": {
            "url": {"type": "string", "minLength": 4, "maxLength": 500},
        }, "required": ["url"]},
        _tool_browser_check, READ_ONLY, 130.0, False,
        "build_selftest.run_selftest + browser_render + local vision"),
    )
    reg.register(Tool(
        "web_search",
        "Search the public web. Returns candidate sources, and for the top result also the beginning of its actual page text, so a fact that lives on the page rather than in the snippet is already in front of you. Use for one specific current external fact, version, status, date, or to find a source URL. Pass 2-5 KEYWORDS, never a sentence or a question: \"lr1 programma\" returns the schedule page itself, while \"cikos Latvijas Radio atskano himnu\" returns unrelated pages. Name the thing and what you want about it; drop question words, verbs and today's date. Search in the language the ANSWER lives in, not the language of the question: Latvian for Latvian institutions, schedules, shops and local news; ENGLISH for international science, technology, world news and products. A Latvian query for an international story finds nothing, because the sources are English. Do not use for broad synthesis, private/local data, or a second equivalent query after useful results; use web_read for a chosen URL and research for multi-source investigation.",
        {"type": "object", "properties": {
            "query": {"type": "string", "minLength": 2, "maxLength": 500},
            "limit": {"type": "integer", "minimum": 1, "maximum": 8},
        }, "required": ["query"]},
        _tool_web_search, READ_ONLY, 45.0, False, "coder.web_search / messaging public research"),
    )
    reg.register(Tool(
        "street_imagery",
        "Look at street-level photographs of a real-world place and describe what is physically there. Use this for questions about appearance and layout: what a road, junction, building frontage or entrance looks like, whether there is visible parking, how wide the street is, what signs or landmarks are on it, whether a driveway or doorway is easy to miss. Pass latitude and longitude when you have them, or a street address as place and it will be geocoded. The default action inspect_location finds a few useful nearby frames and returns what the vision model sees in each. IMPORTANT: this imagery is historical -- often several years old -- so read captured_at and never describe it as the current or live view; coverage is thin outside cities and a point with no coverage returns no images rather than a guess. Do not use this for opening hours, reviews, prices, news or anything written down rather than seen (use web_search), and do not use it for an address, a route or a distance (use geocode_place or map data).",
        {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["inspect_location", "search_nearby", "get_image", "get_sequence"]},
            "latitude": {"type": "number", "minimum": -90, "maximum": 90},
            "longitude": {"type": "number", "minimum": -180, "maximum": 180},
            "place": {"type": "string", "maxLength": 240},
            "radius_m": {"type": "integer", "minimum": 5, "maximum": 500},
            "max_images": {"type": "integer", "minimum": 1, "maximum": 6},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20},
            "image_id": {"type": "string", "maxLength": 64},
            "provider": {"type": "string", "enum": ["mapillary", "kartaview"]},
            "before": {"type": "integer", "minimum": 0, "maximum": 5},
            "after": {"type": "integer", "minimum": 0, "maximum": 5},
            "question": {"type": "string", "maxLength": 400},
            "detail": {"type": "boolean"}
        }},
        _tool_street_imagery, READ_ONLY, 90.0, False, "street_imagery providers + local vision"),
    )
    reg.register(Tool(
        "geocode_place",
        "Turn a place name or street address into coordinates using OpenStreetMap. Use before street_imagery or any other tool that needs a latitude and longitude, and when the user asks where something is. Do not use for what a place looks like (street_imagery) or for facts about it (web_search).",
        {"type": "object", "properties": {
            "place": {"type": "string", "minLength": 2, "maxLength": 240},
        }, "required": ["place"]},
        _tool_geocode_place, READ_ONLY, 30.0, False, "date_flow.geocode / Nominatim"),
    )
    reg.register(Tool(
        "web_read",
        "Read one known public webpage, PDF, or JSON API URL. Returns the page title and text, its on-site links, and — for a JavaScript app that renders no text on its own — `api_responses`, the JSON the page loaded from its own API (prices, schedules, results: the real data). Use after web_search finds a promising source, or when the user gives a URL that matters. For long pages request enough characters to reach a table below the introduction. When the page names the answer but its links point at the page that holds it (a schedule, timetable, price list), read that link next. If a page comes back nearly empty, retry once with `render:true`; if the data only appears after pressing a control, pass `click` with that control's visible text (e.g. \"See prices\"). Do not re-read the same URL unchanged, read private/local URLs, or keep opening pages once the answer is in hand.",
        {"type": "object", "properties": {
            "url": {"type": "string", "minLength": 8, "maxLength": 1200},
            "max_chars": {"type": "integer", "minimum": 300, "maximum": 20000},
            "query": {"type": "string", "maxLength": 500},
            "render": {"type": "boolean",
                       "description": "force a full browser render (runs the page's JavaScript)"},
            "click": {"type": "string", "maxLength": 60,
                      "description": "visible text of one button/link/tab to press after the page loads, to reveal data"},
        }, "required": ["url"]},
        _tool_web_read, READ_ONLY, 60.0, False, "messaging.read_link + browser_render"),
    )
    reg.register(Tool(
        "research",
        "Run a compact multi-source public research pass that already searches and reads top pages. Use when the user asks to research, investigate, compare, synthesize tradeoffs, or answer a broad uncertain question needing multiple sources. Do not use for one latest fact/version/status, a known URL, or immediately follow with web_search/web_read unless research exposed a specific missing or conflicting point.",
        {"type": "object", "properties": {
            "question": {"type": "string", "minLength": 4, "maxLength": 700},
            "limit": {"type": "integer", "minimum": 2, "maximum": 5},
        }, "required": ["question"]},
        _tool_research, READ_ONLY, 35.0, False, "messaging._research_reply components"),
    )
    reg.register(Tool(
        "search_personal_memory",
        "Search saved durable personal facts, preferences, decisions, and standing notes. Use when the user asks what you remember, what they decided/prefer, or needs saved personal context. Do not use for verbatim past messages, who said what, public facts, files, or indexed documents.",
        {"type": "object", "properties": {
            "query": {"type": "string", "minLength": 2, "maxLength": 500},
            "limit": {"type": "integer", "minimum": 1, "maximum": 10},
        }, "required": ["query"]},
        _tool_search_personal_memory, READ_ONLY, 5.0, True, "ai_stack.search_memories"),
    )
    reg.register(Tool(
        "search_chat_history",
        "Search actual prior message text from local chats. Use for what someone said, what was discussed, sender/date/person-specific chat recall, or recent conversation references before asking the user to repeat known context. In trusted contact chats, prefer scope=current_conversation unless the user clearly asks across chats. Do not use for stable saved preferences, public facts, local files, or general knowledge-base/reference material.",
        {"type": "object", "properties": {
            "query": {"type": "string", "minLength": 2, "maxLength": 500},
            "person": {"type": "string", "maxLength": 120},
            "scope": {"type": "string", "enum": ["all", "current_conversation"]},
            "since_days": {"type": "integer", "minimum": 1, "maximum": 365},
            "limit": {"type": "integer", "minimum": 1, "maximum": 12},
        }, "required": ["query"]},
        _tool_search_chat_history, READ_ONLY, 8.0, True, "messaging inbox/outbox history"),
    )
    reg.register(Tool(
        "search_knowledge_base",
        "Search indexed local reference/document corpora. Use when the answer likely lives in local RAG material, notes, reference docs, or named corpora. Do not use for live/current web facts, verbatim chat logs, personal preferences, or discovering file paths.",
        {"type": "object", "properties": {
            "query": {"type": "string", "minLength": 2, "maxLength": 500},
            "corpora": {"type": "array", "items": {"type": "string", "enum": ["facts", "bible_lv", "bible_kjv", "own_msgs"]}, "maxItems": 4},
            "limit": {"type": "integer", "minimum": 1, "maximum": 8},
        }, "required": ["query"]},
        _tool_search_knowledge_base, READ_ONLY, 20.0, True, "app.rag.search"),
    )
    reg.register(Tool(
        "search_latvian_grammar",
        "Analyze Latvian text or local Latvian grammar coverage. Use for Latvian grammar explanation, validator behavior, or local grammar-engine coverage questions. Do not use for normal translation/rewriting, outgoing-message correction control, public Latvian NLP research, or unrelated KB search.",
        {"type": "object", "properties": {
            "query": {"type": "string", "maxLength": 300},
            "text": {"type": "string", "maxLength": 1000},
            "context": {"type": "string", "maxLength": 1000},
        }, "required": []},
        _tool_search_latvian_grammar, READ_ONLY, 5.0, False, "lv_grammar/lv_engine read-only lookup"),
    )
    reg.register(Tool(
        "search_files",
        "Search allowed local filenames/paths only. Use once with distinctive name/path terms to find an existing local resource. Do not answer from file contents, search chats/memory/KB, or repeat reordered query terms after a plausible path is returned; use inspect_file next.",
        {"type": "object", "properties": {
            "query": {"type": "string", "minLength": 2, "maxLength": 240},
            "root": {"type": "string", "enum": ["project", "ai", "downloads", "all"]},
            "limit": {"type": "integer", "minimum": 1, "maximum": 30},
        }, "required": ["query"]},
        _tool_search_files, READ_ONLY, 10.0, True, "file/resource lookup"),
    )
    reg.register(Tool(
        "inspect_file",
        "Inspect metadata and optional small preview for one known allowed local path. Use after search_files or when the user provides a concrete path. Do not use for broad discovery, protected paths, chat history, or multiple files unless comparison is required.",
        {"type": "object", "properties": {
            "path": {"type": "string", "minLength": 1, "maxLength": 1200},
            "include_preview": {"type": "boolean"},
        }, "required": ["path"]},
        _tool_inspect_file, READ_ONLY, 5.0, True, "file metadata inspection"),
    )
    reg.register(Tool(
        "system_status",
        "Read current local machine/model runtime status: CPU, RAM, disk, GPU summary, and resident Ollama models. Use when the user asks about this machine's current state. Do not use for ordinary conversation, public tech news, or external service status.",
        {"type": "object", "properties": {}, "required": []},
        _tool_system_status, READ_ONLY, 6.0, False, "dashboard status/capability probes"),
    )
    reg.register(Tool(
        "rig_lookup",
        "Live answers from this rig and the home services, picked by name: gpu, cpu, memory, disks, temps, power, models, services, status, uptime (this machine, owner only), calendar (his calendar, owner only), kino (what is on in the cinemas), watch (a film or show suggestion from the person's own Jellyfin library), voicenote (the exact text of the last voice message you sent in THIS chat). The person's message is passed along. Use it instead of guessing a number or saying you cannot check; if it says not permitted, say so plainly.",
        {"type": "object", "properties": {
            "skill": {"type": "string", "enum": list(RIG_LOOKUP_SKILLS)},
        }, "required": ["skill"]},
        _tool_rig_lookup, READ_ONLY, 20.0, True, "rig_agent skill router"),
    )
    reg.register(Tool(
        "disk_report",
        "Read-only report of what is using the disk and what could give space back. Use when the user asks how full the drives are, why space keeps disappearing, whether there is room for a download, or what to clean up. It returns free space per drive and lists the orphaned Ollama model blobs (files on disk that no manifest references) with their combined size in GB, so the answer to \"what can I delete?\" is concrete. It only reports; it changes nothing.",
        {"type": "object", "properties": {}, "required": []},
        _tool_disk_report, READ_ONLY, 8.0, False, "local disk + Ollama blob audit (read-only)"),
    )
    reg.register(Tool(
        "transcribe_audio",
        "Return the transcript for one available voice/audio attachment. Use when the user asks what an audio or voice note says, or when the latest request depends on audio content. If no cached/live transcript exists, the tool returns a structured failure; do not infer audio content yourself.",
        {"type": "object", "properties": {
            "attachment_id": {"type": "string", "maxLength": 120},
            "language": {"type": "string", "maxLength": 32},
        }, "required": []},
        _tool_transcribe_audio, READ_ONLY, 90.0, False,
        "BlueBubbles audio transcript/STT attachment tool", "audio"),
    )
    reg.register(Tool(
        "read_document",
        "Read text from one available PDF attachment locally. Use when the latest request depends on a PDF file or asks what a PDF says. It returns extracted source text and page counts; do not claim to have read a scanned/image-only PDF when extraction fails.",
        {"type": "object", "properties": {
            "attachment_id": {"type": "string", "maxLength": 120},
            "max_chars": {"type": "integer", "minimum": 500, "maximum": 30000},
            "query": {"type": "string", "maxLength": 500},
        }, "required": []},
        _tool_read_document, READ_ONLY, 30.0, False,
        "BlueBubbles local PDF attachment reader", "file"),
    )
    reg.register(Tool(
        "inspect_audio",
        "Inspect what an audio attachment actually contains: whether it is speech, music, noise or silence, its real duration/codec/channels, measured audio characteristics (loudness, tempo estimate, spectral shape), and — for speech — the transcript. Use when the user asks what a sound, song, recording or voice note IS or is LIKE, or plays something and asks for your impression. Use transcribe_audio instead when only the spoken words are needed. It cannot identify a song, artist or genre.",
        {"type": "object", "properties": {
            "attachment_id": {"type": "string", "maxLength": 120},
            "question": {"type": "string", "maxLength": 500},
        }, "required": []},
        _tool_inspect_audio, READ_ONLY, 120.0, False,
        "local audio analysis (ffprobe/DSP) + cached STT", "audio"),
    )
    reg.register(Tool(
        "inspect_image",
        "Inspect one available image/photo attachment with the configured vision model. Use when the user asks about a photo, screenshot, visible text, or visual details. Do not use for non-image attachments or images already fully described in text.",
        {"type": "object", "properties": {
            "attachment_id": {"type": "string", "maxLength": 120},
            "question": {"type": "string", "maxLength": 500},
        }, "required": []},
        _tool_inspect_image, READ_ONLY, 120.0, False,
        "BlueBubbles image vision attachment tool", "image"),
    )
    reg.register(Tool(
        "inspect_video",
        "Inspect metadata/known transcript for one available video attachment. Frame extraction is currently limited, so use this to learn what is actually available and to avoid hallucinating video contents.",
        {"type": "object", "properties": {
            "attachment_id": {"type": "string", "maxLength": 120},
            "question": {"type": "string", "maxLength": 500},
        }, "required": []},
        _tool_inspect_video, READ_ONLY, 10.0, False,
        "BlueBubbles video limitation/metadata attachment tool", "video"),
    )
    reg.register(Tool(
        "search_goals",
        "Search the user's persistent goals/projects by semantic terms. Use when the current turn asks about goals, progress, priorities, or a likely saved intention. Do not use for reminders, calendar events, messaging actions, public facts, or unrelated personal memory.",
        {"type": "object", "properties": {
            "query": {"type": "string", "minLength": 2, "maxLength": 500},
            "status": {"type": "string", "maxLength": 40},
            "limit": {"type": "integer", "minimum": 1, "maximum": 12},
        }, "required": ["query"]},
        _tool_search_goals, READ_ONLY, 5.0, True,
        "goals.search_goals"),
    )
    reg.register(Tool(
        "list_goals",
        "List saved goals/projects, usually active ones. Use when the user asks what goals are currently open or what they should focus on. Do not recite goals unprompted.",
        {"type": "object", "properties": {
            "status": {"type": "string", "maxLength": 40},
            "limit": {"type": "integer", "minimum": 1, "maximum": 30},
        }, "required": []},
        _tool_list_goals, READ_ONLY, 5.0, True,
        "goals.list_goals"),
    )
    reg.register(Tool(
        "add_goal",
        "Create one persistent goal/project once the CONVERSATION has asked for one. LOOK FIRST: call list_goals or search_goals before writing, and if a goal about the same thing already exists, use update_goal on THAT one instead of creating a near-duplicate — \"Autosports\" and \"Start new hobby in auto sport\" are now two records of one intention (2026-08-26). If he asks for SEVERAL goals in one message (\"the rest, make up your own names and deadlines\"), call this tool once per goal rather than packing them all into one description. The request and the goal itself usually arrive in different messages — \"pievieno jaunu mērķi\", then \"gribu iemācīties spēlēt ģitāru\" — so do not wait for a single turn to contain both; requiring that made the tool unusable and the thread drifted into small talk (2026-08-25). SAVE AS SOON AS YOU CAN NAME IT. \"gribu iemācīties spēlēt ģitāru\" is already a goal — call this tool with that as the title. Everything else is optional and can be filled in later with update_goal, so do NOT interrogate him first: asked for a goal four turns running it kept requesting one more detail and never wrote anything down (2026-08-25). At most ONE short question, and only if you cannot name the goal at all. Whatever he does give you goes in as you go: his examples and notes in `description`, a deadline in `target_date`, a rough timeframe in `horizon`. Tell him what you saved, in one line. Still not for reminders, alarms, calendar events, messages to send, or vague 'I should maybe' thoughts: a reminder at a time is a calendar_create entry, not a goal. Only his own messages can write goals.",
        {"type": "object", "properties": {
            "title": {"type": "string", "minLength": 1, "maxLength": 240},
            "description": {"type": "string", "maxLength": 1000},
            "horizon": {"type": "string", "enum": ["now", "week", "month", "quarter", "year", "life"]},
            "area": {"type": "string", "maxLength": 80},
            "parent_goal_id": {"type": "integer", "minimum": 1},
            "target_date": {"type": "string", "maxLength": 80},
            "importance": {"type": "string", "enum": ["small", "normal", "important", "major"]},
            "contexts": {"type": "array", "items": {"type": "string", "maxLength": 40}, "maxItems": 12},
            "resurface_mode": {"type": "string", "enum": ["contextual", "manual_only"]},
            "kind": {"type": "string", "enum": ["goal", "project"]},
            "done_definition": {"type": "string", "maxLength": 1000},
        }, "required": ["title"]},
        _tool_add_goal, LOCAL_WRITE, 5.0, True,
        "goals.create"),
    )
    reg.register(Tool(
        "update_goal",
        "Record progress or revise an existing goal. Use it whenever he adds DETAIL to a goal that is being discussed — an example, a song, a constraint, a deadline, a target — not only when he says the word \"update\": right after saving \"Iemācīties spēlēt ģitāru\" he named the song he wants to learn and it was answered with \"Noted\" and thrown away (2026-08-25). Put what he tells you in `description` and any deadline in `target_date`. Saying it is noted without calling this tool means it is NOT noted.",
        {"type": "object", "properties": {
            "goal_id": {"type": "integer", "minimum": 1},
            "progress_note": {"type": "string", "maxLength": 1000},
            "progress": {"type": "integer", "minimum": 0, "maximum": 100},
            "title": {"type": "string", "maxLength": 240},
            "description": {"type": "string", "maxLength": 1000},
            "importance": {"type": "string", "enum": ["small", "normal", "important", "major"]},
            "contexts": {"type": "array", "items": {"type": "string", "maxLength": 40}, "maxItems": 12},
            "resurface_mode": {"type": "string", "enum": ["contextual", "manual_only"]},
            "target_date": {"type": "string", "maxLength": 80},
        }, "required": ["goal_id"]},
        _tool_update_goal, LOCAL_WRITE, 5.0, True,
        "goals.progress / goals.update"),
    )
    reg.register(Tool(
        "complete_goal",
        "Mark an existing goal completed when he says it is done, in whatever words or language he uses. Never on your own guess: finishing something takes him saying so. Only his own messages can change his goals.",
        {"type": "object", "properties": {
            "goal_id": {"type": "integer", "minimum": 1},
            "note": {"type": "string", "maxLength": 1000},
        }, "required": ["goal_id"]},
        _tool_complete_goal, LOCAL_WRITE, 5.0, True,
        "goals.complete"),
    )
    reg.register(Tool(
        "defer_goal",
        "Pause or snooze an existing goal when he asks to put it off or come back to it later, in whatever words or language he uses. Never on your own guess. Only his own messages can change his goals.",
        {"type": "object", "properties": {
            "goal_id": {"type": "integer", "minimum": 1},
            "until": {"type": "string", "maxLength": 120},
            "note": {"type": "string", "maxLength": 1000},
        }, "required": ["goal_id"]},
        _tool_defer_goal, LOCAL_WRITE, 5.0, True,
        "goals.defer"),
    )
    reg.register(Tool(
        "cancel_goal",
        "Cancel or drop an existing goal when he says he is no longer doing it, in whatever words or language he uses. Never on your own guess: dropping something takes him saying so. Only his own messages can change his goals.",
        {"type": "object", "properties": {
            "goal_id": {"type": "integer", "minimum": 1},
            "note": {"type": "string", "maxLength": 1000},
        }, "required": ["goal_id"]},
        _tool_cancel_goal, LOCAL_WRITE, 5.0, True,
        "goals.cancel"),
    )
    reg.register(Tool(
        "note_goal_surfaced",
        "Record that the assistant explicitly mentioned one contextual goal in the final answer. Use only after a Relevant Active Goals prompt item will actually be mentioned.",
        {"type": "object", "properties": {
            "goal_id": {"type": "integer", "minimum": 1},
        }, "required": ["goal_id"]},
        _tool_note_goal_surfaced, LOCAL_WRITE, 5.0, True,
        "goals.mark_surfaced"),
    )
    reg.register(Tool(
        "contact_lookup",
        "Resolve a configured personal contact or the current trusted conversation to a contact record and messaging conversation id. Use before send_message when the user names a person or says this/current chat. Do not guess unconfigured recipients.",
        {"type": "object", "properties": {
            "query": {"type": "string", "maxLength": 160},
        }, "required": []},
        _tool_contact_lookup, READ_ONLY, 5.0, True,
        "personal-assistant configured contacts"),
    )
    reg.register(Tool(
        "calendar_search",
        "Read the assistant calendar over a natural or ISO time range. Use before calendar_create when conflicts matter, or when the user asks what is on the calendar. Dates are interpreted in the configured local timezone unless a timezone is supplied.",
        {"type": "object", "properties": {
            "when": {"type": "string", "maxLength": 240},
            "start": {"type": "string", "maxLength": 120},
            "end": {"type": "string", "maxLength": 120},
            "query": {"type": "string", "maxLength": 160},
            "timezone": {"type": "string", "maxLength": 80},
        }, "required": []},
        _tool_calendar_search, READ_ONLY, 8.0, True,
        "date_plans/rig calendar read"),
    )
    reg.register(Tool(
        "calendar_create",
        f"Put one event in the calendar {OWNER} sees on his phone (the Anton calendar). You decide when a message asks for it: to add, schedule or book something. The entry shows in his calendar but does not ring or message him by itself, so for 'remind me' use set_reminder, and after this tool say you put it in the calendar, never that you will remind him. If he did not say when, ask him when before calling this. Only his own messages can add events; one written by someone else is refused. Use calendar_search first when he asks whether a time is free.",
        {"type": "object", "properties": {
            "title": {"type": "string", "minLength": 1, "maxLength": 240},
            "start": {"type": "string", "minLength": 1, "maxLength": 160},
            "end": {"type": "string", "maxLength": 160},
            "duration_minutes": {"type": "integer", "minimum": 1, "maximum": 1440},
            "timezone": {"type": "string", "maxLength": 80},
            "location": {"type": "string", "maxLength": 240},
            "notes": {"type": "string", "maxLength": 1000},
            "allow_conflicts": {"type": "boolean"},
        }, "required": ["title", "start"]},
        _tool_calendar_create, EXTERNAL_WRITE, 10.0, True,
        "personal-assistant calendar write"),
    )
    reg.register(Tool(
        "set_reminder",
        "Remind him at a time: YOU send him a message then. At `when` you post '⏰ ' + `text` in THIS chat, so write `text` as the reminder he should read ('Depo: melna krāsa, urbis, plastmasas kaste'). `when` is a time: 'rīt 9:00', 'pēc 2 stundām', 'piektdien 18:00' or '2026-10-01 09:00'; it is read in the time zone of the country he is in. If he did not say when, ask him when first; do not guess a time. After it succeeds you may say you will remind him, and when. Only his own messages can set reminders.",
        {"type": "object", "properties": {
            "text": {"type": "string", "minLength": 1, "maxLength": 500},
            "when": {"type": "string", "minLength": 1, "maxLength": 120},
        }, "required": ["text", "when"]},
        _tool_set_reminder, EXTERNAL_WRITE, 15.0, True, "reminders.add"),
    )
    reg.register(Tool(
        "list_reminders",
        "The reminders still waiting to be sent in this chat (or in all his chats with all_chats=true): id, text, local time.",
        {"type": "object", "properties": {
            "all_chats": {"type": "boolean"},
        }},
        _tool_list_reminders, READ_ONLY, 5.0, True, "reminders.pending"),
    )
    reg.register(Tool(
        "cancel_reminder",
        "Cancel one waiting reminder by its id (from list_reminders) when he says he no longer needs it.",
        {"type": "object", "properties": {
            "reminder_id": {"type": "string", "minLength": 2, "maxLength": 20},
        }, "required": ["reminder_id"]},
        _tool_cancel_reminder, EXTERNAL_WRITE, 5.0, True, "reminders.cancel"),
    )
    reg.register(Tool(
        "plan_route",
        "Plan a drive when someone needs to drive somewhere. Compares up to five real routes to `destination` (fastest, toll-free, country roads, the most scenic, alternatives) and checks each one along its whole length: the road surface of every segment from the map, tolls or vignettes by country, what lies beside the road (forests, water, nature parks, towns, industry), and street-level photos along the route read for road condition and scenery. Then it sends a report, a map image and Google Maps links to THIS chat by itself, in a few minutes. After calling it, say only that the check is running and roughly when the result comes; never give distances, times or roads from memory, not even when the tool fails. Pass every place in its dictionary form with the country, best in English or the local spelling: 'Lübeck, Germany', 'Kuldīga, Latvia', never an inflected form like 'Lībekas' or 'Kuldīgu' (those are not found, or match the wrong place). The result names the places it resolved: if one is not what he meant, say so and ask. `origin`: leave empty to start from where he is now (his phone); if the tool says the start is unknown or needed, ask where the drive starts. `via`: places to drive through, only if asked.",
        {"type": "object", "properties": {
            "destination": {"type": "string", "minLength": 2, "maxLength": 160},
            "origin": {"type": "string", "maxLength": 160},
            "via": {"type": "array", "items": {"type": "string", "maxLength": 120},
                    "maxItems": 3},
        }, "required": ["destination"]},
        _tool_plan_route, EXTERNAL_WRITE, 60.0, True, "route_planner.start"),
    )
    reg.register(Tool(
        "trip_map",
        f"Draw a map of {OWNER}' trip and send it into THIS chat as a picture, with a one-line summary (dates, kilometres driven, countries, the places with dates). It is drawn from every drive that really happened -- his Waze GPS tracks and the Find My fixes -- never from routes he only looked at. Use it whenever he or {PARTNER} wants to see the trip, the route so far or where they have been; call it again to redraw it (with the newest data, another title, a date range, or the dark style). It runs by itself in about a minute: after calling it say only that the map is being drawn, and never list cities, dates or kilometres yourself. `title`: only if they asked for one. `since`/`until`: YYYY-MM-DD, only to show part of the trip. `style`: \"light\" (default) or \"dark\".",
        {"type": "object", "properties": {
            "title": {"type": "string", "maxLength": 60},
            "since": {"type": "string", "maxLength": 10},
            "until": {"type": "string", "maxLength": 10},
            "style": {"type": "string", "enum": ["light", "dark"]},
        }},
        _tool_trip_map, EXTERNAL_WRITE, 20.0, True, "trip_log.start_map_job"),
    )
    reg.register(Tool(
        "send_message",
        "Send one real iMessage/BlueBubbles message to an exact trusted configured conversation only after the current user turn explicitly asks to send/tell/reply to that recipient. Use contact_lookup first unless the conversation_id is already known from a trusted current conversation. The server rejects drafts, quoted old commands, maybe/should discussions, mismatched recipients, display-name-only trust, and untrusted recipients.",
        {"type": "object", "properties": {
            "conversation_id": {"type": "string", "maxLength": 240},
            "text": {"type": "string", "minLength": 1, "maxLength": 2000},
        }, "required": ["text"]},
        _tool_send_message, EXTERNAL_WRITE, 12.0, True,
        "BlueBubbles controlled send queue"),
    )
    reg.register(Tool(
        "classifieds_search",
        "Search ss.com classifieds - Riga flats, houses, land, or cars. YOU resolve the request into fields: `kind` (flats/houses/land/cars), `area` as the Riga district in Latvian for property (\"Torņkalns\", \"Āgenskalns\"), `deal` rent or sell, and for cars `make` and `model`. WHEN HE NAMES MORE THAN ONE MODEL pass them as a LIST: [\"Cooper\", \"Cooper S\"] - each is a separate ss.com page, all of them are searched in that one call, and `found_per_model` says how many each had. Never answer about one model when he named two. ALWAYS ANSWER WITH `ready_to_send` PASTED AS IT IS: the rows are already written out with each advert's own url, and retyping them is where a price turns into a sentence and two cars end up sharing one link. Put your own words before or after that block, never inside it. FILTERS: every result carries `available_filters` - exactly what THAT page can filter on, read from ss.com itself - plus `filter_means`, which says in words what was applied. Use them: red convertibles is options=[\"Sarkana\", \"Kabriolets\"] in ONE search, not a search for red cars followed by reading the body types yourself. TRADE-INS ARE SEPARATE ON SS.COM: a seller who will take a car in part-exchange is filed under \"Maina\", not \"Pārdod\", and a car search now covers both — a row from there carries `deal_page: exchange` and `wants_in_exchange` (what the seller wants back). When he is trading something in, say which listings are trade adverts and what they want. COLOUR IS THE EXCEPTION: `Krāsa` is the word the SELLER picked, and the same shade turns up as \"Zila\", \"Zila metālika\" and \"Gaiši zila\" across adverts — filtering on one of them hides the others, which is how a light-blue Eos listed as \"Zila metālika\" was missed. When the colour matters, search WITHOUT the colour filter and settle it with `photo_question`, or accept every neighbouring shade. `min_price`/`max_price` in EUR, `min_year`/`max_year`, `min_area`/`max_area` in m2 and `min_floor`/`max_floor` for property; a size is always worth filtering on, so \"ap 50 kvadrāti\" is min_area 45, max_area 55. WHAT THE FILTER CANNOT REACH: use `ad_question` and the top adverts are opened and answered from their own attribute tables, equipment lists and text in one call - that is how you settle 4x4 / pilnpiedziņa, a tow bar, a sunroof or service history. Use `photo_question` for what is visible rather than written (a bathtub, rust, the state of a kitchen); `inspect_limit` says how many adverts to open. ss.com files cars under a make and property under a district, so a vague request (\"a cheap winter beater\") is YOUR job: work out which makes fit and search them. If the tool answers with `needs` and `choices`, that is what it wants. People name makes in slang (a bembis is a BMW, a vāģis is a Volkswagen) - pass the manufacturer's name. Anything that is neither property nor a car goes in `text`, optionally with `path`. For a standing request to be told about NEW listings later, use classifieds_watch.",
        {"type": "object", "properties": {
            "kind": {"type": "string", "enum": ["flats", "houses", "land", "cars"]},
            "area": {"type": "string", "maxLength": 60},
            "deal": {"type": "string", "enum": ["rent", "sell"]},
            "make": {"type": "string", "maxLength": 40},
            "model": {"maxLength": 40,
                      "anyOf": [{"type": "string"},
                                {"type": "array", "maxItems": 4,
                                 "items": {"type": "string", "maxLength": 40}}]},
            "max_price": {"type": "integer", "minimum": 1, "maximum": 100000000},
            "min_price": {"type": "integer", "minimum": 1, "maximum": 100000000},
            "min_year": {"type": "integer", "minimum": 1900, "maximum": 2100},
            "max_year": {"type": "integer", "minimum": 1900, "maximum": 2100},
            "min_area": {"type": "integer", "minimum": 1, "maximum": 100000},
            "max_area": {"type": "integer", "minimum": 1, "maximum": 100000},
            "min_floor": {"type": "integer", "minimum": 0, "maximum": 200},
            "max_floor": {"type": "integer", "minimum": 0, "maximum": 200},
            "options": {"type": "array", "maxItems": 5,
                        "items": {"type": "string", "maxLength": 30}},
            "text": {"type": "string", "maxLength": 80},
            "path": {"type": "string", "maxLength": 120},
            "limit": {"type": "integer", "minimum": 1, "maximum": 10},
            "photo_question": {"type": "string", "maxLength": 160},
            "ad_question": {"type": "string", "maxLength": 160},
            "equipment_required": {"type": "array", "maxItems": 4,
                                   "items": {"type": "string", "maxLength": 40}},
            "inspect_limit": {"type": "integer", "minimum": 1, "maximum": 8},
        }, "required": []},
        _tool_classifieds_search, READ_ONLY, 300.0, True,
        "ss.com classifieds search"),
    )
    reg.register(Tool(
        "classifieds_watch",
        "Keep an eye on an ss.com search and report NEW listings as they appear. Use when the person asks you to watch, follow, keep an eye on, or tell them when something comes up — not for a one-off look, which is classifieds_search. Same fields as classifieds_search, plus a short `label` naming the watch (\"Torņkalna īre\"). action=add starts one, action=list shows the watches, action=move makes an existing watch report into THIS chat from now on (use it when he asks for the notifications to come to his chat), action=remove stops one. move and remove take `watch_id` from list. What is already listed today is recorded as known and never reported; only listings that appear afterwards are.",
        {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["add", "list", "move", "remove"]},
            "watch_id": {"type": "string", "maxLength": 40},
            "label": {"type": "string", "maxLength": 80},
            "kind": {"type": "string", "enum": ["flats", "houses", "land", "cars"]},
            "area": {"type": "string", "maxLength": 60},
            "deal": {"type": "string", "enum": ["rent", "sell"]},
            "make": {"type": "string", "maxLength": 40},
            "model": {"type": "string", "maxLength": 40},
            "max_price": {"type": "integer", "minimum": 1, "maximum": 100000000},
            "min_price": {"type": "integer", "minimum": 1, "maximum": 100000000},
            "min_year": {"type": "integer", "minimum": 1900, "maximum": 2100},
            "max_year": {"type": "integer", "minimum": 1900, "maximum": 2100},
            "options": {"type": "array", "maxItems": 5,
                        "items": {"type": "string", "maxLength": 30}},
            "text": {"type": "string", "maxLength": 80},
            "path": {"type": "string", "maxLength": 120},
        }, "required": []},
        _tool_classifieds_watch, LOCAL_WRITE, 45.0, True,
        "ss.com saved searches"),
    )
    reg.register(Tool(
        "classifieds_browse",
        "Walk ss.com's own category tree. Give a `path` and it lists what sits under it, each with the path to go deeper; start at \"/lv/\" for the top. Use it when a words-search came back with the wrong sort of advert and you need the section the real ones live in — a Mac mini is under Elektrotehnika, then Datori un orgtehnika, then Datori — and then search there with classifieds_search `text` plus that `path`.",
        {"type": "object", "properties": {
            "path": {"type": "string", "maxLength": 120},
        }, "required": []},
        _tool_classifieds_browse, READ_ONLY, 25.0, True,
        "ss.com categories"),
    )
    reg.register(Tool(
        "classifieds_listing",
        "Open ONE ss.com advert and read it properly: the seller's own text, the listed attributes, the photo links, and optionally how its price has moved. Use it before recommending anything from classifieds_search — a row in the results is only a headline, and the body is where \"nav tehniskās\", \"pēc avārijas\" or a swap-only seller shows up. Pass `url` from a search result. The attributes it returns are the ones ss.com cannot search on and the results table does not show — Tehniskā apskate (the roadworthiness date), gearbox, body type, VIN — so when roadworthiness, condition or drivetrain matter, open the candidates and read them rather than answering from the list. Set `price_history` true to also ask tirgusdati.lv how long it has been listed and whether the price has come down; that is the strongest signal of a seller who will negotiate. Set `inspect_photos` true and the advert's own photos are put through the vision model, which reports rust and bubbling, dents, mismatched paint, kerbed wheels, tyre and interior wear, and the odometer when the dashboard is visible. Pass `photo_question` to look for something SPECIFIC he described — \"does this car have unusual seat covers?\", \"is the interior beige?\", \"is there a towbar?\" — which is how you find a car he remembers by sight rather than by make and price. Leave it out for a general condition report. USE IT when he is choosing between cars. He can already see the price, year and mileage on ss.com himself — repeating that table back to him is worth nothing. What he cannot get from the list is the condition in the pictures and what that engine and generation are known to suffer from, so open the shortlist, look at the photos, and search the known faults for that exact model, engine and year (timing chains, swirl flaps, DPF, rust spots, gearbox) before you recommend one.",
        {"type": "object", "properties": {
            "url": {"type": "string", "maxLength": 300},
            "price_history": {"type": "boolean"},
            "inspect_photos": {"type": "boolean"},
            "photo_question": {"type": "string", "maxLength": 300},
        }, "required": ["url"]},
        _tool_classifieds_listing, READ_ONLY, 40.0, True,
        "ss.com advert"),
    )
    reg.register(Tool(
        "media_library_search",
        "List what is ALREADY on the personal media server, optionally filtered by title. Use it whenever the user asks what they already have or whether a film or show is on the server. Pass `title` as a plain word or two from the title (\"Matrix\", \"Narnia\"); leave it empty to see everything. If it reports the library is unavailable, say so — never assume the server is empty.",
        {"type": "object", "properties": {
            "title": {"type": "string", "maxLength": 120},
        }, "required": []},
        _tool_media_library_search, READ_ONLY, 20.0, True,
        "media_agent Jellyfin library"),
    )
    reg.register(Tool(
        "download_file",
        "Download one public URL to the assistant downloads directory. Use only when the user explicitly asks to save/download a concrete public URL and local-write policy allows it. Do not use to read page contents, inspect media, gather context, fetch private/local URLs, or guess a file to save.",
        {"type": "object", "properties": {
            "url": {"type": "string", "minLength": 8, "maxLength": 1200},
            "filename": {"type": "string", "maxLength": 160},
            "max_mb": {"type": "integer", "minimum": 1, "maximum": 2048},
        }, "required": ["url"]},
        _tool_download_file, LOCAL_REVERSIBLE_WRITE, 60.0, False,
        "media/download-file migration target"),
    )
    reg.register(Tool(
        "antons_investigate",
        "Diagnose your OWN past behaviour from evidence — logs, the route a message took, which guard changed or deleted a reply, the line of code responsible. Owner-only. Use when the owner asks why you did (or didn't) do something, or how a reply of yours came out the way it did. Read-only: never writes or changes anything.",
        {"type": "object", "properties": {
            "question": {"type": "string", "minLength": 4, "maxLength": 500},
        }, "required": ["question"]},
        _tool_antons_investigate, READ_ONLY, 45.0, True,
        "app/investigate.py, same loop as /api/messaging/investigate"),
    )
    reg.register(Tool(
        "antons_read_code",
        "Read your OWN source code. Owner-only, read-only, cannot change anything. action='symbols' finds WHERE a named function/class/constant is defined (give name); action='read' returns the actual lines of a file (give path, and optionally start/end); action='refs' lists everywhere a name is used; action='search' finds code by plain description when you cannot name it; action='grep' finds an EXACT string in the file contents -- use grep for a button label, a tile name, a message the dashboard shows or anything you can quote literally, because 'search' ranks symbol names and will not find a label buried inside a function; grep now covers .html/.css too, and you can pass `path` to grep inside ONE file or subtree (e.g. a single-file HTML game) instead of the whole repo. Use this whenever you are asked about a specific function, file, or piece of your own behaviour and you need to SEE the code -- never ask the owner to paste his own source at you, you can read it yourself. For diagnosing why a past reply came out wrong use antons_investigate instead, and to actually write a fix use antons_propose_fix (which does its own reading, so you do not need to call this first).",
        {"type": "object", "properties": {
            "action": {"type": "string",
                       "enum": ["symbols", "read", "refs", "search", "grep"]},
            "name": {"type": "string", "maxLength": 200},
            "path": {"type": "string", "maxLength": 400},
            "query": {"type": "string", "maxLength": 300},
            "kind": {"type": "string", "maxLength": 40},
            "start": {"type": "integer", "minimum": 1},
            "end": {"type": "integer", "minimum": 1},
            "limit": {"type": "integer", "minimum": 1, "maximum": 60},
        }, "required": ["action"]},
        _tool_antons_read_code, READ_ONLY, 30.0, True,
        "app/selfinspect.py repo_symbols/repo_read/repo_refs/repo_search/code_search"),
    )
    reg.register(Tool(
        "antons_propose_fix",
        "Change your OWN code. THE tool for any code change to yourself — fix a bug, change behaviour, OR add a new capability/tool — when the owner asks for it. It does its OWN investigation (reads the files, finds where things are defined, checks how existing tools register): give it a plain-language `goal` and do NOT run antons_read_code first. Writes down the exact edits (file, find, replace) plus a test, as text for the owner to read and decide on — writes NOTHING itself; applying is a separate deliberate step, never from this tool. Owner-only, self-chat-only.",
        {"type": "object", "properties": {
            "goal": {"type": "string", "minLength": 4, "maxLength": 4000},
        }, "required": ["goal"]},
        _tool_antons_propose_fix, PRIVILEGED, 900.0, True,
        "app/propose_patch.py, same call as /api/messaging/propose-fix"),
    )
    reg.register(Tool(
        "antons_apply_fix",
        "Apply the fix antons_propose_fix last wrote down in THIS chat: re-checks it against the file, applies it, runs its test, and reverts automatically unless the test passes. Owner-only. Call this ONLY after the owner has actually said to go ahead with that specific proposal in the conversation -- a proposal being written is not the same as permission to apply it, and this tool takes no arguments because it always acts on the one pending proposal, never on a guess.",
        {"type": "object", "properties": {}},
        _tool_antons_apply_fix, PRIVILEGED, 900.0, True,
        "app/self_heal.py, same call as /api/messaging/apply-fix"),
    )
    reg.register(Tool(
        "antons_restart_service",
        "Restart your own service (SysMonitor) so code you just applied actually takes effect -- applied edits sit on disk doing nothing until you do. Owner-only. Call this ONLY when he has explicitly said to restart; it drops every in-flight reply, so it is his decision, never something you do on the way to something else. This restarts YOUR service only and never touches Ollama, the models or the GPU.",
        {"type": "object", "properties": {}},
        _tool_antons_restart_service, PRIVILEGED, 30.0, True,
        "app/service_restart.py, same call as /api/service/restart"),
    )
    reg.register(Tool(
        "antons_build",
        "Build something NEW that does not exist yet — a new page, screen, "
        "dashboard, small tool, or a game. Counterpart to antons_propose_fix: "
        "propose_fix makes one small surgical edit to fix a bug in existing "
        "code; antons_build authors a whole new self-contained HTML page "
        "(app/static/<name>.html, live at "
        "http://127.0.0.1:8765/static/<name>.html with no restart) and writes "
        f"it. {OWNER} can ask for it in any of his chats, and {PARTNER} can ask "
        "for it herself in hers; anyone else cannot, and you tell them so "
        "plainly. ALWAYS: first read "
        "whatever you need, then reply with a concrete plan for the person "
        "who asked to approve, and call this ONLY after that person says yes "
        f"({PARTNER}'s own yes counts for what she asked for). When you ask for "
        "approval, do it in one message that also states what it will build, "
        "which page it will touch, and what will change -- never ask for a "
        "yes without that context in the same message. If you have nothing "
        "concrete yet, read more or ask a real question instead of asking "
        "for approval. Args: `goal` = the "
        "full spec in one paragraph (what it looks like, how it behaves, who "
        "it is for, any device/layout constraints — put everything you know "
        "in here, including anything you found in chat history); `files` = a "
        "short name for the page -- to CHANGE a page that already exists, pass "
        "its exact name; without a name this always makes a NEW page and "
        "never touches an existing one; `target` = \"ios9\" ONLY for the in-case "
        "iPhone 4S dashboard, otherwise leave it (\"modern\"); `polish` = true "
        "(or 1-3) when the result has to be genuinely good and he is willing "
        "to wait — it then reviews and rewrites its own work that many times "
        "(each pass ~1-2 min). It runs in the background and messages this "
        "chat with the URL; do NOT wait for it and do NOT call it twice for "
        "the same request.",
        {"type": "object", "properties": {
            "goal": {"type": "string", "minLength": 8, "maxLength": 4000},
            "files": {"type": "string", "maxLength": 60},
            "target": {"type": "string", "maxLength": 20},
            "polish": {"type": "integer", "minimum": 0, "maximum": 3},
        }, "required": ["goal"]},
        _tool_antons_build, PRIVILEGED, 20.0, True,
        "app/agent_tools.py _run_build -> direct qwen /api/chat"),
    )
    reg.register(Tool(
        "antons_shares",
        "What this machine is exposing over Tailscale right now: every URL, "
        "what it points at, and whether it is PUBLIC (funnel -- anyone with "
        f"the link) or tailnet-only (just {OWNER}' own devices). Read-only. "
        f"Owner-only. Use when he asks what is shared, what can {PARTNER} (or "
        "anyone) reach, or whether something is public. To cut a share use "
        "antons_unshare.",
        {"type": "object", "properties": {}},
        _tool_antons_shares, READ_ONLY, 20.0, True,
        "app/tailscale_shares.py list_shares / tailscale serve status"),
    )
    reg.register(Tool(
        "antons_unshare",
        "Stop ONE Tailscale share so its link stops working for everyone. "
        "Pass `port` (e.g. \"8443\") from antons_shares -- it turns off the "
        "funnel and the serve on that port. Owner-only. Use when he says to "
        "cut someone's access to a game or page. Refuses his standing service "
        "ports (443, 47991).",
        {"type": "object", "properties": {
            "port": {"type": "string", "maxLength": 8},
        }, "required": ["port"]},
        _tool_antons_unshare, PRIVILEGED, 30.0, True,
        "app/tailscale_shares.py stop_share / tailscale funnel off"),
    )
    reg.register(Tool(
        "antons_publish",
        f"Put a page you built onto the media box so {PARTNER} can open it on her "
        "phone, and hand back the link. Pass `slug` (defaults to the page you "
        "built most recently). Keeps a timestamped backup of what was there, "
        "copies the file over, checks it really serves, returns the tailnet "
        "link -- her devices only, never public. Owner-only. Use when he says "
        f"to send {PARTNER} the game / publish it / put the new version live for "
        "her. Editing the file here does NOT reach her until this runs.",
        {"type": "object", "properties": {
            "slug": {"type": "string", "maxLength": 60},
        }},
        _tool_antons_publish, PRIVILEGED, 90.0, True,
        "app/agent_tools.py _sftp_to_media -> <media home>/pages/ + serve check"),
    )
    reg.register(Tool(
        "antons_page_versions",
        "The copies of a page that still exist, so a page that was built over "
        "or went wrong can be brought back. Pass `slug` (a page name, e.g. "
        "\"tile-mahjong\"): lists the page here, its last browser-verified "
        "copy, loose snapshots next to it, and every copy the games box kept "
        "when a publish replaced it -- each with its title, size, and when it "
        "stopped being the live one (`live_until`). Leave `slug` out to list "
        "the pages that exist, with their titles. Read-only. Owner-only. Use "
        "when a game has the wrong content, was overwritten, or he wants an "
        "older version back; then antons_restore_page.",
        {"type": "object", "properties": {
            "slug": {"type": "string", "maxLength": 60},
        }},
        _tool_antons_page_versions, READ_ONLY, 60.0, True,
        "app/static/<slug>.html* + <media home>/pages/<slug>.html.bak-*"),
    )
    reg.register(Tool(
        "antons_restore_page",
        "Put an earlier copy of a page back. Pass `slug` and `version` (a "
        "version from antons_page_versions, e.g. \"box:bak-20260910T120225\"). "
        "Pass `to_slug` to save that copy under a DIFFERENT page name instead "
        "-- e.g. to keep what a page holds now as its own page before an older "
        "copy goes back over it. Writes app/static/<page>.html, publishes it to "
        "the games box (which keeps what it replaces) and returns both links. "
        "Refuses to replace a page whose current content is kept nowhere else. "
        "Owner-only.",
        {"type": "object", "properties": {
            "slug": {"type": "string", "maxLength": 60},
            "version": {"type": "string", "maxLength": 80},
            "to_slug": {"type": "string", "maxLength": 60},
        }, "required": ["slug", "version"]},
        _tool_antons_restore_page, PRIVILEGED, 150.0, True,
        "app/agent_tools.py _media_box_read + _publish_slug_to_media"),
    )
    reg.register(Tool(
        "antons_fix_page_text",
        "Fix the words a person sees on a page or game -- made-up or broken "
        "Latvian. Pass `slug`. You read the page and say what each visible "
        "string means in plain English; the same translator your chat replies "
        "go through writes the Latvian; each string is replaced where it "
        "stands (code is never touched). The page is browser-tested before and "
        "after and the change is kept only when nothing new fails; a page that "
        "is on the games box is published. Returns the old and new text -- show "
        "him both, he judges the Latvian. Owner-only.",
        {"type": "object", "properties": {
            "slug": {"type": "string", "maxLength": 60},
        }, "required": ["slug"]},
        _tool_antons_fix_page_text, PRIVILEGED, 480.0, True,
        "app/page_texts.py retext -> messaging._nmt_lv + build selftest"),
    )
    reg.register(Tool(
        "antons_set_auto_accept",
        "Turn auto-accept on or off. While it is ON you apply code fixes and restart the service straight away instead of stopping to ask him first; while it is OFF you wait for his go-ahead every time. Owner-only. Call this when he asks for it in whatever words he uses -- 'auto mode on', 'ieslēdz auto akceptēšanu', 'turpmāk dari pats', or the same thing said any other way -- and pass minutes when he names a duration. Relay what the tool tells you back to him: he needs to know it is on, until when, and what you will now do without asking. Never switch it on because it would be convenient for you; only he decides this.",
        {"type": "object",
         "properties": {
             "enabled": {"type": "boolean",
                         "description": "true turns auto-accept on, false turns it off"},
             "minutes": {"type": "integer",
                         "description": "how long it stays on, default 120"},
         },
         "required": ["enabled"]},
        _tool_antons_set_auto_accept, PRIVILEGED, 15.0, True,
        "app/messaging.py set_auto_accept, persisted in config/messaging.json"),
    )
    reg.register(Tool(
        "antons_find_message",
        "Search further back in THIS conversation than you can currently see. You are only shown the last 10 messages by default so old, unrelated exchanges do not crowd out the one you are actually answering -- call this with a word or phrase from what you are trying to recall (a topic, a decision, something he said) when you need something older than that. Owner-only, read-only, this chat only. Prefer this over search_chat_history for recalling something in THIS conversation -- it is scoped here automatically; use search_chat_history instead only when you need to search a DIFFERENT chat or filter by person/date.",
        {"type": "object",
         "properties": {
             "query": {"type": "string",
                       "description": "a word or phrase to search for, literal match"},
             "limit": {"type": "integer",
                       "description": "how many matches to return, default 8, max 20"},
         },
         "required": ["query"]},
        _tool_antons_find_message, READ_ONLY, 10.0, True,
        "app/messaging.py find_chat_messages"),
    )
    reg.register(Tool(
        "antons_run_tests",
        "Run one existing test suite and report pass/fail. Read-only, changes nothing -- no approval needed to CALL this, only to actually apply a fix (that stays antons_apply_fix's gate). Use it to check whether a suite already exists and passes before proposing a change, or to confirm one still passes without a full propose/apply cycle. Owner-only.",
        {"type": "object",
         "properties": {
             "suite": {"type": "string",
                       "description": "path under tests/, e.g. tests/rig_lookup_cases.py"},
         },
         "required": ["suite"]},
        _tool_antons_run_tests, READ_ONLY, 30.0, True,
        "app/self_heal.py run_suite, same runner antons_apply_fix verifies with"),
    )
    return reg


_DEFAULT_REGISTRY = None


def default_registry() -> ToolRegistry:
    global _DEFAULT_REGISTRY
    if _DEFAULT_REGISTRY is None:
        _DEFAULT_REGISTRY = build_default_registry()
    return _DEFAULT_REGISTRY


def tool_inventory(ctx: AgentContext | None = None, include_unavailable: bool = True) -> list[dict]:
    ctx = ctx or AgentContext()
    rows = []
    for tool in default_registry().available(ctx, include_unavailable=include_unavailable):
        ok, why = authorize(tool, ctx)
        rows.append({
            "name": tool.name,
            "description": tool.description,
            "risk": tool.risk,
            "private": tool.private,
            "available": ok,
            "unavailable_reason": why,
            "timeout_seconds": tool.timeout_seconds,
            "migrated_from": tool.migrated_from,
        })
    return rows


def append_trace(row: dict) -> None:
    try:
        TRACE_PATH.parent.mkdir(parents=True, exist_ok=True)
        row = dict(row)
        row.setdefault("ts", time.time())
        with open(TRACE_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass


def trace_metrics(path: Path | str = TRACE_PATH, limit: int = 5000) -> dict:
    """Aggregate agent trace rows without exposing prompt or result content."""
    p = Path(path)
    rows = []
    try:
        raw = p.read_text(encoding="utf-8", errors="ignore").splitlines()
    except Exception:
        raw = []
    for line in raw[-max(1, int(limit or 5000)):]:
        try:
            item = json.loads(line)
        except Exception:
            continue
        if isinstance(item, dict):
            rows.append(item)
    by_tool: dict[str, int] = {}
    errors = 0
    loop_limits = 0
    cancelled = 0
    final_turns = set()
    tool_steps_by_trace: dict[str, int] = {}
    model_calls_by_trace: dict[str, int] = {}
    latencies = []
    for row in rows:
        trace_id = str(row.get("trace_id") or "")
        event = str(row.get("event") or "")
        if event == "tool":
            tool = str(row.get("tool") or "unknown")
            by_tool[tool] = by_tool.get(tool, 0) + 1
            tool_steps_by_trace[trace_id] = max(
                tool_steps_by_trace.get(trace_id, 0), int(row.get("step") or 0))
            if not row.get("ok"):
                errors += 1
        elif event == "final":
            final_turns.add(trace_id)
            model_calls_by_trace[trace_id] = max(
                model_calls_by_trace.get(trace_id, 0), int(row.get("model_calls") or 0))
            if row.get("loop_limit_reached"):
                loop_limits += 1
            try:
                latencies.append(float(row.get("seconds") or 0))
            except Exception:
                pass
        elif event == "cancelled":
            cancelled += 1
    traces = sorted({str(r.get("trace_id") or "") for r in rows if r.get("trace_id")})
    total_turns = len(final_turns)
    no_tool_turns = sum(1 for tid in final_turns if not tool_steps_by_trace.get(tid))
    total_tool_calls = sum(by_tool.values())
    duplicate_rows = [
        r for r in rows
        if str(r.get("event") or "") == "tool" and r.get("duplicate_call")
    ]
    reused_rows = [
        r for r in rows
        if str(r.get("event") or "") == "tool" and r.get("reused")
    ]
    executed_rows = [
        r for r in rows
        if str(r.get("event") or "") == "tool" and r.get("executed") is not False
    ]
    duplicate_by_tool: dict[str, int] = {}
    duplicate_by_type: dict[str, int] = {}
    for row in duplicate_rows:
        tool = str(row.get("tool") or "unknown")
        typ = str(row.get("duplicate_type") or "unknown")
        duplicate_by_tool[tool] = duplicate_by_tool.get(tool, 0) + 1
        duplicate_by_type[typ] = duplicate_by_type.get(typ, 0) + 1
    return {
        "trace_rows": len(rows),
        "traces_seen": len(traces),
        "completed_turns": total_turns,
        "tool_calls": dict(sorted(by_tool.items(), key=lambda kv: (-kv[1], kv[0]))),
        "executed_tool_calls": len(executed_rows),
        "duplicate_calls": len(duplicate_rows),
        "duplicate_reused": len(reused_rows),
        "duplicate_by_tool": dict(sorted(duplicate_by_tool.items(),
                                         key=lambda kv: (-kv[1], kv[0]))),
        "duplicate_by_type": dict(sorted(duplicate_by_type.items(),
                                         key=lambda kv: (-kv[1], kv[0]))),
        "average_steps_per_turn": (
            round(sum(tool_steps_by_trace.get(tid, 0) for tid in final_turns) / total_turns, 3)
            if total_turns else 0.0),
        "average_executed_steps_per_turn": (
            round(len(executed_rows) / total_turns, 3) if total_turns else 0.0),
        "no_tool_turns": no_tool_turns,
        "no_tool_rate": round(no_tool_turns / total_turns, 3) if total_turns else 0.0,
        "tool_error_rate": round(errors / total_tool_calls, 3) if total_tool_calls else 0.0,
        "tool_errors": errors,
        "loop_limits": loop_limits,
        "cancelled": cancelled,
        "average_latency_s": round(sum(latencies) / len(latencies), 3) if latencies else 0.0,
        "average_model_calls_per_turn": (
            round(sum(model_calls_by_trace.get(tid, 0) for tid in final_turns) / total_turns, 3)
            if total_turns else 0.0),
    }
