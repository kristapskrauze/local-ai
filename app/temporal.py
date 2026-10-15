# -*- coding: utf-8 -*-
"""Small centralized temporal normalizer for assistant tools.

It is deliberately conservative: when a local time is nonexistent or ambiguous
around DST, write tools get a structured refusal instead of a guessed instant.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

DEFAULT_TIMEZONE = "Europe/Riga"


@dataclass(frozen=True)
class TemporalResult:
    ok: bool
    dt: datetime | None = None
    timezone: str = DEFAULT_TIMEZONE
    error_type: str = ""
    message: str = ""
    source: str = ""


WEEKDAYS = {
    "monday": 0, "mon": 0, "pirmdien": 0, "pirmdiena": 0,
    "tuesday": 1, "tue": 1, "otrdien": 1, "otrdiena": 1,
    "wednesday": 2, "wed": 2, "tresdien": 2, "tresdiena": 2,
    "trešdien": 2, "trešdiena": 2,
    "thursday": 3, "thu": 3, "ceturtdien": 3, "ceturtdiena": 3,
    "friday": 4, "fri": 4, "piektdien": 4, "piektdiena": 4,
    "saturday": 5, "sat": 5, "sestdien": 5, "sestdiena": 5,
    "sunday": 6, "sun": 6, "svetdien": 6, "svetdiena": 6,
    "svētdien": 6, "svētdiena": 6,
}

MONTHS = {
    "janvar": 1, "janvār": 1, "january": 1,
    "februar": 2, "februār": 2, "february": 2,
    "mart": 3, "marts": 3, "march": 3,
    "april": 4, "aprīl": 4,
    "maij": 5, "may": 5,
    "junij": 6, "jūnij": 6, "june": 6,
    "julij": 7, "jūlij": 7, "july": 7,
    "august": 8,
    "septembr": 9, "september": 9,
    "oktobr": 10, "october": 10,
    "novembr": 11, "november": 11,
    "decembr": 12, "december": 12,
}


def timezone_name(configured: str = "") -> str:
    value = str(configured or "").strip() or DEFAULT_TIMEZONE
    try:
        ZoneInfo(value)
        return value
    except Exception:
        return DEFAULT_TIMEZONE


def now_in(tz_name: str = "") -> datetime:
    tz = ZoneInfo(timezone_name(tz_name))
    return datetime.now(tz)


def normalize_datetime(value: str, *, now: datetime | None = None,
                       tz_name: str = "", allow_past: bool = False) -> TemporalResult:
    tz_name = timezone_name(tz_name)
    tz = ZoneInfo(tz_name)
    now = _aware_now(now, tz)
    raw = " ".join(str(value or "").strip().split())
    if not raw:
        return TemporalResult(False, timezone=tz_name, error_type="missing_time",
                              message="date/time is required")

    iso = _parse_iso(raw, tz)
    if iso is not None:
        return _validate_local(iso, now, tz_name, allow_past, "iso")

    low = _fold(raw)
    base = now.date()
    source = "natural"

    m = re.search(r"\bin\s+(\d{1,4})\s*(minutes?|mins?|min|hours?|hrs?|h|days?|d)\b", low)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        delta = timedelta(minutes=n) if unit.startswith("min") else (
            timedelta(hours=n) if unit.startswith(("h", "hour", "hr")) else timedelta(days=n))
        return _validate_local(now + delta, now, tz_name, allow_past, "relative")

    m = re.search(r"\bpec\s+(\d{1,4})\s*(minut|stund|dien|nedel|nedēļ)", low)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        delta = timedelta(minutes=n) if unit.startswith("minut") else (
            timedelta(hours=n) if unit.startswith("stund") else (
                timedelta(days=7 * n) if unit.startswith(("nedel", "nedēļ")) else timedelta(days=n)))
        return _validate_local(now + delta, now, tz_name, allow_past, "relative")

    if re.search(r"\b(day after tomorrow|parit|parīt)\b", low):
        base = now.date() + timedelta(days=2)
    elif re.search(r"\b(tomorrow|rit|rīt)\b", low):
        base = now.date() + timedelta(days=1)
    elif re.search(r"\b(yesterday|vakar)\b", low):
        base = now.date() - timedelta(days=1)
        source = "relative_past"
    elif re.search(r"\b(today|sodien|šodien)\b", low):
        base = now.date()
    else:
        explicit = _explicit_date(low, now)
        if explicit is not None:
            base = explicit
            source = "explicit_date"
            low = _strip_date_text(low)
        else:
            wday, next_word = _weekday(low)
            if wday is not None:
                days = (wday - now.weekday()) % 7
                if next_word:
                    # "nākampiektdien" is next week's Friday, not this one.
                    days += 7
                else:
                    _clock = _time_from_text(low, None)
                    if days == 0 and _clock and now.time() > _clock.time():
                        days = 7            # today, but that hour has gone
                base = now.date() + timedelta(days=days)
                source = "weekday"

    clock = _time_from_text(low, default=_default_time(low))
    if clock is None:
        return TemporalResult(False, timezone=tz_name, error_type="missing_time",
                              message="time of day is required", source=source)
    naive = datetime.combine(base, clock.time())
    local = naive.replace(tzinfo=tz)
    return _validate_local(local, now, tz_name, allow_past, source)


def normalize_range(start: str = "", end: str = "", when: str = "",
                    *, now: datetime | None = None, tz_name: str = "") -> tuple[TemporalResult, TemporalResult]:
    tz_name = timezone_name(tz_name)
    tz = ZoneInfo(tz_name)
    now = _aware_now(now, tz)
    first = normalize_datetime(start or when, now=now, tz_name=tz_name, allow_past=True)
    if not first.ok:
        return first, TemporalResult(False, timezone=tz_name, error_type="missing_end",
                                     message="end date/time is required")
    if end:
        second = normalize_datetime(end, now=now, tz_name=tz_name, allow_past=True)
    else:
        second = TemporalResult(True, first.dt + timedelta(days=1), tz_name, source="default_1d")
    return first, second


def _parse_iso(raw: str, tz: ZoneInfo) -> datetime | None:
    value = raw.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(value)
    except Exception:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=tz)
    return dt.astimezone(tz)


def _validate_local(dt: datetime, now: datetime, tz_name: str,
                    allow_past: bool, source: str) -> TemporalResult:
    tz = ZoneInfo(tz_name)
    local = dt.astimezone(tz) if dt.tzinfo else dt.replace(tzinfo=tz)
    status = _local_status(local.replace(tzinfo=None), tz)
    if status == "nonexistent":
        return TemporalResult(False, timezone=tz_name, error_type="nonexistent_time",
                              message="that local time does not exist in %s" % tz_name,
                              source=source)
    if status == "ambiguous":
        return TemporalResult(False, timezone=tz_name, error_type="ambiguous_time",
                              message="that local time occurs twice in %s; please clarify" % tz_name,
                              source=source)
    if not allow_past and local < now:
        return TemporalResult(False, timezone=tz_name, error_type="past_time",
                              message="that time is already in the past",
                              source=source)
    return TemporalResult(True, local, tz_name, source=source)


def _local_status(naive: datetime, tz: ZoneInfo) -> str:
    offsets = set()
    matches = 0
    for fold in (0, 1):
        aware = naive.replace(tzinfo=tz, fold=fold)
        back = aware.astimezone(timezone.utc).astimezone(tz).replace(tzinfo=None)
        if back == naive:
            matches += 1
            offsets.add(aware.utcoffset())
    if matches == 0:
        return "nonexistent"
    if len(offsets) > 1:
        return "ambiguous"
    return "ok"


def _aware_now(now: datetime | None, tz: ZoneInfo) -> datetime:
    if now is None:
        return datetime.now(tz)
    if now.tzinfo is None:
        return now.replace(tzinfo=tz)
    return now.astimezone(tz)


def _fold(text: str) -> str:
    out = str(text or "").lower()
    table = str.maketrans({"ā": "a", "č": "c", "ē": "e", "ģ": "g", "ī": "i",
                           "ķ": "k", "ļ": "l", "ņ": "n", "š": "s", "ū": "u",
                           "ž": "z"})
    return out.translate(table)


def _time_from_text(low: str, default=None):
    m = re.search(r"\b([01]?\d|2[0-3])[:.](\d{2})\b", low)
    if m:
        return datetime(2000, 1, 1, int(m.group(1)), int(m.group(2)))
    m = re.search(r"\b(?:at|ap|around|around\s+at)?\s*([01]?\d|2[0-3])\s*(am|pm)?\b", low)
    if m:
        hour = int(m.group(1))
        ampm = m.group(2)
        if ampm == "pm" and hour < 12:
            hour += 12
        if ampm == "am" and hour == 12:
            hour = 0
        return datetime(2000, 1, 1, hour, 0)
    return default


def _default_time(low: str):
    if re.search(r"\b(tonight|evening|vakara|vakarpuse|pecpusdien|pēcpusdien)\b", low):
        return datetime(2000, 1, 1, 19, 0)
    if re.search(r"\b(morning|rita|rīta)\b", low):
        return datetime(2000, 1, 1, 9, 0)
    if re.search(r"\b(afternoon|pusdien)\b", low):
        return datetime(2000, 1, 1, 14, 0)
    return None


_DATE_PATTERNS = (
    r"\b[0-3]?\d[./-][01]?\d(?:[./-]20\d{2})?\b",
    r"\b[0-3]?\d\.?\s+[a-zāčēģīķļņšūž]{3,10}\w*",
)


def _strip_date_text(low: str) -> str:
    """The text with the date expression removed, so the clock parser cannot
    read it as an hour. Only ever called once the date is already understood."""
    for pat in _DATE_PATTERNS:
        cut = re.sub(pat, " ", low, count=1)
        if cut != low:
            return cut
    return low


def _weekday(low: str) -> tuple[int | None, bool]:
    low = re.sub(r"\b(n[āa]kam)(?=[a-zāčēģīķļņšūž])", r"\1 ", low)
    next_word = bool(re.search(r"\b(next|nakam|nākam)\w*\b", low))
    for word, idx in WEEKDAYS.items():
        if re.search(r"\b%s\w*\b" % re.escape(word), low):
            return idx, next_word
    return None, next_word


def _explicit_date(low: str, now: datetime):
    m = re.search(r"\b([0-3]?\d)[./-]([01]?\d)(?:[./-](20\d{2}))?\b", low)
    if m:
        day, month = int(m.group(1)), int(m.group(2))
        year = int(m.group(3) or now.year)
        try:
            dt = datetime(year, month, day)
        except Exception:
            return None
        if not m.group(3) and dt.date() < now.date():
            dt = datetime(year + 1, month, day)
        return dt.date()
    for stem, month in MONTHS.items():
        m = re.search(r"\b([0-3]?\d)\.?\s+%s\w*" % stem, low)
        if m:
            day = int(m.group(1))
            try:
                dt = datetime(now.year, month, day)
            except Exception:
                return None
            if dt.date() < now.date():
                dt = datetime(now.year + 1, month, day)
            return dt.date()
    return None
