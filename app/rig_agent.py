"""Factual answers about this rig, for chat channels (iMessage, dashboard).

The persona pipeline in `messaging.py` is a *conversation* engine: it takes tens
of seconds, writes in Latvian character, and will happily invent a GPU
temperature because it has no way to look one up. This module is the opposite —
a deterministic lookup layer. It matches a question against a small set of
skills, reads live data, and returns a short factual line. No LLM in the path,
so "cik grādi GPU?" comes back in well under a second.

Answers are built from numbers and universal labels (°C, GB, %, MHz, W) so the
same reply reads correctly in Latvian or English; only the few framing words
that need translating carry a language table.

Security: this exposes rig state to whoever can text the bridge, so
`answer_for()` requires the sender to be a configured owner. A wildcard
allowed-senders list ("*") deliberately does NOT grant rig access.
"""
import json
import logging
import re
import threading
import time
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

LOGGER = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "rig_agent.json"

DEFAULT_CONFIG = {
    "enabled": True,
    # Handles (phone / email) allowed to query the rig. Empty falls back to the
    # auto-reply allowed_senders list, but only when that list is not a wildcard.
    "owners": [],
    # Published calendar URLs: iCloud "Public Calendar" link or Google Calendar
    # "Secret address in iCal format". webcal:// is rewritten to https://.
    "calendar_ics": [],
    "calendar_days": 7,
    "calendar_max_events": 6,
    # --- film/episode suggestions from the Jellyfin library ---
    # Master switch for the "watch" skill.
    "watch_enabled": True,
    "jellyfin_users": {},
}

_cfg_cache = {"mtime": None, "data": None}
_ics_cache = {}
_ics_lock = threading.Lock()
ICS_TTL_SECONDS = 900



def load_config():
    try:
        mtime = CONFIG_PATH.stat().st_mtime
    except OSError:
        mtime = None
    if _cfg_cache["data"] is not None and _cfg_cache["mtime"] == mtime:
        return _cfg_cache["data"]
    cfg = dict(DEFAULT_CONFIG)
    if mtime is not None:
        try:
            raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                cfg.update(raw)
        except Exception as exc:
            LOGGER.warning("rig_agent config unreadable: %s", exc)
    _cfg_cache["mtime"] = mtime
    _cfg_cache["data"] = cfg
    return cfg


def save_config(patch):
    cfg = dict(load_config())
    for key in DEFAULT_CONFIG:
        if key in patch:
            cfg[key] = patch[key]
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    _cfg_cache["mtime"] = None
    _cfg_cache["data"] = None
    return load_config()


def _normalize_handle(value):
    out = str(value or "").strip().lower()
    for ch in " ()-.":
        out = out.replace(ch, "")
    return out


def owner_allowed(sender, acfg=None):
    """True when `sender` may ask the rig about itself.

    Fails closed: with no owners configured nobody gets rig answers. The
    auto-reply allow-list is deliberately NOT used as a fallback — it contains
    the friends and family the persona bot chats with, and they have no
    business reading disk usage, loaded models, or calendar entries.
    """
    cfg = load_config()
    if not cfg.get("enabled", True):
        return False
    norm = _normalize_handle(sender)
    if not norm:
        return False
    owners = [o for o in (cfg.get("owners") or []) if str(o or "").strip()]
    for item in owners:
        if _normalize_handle(item) == norm:
            return True
    return False


def _main():
    from app import main as _m
    return _m


def _stats():
    return _main().api_stats()


def _services():
    return (_main().api_services() or {}).get("services") or []


def _ollama():
    try:
        payload = _main().api_ollama_models()
    except Exception as exc:
        return {"models": [], "loaded": [], "error": str(exc)}
    if not isinstance(payload, dict):
        return {"models": [], "loaded": []}
    return {"models": payload.get("models") or [], "loaded": payload.get("loaded") or []}



def _n(value, digits=0, suffix=""):
    if value is None:
        return "?"
    try:
        num = float(value)
    except (TypeError, ValueError):
        return str(value)
    if digits <= 0:
        return "%d%s" % (round(num), suffix)
    return ("%." + str(digits) + "f%s") % (num, suffix)


def _pct(value):
    return _n(value, 0, "%")


def _dur(seconds):
    try:
        total = int(float(seconds))
    except (TypeError, ValueError):
        return "?"
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    mins = rem // 60
    if days:
        return "%dd %dh" % (days, hours)
    if hours:
        return "%dh %dm" % (hours, mins)
    return "%dm" % mins



def _skill_gpu(_text, _lang):
    gpu = (_stats() or {}).get("gpu") or {}
    if not gpu.get("available"):
        return "GPU: nav datu / no data"
    used = gpu.get("memory_used_mb")
    total = gpu.get("memory_total_mb")
    vram = "?"
    if used is not None and total:
        vram = "%s/%s GB" % (_n(float(used) / 1024, 1), _n(float(total) / 1024, 0))
    return "\n".join([
        "%s · %s" % (gpu.get("name") or "GPU", gpu.get("pstate") or ""),
        "%s°C · %s load · VRAM %s (%s)" % (
            _n(gpu.get("temperature_c")), _pct(gpu.get("gpu_utilization_percent")),
            vram, _pct(gpu.get("memory_utilization_percent"))),
        "%s W / %s W · fan %s · %s MHz" % (
            _n(gpu.get("power_draw_w")), _n(gpu.get("power_limit_w")),
            _pct(gpu.get("fan_speed_percent")), _n(gpu.get("clock_graphics_mhz"))),
    ]).strip()


def _skill_cpu(_text, _lang):
    data = _stats() or {}
    cpu = data.get("cpu") or {}
    temp = cpu.get("package_temperature_c")
    if temp is None:
        temp = cpu.get("max_c")
    line2 = "%s°C · %s MHz · %s threads" % (
        _n(temp), _n(cpu.get("frequency_mhz")), _n(cpu.get("thread_count")))
    if not cpu.get("temperature_available"):
        line2 = "temp n/a (Core Temp not running) · " + line2
    return "\n".join([
        "CPU %s load" % _pct(data.get("cpu_percent")),
        line2,
    ])


def _skill_memory(_text, _lang):
    mem = (_stats() or {}).get("memory") or {}
    swap = mem.get("swap") or {}
    return "RAM %s/%s GB (%s) · swap %s GB (%s)" % (
        _n(mem.get("used_gb"), 1), _n(mem.get("total_gb"), 1), _pct(mem.get("percent")),
        _n(swap.get("used_gb"), 1), _pct(swap.get("percent")))


def _media_disks():
    """The media server's own disks — where the library lives."""
    try:
        import urllib.request
        with urllib.request.urlopen(
                "http://127.0.0.1:8765/api/remote/status", timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        return ((data.get("data") or {}).get("disks") or [])
    except Exception:
        return []


def _skill_disks(_text, _lang):
    """Free space, media server first."""
    lines = []
    media = _media_disks()
    if media:
        lines.append("Media serveris:")
        for disk in media:
            label = str(disk.get("label") or disk.get("mount") or "?")
            free = float(disk.get("free_gb") or 0)
            pct = float(disk.get("used_pct") or 0)
            flag = "  ← pilns" if pct >= 90 else ""
            lines.append("  %s: %s GB brīvi no %s GB (%s%% aizņemts)%s" % (
                label, _n(free, 0), _n(disk.get("total_gb"), 0), _n(pct, 0), flag))
    else:
        lines.append("Media serveris nav sasniedzams")
    disks = (_stats() or {}).get("disks") or []
    if disks:
        lines.append("Šī mašīna: " + " · ".join(
            "%s %s GB brīvi" % (d.get("letter") or "?", _n(d.get("free_gb"), 0))
            for d in disks))
    return "\n".join(lines)


def _skill_temps(_text, _lang):
    data = _stats() or {}
    gpu = data.get("gpu") or {}
    cpu = data.get("cpu") or {}
    ctemp = cpu.get("package_temperature_c")
    if ctemp is None:
        ctemp = cpu.get("max_c")
    return "CPU %s°C · GPU %s°C (fan %s)" % (
        _n(ctemp), _n(gpu.get("temperature_c")), _pct(gpu.get("fan_speed_percent")))


def _skill_power(_text, _lang):
    data = _stats() or {}
    gpu = data.get("gpu") or {}
    gpu_w = gpu.get("power_draw_w") or 0
    load = data.get("cpu_percent") or 0
    # Same estimate the /mini dashboard uses: i9-9900KF 13->158 W across load,
    # 55 W for board/drives/fans, all divided by a 92% efficient PSU.
    cpu_w = 13 + (158 - 13) * (float(load) / 100.0)
    wall = (float(gpu_w) + cpu_w + 55) / 0.92
    return "~%s W wall · GPU %s W · CPU ~%s W" % (_n(wall), _n(gpu_w), _n(cpu_w))


def _skill_models(_text, _lang):
    info = _ollama()
    loaded = info.get("loaded") or []
    models = info.get("models") or []
    lines = []
    if loaded:
        # /api/ps reports raw bytes; size_vram < size means part of it spilled to CPU.
        for item in loaded:
            size = float(item.get("size") or 0)
            vram = float(item.get("size_vram") or 0)
            where = ""
            if size and vram:
                pct = round(vram / size * 100)
                where = " · %d%% GPU" % pct if pct < 100 else " · all GPU"
            lines.append("VRAM: %s (%s GB%s)" % (
                item.get("name") or "?", _n(size / (1024 ** 3), 1), where))
    else:
        lines.append("VRAM: nothing loaded")
    names = [str(m.get("name") or "") for m in models]
    lines.append("%d installed: %s" % (len(names), ", ".join(names[:8])))
    if len(names) > 8:
        lines.append("...and %d more" % (len(names) - 8))
    return "\n".join(lines)


def _skill_services(_text, _lang):
    services = _services()
    if not services:
        return "No service data"
    up = [s.get("name") for s in services if s.get("up")]
    down = [s.get("name") for s in services if not s.get("up")]
    lines = []
    if up:
        lines.append("UP: " + ", ".join(str(n) for n in up))
    if down:
        lines.append("DOWN: " + ", ".join(str(n) for n in down))
    return "\n".join(lines)


def _skill_status(_text, lang):
    data = _stats() or {}
    gpu = data.get("gpu") or {}
    mem = data.get("memory") or {}
    cpu = data.get("cpu") or {}
    ctemp = cpu.get("package_temperature_c")
    if ctemp is None:
        ctemp = cpu.get("max_c")
    warnings = data.get("warnings") or []
    info = _ollama()
    loaded = [str(m.get("name") or "") for m in (info.get("loaded") or [])]
    lines = [
        "CPU %s · %s°C   GPU %s · %s°C" % (
            _pct(data.get("cpu_percent")), _n(ctemp),
            _pct(gpu.get("gpu_utilization_percent")), _n(gpu.get("temperature_c"))),
        "RAM %s · VRAM %s · up %s" % (
            _pct(mem.get("percent")), _pct(gpu.get("memory_utilization_percent")),
            _dur(data.get("system_uptime_seconds"))),
    ]
    lines.append("model: " + (", ".join(loaded) if loaded else "none loaded"))
    if warnings:
        lines.append(("Brīdinājumi: " if lang == "lv" else "Warnings: ") + " · ".join(str(w) for w in warnings))
    else:
        lines.append("Viss kārtībā ✅" if lang == "lv" else "All clear ✅")
    return "\n".join(lines)


def _skill_uptime(_text, lang):
    data = _stats() or {}
    label = "Dators augšā" if lang == "lv" else "Rig up"
    return "%s %s · dashboard %s" % (
        label, _dur(data.get("system_uptime_seconds")), _dur(data.get("uptime_seconds")))



_ICS_FOLD_RE = re.compile(r"\r?\n[ \t]")


def _ics_text(url):
    url = str(url or "").strip()
    if url.lower().startswith("webcal://"):
        url = "https://" + url[len("webcal://"):]
    if not url.lower().startswith(("http://", "https://")):
        return ""
    now = time.time()
    with _ics_lock:
        hit = _ics_cache.get(url)
        if hit and now - hit[0] < ICS_TTL_SECONDS:
            return hit[1]
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "dobby-rig-agent/1.0"})
        with urllib.request.urlopen(req, timeout=20) as fh:
            body = fh.read().decode("utf-8", "replace")
    except Exception as exc:
        LOGGER.warning("calendar fetch failed (%s): %s", url, exc)
        body = ""
    with _ics_lock:
        _ics_cache[url] = (now, body)
    return body


def _ics_value(line):
    return line.split(":", 1)[1].strip() if ":" in line else ""


def _parse_ics_dt(raw, params):
    """Return (datetime, all_day). Naive local time — the rig lives in one zone."""
    value = raw.strip()
    if not value:
        return None, False
    if value.endswith("Z"):
        try:
            aware = datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
            return aware.astimezone().replace(tzinfo=None), False
        except ValueError:
            return None, False
    if "VALUE=DATE" in params or len(value) == 8:
        try:
            return datetime.strptime(value[:8], "%Y%m%d"), True
        except ValueError:
            return None, False
    try:
        return datetime.strptime(value[:15], "%Y%m%dT%H%M%S"), False
    except ValueError:
        return None, False


def _rrule_occurrences(start, rule, window_start, window_end, all_day):
    """Expand the common repeat shapes (birthdays, weekly standups) only."""
    parts = {}
    for chunk in str(rule or "").split(";"):
        if "=" in chunk:
            key, val = chunk.split("=", 1)
            parts[key.upper()] = val
    freq = parts.get("FREQ", "").upper()
    if freq not in ("DAILY", "WEEKLY", "MONTHLY", "YEARLY"):
        return []
    try:
        interval = max(1, int(parts.get("INTERVAL", "1")))
    except ValueError:
        interval = 1
    until = None
    if parts.get("UNTIL"):
        until, _ = _parse_ics_dt(parts["UNTIL"], "")
    try:
        count = int(parts.get("COUNT", "0"))
    except ValueError:
        count = 0

    out = []
    cur = start
    emitted = 0
    for _ in range(2000):
        if until and cur > until:
            break
        if count and emitted >= count:
            break
        if cur > window_end:
            break
        if cur >= window_start:
            out.append(cur)
        emitted += 1
        if freq == "DAILY":
            cur = cur + timedelta(days=interval)
        elif freq == "WEEKLY":
            cur = cur + timedelta(weeks=interval)
        elif freq == "MONTHLY":
            month = cur.month - 1 + interval
            year = cur.year + month // 12
            month = month % 12 + 1
            day = min(cur.day, _days_in_month(year, month))
            cur = cur.replace(year=year, month=month, day=day)
        else:
            try:
                cur = cur.replace(year=cur.year + interval)
            except ValueError:      # Feb 29
                cur = cur.replace(year=cur.year + interval, day=28)
        if len(out) > 60:
            break
    return [(dt, all_day) for dt in out]


def _days_in_month(year, month):
    if month == 12:
        return 31
    return (date(year, month + 1, 1) - date(year, month, 1)).days


_ICS_DURATION_RE = re.compile(
    r"^P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$", re.I)


def _parse_ics_duration(value):
    """RFC 5545 DURATION ("PT1H30M", "P1D") -> timedelta, or None."""
    m = _ICS_DURATION_RE.match(str(value or "").strip().lstrip("+-"))
    if not m or not any(m.groups()):
        return None
    weeks, days, hours, mins, secs = (int(g or 0) for g in m.groups())
    return timedelta(weeks=weeks, days=days, hours=hours, minutes=mins,
                     seconds=secs)


def event_fields(item):
    """(start, all_day, summary, end) from an upcoming_events() row."""
    item = tuple(item or ())
    start = item[0] if len(item) > 0 else None
    all_day = bool(item[1]) if len(item) > 1 else False
    summary = str(item[2] or "") if len(item) > 2 else ""
    end = item[3] if len(item) > 3 else None
    if end is None and isinstance(start, datetime):
        # No DTEND/DURATION in the feed. An all-day event owns the whole day;
        # anything else gets one hour, which is the old behaviour and is only
        # ever a fallback now.
        end = (start + timedelta(days=1)) if all_day else (start + timedelta(hours=1))
    return start, all_day, summary, end


def upcoming_events(days=None):
    """Calendar rows as (start, all_day, summary, end). Use event_fields()."""
    cfg = load_config()
    urls = [u for u in (cfg.get("calendar_ics") or []) if str(u or "").strip()]
    if not urls:
        return None
    span = int(days or cfg.get("calendar_days") or 7)
    now = datetime.now()
    window_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    window_end = window_start + timedelta(days=span)

    events = []
    for url in urls:
        body = _ics_text(url)
        if not body:
            continue
        body = _ICS_FOLD_RE.sub("", body)     # RFC 5545 line unfolding
        for block in body.split("BEGIN:VEVENT")[1:]:
            block = block.split("END:VEVENT")[0]
            summary = ""
            start = None
            end = None
            duration = None
            all_day = False
            rrule = ""
            for line in block.splitlines():
                upper = line.upper()
                if upper.startswith("SUMMARY"):
                    summary = _ics_value(line)
                elif upper.startswith("DTSTART"):
                    head = line.split(":", 1)[0]
                    start, all_day = _parse_ics_dt(_ics_value(line), head.upper())
                elif upper.startswith("DTEND"):
                    head = line.split(":", 1)[0]
                    end, _ = _parse_ics_dt(_ics_value(line), head.upper())
                elif upper.startswith("DURATION"):
                    duration = _parse_ics_duration(_ics_value(line))
                elif upper.startswith("RRULE"):
                    rrule = _ics_value(line)
            if not start:
                continue
            # length of THIS event, carried onto every repeat occurrence
            if duration is None:
                if end is not None and end > start:
                    duration = end - start
                else:
                    duration = timedelta(days=1) if all_day else timedelta(hours=1)
            if rrule:
                for occ, is_all_day in _rrule_occurrences(
                        start, rrule, window_start, window_end, all_day):
                    events.append((occ, is_all_day, summary, occ + duration))
            elif window_start <= start <= window_end:
                events.append((start, all_day, summary, start + duration))
    events.sort(key=lambda item: item[0])
    return events


def _fmt_event(when, all_day, summary, lang):
    today = datetime.now().date()
    day = when.date()
    if day == today:
        label = "Šodien" if lang == "lv" else "Today"
    elif day == today + timedelta(days=1):
        label = "Rīt" if lang == "lv" else "Tomorrow"
    else:
        label = when.strftime("%a %d.%m")
    stamp = label if all_day else "%s %s" % (label, when.strftime("%H:%M"))
    return "%s — %s" % (stamp, summary or "(no title)")


def _skill_calendar(text, lang):
    cfg = load_config()
    days = cfg.get("calendar_days") or 7
    low = text.lower()
    if any(word in low for word in ("šodien", "today", "šodienas")):
        days = 1
    elif any(word in low for word in ("rīt", "tomorrow", "ritdien")):
        days = 2
    events = upcoming_events(days)
    if events is None:
        return ("Kalendārs nav pieslēgts — pievieno .ics adresi rig_agent config."
                if lang == "lv" else
                "No calendar connected — add an .ics URL in the rig_agent config.")
    if not events:
        return "Nekā kalendārā 🎉" if lang == "lv" else "Nothing on the calendar 🎉"
    limit = int(cfg.get("calendar_max_events") or 6)
    lines = [_fmt_event(*event_fields(ev)[:3], lang) for ev in events[:limit]]
    if len(events) > limit:
        lines.append("+%d vairāk" % (len(events) - limit) if lang == "lv"
                     else "+%d more" % (len(events) - limit))
    return "\n".join(lines)


def _skill_goals(_text, lang):
    try:
        from app import goals as goals_mod
        payload = goals_mod.board(weeks=1)
    except Exception as exc:
        return "Goals unavailable: %s" % exc
    items = (payload or {}).get("goals") or []
    stats = (payload or {}).get("stats") or {}
    active = [g for g in items if g.get("status") == "active" and not g.get("parent_id")]
    lines = ["%s%% overall · %s active · streak %s" % (
        _n(stats.get("overall_progress")), _n(stats.get("active")),
        _n((payload or {}).get("streak")))]
    for goal in active[:5]:
        lines.append("· %s — %s%%" % (goal.get("title") or "?",
                                      _n(goal.get("effective_progress"))))
    if not active:
        lines.append("Nav aktīvu mērķu" if lang == "lv" else "No active goals")
    return "\n".join(lines)


def watch_handle_map(acfg=None):
    """handle -> Jellyfin account name, from every source, owners included."""
    names = {}
    try:
        from app import messaging
        src = acfg if isinstance(acfg, dict) and acfg.get("contact_names") else \
            (messaging.load_config().get("auto_reply") or {})
        names = dict(src.get("contact_names") or {})
    except Exception:
        pass
    cfg = load_config()
    from app import media_agent as _ma
    for handle in (cfg.get("owners") or []):
        names.setdefault(str(handle), _ma.DEFAULT_USER)
    names.update({str(k): str(v) for k, v in (cfg.get("jellyfin_users") or {}).items()})
    return names


def _watch_allowed(sender, acfg=None):
    """True only when this person maps to a real Jellyfin account."""
    try:
        from app import media_agent
        return media_agent.resolve_user(sender, watch_handle_map(acfg)) is not None
    except Exception:
        LOGGER.debug("watch gate failed", exc_info=True)
        return False


def recent_context(sender, limit=8):
    """Read from the local inbox rather than BlueBubbles: no network, no GPU, and
    it is already deduped. Returns "" on any problem; a suggestion must never
    fail because the history lookup did.
    """
    try:
        from app import messaging               # late: messaging imports us
        want = _norm_handle(sender)
        out = []
        rows = sorted(messaging.inbox(limit=120) or [],
                      key=lambda e: messaging._epoch_seconds(e.get("ts")), reverse=True)
        for e in rows:
            if want and _norm_handle(e.get("sender")) != want:
                continue
            txt = str(e.get("text") or "").strip()
            if txt:
                out.append(txt)
            if len(out) >= limit:
                break
        return "\n".join(reversed(out))
    except Exception:
        LOGGER.debug("recent_context failed", exc_info=True)
        return ""


def _norm_handle(v):
    return re.sub(r"[^\w@.+]", "", str(v or "")).lower()


def _skill_watch(text, lang, ctx=None):
    from app import media_agent            # late import: keeps rig_agent light
    history = (ctx or {}).get("history") or ""
    sender = (ctx or {}).get("sender") or ""
    names = watch_handle_map()
    cfg = load_config()
    if not cfg.get("watch_enabled", True):
        return None
    try:
        res = media_agent.suggest(text, limit=3, history=history,
                                  who=sender, contact_names=names)
    except media_agent.NoAccount:
        # Silent: returning None makes answer() decline, so the message falls
        # through to the normal reply path instead of getting a media answer
        # built from somebody else's viewing history.
        LOGGER.info("watch: %s has no Jellyfin account — skill declined", sender)
        return None
    except media_agent.MediaError as exc:
        LOGGER.info("media_agent unavailable: %s", exc)
        return ("Nevaru piekļūt bibliotēkai 😕" if lang == "lv"
                else "Can't reach the media library 😕")
    except Exception:
        LOGGER.exception("media_agent failed")
        return ("Neizdevās pajautāt bibliotēkai" if lang == "lv"
                else "Media library lookup failed")
    # format_reply already opens with "Varētu paskatīties:" / "Noskaties …" —
    # adding a second header stacked two intros on one message.
    return media_agent.format_reply(res)


def _skill_kino(text, lang, ctx=None):
    """What is showing at the CINEMA today — from the operator's XML feed."""
    from app import kino                  # late import: keeps rig_agent light
    try:
        picked = kino.find(text)
    except kino.KinoError as exc:
        LOGGER.info("kino feed unavailable: %s", exc)
        return ("Neizdevās piekļūt kino repertuāram 😕" if lang == "lv"
                else "Can't reach the cinema schedule 😕")
    except Exception:
        LOGGER.exception("kino lookup failed")
        return None
    return kino.format_reply(picked, lang=lang)


def last_voice_note(chat_guid):
    """The text of the newest voice note WE sent to this chat, or ""."""
    if not chat_guid:
        return ""
    try:
        from app import messaging
        records = messaging._bot_outbox_records(chat_guid, limit=300)
    except Exception:
        LOGGER.exception("voice note lookup failed")
        return ""
    best, best_ts = "", -1.0
    for rec in records or []:
        if str(rec.get("mode") or "") != "voice":
            continue
        try:
            ts = float(rec.get("ts") or 0)
        except (TypeError, ValueError):
            ts = 0.0
        body = str(rec.get("content") or rec.get("text") or "")
        body = re.sub(r"^\s*\[voice\]\s*", "", body).strip()
        if body and ts >= best_ts:
            best, best_ts = body, ts
    return best


def _skill_voicenote(text, lang, ctx=None):
    """So this is a lookup, for the same reason rig_agent exists at all: the audio
    was generated FROM this text, the text is on disk, and a model that "cannot
    hear" its own file has nothing to add to it.
    """
    chat_guid = (ctx or {}).get("chat_guid") or ""
    body = last_voice_note(chat_guid)
    if not body:
        return ("Neatrodu nevienu savu balss ziņu šajā sarakstē" if lang == "lv"
                else "I can't find a voice note of mine in this chat")
    return (("Es ierunāju: «%s»" % body) if lang == "lv"
            else "I said: \u00ab%s\u00bb" % body)


def _skill_help(_text, lang):
    if lang == "lv":
        return ("Varu pateikt: gpu · cpu · ram · diski · gpu temperatūra · "
                "jauda · modeļi · servisi · statuss · kalendārs · mērķi · "
                "un ieteikt filmu")
    return ("I can report: gpu · cpu · ram · disks · gpu temperature · "
            "watts · models · services · status · calendar · goals · "
            "and suggest a film")


# "ko tu ierunāji tajā balss ziņā" — a question about OUR OWN voice note. It has
# to sit in the deterministic lane: see _skill_voicenote for the 16/16 measured
# failures of every prompt-side fix.
_VOICE_RECALL_RE = re.compile(
    r"\bko\s+tu\s+(?:tur\s+|vi\u0146\s+)?(?:ierun[āa]ji|teici|pateici|saki)\b"
    r"(?:[^?.!]{0,40}\b(?:balss|zi\u0146|audio|ierakst))?|"
    r"\batk[āa]rto\b[^?.!]{0,20}\b(?:balss\s*zi\u0146|ierakst)|"
    r"\bpasaki\b[^?.!]{0,25}\bko\s+tu\s+ierun[āa]ji|"
    r"\bwhat\s+did\s+you\s+say\s+in\s+(?:that|the|your)\s+(?:voice|audio)|"
    r"\brepeat\s+(?:that|the|your)\s+voice\s*(?:note|message)",
    re.I)

# skill name -> (trigger words, handler). Order matters: the first hit wins, so
# specific topics come before the catch-all status check.
_SKILLS = [
    ("voicenote", (_VOICE_RECALL_RE.pattern,), _skill_voicenote),
    ("help", (r"^\s*/?(help|palīdzība|palidziba|komandas)\s*[?!.]*\s*$",
              r"\bko\s+tu\s+(vari|proti|māki|maki)\b",
              r"\bkādas\s+(?:tev\s+)?(?:ir\s+)?komandas\b",
              r"\bwhat\s+can\s+you\s+do\b",
              r"\bkā\s+tu\s+vari\s+palīdz\w*"), _skill_help),
    # BEFORE "status": "ko šovakar skatīties?" contains no rig noun, but
    # "services" claims r"jellyfin" and status claims a bare "kā iet", so a
    # film question placed later would be swallowed by them.
    # Needs a WATCH verb plus a media noun, or an explicit ask — "filma" alone
    # is conversation ("vakar redzēju labu filmu"), which is the same trap the
    # "cik ilgi" matcher fell into.
    ("watch", (r"\b(ko|kādu|kāda)\b[^?]{0,30}\b(skatīt|paskatīt|noskatīt)",
               r"\b(iesak|iesaki|piedāvā|ieteic)\w*\b[^?]{0,30}"
               r"\b(filmu|filma|seriāl|epizod|sēriju|movie|show|series)",
               r"\bko\s+(?:mēs\s+)?(?:šovakar|šodien|vakarā)\b[^?]{0,20}\bskat",
               r"\b(suggest|recommend)\b[^?]{0,30}\b(movie|film|show|series|episode)",
               r"\bwhat\b[^?]{0,20}\b(to )?watch\b",
               r"\bkaut ko\b[^?]{0,25}\b(skatīt|paskatīt)",
               # command form: match() strips the leading "/" before probing,
               # so anchoring on "/" here would never fire
               r"^\s*/?(watch|movie|skaties|filma)\b"), _skill_watch),
    ("kino", (r"\bkino\b", r"kinoteātr", r"kinoteatr", r"\bseans",
              r"forum cinemas", r"apollo kino", r"splendid palace",
              r"\brepertuār",
              r"\bfilm\w*\b[^?]{0,40}\b(?:[01]?\d|2[0-3])[:.][0-5]\d\b",
              r"\b(?:[01]?\d|2[0-3])[:.][0-5]\d\b[^?]{0,40}\bfilm\w*",
              # "ko šodien rāda" / "kas rāda" — showing, in the cinema sense
              r"\brāda\b[^?]{0,30}\bfilm|\bfilm\w*\b[^?]{0,20}\brāda\b"),
     _skill_kino),
    ("calendar", (r"kalendār", r"calendar", r"tikšan", r"pieraksts", r"\bplāni\b",
                  r"\bschedule\b", r"\bevents?\b", r"\bmeeting"), _skill_calendar),
    ("goals", (r"\bmērķ", r"\bgoals?\b", r"progres"), _skill_goals),
    ("models", (r"\bmodel", r"\bmodeļ", r"ollama", r"ielādēt", r"\bloaded\b", r"\bllm\b"), _skill_models),
    ("services", (r"servis", r"\bservices?\b", r"comfy", r"jellyfin", r"webui",
                  r"searxng", r"docker"), _skill_services),
    ("gpu", (r"\bgpu\b", r"\bvram\b", r"videokart", r"grafisk", r"3090"), _skill_gpu),
    ("cpu", (r"\bcpu\b", r"procesor", r"9900"), _skill_cpu),
    ("memory", (r"\bram\b", r"operatīv", r"\bmemory\b", r"\bswap\b"), _skill_memory),
    ("disks", (r"\bdisk", r"\bssd\b", r"brīv.{0,6}viet", r"\bfree space\b", r"\bc:\b",
               r"\bvieta\b", r"cik.{0,12}(vietas|palicis)", r"\bstorage\b",
               r"serverī|serveram|servera"), _skill_disks),
    ("power", (r"\bvat[iu]\b", r"\bwatt", r"patēriņ", r"\bpower\b", r"strāv",
               r"\bjaud"), _skill_power),
    # A bare "grādi"/"temperatūra" is weather or physics ("cik grādi ārā?",
    # "ūdens viršanas temperatūra"). Only claim it with a rig part named.
    ("temps", (r"\btemps?\b",
               r"(grād|temperatūr|karst|hot)\w*\s+(?:\w+\s+){0,2}"
               r"(gpu|cpu|dator|rig\w*|karte|procesor|videokart)",
               r"(gpu|cpu|dator|rig\w*|karte|procesor|videokart)\w*\s+(?:\w+\s+){0,2}"
               r"(grād|temperatūr|karst|hot)"), _skill_temps),
    ("uptime", (r"\buptime\b",
                r"cik ilgi\s+(?:\w+\s+){0,2}(dators?|rig|serveri?s|sistēma|mašīna)",
                r"how long.*(been )?up\b"), _skill_uptime),
    ("status", (r"\brig\w*\b", r"\bdators?\b", r"\bstatuss?\b", r"\bstatus\b",
                r"\bstats\b",
                r"kā iet\s+(?:ar\s+)?(rig\w*|dator\w*|sistēm\w*)",
                r"viss kārtībā\s+(?:ar\s+)?(rig\w*|dator\w*)",
                r"\bsistēmas?\s+(status|stāvokl)", r"\bsystem\s+status\b",
                r"\bdobby\b"), _skill_status),
]

PRIVATE_SKILLS = frozenset({
    "gpu", "cpu", "memory", "disks", "temps", "power", "models", "services",
    "status", "uptime", "goals", "calendar",
})

_SKILL_BY_NAME = {name: fn for name, _pats, fn in _SKILLS}
# which handlers take the optional third `ctx` argument
_SKILL_WANTS_CTX = {
    name: fn.__code__.co_argcount >= 3 for name, _pats, fn in _SKILLS}
_COMPILED = [(name, [re.compile(p, re.IGNORECASE) for p in pats], fn)
             for name, pats, fn in _SKILLS]

# A rig word alone is not enough — "man dators sabojājās" is conversation, not a
# query. Require question shape, an explicit command, or a bare keyword message.
_QUESTION_RE = re.compile(
    r"(\?|^\s*(cik|kāda|kāds|kā|kas|vai|kur|kad|parād|saki|rādi|check|show|what|"
    r"iesak\w*|piedāvā|ieteic\w*|suggest|recommend|"
    # "Pasaki vēlreiz ko tu ierunāji" is an imperative request with no "?" and
    # six words, so it was dropped. `saki` was already here but only as the
    # FIRST word.
    r"pasaki\w*|atk[āa]rto\w*|repeat|"
    r"whats|what's|how|is|are|any|give|tell|list)\b)", re.IGNORECASE)

_LV_HINT_RE = re.compile(
    r"[āčēģīķļņšūž]|\b(cik|kāds|kāda|vai|man|ir|tev|šodien|rīt|kā|ko|tu|"
    r"kas|mums|vari|esi|būs)\b", re.IGNORECASE)
_LV_DIACRITIC_RE = re.compile(r"[āčēģīķļņšūž]", re.IGNORECASE)
# Latvian function words that are NOT also English words or common title words.
_LV_STRONG = {
    "cik", "kāds", "kāda", "kads", "kada", "vai", "tev", "tevi", "šodien", "sodien",
    "rīt", "rit", "kā", "ko", "tu", "kas", "mums", "vari", "esi", "būs", "bus",
    "lūdzu", "ludzu", "visas", "visus", "visu", "sezonas", "sezona", "filmas",
    "filmu", "filma", "seriāls", "serials", "nokačā", "nokaca", "kačā", "lejupielādē",
    "lejupielade", "dabū", "dabu", "velc", "novelc", "paldies", "labi", "jā", "ja",
    "nē", "man", "mani", "manas", "mūsu", "musu", "arī", "ari", "tagad", "vēl", "vel",
}
_LV_AMBIGUOUS = {"man", "ir", "no", "to", "es", "la", "da", "ka"}
_EN_STRONG = {
    "download", "downloads", "the", "all", "every", "season", "seasons", "episode",
    "episodes", "movie", "movies", "show", "series", "get", "me", "please", "grab",
    "fetch", "what", "whats", "is", "are", "new", "day", "for", "and",
    "with", "of", "my", "your", "how", "much", "free", "space", "status", "watch",
    "suggest", "recommend", "something", "anything", "complete", "full",
}


def _lang_of(text):
    """Language of THIS message, decided on the message alone."""
    raw = str(text or "")
    if _LV_DIACRITIC_RE.search(raw):
        return "lv"
    words = [w.lower() for w in re.findall(r"[\w']+", raw)]
    lv = sum(1 for w in words if w in _LV_STRONG and w not in _LV_AMBIGUOUS)
    en = sum(1 for w in words if w in _EN_STRONG)
    if lv and lv >= en:
        return "lv"
    if en:
        return "en"
    # nothing decisive: fall back to the old hint list, which at least knows
    # "kas"/"cik" shapes without diacritics
    return "lv" if _LV_HINT_RE.search(raw) else "en"


def is_command(text):
    """True for an explicit command: "/gpu", "!status", "rig: disks"."""
    raw = str(text or "").strip()
    return bool(raw.startswith(("/", "!"))
                or re.match(r"^(rig|dobby)\s*[:,-]\s*", raw, re.IGNORECASE))


def match(text):
    """Return a skill name when this message is a rig question, else None."""
    raw = str(text or "").strip()
    if not raw or len(raw) > 400:
        return None
    # Explicit command form always wins: "/gpu", "/status", "rig: disks"
    command = None
    stripped = raw.lstrip("/!")
    if raw.startswith(("/", "!")):
        command = stripped
    elif re.match(r"^(rig|dobby)\s*[:,-]\s*", raw, re.IGNORECASE):
        command = re.sub(r"^(rig|dobby)\s*[:,-]\s*", "", raw, flags=re.IGNORECASE)
    elif re.match(r"^antons?\s*[:,-]?\s+", raw, re.IGNORECASE):
        command = re.sub(r"^antons?\s*[:,-]?\s+", "", raw, flags=re.IGNORECASE)
    probe = command if command is not None else raw
    for name, patterns, _fn in _COMPILED:
        for pattern in patterns:
            if pattern.search(probe):
                if command is not None:
                    return name
                words = re.findall(r"\w+", probe)
                # a question or a short keyword message
                if _QUESTION_RE.search(probe) or len(words) <= 3:
                    return name
                return None
    return None


def answer(text, skill=None, ctx=None):
    """Answer a rig question. Returns {"skill", "text"} or None if not a match.

    `ctx` is optional extra state (currently just recent conversation, for the
    watch skill). Arity is checked once at import rather than by catching
    TypeError, which would silently swallow a real TypeError from inside a skill.
    """
    name = skill or match(text)
    if not name:
        return None
    fn = _SKILL_BY_NAME.get(name)
    if not fn:
        return None
    lang = _lang_of(text)
    try:
        if _SKILL_WANTS_CTX.get(name):
            body = fn(str(text or ""), lang, ctx or {})
        else:
            body = fn(str(text or ""), lang)
    except Exception as exc:
        LOGGER.exception("rig_agent skill %s failed", name)
        body = ("Neizdevās nolasīt (%s)" % exc) if lang == "lv" else ("Lookup failed (%s)" % exc)
    # A skill may DECLINE by returning nothing (watch does this when the asker
    # has no Jellyfin account). Without this the caller got text "None".
    if body is None or not str(body).strip():
        return None
    return {"skill": name, "text": str(body).strip(), "lang": lang}


def answer_for(text, sender, acfg=None, chat_guid=None):
    """Gated entry point for the messaging pipeline.

    Returns an answer dict, or None when the message is not a rig question
    at all. `chat_guid` is optional and passed through to skills that read
    this conversation (the voice-note recall).
    """
    if not load_config().get("enabled", True):
        return None
    name = match(text)
    if not name:
        return None
    return answer_skill(name, text, sender, acfg, chat_guid=chat_guid)


def answer_skill(name, text, sender, acfg=None, chat_guid=None):
    """The permission rules below, applied to a lookup chosen BY NAME."""
    if not load_config().get("enabled", True):
        return None
    if name not in _SKILL_BY_NAME:
        return None
    if name == "watch":
        if not _watch_allowed(sender, acfg):
            LOGGER.info("rig_agent: %s has no Jellyfin account — watch declined",
                        sender)
            return None
    elif name == "voicenote":
        pass
    elif not owner_allowed(sender, acfg):
        # Not terminal: rig introspection is not a command a friend issued —
        # "kāds tev dators?" is small talk that happens to hit a rig pattern,
        # and it should still get a human-sounding answer.
        LOGGER.info("rig_agent: %s asked a rig question but is not an owner", sender)
        return None
    ctx = {"sender": sender, "chat_guid": chat_guid}
    if _SKILL_WANTS_CTX.get(name):
        ctx["history"] = recent_context(sender)
    return answer(text, skill=name, ctx=ctx)


def status_payload():
    cfg = load_config()
    return {
        "ok": True,
        "enabled": bool(cfg.get("enabled", True)),
        "owners": list(cfg.get("owners") or []),
        "calendar_urls": len([u for u in (cfg.get("calendar_ics") or []) if str(u or "").strip()]),
        "calendar_days": cfg.get("calendar_days"),
        "skills": [name for name, _p, _f in _SKILLS],
        "config_path": str(CONFIG_PATH),
        "watch": _watch_status(cfg),
    }


def _watch_status(cfg):
    """State for the dashboard's film-suggestion panel.

    Lists the real Jellyfin accounts so the UI offers a dropdown instead of a
    free-text box — a typo'd account name silently turns the feature off for
    that person, which is exactly the failure this panel exists to prevent.
    """
    out = {
        "enabled": bool(cfg.get("watch_enabled", True)),
        "mapped": dict(cfg.get("jellyfin_users") or {}),
        "default_user": None,
        "accounts": [],
        "server_ok": False,
        "error": "",
    }
    try:
        from app import media_agent
        out["default_user"] = media_agent.DEFAULT_USER
        out["server_ok"] = media_agent.available()
        if out["server_ok"]:
            out["accounts"] = sorted(media_agent.users().keys())
    except Exception as exc:
        out["error"] = str(exc)[:160]
    return out
