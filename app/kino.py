# -*- coding: utf-8 -*-
"""Cinema showtimes from the Forum Cinemas XML feed.

Forum Cinemas publishes a plain XML API — no browser, no scraping, no key:

    /xml/Schedule/       every showing, with start times and auditorium
    /xml/Events/         the films themselves
    /xml/TheatreAreas/   the cinemas

so the answer comes from the operator's own data rather than a model's memory.
Same principle as media_agent and the rig skills: look it up, never guess.
"""
import re
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime

SCHEDULE_URL = "https://www.forumcinemas.lv/xml/Schedule/"
_CACHE = {"at": 0.0, "shows": None}
CACHE_SECONDS = 900          # the schedule changes daily, not minutely


class KinoError(Exception):
    pass


def _text(node, tag):
    el = node.find(tag)
    return (el.text or "").strip() if el is not None and el.text else ""


def shows(force=False):
    """[{start: datetime, title, theatre, auditorium}] for the whole feed."""
    now = time.time()
    if not force and _CACHE["shows"] is not None and now - _CACHE["at"] < CACHE_SECONDS:
        return _CACHE["shows"]
    req = urllib.request.Request(SCHEDULE_URL, headers={"User-Agent": "curl/8"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            root = ET.fromstring(r.read())
    except Exception as exc:
        raise KinoError("schedule feed unreachable: %s" % exc)
    out = []
    for s in root.findall(".//Show"):
        raw = _text(s, "dttmShowStart")
        if not raw:
            continue
        try:
            start = datetime.strptime(raw[:16], "%Y-%m-%dT%H:%M")
        except ValueError:
            continue
        out.append({
            "start": start,
            "title": _text(s, "Title"),
            "theatre": _text(s, "Theatre"),
            "auditorium": _text(s, "TheatreAndAuditorium") or _text(s, "Theatre"),
        })
    out.sort(key=lambda x: x["start"])
    _CACHE["at"], _CACHE["shows"] = now, out
    return out


_TIME_RE = re.compile(r"\b([01]?\d|2[0-3])[:.]([0-5]\d)\b")
_TONIGHT_RE = re.compile(r"vakar[āa]|šovakar|sovakar|vakaru", re.I)


def find(question="", when=None, window_minutes=45, limit=6):
    """Today's showings, narrowed by a time in the question if there is one.

    "šodien rāda 13:30" -> showings from 13:30 within the window, so the answer
    covers the 13:20 and 13:50 screenings a person would also accept rather
    than only an exact match that may not exist.
    """
    today = when or date.today()
    todays = [s for s in shows() if s["start"].date() == today]
    if not todays:
        return []
    q = str(question or "")
    m = _TIME_RE.search(q)
    if m:
        target = int(m.group(1)) * 60 + int(m.group(2))
    elif _TONIGHT_RE.search(q):
        target = 18 * 60
    else:
        now = datetime.now()
        target = (now.hour * 60 + now.minute) if now.date() == today else 12 * 60
    picked = [s for s in todays
              if 0 <= (s["start"].hour * 60 + s["start"].minute) - target <= window_minutes]
    if not picked:                      # nothing in the window: next ones after it
        picked = [s for s in todays
                  if (s["start"].hour * 60 + s["start"].minute) >= target]
    return picked[:limit]


def format_reply(picked, lang="lv"):
    if not picked:
        return ("Šodien tādā laikā neko neatrodu 😕" if lang == "lv"
                else "Nothing showing around then today 😕")
    lines = []
    seen = set()
    for s in picked:
        key = (s["start"].strftime("%H:%M"), s["title"])
        if key in seen:
            continue
        seen.add(key)
        lines.append("%s %s" % (s["start"].strftime("%H:%M"), s["title"]))
    head = "Šodien kinoteātrī:" if lang == "lv" else "In cinemas today:"
    return head + "\n" + "\n".join("· %s" % l for l in lines)
