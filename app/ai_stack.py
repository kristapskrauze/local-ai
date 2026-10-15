"""Local-first AI stack router, reviewer, and memory helpers."""

import json
import os
import re
import sqlite3
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from app import paths


ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "ai_stack.json"
DEFAULT_DB_PATH = ROOT / ".ai_memory" / "memory.sqlite3"
MEMORY_DB_PATH = Path(os.environ.get("AI_MEMORY_DB", str(DEFAULT_DB_PATH)))

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")

DEFAULT_CONFIG = {
    "local_root": paths.AI_HOME,
    "policy": {
        "autonomy": "review_gate",
        "cloud": "review_only_explicit",
        "redact_cloud_prompts": True,
        "memory_enabled_default": True,
    },
    "models": {
        "assistant": "chat:latest",
        "coding": "qwen3.8:latest",
        "fast_coding": "qwen3.8:latest",
        "local_reviewer": "qwen3.8:latest",
        "general_reviewer": "chat:latest",
    },
    "cloud_reviewers": {
        "anthropic": {
            "enabled": True,
            "api_key_env": "ANTHROPIC_API_KEY",
            "model": "",
            "endpoint": "https://api.anthropic.com/v1/messages",
            "timeout_seconds": 90,
        },
        "openrouter": {
            "enabled": True,
            "api_key_env": "OPENROUTER_API_KEY",
            "endpoint": "https://openrouter.ai/api/v1/chat/completions",
            "models": [],
            "timeout_seconds": 90,
        },
    },
    "routes": {
        "assistant_chat": {"provider": "ollama", "model": "assistant"},
        "coding": {"provider": "ollama", "model": "coding"},
        "local_review": {"provider": "ollama", "model": "local_reviewer"},
        "cloud_review": {"provider": "anthropic", "fallback": "openrouter"},
    },
    "comfyui": {
        "preferred_root": paths.COMFYUI_DIR,
        "legacy_root": "",
        "note": "Move runtime under the dobby AI root before enabling portable control.",
    },
}


SECRET_PATTERNS = [
    (
        re.compile(r"(?i)\b(api[_-]?key|token|password|secret)\s*[:=]\s*[\"']?[^\"'\s,;]+"),
        lambda m: "%s=[REDACTED]" % m.group(1),
    ),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"), lambda _m: "[REDACTED_OPENAI_KEY]"),
    (re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b"), lambda _m: "[REDACTED_ANTHROPIC_KEY]"),
    (re.compile(r"\bBearer\s+[A-Za-z0-9._-]{20,}\b", re.I), lambda _m: "Bearer [REDACTED]"),
]


def _now():
    return datetime.now(timezone.utc).isoformat()


def _merge_dict(base, override):
    out = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge_dict(out[key], value)
        else:
            out[key] = value
    return out


def load_config():
    if not CONFIG_PATH.exists():
        return DEFAULT_CONFIG
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
    except Exception:
        return DEFAULT_CONFIG
    if not isinstance(data, dict):
        return DEFAULT_CONFIG
    return _merge_dict(DEFAULT_CONFIG, data)


def _connect():
    MEMORY_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(MEMORY_DB_PATH))
    db.row_factory = sqlite3.Row
    db.execute(
        "create table if not exists memories ("
        "id integer primary key autoincrement, "
        "kind text not null, "
        "title text not null, "
        "content text not null, "
        "source text not null default '', "
        "metadata_json text not null default '{}', "
        "pinned integer not null default 0, "
        "disabled integer not null default 0, "
        "created_at text not null, "
        "updated_at text not null)"
    )
    db.execute(
        "create table if not exists settings ("
        "key text primary key, "
        "value text not null)"
    )
    db.commit()
    return db


def _setting(db, key, default=None):
    row = db.execute("select value from settings where key = ?", (key,)).fetchone()
    if row is None:
        return default
    return row["value"]


def memory_enabled():
    cfg = load_config()
    default = "1" if cfg.get("policy", {}).get("memory_enabled_default", True) else "0"
    if os.environ.get("AI_MEMORY_DISABLED", "").strip().lower() in ("1", "true", "yes", "on"):
        return False
    with _connect() as db:
        return _setting(db, "memory_enabled", default) == "1"


def set_memory_enabled(enabled):
    with _connect() as db:
        db.execute(
            "insert into settings(key, value) values(?, ?) "
            "on conflict(key) do update set value = excluded.value",
            ("memory_enabled", "1" if enabled else "0"),
        )
        db.commit()
    return {"ok": True, "enabled": bool(enabled)}


def _row_to_dict(row):
    out = dict(row)
    try:
        out["metadata"] = json.loads(out.pop("metadata_json") or "{}")
    except Exception:
        out["metadata"] = {}
    out["pinned"] = bool(out.get("pinned"))
    out["disabled"] = bool(out.get("disabled"))
    return out


def add_memory(kind, title, content, source="", metadata=None, pinned=False, force=False):
    kind = (kind or "note").strip()[:40] or "note"
    title = (title or "").strip()[:180]
    content = (content or "").strip()
    if not title:
        title = content[:80] or kind
    if not content:
        return {"ok": False, "error": "memory content is required", "status_code": 400}
    if not force and not memory_enabled():
        return {"ok": False, "error": "memory is disabled", "status_code": 403}
    ts = _now()
    with _connect() as db:
        cur = db.execute(
            "insert into memories(kind, title, content, source, metadata_json, pinned, created_at, updated_at) "
            "values(?, ?, ?, ?, ?, ?, ?, ?)",
            (
                kind,
                title,
                content,
                (source or "").strip()[:400],
                json.dumps(metadata or {}, sort_keys=True),
                1 if pinned else 0,
                ts,
                ts,
            ),
        )
        db.commit()
        memory_id = cur.lastrowid
    return {"ok": True, "id": memory_id}


def list_memories(limit=50, q="", kind=None, include_disabled=False):
    limit = max(1, min(int(limit or 50), 200))
    params = []
    where = []
    if not include_disabled:
        where.append("disabled = 0")
    if kind:
        where.append("kind = ?")
        params.append(kind)
    if q:
        where.append("(title like ? or content like ? or source like ?)")
        term = "%" + q + "%"
        params.extend([term, term, term])
    sql = "select * from memories"
    if where:
        sql += " where " + " and ".join(where)
    sql += " order by pinned desc, id desc limit ?"
    params.append(limit)
    with _connect() as db:
        rows = db.execute(sql, params).fetchall()
    return {"ok": True, "items": [_row_to_dict(r) for r in rows]}


def update_memory(memory_id, kind=None, title=None, content=None, pinned=None, disabled=None):
    sets, params = [], []
    if kind is not None:
        sets.append("kind = ?")
        params.append((str(kind).strip()[:40]) or "note")
    if title is not None:
        sets.append("title = ?")
        params.append(str(title).strip()[:180])
    if content is not None:
        content = str(content).strip()
        if not content:
            return {"ok": False, "error": "memory content cannot be empty", "status_code": 400}
        sets.append("content = ?")
        params.append(content)
    if pinned is not None:
        sets.append("pinned = ?")
        params.append(1 if pinned else 0)
    if disabled is not None:
        sets.append("disabled = ?")
        params.append(1 if disabled else 0)
    if not sets:
        return {"ok": False, "error": "nothing to update", "status_code": 400}
    sets.append("updated_at = ?")
    params.append(_now())
    params.append(int(memory_id))
    with _connect() as db:
        cur = db.execute("update memories set " + ", ".join(sets) + " where id = ?", params)
        db.commit()
    if cur.rowcount == 0:
        return {"ok": False, "error": "memory not found", "status_code": 404}
    return {"ok": True, "id": int(memory_id)}


def delete_memory(memory_id):
    with _connect() as db:
        cur = db.execute("delete from memories where id = ?", (int(memory_id),))
        db.commit()
    return {"ok": True, "deleted": cur.rowcount}


# A raw chat prompt used as a LIKE pattern ("%<whole sentence>%") virtually
# never matches, so relevance search scores by word overlap instead.
# \w (unicode) instead of [A-Za-z]: Latvian diacritics must not split words.
_WORD_RE = re.compile(r"\w{3,}", re.UNICODE)
_STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "what", "when", "where", "how",
    "are", "you", "your", "should", "would", "could", "from", "have", "has",
    "not", "can", "all", "use", "using", "about", "into", "over", "its", "was",
    "were", "will", "which", "their", "there", "then", "than", "them", "some",
    "any", "our", "out", "get", "set", "please", "make", "want", "need", "just",
}


def search_memories(query, limit=6):
    """Token-overlap relevance search over enabled memories.

    Pinned memories get a small baseline score so durable preferences are
    always available to the assistant even without a keyword hit."""
    limit = max(1, min(int(limit or 6), 20))
    words = []
    for w in _WORD_RE.findall(str(query or "").lower()):
        if w not in _STOPWORDS and w not in words:
            words.append(w)
    words = words[:16]
    items = list_memories(limit=200).get("items") or []
    scored = []
    for m in items:
        hay = " ".join([
            str(m.get("title") or ""), str(m.get("content") or ""),
            str(m.get("kind") or ""), str(m.get("source") or ""),
        ]).lower()
        score = float(sum(1 for w in words if w in hay))
        if m.get("pinned"):
            score += 0.25
        if score > 0:
            scored.append((score, m))
    scored.sort(key=lambda t: (-t[0], -(t[1].get("id") or 0)))
    return [m for _s, m in scored[:limit]]


def memory_context(q="", limit=6):
    """Formatted Hermes memory block for injecting into assistant chats."""
    if not memory_enabled():
        return {"ok": True, "enabled": False, "text": "", "count": 0, "items": []}
    items = search_memories(q, limit=limit)
    lines = []
    for m in items:
        title = m.get("title") or m.get("kind")
        lines.append("- [%s] %s: %s" % (m.get("kind"), title, (m.get("content") or "")[:400]))
    text = ""
    if lines:
        text = ("Operator's saved preferences and facts (Hermes memory). "
                "Honor these unless the user overrides them:\n" + "\n".join(lines))
    return {"ok": True, "enabled": True, "text": text, "count": len(items), "items": items}


def memory_stats():
    with _connect() as db:
        total = db.execute("select count(*) as c from memories where disabled = 0").fetchone()["c"]
        kinds = db.execute(
            "select kind, count(*) as c from memories where disabled = 0 group by kind order by c desc"
        ).fetchall()
    return {
        "enabled": memory_enabled(),
        "db_path": str(MEMORY_DB_PATH),
        "count": total,
        "kinds": [{"kind": r["kind"], "count": r["c"]} for r in kinds],
    }


def redact_for_cloud(text):
    out = text or ""
    for regex, repl in SECRET_PATTERNS:
        out = regex.sub(repl, out)
    return out


def _post_json(url, payload, headers=None, timeout=60):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data)
    req.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    with urllib.request.urlopen(req, timeout=timeout) as res:
        return json.loads(res.read().decode("utf-8", errors="replace"))


def _provider_status(provider_id, provider_cfg):
    key_env = provider_cfg.get("api_key_env") or ""
    has_key = bool(key_env and os.environ.get(key_env))
    enabled = bool(provider_cfg.get("enabled", True))
    if not enabled:
        status = "disabled"
    elif has_key:
        status = "ready"
    else:
        status = "missing_key"
    public = {k: v for k, v in provider_cfg.items() if k not in ("api_key",)}
    public["has_key"] = has_key
    public["status"] = status
    public["id"] = provider_id
    return public


def _bluebubbles_status():
    path = ROOT / "config" / "messaging.json"
    try:
        cfg = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        return {"status": "missing_config", "error": str(exc)}
    bb = ((cfg.get("providers") or {}).get("bluebubbles_http") or {})
    password_env = bb.get("password_env") or "BLUEBUBBLES_PASSWORD"
    enabled = bool(bb.get("enabled"))
    server_url = (bb.get("server_url") or "").strip()
    has_password = bool(os.environ.get(password_env))
    ready = enabled and bool(server_url) and has_password
    return {
        "status": "ready" if ready else "not_ready",
        "enabled": enabled,
        "server_url_configured": bool(server_url),
        "password_env": password_env,
        "password_env_set": has_password,
    }


def status_payload():
    cfg = load_config()
    local_root = Path(cfg.get("local_root") or DEFAULT_CONFIG["local_root"])
    subdirs = ["configs", "logs", "memory", "models", "tools", "workflows"]
    cloud = cfg.get("cloud_reviewers") or {}
    return {
        "ok": True,
        "config_path": str(CONFIG_PATH),
        "local_root": str(local_root),
        "local_root_exists": local_root.exists(),
        "subdirs": [
            {"name": name, "path": str(local_root / name), "exists": (local_root / name).exists()}
            for name in subdirs
        ],
        "policy": cfg.get("policy") or {},
        "models": cfg.get("models") or {},
        "routes": cfg.get("routes") or {},
        "cloud_reviewers": [
            _provider_status(provider_id, provider_cfg)
            for provider_id, provider_cfg in cloud.items()
        ],
        "memory": memory_stats(),
        "bluebubbles": _bluebubbles_status(),
        "comfyui": cfg.get("comfyui") or {},
    }


def _review_prompt(prompt, subject="", cloud=False):
    memories = search_memories(prompt, limit=5)
    memory_text = "\n".join(
        "- %s: %s" % (m.get("title") or m.get("kind"), (m.get("content") or "")[:400])
        for m in memories
    )
    text = (
        "You are a review-only senior engineering assistant for a local Windows AI rig.\n"
        "Do not claim you changed files. Find concrete bugs, risks, missing tests, "
        "unsafe automation, privacy leaks, and setup gaps. Keep the answer concise.\n\n"
    )
    if cloud:
        text += (
            "Cloud privacy rule: the prompt was redacted before sending. "
            "Do not ask for secrets or full repository dumps.\n\n"
        )
    if memory_text:
        text += "Relevant local memories:\n%s\n\n" % memory_text
    if subject:
        text += "Subject/context:\n%s\n\n" % subject
    text += "Review request:\n%s\n" % prompt
    return text


def _ollama_review(model, prompt):
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "keep_alive": "30m",
        "options": {"temperature": 0.1},
    }
    out = _post_json(OLLAMA_URL + "/api/generate", payload, timeout=300)
    return out.get("response") or ""


def _anthropic_review(provider_cfg, prompt):
    key_env = provider_cfg.get("api_key_env") or "ANTHROPIC_API_KEY"
    key = os.environ.get(key_env)
    if not key:
        return {"ok": False, "error": "%s is not set" % key_env, "status_code": 403}
    if not provider_cfg.get("model"):
        return {"ok": False, "error": "no model configured for this reviewer", "status_code": 400}
    payload = {
        "model": provider_cfg["model"],
        "max_tokens": 2200,
        "temperature": 0.1,
        "messages": [{"role": "user", "content": prompt}],
    }
    headers = {
        "x-api-key": key,
        "anthropic-version": "2023-06-01",
    }
    out = _post_json(
        provider_cfg.get("endpoint") or "https://api.anthropic.com/v1/messages",
        payload,
        headers=headers,
        timeout=int(provider_cfg.get("timeout_seconds") or 90),
    )
    parts = []
    for item in out.get("content") or []:
        if item.get("type") == "text":
            parts.append(item.get("text") or "")
    return {"ok": True, "output": "\n".join(parts).strip(), "model": payload["model"]}


def _openrouter_review(provider_cfg, prompt):
    key_env = provider_cfg.get("api_key_env") or "OPENROUTER_API_KEY"
    key = os.environ.get(key_env)
    if not key:
        return {"ok": False, "error": "%s is not set" % key_env, "status_code": 403}
    models = provider_cfg.get("models") or []
    if not models:
        return {"ok": False, "error": "no model configured for this reviewer", "status_code": 400}
    payload = {
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1,
        "max_tokens": 2200,
        "provider": {"data_collection": "deny"},
    }
    if len(models) > 1:
        payload["models"] = models
    else:
        payload["model"] = models[0]
    headers = {
        "Authorization": "Bearer " + key,
        "HTTP-Referer": "http://127.0.0.1:8765/",
        "X-Title": "Dobby Console",
    }
    out = _post_json(
        provider_cfg.get("endpoint") or "https://openrouter.ai/api/v1/chat/completions",
        payload,
        headers=headers,
        timeout=int(provider_cfg.get("timeout_seconds") or 90),
    )
    choice = (out.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    return {"ok": True, "output": msg.get("content") or "", "model": out.get("model") or payload.get("model") or "multi"}


def run_review(provider, prompt, subject="", allow_cloud=False, save_memory=True):
    cfg = load_config()
    provider = (provider or "local").strip().lower()
    prompt = (prompt or "").strip()
    subject = (subject or "").strip()
    if not prompt:
        return {"ok": False, "error": "review prompt is required", "status_code": 400}
    if provider in ("local", "ollama"):
        model = (cfg.get("models") or {}).get("local_reviewer") or "qwen3.8:latest"
        review_text = _review_prompt(prompt, subject=subject, cloud=False)
        try:
            output = _ollama_review(model, review_text)
            result = {"ok": True, "provider": "local", "model": model, "output": output}
        except Exception as exc:
            result = {"ok": False, "provider": "local", "error": str(exc), "status_code": 502}
    else:
        if not allow_cloud:
            return {
                "ok": False,
                "provider": provider,
                "error": "cloud review requires allow_cloud=true",
                "status_code": 403,
            }
        cloud_cfg = (cfg.get("cloud_reviewers") or {}).get(provider)
        if not cloud_cfg:
            return {"ok": False, "error": "unknown review provider: %s" % provider, "status_code": 400}
        if not cloud_cfg.get("enabled", True):
            return {"ok": False, "error": "review provider is disabled: %s" % provider, "status_code": 403}
        cloud_subject = redact_for_cloud(subject) if cfg.get("policy", {}).get("redact_cloud_prompts", True) else subject
        cloud_prompt = redact_for_cloud(prompt) if cfg.get("policy", {}).get("redact_cloud_prompts", True) else prompt
        review_text = _review_prompt(cloud_prompt, subject=cloud_subject, cloud=True)
        try:
            if provider == "anthropic":
                result = _anthropic_review(cloud_cfg, review_text)
            elif provider == "openrouter":
                result = _openrouter_review(cloud_cfg, review_text)
            else:
                return {"ok": False, "error": "unknown review provider: %s" % provider, "status_code": 400}
            result["provider"] = provider
            result["redacted"] = True
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:1000]
            result = {"ok": False, "provider": provider, "error": "HTTP %s: %s" % (exc.code, detail), "status_code": 502}
        except Exception as exc:
            result = {"ok": False, "provider": provider, "error": str(exc), "status_code": 502}

    if result.get("ok") and save_memory:
        add_memory(
            "review",
            prompt[:120],
            result.get("output") or "",
            source="aistack:%s" % result.get("provider", provider),
            metadata={"provider": result.get("provider"), "model": result.get("model"), "ts": time.time()},
        )
    return result


def compact_memories(dry_run=False, model=None, dedupe_threshold=0.85,
                     consolidate_after=25, keep_newest=15):
    """Daily Hermes hygiene: disable near-duplicates, consolidate old bulk.

    - Pinned memories are never touched.
    - Duplicates: jaccard word overlap >= threshold -> newest survives, older
      copies are disabled (soft delete, reversible from the Hermes tab).
    - Consolidation: when an unpinned kind grows past `consolidate_after`,
      everything but the newest `keep_newest` is summarized into ONE 'summary'
      memory by the local model and the originals are disabled.
    """
    if not memory_enabled():
        return {"ok": False, "error": "memory is disabled", "status_code": 403}
    items = list_memories(limit=200).get("items") or []
    report = {"ok": True, "dry_run": bool(dry_run), "checked": len(items),
              "duplicates_disabled": 0, "consolidated_kinds": [], "details": []}

    def words(m):
        return set(_WORD_RE.findall(
            ((m.get("title") or "") + " " + (m.get("content") or "")).lower()))

    word_sets = {m["id"]: words(m) for m in items}
    newest_first = sorted(items, key=lambda m: -(m.get("id") or 0))
    dropped = set()
    for i, a in enumerate(newest_first):
        if a["id"] in dropped:
            continue
        for b in newest_first[i + 1:]:
            if b["id"] in dropped or b.get("pinned"):
                continue
            wa, wb = word_sets[a["id"]], word_sets[b["id"]]
            if not wa or not wb:
                continue
            overlap = len(wa & wb) / float(len(wa | wb))
            if overlap >= dedupe_threshold:
                dropped.add(b["id"])
                report["details"].append(
                    "dup: #%s ~ #%s (%.2f) -> disable older" % (a["id"], b["id"], overlap))
    if not dry_run:
        for mid in dropped:
            update_memory(mid, disabled=True)
    report["duplicates_disabled"] = len(dropped)

    groups = {}
    for m in items:
        if m["id"] in dropped or m.get("pinned") or m.get("kind") == "summary":
            continue
        groups.setdefault(m.get("kind") or "note", []).append(m)
    cfg = load_config()
    model = model or (cfg.get("models") or {}).get("local_reviewer") or "qwen3.8:latest"
    for kind, group in groups.items():
        if len(group) < max(5, int(consolidate_after)):
            continue
        group.sort(key=lambda m: m.get("id") or 0)
        old = group[: len(group) - max(1, int(keep_newest))]
        if not old:
            continue
        if dry_run:
            report["details"].append(
                "would consolidate %d old '%s' memories" % (len(old), kind))
            report["consolidated_kinds"].append(kind)
            continue
        blob = "\n".join("- %s: %s" % (m.get("title"), (m.get("content") or "")[:300])
                         for m in old)
        prompt = (
            "Consolidate these saved notes into ONE concise entry. Keep every "
            "concrete fact (names, numbers, dates, preferences); remove "
            "repetition and chatter. Answer in the same language the notes are "
            "written in. Reply with the consolidated text only.\n\n" + blob)
        try:
            output = _ollama_review(model, prompt)
        except Exception as exc:
            report["details"].append("consolidate '%s' failed: %s" % (kind, exc))
            continue
        saved = add_memory(
            "summary", "Konsolidēts: %s (%s)" % (kind, _now()[:10]), output,
            source="hermes:compact",
            metadata={"model": model, "input_count": len(old), "kind": kind},
            force=True)
        if saved.get("ok"):
            for m in old:
                update_memory(m["id"], disabled=True)
            report["consolidated_kinds"].append(kind)
            report["details"].append(
                "consolidated %d old '%s' memories -> #%s" % (len(old), kind, saved.get("id")))
    return report


def summarize_memory(model=None):
    if not memory_enabled():
        return {"ok": False, "error": "memory is disabled", "status_code": 403}
    items = list_memories(limit=30).get("items") or []
    if not items:
        return {"ok": False, "error": "no memories to summarize", "status_code": 400}
    cfg = load_config()
    model = model or (cfg.get("models") or {}).get("local_reviewer") or "qwen3.8:latest"
    body = "\n\n".join(
        "[%s] %s\n%s" % (m.get("kind"), m.get("title"), (m.get("content") or "")[:1000])
        for m in items
    )
    prompt = (
        "Summarize these local AI rig memories into stable preferences, recurring failures, "
        "project constraints, and next-action hints. Keep it under 500 words. "
        "Do not invent facts.\n\n%s" % body
    )
    try:
        output = _ollama_review(model, prompt)
    except Exception as exc:
        return {"ok": False, "error": str(exc), "status_code": 502}
    saved = add_memory(
        "summary",
        "AI stack memory summary",
        output,
        source="aistack:summarize",
        metadata={"model": model, "input_count": len(items)},
        pinned=True,
        force=True,
    )
    return {"ok": True, "model": model, "output": output, "memory_id": saved.get("id")}
