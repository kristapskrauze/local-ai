# -*- coding: utf-8 -*-
"""Reminders that actually remind: Antons messages the chat at the time.

Stored in .messaging_history/reminders.json, written atomically. Delivery is
at-most-once per reminder: it is marked before the send, and a transport
failure is left to the durable send queue that send_message already uses.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

LOGGER = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]
STORE_PATH = ROOT / ".messaging_history" / "reminders.json"
_LOCK = threading.RLock()

# A reminder found this late (the service was down at the time) is still sent,
# saying how late; one older than this is marked missed instead of arriving
# the next morning as if it were news.
MAX_LATE_SECONDS = 12 * 3600
LOCATION_MAX_AGE_SECONDS = 12 * 3600
_worker = {"thread": None}

# One zone per country covers every country on a European road trip; the
# islands (Azores, Canaries) are the exceptions and are not on the route.
COUNTRY_TZ = {
    "lv": "Europe/Riga", "lt": "Europe/Vilnius", "ee": "Europe/Tallinn",
    "pl": "Europe/Warsaw", "de": "Europe/Berlin", "nl": "Europe/Amsterdam",
    "be": "Europe/Brussels", "lu": "Europe/Luxembourg", "fr": "Europe/Paris",
    "dk": "Europe/Copenhagen", "se": "Europe/Stockholm", "no": "Europe/Oslo",
    "fi": "Europe/Helsinki", "cz": "Europe/Prague", "sk": "Europe/Bratislava",
    "at": "Europe/Vienna", "ch": "Europe/Zurich", "li": "Europe/Vaduz",
    "it": "Europe/Rome", "sm": "Europe/San_Marino", "va": "Europe/Vatican",
    "mc": "Europe/Monaco", "es": "Europe/Madrid", "ad": "Europe/Andorra",
    "pt": "Europe/Lisbon", "gb": "Europe/London", "ie": "Europe/Dublin",
    "hu": "Europe/Budapest", "si": "Europe/Ljubljana", "hr": "Europe/Zagreb",
    "ba": "Europe/Sarajevo", "rs": "Europe/Belgrade", "me": "Europe/Podgorica",
    "xk": "Europe/Belgrade", "al": "Europe/Tirane", "mk": "Europe/Skopje",
    "gr": "Europe/Athens", "bg": "Europe/Sofia", "ro": "Europe/Bucharest",
    "md": "Europe/Chisinau", "ua": "Europe/Kyiv", "tr": "Europe/Istanbul",
    "is": "Atlantic/Reykjavik", "mt": "Europe/Malta", "cy": "Asia/Nicosia",
}
_country_cache = {}


def _configured_tz():
    try:
        from app import messaging
        return str((messaging.load_config().get("auto_reply") or {}).get("timezone")
                   or "Europe/Riga")
    except Exception:
        return "Europe/Riga"


def country_code_at(lat, lon, fetch=None):
    """Two-letter country code for a point, or "". Only the point rounded to
    one decimal (about 10 km) leaves the rig, and answers are cached."""
    key = (round(float(lat), 1), round(float(lon), 1))
    if key in _country_cache:
        return _country_cache[key]
    url = ("https://nominatim.openstreetmap.org/reverse?"
           + urllib.parse.urlencode({"lat": key[0], "lon": key[1], "zoom": 3,
                                     "format": "json"}))
    try:
        if fetch is not None:
            data = fetch(url)
        else:
            req = urllib.request.Request(url, headers={
                "User-Agent": "dobby-rig-anton/1.0 (personal assistant)"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
        code = str(((data or {}).get("address") or {}).get("country_code") or "").lower()
    except Exception:
        code = ""
    if code:
        _country_cache[key] = code
    return code


def fresh_location(max_age=LOCATION_MAX_AGE_SECONDS, now=None):
    """The latest Find My reading when it is recent enough to act on, else None."""
    try:
        from app import location
        entry = location.latest()
    except Exception:
        entry = None
    if not entry or entry.get("lat") is None or entry.get("lon") is None:
        return None
    age = float(now if now is not None else time.time()) - float(entry.get("ts") or 0)
    if age > max_age:
        return None
    return entry


def local_timezone(now=None, fetch=None):
    entry = fresh_location(now=now)
    if entry:
        code = country_code_at(entry["lat"], entry["lon"], fetch=fetch)
        tz = COUNTRY_TZ.get(code)
        if tz:
            return tz, "location:%s" % (entry.get("city") or code)
    return _configured_tz(), "configured"


def _load():
    try:
        rows = json.loads(STORE_PATH.read_text(encoding="utf-8"))
        return rows if isinstance(rows, list) else []
    except FileNotFoundError:
        return []
    except Exception:
        LOGGER.warning("reminders store unreadable; starting empty")
        return []


def _save(rows):
    STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STORE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, STORE_PATH)


def _public(row):
    return {k: row.get(k) for k in ("id", "text", "due_local", "timezone", "status",
                                    "chat_guid", "created_at", "sent_at")}


def add(chat_guid, text, due, tz_name, created_by=""):
    """Store one reminder. `due` is an aware datetime."""
    text = " ".join(str(text or "").split())
    if not chat_guid or not text or due is None or due.tzinfo is None:
        raise ValueError("chat, text and an aware due time are required")
    row = {
        "id": "r" + uuid.uuid4().hex[:8],
        "chat_guid": str(chat_guid),
        "text": text[:500],
        "due_utc": due.astimezone(timezone.utc).isoformat(timespec="seconds"),
        "due_local": due.isoformat(timespec="minutes"),
        "timezone": str(tz_name),
        "created_by": str(created_by or ""),
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": "pending",
        "attempts": 0,
    }
    with _LOCK:
        rows = _load()
        rows.append(row)
        _save(rows)
    return _public(row)


def pending(chat_guid=None):
    with _LOCK:
        rows = [r for r in _load() if r.get("status") == "pending"]
    if chat_guid:
        rows = [r for r in rows if r.get("chat_guid") == chat_guid]
    rows.sort(key=lambda r: r.get("due_utc") or "")
    return [_public(r) for r in rows]


def cancel(reminder_id, chat_guid=None):
    with _LOCK:
        rows = _load()
        for r in rows:
            if r.get("id") == reminder_id and r.get("status") == "pending" and (
                    not chat_guid or r.get("chat_guid") == chat_guid):
                r["status"] = "cancelled"
                r["cancelled_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
                _save(rows)
                return _public(r)
    return None


def _due_dt(row):
    try:
        return datetime.fromisoformat(str(row.get("due_utc")))
    except Exception:
        return None


def _message(row, late_seconds):
    text = "⏰ " + str(row.get("text") or "")
    if late_seconds >= 15 * 60:
        mins = int(late_seconds // 60)
        late = ("%d min" % mins) if mins < 120 else ("%d h" % round(mins / 60))
        text += " (nokavēju par %s)" % late
    return text


def deliver_due(send, now=None):
    """Send every reminder whose time has come. `send(chat_guid, text)` returns
    True when the message was sent or queued. Returns what was done."""
    now_dt = now or datetime.now(timezone.utc)
    done = []
    with _LOCK:
        rows = _load()
        todo = []
        for r in rows:
            if r.get("status") != "pending":
                continue
            due = _due_dt(r)
            if due is None or due > now_dt:
                continue
            late = (now_dt - due).total_seconds()
            if late > MAX_LATE_SECONDS:
                r["status"] = "missed"
                done.append(("missed", r["id"]))
                continue
            # marked BEFORE the send: at most once, even across a crash
            r["status"] = "sending"
            r["attempts"] = int(r.get("attempts") or 0) + 1
            todo.append((r, late))
        if done or todo:
            _save(rows)
    for r, late in todo:
        ok = False
        try:
            ok = bool(send(r["chat_guid"], _message(r, late)))
        except Exception as exc:
            LOGGER.warning("reminder %s send failed: %s", r["id"], str(exc)[:160])
        with _LOCK:
            rows = _load()
            for row in rows:
                if row.get("id") == r["id"]:
                    if ok:
                        row["status"] = "sent"
                        row["sent_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
                    elif int(row.get("attempts") or 0) < 5:
                        row["status"] = "pending"      # try again next round
                    else:
                        row["status"] = "failed"
            _save(rows)
        done.append(("sent" if ok else "retry", r["id"]))
    return done


def _send_real(chat_guid, text):
    from app import messaging
    acfg = messaging.load_config().get("auto_reply") or {}
    res = messaging.send_message(
        provider=str(acfg.get("provider") or "bluebubbles_http"), target=chat_guid,
        text=text, metadata={"auto_reply": True, "mode": "reminder"}, real_send=True)
    return bool(isinstance(res, dict) and (res.get("sent_real_message") or res.get("queued")
                                           or res.get("ok")))


def _recover_sending():
    """A reminder left 'sending' by a crash mid-send may have gone out; it is
    not sent twice. Logged so it can be checked."""
    with _LOCK:
        rows = _load()
        stuck = [r for r in rows if r.get("status") == "sending"]
        for r in stuck:
            r["status"] = "unknown"
        if stuck:
            _save(rows)
            LOGGER.warning("reminders left mid-send by a restart (not resent): %s",
                           ", ".join(r["id"] for r in stuck))


def loop(interval=20):
    _recover_sending()
    while True:
        try:
            for what, rid in deliver_due(_send_real):
                LOGGER.info("reminder %s %s", rid, what)
        except Exception:
            LOGGER.exception("reminder loop round failed")
        time.sleep(max(5, interval))


def ensure_worker(interval=20):
    """Start the delivery loop once. Called from main on startup."""
    if _worker["thread"] is not None:
        return False
    thread = threading.Thread(target=loop, args=(interval,), name="reminders", daemon=True)
    _worker["thread"] = thread
    thread.start()
    return True
