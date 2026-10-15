"""Unresolved requests — what Antons still OWES somebody an answer for.

So a request lives here as its own object, independent of the last inbound
message, and it can only leave in a state that was written down:

    PENDING -> GENERATING -> ANSWERED
                          -> SUPERSEDED_BY_OWNER
                          -> EXPLICITLY_CANCELLED
                          -> FAILED_AND_REPORTED
                          -> BLOCKED_BY_PERMISSION

Never "dropped because another message arrived".

Deterministic on purpose: message ids, senders, timestamps, direct-address
regexes and the rig_agent matcher. No extra model call — this layer exists to
make Antons faster AND more reliable, not to add another Qwen round trip.

State is JSON on disk (`.messaging_history/obligations.json`) so a restart
mid-generation does not forget the obligation, plus an append-only lifecycle
log (`obligations.jsonl`) carrying ids and state transitions only — never
message text.
"""
import json
import logging
import re
import threading
import time
import uuid
from pathlib import Path

LOGGER = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = ROOT / ".messaging_history" / "obligations.json"
LOG_PATH = ROOT / ".messaging_history" / "obligations.jsonl"

TTL_SECONDS = 30 * 60
MERGE_WINDOW_SECONDS = 45

PENDING = "PENDING"
GENERATING = "GENERATING"
ANSWERED = "ANSWERED"
SUPERSEDED = "SUPERSEDED_BY_OWNER"
CANCELLED = "EXPLICITLY_CANCELLED"
FAILED = "FAILED_AND_REPORTED"
BLOCKED = "BLOCKED_BY_PERMISSION"
OPEN_STATES = (PENDING, GENERATING)
TERMINAL_STATES = (ANSWERED, SUPERSEDED, CANCELLED, FAILED, BLOCKED)
# The ONLY transitions allowed out of a terminal state, and why:
#   FAILED -> ANSWERED   the durable send queue retried and the message really
#                        went out, so the request genuinely was discharged.
# Everything else is a duplicate or a stale lane and is refused.
ALLOWED_AFTER_TERMINAL = {FAILED: (ANSWERED,)}

_lock = threading.RLock()
_state = {}
_loaded = [False]


def _load():
    if _loaded[0]:
        return _state
    _loaded[0] = True
    try:
        raw = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            _state.update(raw)
    except Exception:
        pass
    return _state


def _save():
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(_state, ensure_ascii=False, indent=1),
                              encoding="utf-8")
    except Exception as exc:
        LOGGER.info("obligations not persisted: %s", exc)


def _log(request, event):
    """Lifecycle only: ids and states, no message text."""
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "ts": round(time.time(), 3),
            "event": event,
            "request_id": request.get("request_id"),
            "thread_id": request.get("thread_id"),
            "message_ids": list(request.get("message_ids") or []),
            "request_type": request.get("request_type"),
            "state": request.get("state"),
            "generation_versions": request.get("version"),
            "cancelled_generations": request.get("cancelled_generations", 0),
            "state_transitions": list(request.get("transitions") or []),
            "final_outbox_id": request.get("final_outbox_id") or None,
        }
        if request.get("refused_state"):
            row["refused_state"] = request.get("refused_state")
            row["refused_reason"] = request.get("refused_reason")
        with LOG_PATH.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _prune(now=None):
    now = now or time.time()
    dead = []
    for key, req in list(_state.items()):
        age = now - float(req.get("updated_at") or req.get("created_at") or 0)
        if req.get("state") in OPEN_STATES and age > TTL_SECONDS:
            req["state"] = FAILED
            req.setdefault("transitions", []).append(FAILED)
            req["reason"] = "expired unanswered after %.0f min" % (age / 60.0)
            _log(req, "expired")
            dead.append(key)
        elif req.get("state") not in OPEN_STATES and age > TTL_SECONDS:
            dead.append(key)
    for key in dead:
        _state.pop(key, None)
    return bool(dead)


_ADDRESS_RE = re.compile(
    r"^\s*(anton[sai]?|dobby|rig)\b[\s,:!-]|"
    r"\b(anton[sai]?)\s*[,!?]|"
    r"^\s*[/!]\w+", re.IGNORECASE)
_REQUEST_SHAPE_RE = re.compile(
    r"\?|^\s*(pārbaudi|parbaudi|paskaties|atrodi|sameklē|sameklee|uzmeklē|izpēti|"
    r"salīdzini|salidzini|pastāsti|pasaki|saki|rādi|parādi|ieteic|iesaki|piedāvā|"
    r"nokačā|nokaca|lejupielādē|dabū|velc|check|find|search|compare|tell|show|"
    r"look|get|download|grab)\b", re.IGNORECASE)
# "un paskaties arī X", "tieši uz 3090", "paga, domāju Cabrio" — a continuation
# of the request that is being worked on, not a new one and NOT an answer to
# another human.
_CONTINUATION_RE = re.compile(
    r"^\s*(un|arī|ari|vēl|vel|plus|tieši|tiesi|paga|pagaidi|nē|ne|domāju|domaju|"
    r"tas ir|tā ir|labāk|labak|also|and|wait|no|actually|i meant)\b", re.IGNORECASE)


def is_direct_request(text, is_from_me=False, assistant_chat=False,
                      rig_skill=None, addressed=None, follows_bot=False):
    """Deterministic: does this message OBLIGE an answer?"""
    body = str(text or "").strip()
    if not body:
        return False
    if addressed is None:
        addressed = bool(_ADDRESS_RE.search(body))
    if addressed or rig_skill or follows_bot:
        return True
    if assistant_chat and is_from_me:
        return bool(_REQUEST_SHAPE_RE.search(body))
    return False


def looks_like_continuation(text):
    """A follow-up to the request already in flight ("un paskaties arī disku")."""
    return bool(_CONTINUATION_RE.search(str(text or "").strip()))


def open_or_extend(thread_id, entry, request_type="DIRECT", author="OWNER"):
    """Start a request, or add this message to the one already open.

    Consecutive messages from the same author inside MERGE_WINDOW_SECONDS are
    ONE request — "salīdzini Qwen ar Gemma" + "tieši uz 3090" + "Latvian arī
    svarīgs" is a single obligation, and every one of those message ids stays
    attached to it.
    """
    with _lock:
        _load()
        _prune()
        now = time.time()
        key = str(thread_id or "")
        req = _state.get(key)
        mid = str((entry or {}).get("message_guid") or "")
        text = str((entry or {}).get("text") or "").strip()
        if (req and req.get("state") in OPEN_STATES
                and req.get("author") == author
                and now - float(req.get("last_message_at") or 0) <= MERGE_WINDOW_SECONDS):
            if mid and mid not in req["message_ids"]:
                req["message_ids"].append(mid)
                req["texts"].append(text)
                req["version"] = int(req.get("version") or 1) + 1
                req["updated_at"] = req["last_message_at"] = now
                req.setdefault("transitions", []).append("UPDATED_BY_OWNER")
                if req.get("state") == GENERATING:
                    # a generation is running against an older version of this
                    # request: its output is stale before it even finishes
                    req["cancelled_generations"] = int(
                        req.get("cancelled_generations") or 0) + 1
                    req["state"] = PENDING
                inv = req.get("investigation")
                if isinstance(inv, dict) and inv.get("state") == INVESTIGATING:
                    # THE GOAL SURVIVES THE CLARIFICATION. "kāpēc tu ignorēji
                    # Annu?" followed by "domāju vakardienu" is the same
                    # investigation with more to go on, not a new one — so the
                    # original goal stays and the clarification is recorded
                    # beside it. The version bump above already makes the
                    # in-flight loop stale, exactly like a normal generation.
                    if text:
                        inv.setdefault("clarifications", []).append(text)
                    if mid:
                        inv.setdefault("source_message_ids", []).append(mid)
                    inv["restarts"] = int(inv.get("restarts") or 0) + 1
                    inv["version"] = req["version"]
                    inv["next_action"] = "RESTART_WITH_CLARIFICATION"
                _save()
                _log(req, "extended")
            return dict(req)
        req = {
            "request_id": "R" + uuid.uuid4().hex[:8],
            "thread_id": key,
            "author": author,
            "request_type": str(request_type or "DIRECT"),
            "message_ids": [mid] if mid else [],
            "texts": [text] if text else [],
            "state": PENDING,
            "version": 1,
            "cancelled_generations": 0,
            "created_at": now,
            "updated_at": now,
            "last_message_at": now,
            "transitions": [PENDING],
            "final_outbox_id": "",
        }
        _state[key] = req
        _save()
        _log(req, "opened")
        return dict(req)


def current(thread_id):
    """The open request for this thread, or None."""
    with _lock:
        _load()
        _prune()
        req = _state.get(str(thread_id or ""))
        if req and req.get("state") in OPEN_STATES:
            return dict(req)
        return None


def begin_generation(thread_id):
    """Mark GENERATING and return the version this generation belongs to."""
    with _lock:
        _load()
        req = _state.get(str(thread_id or ""))
        if not req or req.get("state") not in OPEN_STATES:
            return None
        req["state"] = GENERATING
        req["updated_at"] = time.time()
        req.setdefault("transitions", []).append(GENERATING)
        _save()
        _log(req, "generating")
        return int(req.get("version") or 1)


def is_stale(thread_id, version):
    """True when newer input arrived after this generation started.

    Cancellation cannot always stop Ollama mid-call, so the last line of
    defence is refusing to SEND a result computed from superseded context.
    """
    if version is None:
        return False
    with _lock:
        _load()
        req = _state.get(str(thread_id or ""))
        if not req:
            return False
        return int(req.get("version") or 1) != int(version)


def resolve(thread_id, state, outbox_id="", reason="", version=None):
    """Close the request in a state that was written down.

    IDEMPOTENT AND TERMINAL-SAFE. A request that has already finished can only
    change state through `ALLOWED_AFTER_TERMINAL`; anything else is recorded as
    a refused duplicate and the stored request is left exactly as it was. Pass
    `version` (from `begin_generation`) to also refuse a resolution computed
    from superseded context — the same staleness rule `is_stale` applies to
    sending, applied to bookkeeping.
    """
    with _lock:
        _load()
        req = _state.get(str(thread_id or ""))
        if not req:
            return None
        want = str(state)
        was = str(req.get("state") or "")
        if version is not None and int(req.get("version") or 1) != int(version):
            # a newer message already extended this request; the generation
            # that is finishing now belongs to the previous version of it
            _log_refusal(req, want, "stale version %s (now %s)"
                         % (version, req.get("version")))
            return dict(req)
        if was in TERMINAL_STATES:
            if want == was:
                # the same lane (or a retry of it) saying the same thing again:
                # not an error, and not worth a second lifecycle row
                return dict(req)
            if want not in ALLOWED_AFTER_TERMINAL.get(was, ()):
                _log_refusal(req, want, "already terminal in %s" % was)
                return dict(req)
        req["state"] = want
        req["updated_at"] = time.time()
        # never blank an outbox id that a real send recorded
        if outbox_id or not req.get("final_outbox_id"):
            req["final_outbox_id"] = str(outbox_id or "")
        if reason:
            req["reason"] = str(reason)[:200]
        req.setdefault("transitions", []).append(want)
        _save()
        _log(req, "resolved")
        return dict(req)


def _log_refusal(req, wanted, why):
    """A refused resolution is evidence, not silence — it names the lane's
    intent and the state that survived."""
    LOGGER.info("obligation %s: refused %s -> %s (%s)",
                req.get("request_id"), req.get("state"), wanted, why)
    try:
        row = dict(req)
        row["refused_state"] = wanted
        row["refused_reason"] = why
        _log(row, "resolve_refused")
    except Exception:
        pass



INVESTIGATING = "INVESTIGATING"
INV_ANSWERED = "ANSWERED"
INV_INSUFFICIENT = "INSUFFICIENT"
INV_FAILED = "FAILED"
INV_SUPERSEDED = "SUPERSEDED"
INV_TERMINAL = (INV_ANSWERED, INV_INSUFFICIENT, INV_FAILED, INV_SUPERSEDED)


def start_investigation(thread_id, goal, requester="OWNER", steps_budget=5,
                        source_message_ids=None):
    """Open (or reopen) the investigation attached to this thread's request."""
    with _lock:
        _load()
        key = str(thread_id or "")
        req = _state.get(key)
        if not req:
            return None
        now = time.time()
        task = {
            "task_id": "T" + uuid.uuid4().hex[:8],
            "requester": str(requester or "OWNER"),
            "thread_id": key,
            "source_message_ids": list(source_message_ids
                                       or req.get("message_ids") or []),
            "goal": str(goal or "")[:500],
            "state": INVESTIGATING,
            "observations": [],
            "evidence": [],
            "clarifications": [],
            "steps_used": 0,
            "steps_budget": int(steps_budget or 5),
            "model_calls": 0,
            "next_action": None,
            "version": int(req.get("version") or 1),
            "started_at": now,
            "updated_at": now,
        }
        req["investigation"] = task
        _save()
        _log(req, "investigation_started")
        return dict(task)


def investigation(thread_id):
    with _lock:
        _load()
        req = _state.get(str(thread_id or "")) or {}
        task = req.get("investigation")
        return dict(task) if isinstance(task, dict) else None


def update_investigation(thread_id, observation=None, evidence=None,
                         steps_used=None, model_calls=None, next_action=None):
    """Record progress. Appends are appends — nothing here overwrites history."""
    with _lock:
        _load()
        req = _state.get(str(thread_id or "")) or {}
        task = req.get("investigation")
        if not isinstance(task, dict):
            return None
        if observation is not None:
            task.setdefault("observations", []).append(observation)
        if evidence is not None:
            items = evidence if isinstance(evidence, list) else [evidence]
            task.setdefault("evidence", []).extend(items)
        if steps_used is not None:
            task["steps_used"] = int(steps_used)
        if model_calls is not None:
            task["model_calls"] = int(model_calls)
        task["next_action"] = next_action
        task["updated_at"] = time.time()
        _save()
        return dict(task)


def finish_investigation(thread_id, state, answer=""):
    """Close the task. Terminal states are protected the same way requests are."""
    with _lock:
        _load()
        req = _state.get(str(thread_id or "")) or {}
        task = req.get("investigation")
        if not isinstance(task, dict):
            return None
        was = str(task.get("state") or "")
        if was in INV_TERMINAL and str(state) != was:
            LOGGER.info("investigation %s: refused %s -> %s (already terminal)",
                        task.get("task_id"), was, state)
            return dict(task)
        task["state"] = str(state)
        task["answer"] = str(answer or "")[:2000]
        task["next_action"] = None
        task["updated_at"] = time.time()
        _save()
        _log(req, "investigation_%s" % str(state).lower())
        return dict(task)


def active_investigations():
    """Tasks still running — what the deploy preflight must not interrupt."""
    with _lock:
        _load()
        _prune()
        out = []
        for req in _state.values():
            task = req.get("investigation")
            if isinstance(task, dict) and task.get("state") == INVESTIGATING:
                out.append(dict(task))
        return out

def request_messages(thread_id):
    """The texts that make up the CURRENT request, oldest first."""
    req = current(thread_id)
    return list((req or {}).get("texts") or [])


def open_requests():
    with _lock:
        _load()
        _prune()
        return [dict(r) for r in _state.values() if r.get("state") in OPEN_STATES]


def status_payload():
    with _lock:
        _load()
        _prune()
        rows = list(_state.values())
    return {
        "ok": True,
        "state_path": str(STATE_PATH),
        "log_path": str(LOG_PATH),
        "open": [{"request_id": r.get("request_id"), "thread_id": r.get("thread_id"),
                  "messages": len(r.get("message_ids") or []), "state": r.get("state"),
                  "version": r.get("version"),
                  "age_seconds": round(time.time() - float(r.get("created_at") or 0), 1)}
                 for r in rows if r.get("state") in OPEN_STATES],
        "tracked": len(rows),
    }


def completion_stats(path=None, limit=5000):
    """DIRECT REQUEST COMPLETION RATE from the lifecycle log."""
    rows = []
    try:
        with open(str(path or LOG_PATH), encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except Exception:
                        pass
    except OSError:
        return {"ok": True, "requests": 0}
    final = {}
    for row in rows[-limit:]:
        final[row.get("request_id")] = row
    counts = {}
    for row in final.values():
        counts[row.get("state")] = counts.get(row.get("state"), 0) + 1
    total = sum(counts.values()) or 1
    answered = counts.get(ANSWERED, 0)
    return {
        "ok": True,
        "requests": sum(counts.values()),
        "by_state": counts,
        "completion_rate": round(answered / float(total), 3),
        "silent_drops": counts.get("expired", 0) + counts.get(FAILED, 0),
        "cancelled_generations": sum(int(r.get("cancelled_generations") or 0)
                                     for r in final.values()),
    }
