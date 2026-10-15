# -*- coding: utf-8 -*-
"""Two constraints shaped this:

* `rig_agent.upcoming_events` reads .ics over http(s) and nothing else — there
  is no write path to any calendar, and `calendar_ics` is empty, so there was
  nothing to check against either.
* Writing to Google/iCloud needs credentials, which are not mine to handle.

So Anton keeps its own calendar here and serves it as an .ics feed. Subscribe
to that URL on the phone and the dates show up like any other calendar; add
the same URL to `rig_agent.calendar_ics` and conflict checks see them too. Any
real calendar .ics added there is honoured automatically.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from app.people import OWNER

ROOT = Path(__file__).resolve().parent.parent
PLANS_PATH = ROOT / "config" / "date_plans.json"

# reentrant: list_plans() takes it, and conflicts()/to_ics() call list_plans()
_lock = threading.RLock()

LOG = logging.getLogger(__name__)
STATUSES = ("proposed", "confirmed", "declined", "done", "cancelled")


def _load():
    try:
        raw = json.loads(PLANS_PATH.read_text(encoding="utf-8"))
    except Exception:
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    plans = raw.get("plans")
    return plans if isinstance(plans, list) else []


def _save(plans):
    PLANS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = PLANS_PATH.with_suffix(PLANS_PATH.suffix + ".tmp")
    tmp.write_text(json.dumps({"plans": plans}, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    os.replace(tmp, PLANS_PATH)


def list_plans(include_past=False):
    with _lock:
        plans = _load()
    if include_past:
        return plans
    now = datetime.now()
    out = []
    for p in plans:
        try:
            if datetime.fromisoformat(p.get("start")) >= now - timedelta(hours=6):
                out.append(p)
        except Exception:
            out.append(p)
    return out


def add_plan(start, title, with_whom="", place="", notes="",
             duration_minutes=120, requested_by="", status="proposed"):
    """Record a proposed date. Returns the stored plan."""
    if isinstance(start, datetime):
        start = start.isoformat(timespec="minutes")
    plan = {
        "id": uuid.uuid4().hex[:12],
        "start": str(start),
        "duration_minutes": int(duration_minutes or 120),
        "title": str(title or "Randiņš"),
        "with": str(with_whom or ""),
        "place": str(place or ""),
        "notes": str(notes or ""),
        "requested_by": str(requested_by or ""),
        "status": status if status in STATUSES else "proposed",
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    with _lock:
        plans = _load()
        plans.append(plan)
        _save(plans)
    return plan


def update_plan(plan_id, **fields):
    with _lock:
        plans = _load()
        for p in plans:
            if p.get("id") == plan_id:
                for key, value in fields.items():
                    if key in ("place", "notes", "title", "with", "start",
                               "duration_minutes", "requested_by"):
                        p[key] = value
                    elif key == "status":
                        if value in STATUSES:
                            p[key] = value
                        else:
                            # never drop it in silence: the caller was told the
                            # update succeeded while nothing changed
                            LOG.warning("update_plan: unknown status %r ignored "
                                        "for plan %s", value, plan_id)
                # bumps SEQUENCE in to_ics(), so a subscribed phone picks the
                # change up instead of keeping the original booking
                p["revision"] = int(p.get("revision") or 0) + 1
                _save(plans)
                return p
    return None


def conflicts(start, duration_minutes=120, days_window=14):
    """Anything already booked that overlaps — Anton's plans AND any .ics feed."""
    if isinstance(start, str):
        start = datetime.fromisoformat(start)
    end = start + timedelta(minutes=int(duration_minutes or 120))
    found = []

    for p in list_plans():
        # a cancelled date is not a conflict — without this, cancelling and
        # then re-booking the same slot was refused with "Tajā laikā jau kaut
        # kas ir", pointing at the date that had just been called off
        if p.get("status") in ("declined", "cancelled"):
            continue
        try:
            p_start = datetime.fromisoformat(p.get("start"))
        except Exception:
            continue
        p_end = p_start + timedelta(minutes=int(p.get("duration_minutes") or 120))
        if p_start < end and start < p_end:
            found.append("%s %s" % (p_start.strftime("%d.%m %H:%M"),
                                    p.get("title") or "aizņemts"))

    try:
        from app import rig_agent
        events = rig_agent.upcoming_events(days=int(days_window))
    except Exception:
        events = None
    for item in events or []:
        when, all_day, title, ev_end = rig_agent.event_fields(item)
        if not isinstance(when, datetime):
            continue
        if not isinstance(ev_end, datetime) or ev_end <= when:
            ev_end = when + timedelta(minutes=60)
        if when < end and start < ev_end:
            stamp = when.strftime("%d.%m") if all_day else when.strftime("%d.%m %H:%M")
            found.append("%s %s" % (stamp, title or "aizņemts"))
    return found


def calendar_connected():
    """True when a real calendar feed is configured — otherwise conflict
    checks only see Anton's own plans, and the reply must say so."""
    try:
        from app import rig_agent
        return bool([u for u in (rig_agent.load_config().get("calendar_ics") or [])
                     if str(u or "").strip()])
    except Exception:
        return False


def lv_accusative(name):
    n = str(name or "").strip()
    if not n or " " in n:
        return n
    low = n.lower()
    if low.endswith("is"):
        return n[:-2] + "i"
    if low.endswith("js"):
        return n[:-2] + "ju"
    if low.endswith("us"):          # Markus -> Markusu
        return n[:-1] + "u"
    if low.endswith("s"):
        return n[:-1] + "u"
    if low.endswith("a"):
        return n[:-1] + "u"
    if low.endswith("e"):           # Made -> Madi
        return n[:-1] + "i"
    return n


def organizer_email():
    try:
        from app import messaging
        return str((messaging.load_config().get("auto_reply") or {})
                   .get("organizer_email") or "").strip()
    except Exception:
        return ""


def _ics_escape(text):
    return (str(text or "").replace("\\", "\\\\").replace(";", "\\;")
            .replace(",", "\\,").replace("\n", "\\n"))


def owner_name():
    try:
        from app import messaging
        return str((messaging.load_config().get("auto_reply") or {})
                   .get("owner_name") or OWNER)
    except Exception:
        return OWNER


def to_ics(only_ids=None, share=False, attendee_email=""):
    """The plans as a subscribable calendar feed."""
    ids = {str(i) for i in only_ids} if only_ids else None
    picked = [p for p in list_plans(include_past=True)
              if (ids is None or str(p.get("id")) in ids)
              and p.get("status") != "declined"]
    if not share:
        picked = [p for p in picked if p.get("status") != "cancelled"]
    _cancelled_only = bool(picked) and all(p.get("status") == "cancelled"
                                           for p in picked)
    method = "REQUEST" if (share and not _cancelled_only) else ""
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0",
             "PRODID:-//dobby rig//Anton dates//LV",
             "CALSCALE:GREGORIAN", "X-WR-CALNAME:Anton"]
    if method:
        lines.append("METHOD:%s" % method)
    for p in picked:
        try:
            start = datetime.fromisoformat(p.get("start"))
        except Exception:
            continue
        end = start + timedelta(minutes=int(p.get("duration_minutes") or 120))
        summary = p.get("title") or "Randiņš"
        if p.get("with") and not share:
            summary = "%s ar %s" % (summary, lv_accusative(p["with"]))
        lines += [
            "BEGIN:VEVENT",
            "UID:%s@anton.rig" % p.get("id"),
            "DTSTAMP:%s" % datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
            "DTSTART:%s" % start.strftime("%Y%m%dT%H%M%S"),
            "DTEND:%s" % end.strftime("%Y%m%dT%H%M%S"),
            "SEQUENCE:%d" % int(p.get("revision") or 0),
            "SUMMARY:%s" % _ics_escape(summary),
        ]
        # Calendar ignores a METHOD:CANCEL that has no ORGANIZER — it is the
        # "who is calling this off" of the iTIP exchange, and without it the
        # file is just a preview with nothing to tap.
        if method and organizer_email():
            lines.append("ORGANIZER;CN=%s:mailto:%s"
                         % (_ics_escape(owner_name()), organizer_email()))
            # An RSVP needs someone to ask. Only a real address works — a
            # phone handle is not a mailto and Calendar ignores it — so with a
            # phone-only contact the file stays an add-able event rather than
            # a broken invitation.
            invitee = str(attendee_email or "").strip()
            if "@" in invitee:
                lines.append(
                    "ATTENDEE;CN=%s;ROLE=REQ-PARTICIPANT;PARTSTAT=NEEDS-ACTION;"
                    "RSVP=TRUE:mailto:%s"
                    % (_ics_escape(p.get("with") or invitee), invitee))
        if p.get("place"):
            lines.append("LOCATION:%s" % _ics_escape(p["place"]))
        desc = "" if share else (p.get("notes") or "")
        if p.get("requested_by") and not share:
            desc = ("%s\nPieprasīja: %s" % (desc, p["requested_by"])).strip()
        if desc:
            lines.append("DESCRIPTION:%s" % _ics_escape(desc))
        _status = {"proposed": "TENTATIVE", "cancelled": "CANCELLED"}.get(
            str(p.get("status") or ""), "CONFIRMED")
        if share and _status == "TENTATIVE":
            _status = "CONFIRMED"
        lines.append("STATUS:%s" % _status)
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"
