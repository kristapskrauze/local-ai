"""Goals for Dobby Console — big goals, the small goals that ladder up to
them, and the data behind the visualisations.

Model, deliberately small:
  * a goal has a HORIZON (now -> life). Big goals sit at the far end, small
    goals at the near end.
  * a small goal can hang off a big one (parent_id). A parent's progress is
    DERIVED from its children, so ticking off the small stuff moves the big
    stuff — that is the whole point of the view.
  * check-ins are logged per day, which gives the momentum grid. Progress bars
    lie about whether you touched something this week; a streak grid does not.

SQLite lives next to the Hermes store so all durable console state is in one
place. No new dependencies — stdlib sqlite3, same as ai_stack.
"""
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from datetime import date, datetime, timedelta, timezone

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_DIR = os.path.join(PROJECT_ROOT, ".ai_memory")
DB_PATH = os.path.join(DB_DIR, "goals.sqlite3")

_LOCK = threading.RLock()

# Near -> far. The UI renders them in this order, so it is the source of truth.
HORIZONS = ["now", "week", "month", "quarter", "year", "life"]
HORIZON_LABELS = {
    "now": "Now", "week": "This week", "month": "This month",
    "quarter": "This quarter", "year": "This year", "life": "Life",
}
# Anything at or beyond this horizon counts as a "big" goal.
BIG_FROM = "quarter"
STATUSES = ["active", "done", "paused", "dropped", "blocked", "archived"]
STATUS_ALIASES = {
    "completed": "done",
    "complete": "done",
    "deferred": "paused",
    "defer": "paused",
    "snoozed": "paused",
    "cancelled": "dropped",
    "canceled": "dropped",
    "cancel": "dropped",
}
STATUS_PUBLIC = {
    "active": "active",
    "done": "completed",
    "paused": "deferred",
    "dropped": "cancelled",
    "blocked": "blocked",
    "archived": "archived",
}
IMPORTANCES = ["small", "normal", "important", "major"]
RESURFACE_MODES = ["contextual", "manual_only"]
VISIBILITIES = ["private_self", "assistant_context"]
KINDS = ["goal", "project"]

DEFAULT_AREAS = ["health", "mind", "career", "money", "learning", "home", "people", "fun"]

_WORD_RE = re.compile(r"\w{3,}", re.UNICODE)
_STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "what", "when", "where",
    "how", "are", "you", "your", "should", "would", "could", "from", "have",
    "has", "not", "can", "all", "use", "using", "about", "into", "over",
    "its", "was", "were", "will", "which", "their", "there", "then", "than",
    "them", "some", "any", "our", "out", "get", "set", "please", "make",
    "want", "need", "just", "goal", "goals", "thing", "stuff", "little",
}
_GOALISH_FOCUS_RE = re.compile(
    r"\b(goals?|trying to finish|what should i work on|what should i focus|"
    r"still need to|unfinished|progress on lately|knock out)\b", re.I)


def _connect():
    os.makedirs(DB_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _columns(conn, table):
    return {r["name"] for r in conn.execute("PRAGMA table_info(%s)" % table)}


def _add_column(conn, table, name, ddl):
    if name not in _columns(conn, table):
        conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, name, ddl))


def _norm_status(status):
    value = str(status or "active").strip().lower()
    value = STATUS_ALIASES.get(value, value)
    return value if value in STATUSES else "active"


def _public_status(status):
    return STATUS_PUBLIC.get(_norm_status(status), _norm_status(status))


def _norm_importance(value):
    text = str(value or "normal").strip().lower()
    aliases = {"low": "small", "minor": "small", "medium": "normal",
               "high": "important", "critical": "major", "big": "major"}
    text = aliases.get(text, text)
    return text if text in IMPORTANCES else "normal"


def _norm_resurface_mode(value):
    text = str(value or "contextual").strip().lower()
    return text if text in RESURFACE_MODES else "contextual"


def _norm_visibility(value):
    text = str(value or "private_self").strip().lower()
    aliases = {"private": "private_self", "self": "private_self",
               "assistant": "assistant_context"}
    text = aliases.get(text, text)
    return text if text in VISIBILITIES else "private_self"


def _norm_kind(value, horizon="week"):
    text = str(value or "").strip().lower()
    if text in KINDS:
        return text
    return "project" if horizon in ("quarter", "year", "life") else "goal"


def _contexts(value):
    if value is None:
        return []
    if isinstance(value, str):
        raw = value.strip()
        if raw.startswith("["):
            try:
                value = json.loads(raw)
            except Exception:
                value = raw.split(",")
        else:
            value = raw.split(",")
    out, seen = [], set()
    for item in value if isinstance(value, (list, tuple, set)) else [value]:
        text = re.sub(r"[^a-z0-9_+-]+", "_", str(item or "").strip().lower())
        text = text.strip("_")[:40]
        if text and text not in seen:
            seen.add(text)
            out.append(text)
        if len(out) >= 12:
            break
    return out


def _contexts_json(value):
    return json.dumps(_contexts(value), ensure_ascii=False, sort_keys=True)


def _words(text):
    out = []
    for word in _WORD_RE.findall(str(text or "").lower()):
        if word not in _STOPWORDS and word not in out:
            out.append(word)
    return out


def _dedupe_key(title, area="", contexts=None, parent_id=None):
    words = _words(title)[:10]
    ctx = _contexts(contexts)[:6]
    parent = str(parent_id or "")
    return "|".join([str(area or "").strip().lower(), parent,
                     " ".join(words), ",".join(ctx)])[:240]


def _json_dumps(obj):
    try:
        return json.dumps(obj if obj is not None else {}, ensure_ascii=False,
                          sort_keys=True, default=str)
    except Exception:
        return "{}"


def _event(conn, goal_id, event_type, actor="", source="", action_id="",
           from_state=None, to_state=None, note=""):
    conn.execute(
        """INSERT INTO goal_events
           (goal_id, ts, event_type, actor, source, action_id, from_json, to_json, note)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (int(goal_id), time.time(), str(event_type or "")[:40],
         str(actor or "")[:120], str(source or "")[:240],
         str(action_id or "")[:160], _json_dumps(from_state),
         _json_dumps(to_state), str(note or "")[:2000]))


def _backfill_phase6_fields(conn):
    rows = conn.execute(
        "SELECT id, uuid, title, area, parent_id, horizon, contexts_json, "
        "dedupe_key, kind FROM goals").fetchall()
    for row in rows:
        sets, vals = [], []
        if not row["uuid"]:
            sets.append("uuid=?")
            vals.append("goal_" + uuid.uuid4().hex[:12])
        if not row["contexts_json"]:
            sets.append("contexts_json=?")
            vals.append("[]")
        if not row["dedupe_key"]:
            sets.append("dedupe_key=?")
            vals.append(_dedupe_key(row["title"], row["area"], [], row["parent_id"]))
        if not row["kind"]:
            sets.append("kind=?")
            vals.append(_norm_kind("", row["horizon"]))
        if sets:
            vals.append(row["id"])
            conn.execute("UPDATE goals SET %s WHERE id=?" % ", ".join(sets), vals)


def _init():
    with _LOCK, _connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS goals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                detail TEXT DEFAULT '',
                horizon TEXT NOT NULL DEFAULT 'week',
                area TEXT DEFAULT '',
                parent_id INTEGER,
                status TEXT NOT NULL DEFAULT 'active',
                progress INTEGER NOT NULL DEFAULT 0,
                target_date TEXT,
                sort_order INTEGER DEFAULT 0,
                created REAL NOT NULL,
                updated REAL NOT NULL,
                completed REAL
            )""")
        for name, ddl in (
                ("uuid", "TEXT"),
                ("importance", "TEXT NOT NULL DEFAULT 'normal'"),
                ("contexts_json", "TEXT NOT NULL DEFAULT '[]'"),
                ("resurface_mode", "TEXT NOT NULL DEFAULT 'contextual'"),
                ("last_surfaced", "REAL"),
                ("surface_count", "INTEGER NOT NULL DEFAULT 0"),
                ("last_progress", "REAL"),
                ("snoozed_until", "REAL"),
                ("visibility", "TEXT NOT NULL DEFAULT 'private_self'"),
                ("source", "TEXT NOT NULL DEFAULT ''"),
                ("kind", "TEXT NOT NULL DEFAULT 'goal'"),
                ("dedupe_key", "TEXT NOT NULL DEFAULT ''"),
                ("done_definition", "TEXT NOT NULL DEFAULT ''"),
        ):
            _add_column(conn, "goals", name, ddl)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS goal_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                goal_id INTEGER NOT NULL,
                day TEXT NOT NULL,
                note TEXT DEFAULT '',
                created REAL NOT NULL
            )""")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS goal_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                goal_id INTEGER NOT NULL,
                ts REAL NOT NULL,
                event_type TEXT NOT NULL,
                actor TEXT DEFAULT '',
                source TEXT DEFAULT '',
                action_id TEXT DEFAULT '',
                from_json TEXT DEFAULT '',
                to_json TEXT DEFAULT '',
                note TEXT DEFAULT ''
            )""")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS goal_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_goals_parent ON goals(parent_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_goals_status_visibility ON goals(status, visibility)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_goals_surface ON goals(last_surfaced, snoozed_until)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_goals_updated ON goals(updated)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_goals_dedupe ON goals(dedupe_key)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_log_day ON goal_log(day)")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_log_goal_day ON goal_log(goal_id, day)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_goal_events_goal_ts ON goal_events(goal_id, ts)")
        _backfill_phase6_fields(conn)


_init()


def _now():
    return time.time()


def _today():
    return date.today().isoformat()


def _row(r):
    d = dict(r)
    d["is_big"] = HORIZONS.index(d["horizon"]) >= HORIZONS.index(BIG_FROM) \
        if d["horizon"] in HORIZONS else False
    d["status"] = _norm_status(d.get("status"))
    d["status_label"] = _public_status(d.get("status"))
    d["importance"] = _norm_importance(d.get("importance"))
    d["contexts"] = _contexts(d.get("contexts_json"))
    d["resurface_mode"] = _norm_resurface_mode(d.get("resurface_mode"))
    d["visibility"] = _norm_visibility(d.get("visibility"))
    d["kind"] = _norm_kind(d.get("kind"), d.get("horizon"))
    return d



def _derived_progress(conn, goal_id):
    """A parent's progress is the mean of its children's. Only leaves carry a
    number the user set by hand."""
    kids = conn.execute(
        "SELECT progress, status FROM goals WHERE parent_id=? AND status NOT IN ('dropped')",
        (goal_id,)).fetchall()
    if not kids:
        return None
    total = 0
    for k in kids:
        total += 100 if k["status"] == "done" else max(0, min(100, k["progress"] or 0))
    return int(round(total / len(kids)))


def _decorate(conn, rows):
    out = []
    for r in rows:
        g = _row(r)
        derived = _derived_progress(conn, g["id"])
        g["child_count"] = conn.execute(
            "SELECT COUNT(*) FROM goals WHERE parent_id=?", (g["id"],)).fetchone()[0]
        g["done_children"] = conn.execute(
            "SELECT COUNT(*) FROM goals WHERE parent_id=? AND status='done'", (g["id"],)).fetchone()[0]
        if g["status"] == "done":
            g["effective_progress"] = 100
        elif derived is not None:
            g["effective_progress"] = derived
        else:
            g["effective_progress"] = max(0, min(100, g["progress"] or 0))
        g["derived"] = derived is not None and g["status"] != "done"
        g["checked_today"] = bool(conn.execute(
            "SELECT 1 FROM goal_log WHERE goal_id=? AND day=?", (g["id"], _today())).fetchone())
        out.append(g)
    return out


def _public_goal(goal):
    keep = (
        "id", "uuid", "title", "detail", "horizon", "area", "parent_id",
        "status", "status_label", "progress", "target_date", "sort_order",
        "created", "updated", "completed", "importance", "contexts",
        "resurface_mode", "last_surfaced", "surface_count", "last_progress",
        "snoozed_until", "visibility", "source", "kind", "done_definition",
        "is_big", "child_count", "done_children", "effective_progress",
        "derived", "checked_today", "parent_title",
    )
    return {k: goal.get(k) for k in keep if k in goal}


def _fetch_public(conn, goal_id):
    row = conn.execute("SELECT * FROM goals WHERE id=?", (goal_id,)).fetchone()
    if not row:
        return None
    return _public_goal(_decorate(conn, [row])[0])


def _parse_ts(value):
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    raw = str(value or "").strip()
    if not raw:
        return None
    low = raw.lower()
    now = datetime.now()
    if low in ("tomorrow", "rīt", "rit"):
        return (now + timedelta(days=1)).replace(
            hour=9, minute=0, second=0, microsecond=0).timestamp()
    if low in ("next week", "nākamnedēļ", "nakamnedel"):
        return (now + timedelta(days=7)).replace(
            hour=9, minute=0, second=0, microsecond=0).timestamp()
    if low in ("next month", "nākammēnes", "nakammenes"):
        return (now + timedelta(days=30)).replace(
            hour=9, minute=0, second=0, microsecond=0).timestamp()
    try:
        return float(raw)
    except Exception:
        pass
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            dt = dt.astimezone()
        return dt.timestamp()
    except Exception:
        return None


def _status_value(value):
    raw = str(value or "active").strip().lower()
    status = STATUS_ALIASES.get(raw, raw)
    return status if status in STATUSES else None


def _cooldown_seconds(importance):
    return {
        "small": 14 * 24 * 3600,
        "normal": 7 * 24 * 3600,
        "important": 3 * 24 * 3600,
        "major": 24 * 3600,
    }.get(_norm_importance(importance), 7 * 24 * 3600)


def _goal_search_text(goal):
    parts = [
        goal.get("title") or "",
        goal.get("detail") or "",
        goal.get("area") or "",
        goal.get("horizon") or "",
        goal.get("kind") or "",
        goal.get("done_definition") or "",
        " ".join(goal.get("contexts") or []),
    ]
    return " ".join(str(p) for p in parts if p)


def _score_goal(goal, query_words, goalish=False):
    goal_words = set(_words(_goal_search_text(goal)))
    overlap = [w for w in query_words if w in goal_words]
    score = len(overlap) * 4
    ctx = set(goal.get("contexts") or [])
    score += len([w for w in query_words if w in ctx]) * 3
    if goalish:
        score += {"small": 1, "normal": 2, "important": 4, "major": 6}.get(
            _norm_importance(goal.get("importance")), 2)
    if goal.get("checked_today"):
        score -= 1
    if goal.get("last_progress"):
        try:
            age_days = max(0, (_now() - float(goal.get("last_progress"))) / 86400.0)
            if age_days >= 7:
                score += 1
        except Exception:
            pass
    return max(0, score), overlap



def create(title, horizon="week", area="", detail="", parent_id=None, target_date=None,
           importance="normal", contexts=None, resurface_mode="contextual",
           visibility="private_self", source="", kind="", done_definition="",
           actor="", action_id="", dedupe=True):
    title = (title or "").strip()
    if not title:
        return {"ok": False, "error": "title is required", "status_code": 400}
    if horizon not in HORIZONS:
        return {"ok": False, "error": "unknown horizon: %s" % horizon, "status_code": 400}
    now = _now()
    area = (area or "").strip().lower()
    detail = (detail or "").strip()
    contexts = _contexts(contexts)
    importance = _norm_importance(importance)
    resurface_mode = _norm_resurface_mode(resurface_mode)
    visibility = _norm_visibility(visibility)
    kind = _norm_kind(kind, horizon)
    dedupe_key = _dedupe_key(title, area, contexts, parent_id)
    with _LOCK, _connect() as conn:
        if parent_id:
            if not conn.execute("SELECT 1 FROM goals WHERE id=?", (parent_id,)).fetchone():
                return {"ok": False, "error": "parent goal not found", "status_code": 404}
        if dedupe and dedupe_key:
            duplicate = conn.execute(
                """SELECT * FROM goals
                   WHERE dedupe_key=? AND status NOT IN ('done','dropped','archived')
                   ORDER BY created LIMIT 1""",
                (dedupe_key,)).fetchone()
            if duplicate:
                goal = _fetch_public(conn, duplicate["id"])
                return {
                    "ok": True, "created": False, "duplicate": True,
                    "id": duplicate["id"], "goal": goal,
                }
        cur = conn.execute(
            """INSERT INTO goals
               (uuid, title, detail, horizon, area, parent_id, status, progress,
                target_date, created, updated, importance, contexts_json,
                resurface_mode, visibility, source, kind, dedupe_key,
                done_definition)
               VALUES (?,?,?,?,?,?,'active',0,?,?,?,?,?,?,?,?,?,?,?)""",
            ("goal_" + uuid.uuid4().hex[:12], title, detail, horizon, area,
             parent_id, target_date, now, now, importance,
             _contexts_json(contexts), resurface_mode, visibility,
             str(source or "")[:240], kind, dedupe_key,
             str(done_definition or "")[:1000]))
        goal = _fetch_public(conn, cur.lastrowid)
        _event(conn, cur.lastrowid, "created", actor=actor, source=source,
               action_id=action_id, to_state=goal, note=detail)
        return {"ok": True, "created": True, "id": cur.lastrowid, "goal": goal}


def update(goal_id, **fields):
    allowed = {"title", "detail", "horizon", "area", "status", "progress",
               "target_date", "parent_id", "sort_order", "importance",
               "contexts", "resurface_mode", "visibility", "source", "kind",
               "done_definition", "snoozed_until", "last_surfaced",
               "last_progress", "surface_count"}
    actor = str(fields.pop("actor", "") or "")
    event_source = str(fields.pop("event_source", "") or "")
    action_id = str(fields.pop("action_id", "") or "")
    event_type = str(fields.pop("event_type", "updated") or "updated")
    event_note = str(fields.pop("event_note", "") or "")
    updates = {}
    for key, value in fields.items():
        if key not in allowed or value is None:
            continue
        if key == "horizon" and value not in HORIZONS:
            return {"ok": False, "error": "unknown horizon: %s" % value, "status_code": 400}
        if key == "status":
            value = _status_value(value)
            if value is None:
                return {"ok": False, "error": "unknown status: %s" % fields.get(key),
                        "status_code": 400}
        if key == "progress":
            value = max(0, min(100, int(value)))
        if key == "parent_id" and value == goal_id:
            return {"ok": False, "error": "a goal cannot be its own parent", "status_code": 400}
        if key == "area":
            value = str(value or "").strip().lower()
        elif key in ("title", "detail", "target_date", "source", "done_definition"):
            value = str(value or "").strip()
        elif key == "importance":
            value = _norm_importance(value)
        elif key == "contexts":
            key = "contexts_json"
            value = _contexts_json(value)
        elif key == "resurface_mode":
            value = _norm_resurface_mode(value)
        elif key == "visibility":
            value = _norm_visibility(value)
        elif key == "kind":
            value = _norm_kind(value, fields.get("horizon") or "week")
        elif key in ("snoozed_until", "last_surfaced", "last_progress"):
            value = _parse_ts(value)
        updates[key] = value
    if not updates:
        return {"ok": False, "error": "nothing to update", "status_code": 400}
    now = _now()
    updates["updated"] = now
    if updates.get("status") == "done":
        updates["completed"] = now
        updates["progress"] = 100
    elif "status" in updates:
        updates["completed"] = None
    if "progress" in updates:
        updates["last_progress"] = now
    with _LOCK, _connect() as conn:
        before_row = conn.execute("SELECT * FROM goals WHERE id=?", (goal_id,)).fetchone()
        if not before_row:
            return {"ok": False, "error": "goal not found", "status_code": 404}
        if "parent_id" in updates and updates["parent_id"]:
            if not conn.execute("SELECT 1 FROM goals WHERE id=?",
                                (updates["parent_id"],)).fetchone():
                return {"ok": False, "error": "parent goal not found", "status_code": 404}
        future = dict(before_row)
        future.update(updates)
        if any(k in updates for k in ("title", "area", "contexts_json", "parent_id")):
            updates["dedupe_key"] = _dedupe_key(
                future.get("title"), future.get("area"),
                future.get("contexts_json"), future.get("parent_id"))
        sets = ["%s=?" % k for k in updates]
        values = [updates[k] for k in updates]
        values.append(goal_id)
        cur = conn.execute("UPDATE goals SET %s WHERE id=?" % ", ".join(sets), values)
        if not cur.rowcount:
            return {"ok": False, "error": "goal not found", "status_code": 404}
        goal = _fetch_public(conn, goal_id)
        _event(conn, goal_id, event_type, actor=actor, source=event_source,
               action_id=action_id, from_state=_public_goal(_row(before_row)),
               to_state=goal, note=event_note)
    return {"ok": True, "id": goal_id, "goal": goal}


def delete(goal_id):
    with _LOCK, _connect() as conn:
        # Orphan rather than cascade — losing a week of small goals because a
        # big one got deleted would be a nasty surprise.
        conn.execute("UPDATE goals SET parent_id=NULL WHERE parent_id=?", (goal_id,))
        conn.execute("DELETE FROM goal_log WHERE goal_id=?", (goal_id,))
        cur = conn.execute("DELETE FROM goals WHERE id=?", (goal_id,))
        if not cur.rowcount:
            return {"ok": False, "error": "goal not found", "status_code": 404}
    return {"ok": True, "id": goal_id}


def check_in(goal_id, note=""):
    """Mark today as a day this goal got worked on. Idempotent per day; calling
    it again on the same day clears it, so the grid cell toggles."""
    today = _today()
    with _LOCK, _connect() as conn:
        row = conn.execute("SELECT * FROM goals WHERE id=?", (goal_id,)).fetchone()
        if not row:
            return {"ok": False, "error": "goal not found", "status_code": 404}
        existing = conn.execute(
            "SELECT id FROM goal_log WHERE goal_id=? AND day=?", (goal_id, today)).fetchone()
        if existing:
            conn.execute("DELETE FROM goal_log WHERE id=?", (existing["id"],))
            conn.execute("UPDATE goals SET updated=? WHERE id=?", (_now(), goal_id))
            _event(conn, goal_id, "checkin_removed", from_state=_public_goal(_row(row)),
                   to_state=_fetch_public(conn, goal_id), note=note)
            return {"ok": True, "checked": False, "day": today}
        conn.execute("INSERT INTO goal_log (goal_id, day, note, created) VALUES (?,?,?,?)",
                     (goal_id, today, (note or "").strip(), _now()))
        conn.execute("UPDATE goals SET updated=?, last_progress=? WHERE id=?",
                     (_now(), _now(), goal_id))
        _event(conn, goal_id, "checkin", from_state=_public_goal(_row(row)),
               to_state=_fetch_public(conn, goal_id), note=note)
    return {"ok": True, "checked": True, "day": today}


def get(goal_id):
    with _LOCK, _connect() as conn:
        goal = _fetch_public(conn, goal_id)
    if not goal:
        return {"ok": False, "error": "goal not found", "status_code": 404}
    return {"ok": True, "goal": goal}


def list_goals(status="active", limit=20, include_private=True):
    status = str(status or "active").strip().lower()
    params = []
    where = []
    if status not in ("", "all", "*"):
        status_value = _status_value(status)
        if status_value is None:
            return {"ok": False, "error": "unknown status: %s" % status,
                    "status_code": 400}
        where.append("status=?")
        params.append(status_value)
    if not include_private:
        where.append("visibility='assistant_context'")
    sql = "SELECT * FROM goals"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY sort_order, updated DESC, created DESC LIMIT ?"
    params.append(max(1, min(100, int(limit or 20))))
    with _LOCK, _connect() as conn:
        rows = conn.execute(sql, params).fetchall()
        items = [_public_goal(g) for g in _decorate(conn, rows)]
    return {"ok": True, "goals": items, "count": len(items)}


def search_goals(query, status="active", limit=8, include_private=True):
    words = _words(query)
    if not words:
        return {"ok": True, "goals": [], "count": 0}
    base = list_goals(status=status, limit=100, include_private=include_private)
    if not base.get("ok"):
        return base
    scored = []
    for goal in base.get("goals") or []:
        score, overlap = _score_goal(goal, words, goalish=False)
        if score > 0:
            item = dict(goal)
            item["score"] = score
            item["matched_terms"] = overlap
            scored.append(item)
    scored.sort(key=lambda g: (-g.get("score", 0), -(g.get("updated") or 0), g.get("title") or ""))
    scored = scored[:max(1, min(20, int(limit or 8)))]
    return {"ok": True, "goals": scored, "count": len(scored)}


def progress(goal_id, note="", progress=None, actor="", source="", action_id=""):
    if progress is None and not str(note or "").strip():
        return {"ok": False, "error": "progress note or percentage is required",
                "status_code": 400}
    updates = {}
    if progress is not None:
        updates["progress"] = max(0, min(100, int(progress)))
    now = _now()
    today = _today()
    with _LOCK, _connect() as conn:
        before_row = conn.execute("SELECT * FROM goals WHERE id=?", (goal_id,)).fetchone()
        if not before_row:
            return {"ok": False, "error": "goal not found", "status_code": 404}
        conn.execute(
            "INSERT OR IGNORE INTO goal_log (goal_id, day, note, created) VALUES (?,?,?,?)",
            (goal_id, today, str(note or "").strip(), now))
        if updates:
            conn.execute("UPDATE goals SET progress=?, last_progress=?, updated=? WHERE id=?",
                         (updates["progress"], now, now, goal_id))
        else:
            conn.execute("UPDATE goals SET last_progress=?, updated=? WHERE id=?",
                         (now, now, goal_id))
        goal = _fetch_public(conn, goal_id)
        _event(conn, goal_id, "progress", actor=actor, source=source,
               action_id=action_id, from_state=_public_goal(_row(before_row)),
               to_state=goal, note=note)
    return {"ok": True, "id": goal_id, "goal": goal}


def complete(goal_id, note="", actor="", action_id=""):
    return update(goal_id, status="done", progress=100, actor=actor,
                  action_id=action_id, event_type="completed",
                  event_note=note)


def cancel(goal_id, note="", actor="", action_id=""):
    return update(goal_id, status="dropped", actor=actor, action_id=action_id,
                  event_type="cancelled", event_note=note)


def defer(goal_id, until=None, note="", actor="", action_id=""):
    updates = {
        "status": "paused",
        "actor": actor,
        "action_id": action_id,
        "event_type": "deferred",
        "event_note": note,
    }
    if until not in (None, ""):
        ts = _parse_ts(until)
        if ts is None:
            return {"ok": False, "error": "could not parse snooze time",
                    "status_code": 400}
        updates["snoozed_until"] = ts
    return update(goal_id, **updates)


def mark_surfaced(goal_id, actor="", source="", action_id=""):
    now = _now()
    with _LOCK, _connect() as conn:
        before_row = conn.execute("SELECT * FROM goals WHERE id=?", (goal_id,)).fetchone()
        if not before_row:
            return {"ok": False, "error": "goal not found", "status_code": 404}
        conn.execute(
            """UPDATE goals
               SET last_surfaced=?, surface_count=COALESCE(surface_count,0)+1,
                   updated=?
               WHERE id=?""",
            (now, now, goal_id))
        goal = _fetch_public(conn, goal_id)
        _event(conn, goal_id, "surfaced", actor=actor, source=source,
               action_id=action_id, from_state=_public_goal(_row(before_row)),
               to_state=goal)
    return {"ok": True, "id": goal_id, "goal": goal}


def events(goal_id, limit=20):
    with _LOCK, _connect() as conn:
        if not conn.execute("SELECT 1 FROM goals WHERE id=?", (goal_id,)).fetchone():
            return {"ok": False, "error": "goal not found", "status_code": 404}
        rows = conn.execute(
            """SELECT id, goal_id, ts, event_type, actor, source, action_id,
                      from_json, to_json, note
               FROM goal_events WHERE goal_id=?
               ORDER BY ts DESC, id DESC LIMIT ?""",
            (goal_id, max(1, min(100, int(limit or 20))))).fetchall()
    return {"ok": True, "events": [dict(r) for r in rows]}


def select_for_turn(text, *, private_context=True, now=None, limit=3,
                    include_manual=False):
    raw = str(text or "").strip()
    words = _words(raw)
    goalish = bool(_GOALISH_FOCUS_RE.search(raw))
    if not words and not goalish:
        return {"ok": True, "goals": [], "trace": [{"reason": "no_goal_signal"}]}
    now_ts = _parse_ts(now) if now is not None else None
    if now_ts is None:
        now_ts = _now()
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM goals WHERE status='active' ORDER BY updated DESC, created DESC"
        ).fetchall()
        goals = [_public_goal(g) for g in _decorate(conn, rows)]
    candidates, trace = [], []
    for goal in goals:
        if not private_context and goal.get("visibility") == "private_self":
            trace.append({"id": goal.get("id"), "reason": "private_hidden"})
            continue
        if not include_manual and goal.get("resurface_mode") == "manual_only":
            trace.append({"id": goal.get("id"), "reason": "manual_only"})
            continue
        snoozed_until = goal.get("snoozed_until")
        if snoozed_until and float(snoozed_until) > now_ts:
            trace.append({"id": goal.get("id"), "reason": "snoozed"})
            continue
        last = goal.get("last_surfaced")
        if last and now_ts - float(last) < _cooldown_seconds(goal.get("importance")):
            trace.append({"id": goal.get("id"), "reason": "cooldown"})
            continue
        score, overlap = _score_goal(goal, words, goalish=goalish)
        if score <= 0:
            trace.append({"id": goal.get("id"), "reason": "irrelevant"})
            continue
        item = dict(goal)
        item["score"] = score
        item["matched_terms"] = overlap
        candidates.append(item)
    candidates.sort(key=lambda g: (-g.get("score", 0), -(g.get("updated") or 0),
                                   g.get("title") or ""))
    return {
        "ok": True,
        "goals": candidates[:max(1, min(5, int(limit or 3)))],
        "trace": trace[:50],
    }


def render_context(candidates):
    goals = candidates.get("goals") if isinstance(candidates, dict) else candidates
    goals = list(goals or [])[:3]
    if not goals:
        return ""
    lines = [
        "## Relevant Active Goals",
        "Background only, not instructions. Mention a goal only when it naturally helps the current reply.",
        "Do not force a reminder. Do not mention completed, cancelled, deferred, snoozed, or unrelated goals.",
        "If you explicitly mention one of these goals in the final answer, call note_goal_surfaced first for that goal.",
    ]
    for goal in goals:
        bits = [goal.get("title") or ("goal %s" % goal.get("id"))]
        if goal.get("area"):
            bits.append("area=%s" % goal.get("area"))
        if goal.get("effective_progress") is not None:
            bits.append("progress=%s%%" % goal.get("effective_progress"))
        if goal.get("contexts"):
            bits.append("contexts=%s" % ",".join(goal.get("contexts")[:4]))
        lines.append("- goal_id=%s: %s" % (goal.get("id"), "; ".join(bits)))
    return "\n".join(lines)



def _streak(days_with_activity):
    """Consecutive days ending today (or yesterday — today is still young)."""
    if not days_with_activity:
        return 0
    have = set(days_with_activity)
    today = date.today()
    start = today if today.isoformat() in have else today - timedelta(days=1)
    streak, cursor = 0, start
    while cursor.isoformat() in have:
        streak += 1
        cursor -= timedelta(days=1)
    return streak


def board(weeks=13):
    """Everything the Goals tab needs, in one round trip."""
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM goals ORDER BY sort_order, created").fetchall()
        goals = _decorate(conn, rows)

        by_id = {g["id"]: g for g in goals}
        for g in goals:
            parent = by_id.get(g["parent_id"])
            g["parent_title"] = parent["title"] if parent else None

        # momentum grid: one cell per day for the last `weeks` weeks
        span = weeks * 7
        first = date.today() - timedelta(days=span - 1)
        log = conn.execute(
            "SELECT day, COUNT(*) AS n FROM goal_log WHERE day>=? GROUP BY day",
            (first.isoformat(),)).fetchall()
        counts = {r["day"]: r["n"] for r in log}
        grid = []
        for i in range(span):
            day = (first + timedelta(days=i)).isoformat()
            grid.append({"day": day, "count": counts.get(day, 0)})

        all_days = [r["day"] for r in conn.execute(
            "SELECT DISTINCT day FROM goal_log ORDER BY day").fetchall()]

    active = [g for g in goals if g["status"] == "active"]
    done = [g for g in goals if g["status"] == "done"]

    # per-area rollup for the rings
    areas = {}
    for g in goals:
        if g["status"] == "dropped":
            continue
        key = g["area"] or "unsorted"
        bucket = areas.setdefault(key, {"area": key, "total": 0, "done": 0, "progress_sum": 0})
        bucket["total"] += 1
        bucket["progress_sum"] += g["effective_progress"]
        if g["status"] == "done":
            bucket["done"] += 1
    area_list = []
    for bucket in areas.values():
        bucket["progress"] = int(round(bucket["progress_sum"] / bucket["total"])) if bucket["total"] else 0
        bucket.pop("progress_sum")
        area_list.append(bucket)
    area_list.sort(key=lambda a: (-a["total"], a["area"]))

    tracked = [g for g in goals if g["status"] in ("active", "done")]
    overall = int(round(sum(g["effective_progress"] for g in tracked) / len(tracked))) if tracked else 0

    return {
        "ok": True,
        "goals": goals,
        "horizons": [{"key": h, "label": HORIZON_LABELS[h],
                      "big": HORIZONS.index(h) >= HORIZONS.index(BIG_FROM)} for h in HORIZONS],
        "areas": area_list,
        "known_areas": DEFAULT_AREAS,
        "grid": grid,
        "streak": _streak(all_days),
        "checked_today": sum(1 for g in goals if g["checked_today"]),
        "stats": {
            "total": len(goals),
            "active": len(active),
            "done": len(done),
            "big": len([g for g in active if g["is_big"]]),
            "small": len([g for g in active if not g["is_big"]]),
            "overall_progress": overall,
        },
        "generated": datetime.now(timezone.utc).isoformat(),
    }


def overall_progress():
    """Single number, for the RGB 'goals' mode and the overview strip."""
    try:
        return board(weeks=1)["stats"]["overall_progress"]
    except Exception:
        return None
