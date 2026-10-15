# -*- coding: utf-8 -*-
"""The date-planning conversation, end to end.

Nothing here invents a time, a place or an availability.
"""
from __future__ import annotations

import json
import re
import threading
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

from app import people

UA = {"User-Agent": "local-ai-anton/1.0"}
_OWNER_RE = "|".join(re.escape(f.lower()) for f in sorted(set(people.owner.values()), key=len, reverse=True))

# chat_guid -> {"offered_for": date, "at": timestamp}
_offers = {}
_offers_lock = threading.Lock()

_DAY_WORDS = (("parīt", 2), ("rīt", 1), ("šodien", 0))


def asked_about_day(text):
    low = " ".join(str(text or "").lower().split())
    if not low:
        return None
    # words may sit between the subject and the verb — "Ko viņš RĪT dara?"
    # missed on the first test, the same gap that broke the place regex
    wants = re.search(r"(kalend[āa]r|grafik|br[īi]v|aiz[ņn]emt|"
                      r"ko\s+(?:\w+\s+){0,2}(vi[ņn][šs]|" + _OWNER_RE + r")"
                      r"(?:\s+\w+){0,2}\s+dara|"
                      r"k[āa]d[ai]\s+d[iy]en|pl[āa]n)", low)
    if not wants:
        return None
    for word, offset in _DAY_WORDS:
        if word in low:
            return (datetime.now() + timedelta(days=offset)).date()
    return None


def describe_day(day):
    """(summary, free_from) for that date, or (None, None) if unknown."""
    try:
        from app import rig_agent
    except Exception:
        return None, None
    urls = [u for u in (rig_agent.load_config().get("calendar_ics") or [])
            if str(u or "").strip()]
    if not urls:
        return None, None
    span = max(1, (day - datetime.now().date()).days + 1)
    try:
        events = rig_agent.upcoming_events(days=span)
    except Exception:
        return None, None
    if events is None:
        return None, None
    todays = []
    for item in events:
        when, all_day, title, end = rig_agent.event_fields(item)
        if isinstance(when, datetime) and when.date() == day:
            todays.append((when, all_day, title, end))
    todays.sort(key=lambda row: row[0])
    if not todays:
        return "nekā nav", datetime.combine(day, datetime.min.time()).replace(hour=12)
    parts = []
    last_end = None
    for when, all_day, title, end in todays:
        if all_day:
            parts.append(title or "aizņemts")
        else:
            parts.append("%s-%s %s" % (when.strftime("%H:%M"),
                                       end.strftime("%H:%M") if isinstance(end, datetime) else "",
                                       title or "aizņemts"))
        if all_day:
            continue
        if isinstance(end, datetime) and (last_end is None or end > last_end):
            last_end = end
    if last_end is None:      # only all-day entries: the clock is still free
        last_end = datetime.combine(day, datetime.min.time()).replace(hour=12)
    return ", ".join(parts), last_end


def remember_offer(chat_guid, day):
    with _offers_lock:
        _offers[str(chat_guid)] = {"day": day, "at": datetime.now()}
        # an offer nobody answered is dead after an hour; without this the
        # dict only ever grows
        stale = [k for k, v in _offers.items()
                 if datetime.now() - v["at"] > timedelta(hours=1)]
        for k in stale:
            _offers.pop(k, None)


def take_offer(chat_guid, max_age_minutes=30):
    with _offers_lock:
        item = _offers.get(str(chat_guid))
        if not item:
            return None
        if datetime.now() - item["at"] > timedelta(minutes=max_age_minutes):
            _offers.pop(str(chat_guid), None)
            return None
        return item["day"]


def clear_offer(chat_guid):
    with _offers_lock:
        _offers.pop(str(chat_guid), None)


_CUISINE = {
    "pastas": "pasta", "pasta": "pasta", "itāļu": "italian",
    "picas": "pizza", "picu": "pizza", "suši": "sushi", "sushi": "sushi",
    "burger": "burger", "burgeri": "burger", "steik": "steak",
    "azijas": "asian", "āzijas": "asian", "veģetār": "vegetarian",
    "zivju": "seafood", "gruzīnu": "georgian", "meksik": "mexican",
}


def wanted_cuisine(text):
    low = " ".join(str(text or "").lower().split())
    for word, en in _CUISINE.items():
        if word in low:
            return en
    return ""


_RIGA_BBOX = (56.90, 24.00, 57.02, 24.25)
_bbox_cache = {}
_bbox_lock = threading.Lock()


def city_bbox(city=""):
    """(south, west, north, east) for a city name, Riga if it can't be found."""
    name = " ".join(str(city or "").split())
    if not name:
        return _RIGA_BBOX
    key = name.lower()
    with _bbox_lock:
        hit = _bbox_cache.get(key)
    if hit:
        return hit
    url = ("https://nominatim.openstreetmap.org/search?"
           + urllib.parse.urlencode({"q": name, "format": "json", "limit": 1}))
    box = _RIGA_BBOX
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=25) as fh:
            data = json.loads(fh.read().decode("utf-8", "replace"))
        if data and data[0].get("boundingbox"):
            south, north, west, east = (float(x) for x in data[0]["boundingbox"])
            box = (south, west, north, east)
    except Exception:
        box = _RIGA_BBOX
    with _bbox_lock:
        _bbox_cache[key] = box
    return box


_OH_DAYS = ("mo", "tu", "we", "th", "fr", "sa", "su")


def _oh_open_at(spec, when):
    """Is an OSM `opening_hours` string open at `when`?

    True / False / None (cannot tell). Deliberately conservative: anything
    this does not understand returns None and the venue is KEPT with its raw
    hours surfaced, because dropping a good restaurant on a parse failure is
    worse than mentioning one whose hours we could not read. Sending someone
    to a place that is shut is the same class of failure as inventing it.
    """
    text = " ".join(str(spec or "").strip().lower().split())
    if not text:
        return None
    if "24/7" in text:
        return True
    if text in ("off", "closed"):
        return False
    if "ph" in text.split() or "week" in text or "easter" in text:
        return None                      # public-holiday / seasonal rules
    want_day = _OH_DAYS[when.weekday()]
    prev_day = _OH_DAYS[(when.weekday() - 1) % 7]
    want_min = when.hour * 60 + when.minute
    saw_rule_for_day = False
    for rule in text.split(";"):
        rule = rule.strip()
        if not rule:
            continue
        m = re.match(r"^((?:[a-z]{2}(?:\s*-\s*[a-z]{2})?)(?:\s*,\s*"
                     r"[a-z]{2}(?:\s*-\s*[a-z]{2})?)*)?\s*(.*)$", rule)
        if not m:
            return None
        days_part, times_part = m.group(1) or "", m.group(2) or ""
        days = set()
        if days_part:
            for chunk in days_part.split(","):
                chunk = chunk.strip()
                if "-" in chunk:
                    a, b = (x.strip() for x in chunk.split("-", 1))
                    if a not in _OH_DAYS or b not in _OH_DAYS:
                        return None
                    i, j = _OH_DAYS.index(a), _OH_DAYS.index(b)
                    idx = list(range(i, j + 1)) if i <= j else \
                        list(range(i, 7)) + list(range(0, j + 1))
                    days.update(_OH_DAYS[k] for k in idx)
                elif chunk in _OH_DAYS:
                    days.add(chunk)
                else:
                    return None
        else:
            days = set(_OH_DAYS)
        # A rule for YESTERDAY still matters when it runs past midnight:
        # "We 18:00-02:00" is open at 01:00 on Thursday.
        for day_name, offset in ((want_day, 0), (prev_day, 24 * 60)):
            if day_name not in days:
                continue
            if offset == 0:
                saw_rule_for_day = True
                if times_part in ("off", "closed"):
                    return False
            spans = re.findall(r"(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})",
                               times_part)
            if not spans:
                if offset == 0:
                    return None
                continue
            for h1, m1, h2, m2 in spans:
                open_min = int(h1) * 60 + int(m1)
                close_min = int(h2) * 60 + int(m2)
                if close_min <= open_min:    # past midnight, e.g. 18:00-02:00
                    close_min += 24 * 60
                elif offset:
                    continue
                if open_min <= want_min + offset < close_min:
                    return True
    return False if saw_rule_for_day else None
_CUISINE_OSM = {
    "pasta": "italian|pasta", "italian": "italian|pasta",
    "pizza": "pizza", "sushi": "sushi|japanese", "burger": "burger",
    "steak": "steak_house|steak", "asian": "asian|chinese|thai|vietnamese",
    "vegetarian": "vegetarian|vegan", "seafood": "seafood|fish",
    "georgian": "georgian", "mexican": "mexican",
}


def find_places(cuisine, limit=3, bbox=None, city="", when=None):
    """Real restaurants from OpenStreetMap — name, street, coordinates, hours.

    `when` is the time they would actually turn up. Venues OSM says are shut
    then are dropped; ones whose hours cannot be parsed are kept, with the raw
    string in `hours` so the reply can pass it on rather than promise anything.
    """
    box = bbox or city_bbox(city)
    tag = _CUISINE_OSM.get(str(cuisine or "").lower(), "")
    filt = '["cuisine"~"%s",i]' % tag if tag else ""
    # ask for a wider pool than we need: some get dropped for being closed
    want = max(limit * 8, 24)
    query = ('[out:json][timeout:25];('
             'node["amenity"="restaurant"]%s(%s,%s,%s,%s);'
             ');out body %d;' % (filt, box[0], box[1], box[2], box[3], want))
    try:
        data = urllib.parse.urlencode({"data": query}).encode()
        req = urllib.request.Request(
            "https://overpass-api.de/api/interpreter", data=data, headers=UA)
        with urllib.request.urlopen(req, timeout=60) as fh:
            payload = json.loads(fh.read().decode("utf-8", "replace"))
    except Exception:
        return []
    known_open, unknown = [], []
    for el in payload.get("elements") or []:
        tags = el.get("tags") or {}
        name = str(tags.get("name") or "").strip()
        if not name:
            continue
        street = " ".join(x for x in (tags.get("addr:street"),
                                      tags.get("addr:housenumber")) if x)
        hours = str(tags.get("opening_hours") or "").strip()
        row = {"name": name, "address": street.strip(),
               "coords": (el.get("lat"), el.get("lon")),
               "cuisine": str(tags.get("cuisine") or ""),
               "hours": hours}
        state = _oh_open_at(hours, when) if when is not None else None
        if state is False:
            continue                      # shut when they would arrive
        row["open_confirmed"] = bool(state)
        (known_open if state else unknown).append(row)
    # confirmed-open first, then the ones we could not check
    return (known_open + unknown)[:limit]


def geocode(place, city="Riga, Latvia"):
    url = ("https://nominatim.openstreetmap.org/search?"
           + urllib.parse.urlencode({"q": "%s, %s" % (place, city),
                                     "format": "json", "limit": 1}))
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=25) as fh:
            data = json.loads(fh.read().decode("utf-8", "replace"))
        if data:
            return float(data[0]["lat"]), float(data[0]["lon"])
    except Exception:
        pass
    return None


def travel_minutes(from_coords, to_coords):
    if not (from_coords and to_coords):
        return None
    url = ("https://router.project-osrm.org/route/v1/driving/"
           "%s,%s;%s,%s?overview=false"
           % (from_coords[1], from_coords[0], to_coords[1], to_coords[0]))
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=25) as fh:
            data = json.loads(fh.read().decode("utf-8", "replace"))
        routes = data.get("routes") or []
        if routes:
            return int(round((routes[0].get("duration") or 0) / 60.0))
    except Exception:
        pass
    return None
