import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime

_STATE_DIR = ".messaging_history"


def _state_path(state_dir, name):
    d = state_dir or _STATE_DIR
    return os.path.join(d, name)


def _ensure_dir(state_dir):
    d = state_dir or _STATE_DIR
    os.makedirs(d, exist_ok=True)


def remember_device(name, lat, lon, ts, state_dir=None):
    """Keep a device's latest reading in <state_dir>/devices_last_seen.json.

    One entry per device name: {"lat", "lon", "ts"}, ts in seconds. A
    reading older than the one kept never replaces it; the other devices'
    entries stay as they are.
    """
    _ensure_dir(state_dir)
    path = _state_path(state_dir, "devices_last_seen.json")
    data = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}
    prev = data.get(name)
    if prev is not None and prev.get("ts", 0) >= ts:
        return
    data[name] = {"lat": lat, "lon": lon, "ts": ts}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def device_last_seen(name, state_dir=None):
    """Return the device's last kept reading {"lat", "lon", "ts"}, or None."""
    path = _state_path(state_dir, "devices_last_seen.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None
    return data.get(name)


def _write_latest(entry, state_dir=None): 
    _ensure_dir(state_dir)
    path = _state_path(state_dir, "location_latest.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(entry, f, ensure_ascii=False)


def _append_jsonl(entry, state_dir=None):
    _ensure_dir(state_dir)
    path = _state_path(state_dir, "location.jsonl")
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def record(lat, lon, ts, accuracy=None, geocode=None, state_dir=None):
    """Record a location reading.

    geocode: callable(lat, lon) -> {"city": str, "country": str} or None.
    state_dir: directory for state files (default .messaging_history).
    """
    entry = {"lat": lat, "lon": lon, "ts": ts}
    if accuracy is not None:
        entry["accuracy"] = accuracy

    if geocode is not None:
        geo = geocode(lat, lon)
        if geo is None:
            return None
        entry["city"] = geo["city"]
        entry["country"] = geo["country"]

    # Load previous latest to track 'since' and previous_city
    prev = latest(state_dir=state_dir)
    if prev is not None and "city" in entry:
        if prev.get("city") == entry["city"]:
            entry["since"] = prev.get("since", ts)
        else:
            entry["since"] = ts
            entry["previous_city"] = prev.get("city")
            entry["previous_country"] = prev.get("country")
    elif "city" in entry:
        entry["since"] = ts

    _write_latest(entry, state_dir)
    _append_jsonl(entry, state_dir)
    return entry


def latest(state_dir=None):
    """Return the most recent location reading, or None."""
    path = _state_path(state_dir, "location_latest.json")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def describe(now=None, state_dir=None):
    """Returns "" when nothing is known.
    """
    entry = latest(state_dir=state_dir)
    if entry is None or "city" not in entry:
        return ""

    city = entry["city"]
    country = entry.get("country", "")
    since = entry.get("since")
    prev_city = entry.get("previous_city")

    parts = [f"{city}, {country}" if country else city]

    if since is not None:
        dt = datetime.fromtimestamp(since)
        # Latvian genitive month names
        months = ["janvāra", "februāra", "marta", "aprīļa", "maija",
                  "jūnija", "jūlija", "augusta", "septembra", "oktobra",
                  "novembra", "decembra"]
        month_name = months[dt.month - 1]
        parts.append(f"kopš {dt.day}. {month_name}")

    if prev_city:
        parts.append(f"pirms: {prev_city}")

    return "; ".join(parts)


def poll_once(fetch, geocode=None, state_dir=None, on_fix=None):
    """Fetch a location and record it. Returns the recorded entry or None.
    on_fix(lat, lon, ts, accuracy) gets every raw fix first -- the trip log
    keeps the route even when the city lookup fails."""
    data = fetch()
    if data is None:
        return None
    if on_fix is not None:
        try:
            on_fix(data["lat"], data["lon"], data["ts"], data.get("accuracy"))
        except Exception:
            pass
    return record(
        data["lat"], data["lon"], data["ts"],
        accuracy=data.get("accuracy"),
        geocode=geocode,
        state_dir=state_dir,
    )


def _write_status(state_dir, ok, reason=None, login_needed=False):
    _ensure_dir(state_dir)
    entry = {"ok": ok, "checked": time.time()}
    if reason is not None:
        entry["reason"] = reason
    if login_needed:
        entry["login_needed"] = True
    path = _state_path(state_dir, "location_status.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(entry, f, ensure_ascii=False)


MOVING_KM = 0.3          # moved this far since the last fix = on the road
MOVING_HOLD = 15 * 60    # stay fast this long after the last movement


def _km_between(a, b):
    import math
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = (math.sin((la2 - la1) / 2) ** 2
         + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2)
    return 6371.0 * 2 * math.asin(min(1.0, math.sqrt(h)))


def poll_forever(fetch=None, geocode=None, state_dir=None, interval=600,
                 sleep=time.sleep, rounds=None, fast_interval=120, on_fix=None):
    """Poll location forever (or `rounds` times for tests)."""
    if fetch is None:
        fetch = fetch_icloud
        def _remember(name, lat, lon, ts):
            remember_device(name, lat, lon, ts, state_dir=state_dir)
        fetch = lambda: fetch_icloud(on_device=_remember)
    if geocode is None:
        from app.location import nominatim_geocode
        geocode = nominatim_geocode
    if on_fix is None and state_dir is None:
        try:
            from app import trip_log
            on_fix = trip_log.add_fix
        except Exception:
            on_fix = None
    n = 0
    last, moved_at = None, 0.0
    seen = {}

    def _fetch():
        seen["fix"] = fetch()
        return seen["fix"]
    while rounds is None or n < rounds:
        try:
            seen.clear()
            poll_once(_fetch, geocode, state_dir, on_fix=on_fix)
            _write_status(state_dir, ok=True)
            fix = seen.get("fix")
            if fix:
                here = (float(fix["lat"]), float(fix["lon"]))
                if last is not None and _km_between(last, here) >= MOVING_KM:
                    moved_at = time.time()
                last = here
        except SessionExpired as exc:
            _write_status(state_dir, ok=False, reason=str(exc), login_needed=True)
        except Exception as exc:
            _write_status(state_dir, ok=False, reason=str(exc))
        n += 1
        if rounds is None or n < rounds:
            moving = moved_at and time.time() - moved_at <= MOVING_HOLD
            sleep(fast_interval if moving else interval)


def status(state_dir=None):
    """Return the last poll status dict, or None if no status file exists."""
    path = _state_path(state_dir, "location_status.json")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


_USER_AGENT = "LocalAI-Location/1.0 (personal location tracker)"


class SessionExpired(Exception):
    """The saved iCloud session is gone or wants a fresh login."""


def _default_factory(apple_id, client_id, session_dir):
    """With a stored password, the FIRST refusal or 2FA demand retires it
    (icloud_secret.mark_refused) -- never one failed sign-in per poll again.
    """
    from pyicloud import PyiCloudService
    from app import icloud_secret
    password = icloud_secret.load(session_dir)
    svc = PyiCloudService(
        apple_id,
        password=password,
        cookie_directory=session_dir,
        client_id=client_id,
        authenticate=False,
    )
    svc._rig_password_used = bool(password)
    svc._rig_session_dir = session_dir
    try:
        svc.authenticate()
    except Exception as exc:
        if password and _wants_password(exc):
            icloud_secret.mark_refused(session_dir, "sign-in: %s" % str(exc)[:200])
        raise
    if password and getattr(svc, "requires_2fa", False):
        icloud_secret.mark_refused(session_dir, "Apple wants a 2FA code")
    return svc


def _wants_password(exc):
    """pyicloud's ways of saying only a new sign-in with the password helps."""
    try:
        from pyicloud import exceptions as E
    except Exception:
        return False
    return isinstance(exc, (E.PyiCloudFailedLoginException,
                            E.PyiCloudAuthRequiredException,
                            E.PyiCloud2FARequiredException,
                            E.PyiCloudNoStoredPasswordAvailableException))


def _open_find_my(svc):
    """svc.devices, where Find My asking for the password is SessionExpired."""
    try:
        return svc.devices
    except Exception as exc:
        if _wants_password(exc):
            if getattr(svc, "_rig_password_used", False):
                from app import icloud_secret
                icloud_secret.mark_refused(getattr(svc, "_rig_session_dir", None),
                                           "Find My: %s" % str(exc)[:200])
                raise SessionExpired("Apple refused the stored password; it is not "
                                     "used again until the login runs (%s)" % exc) from exc
            raise SessionExpired("Find My wants the Apple ID password again (%s)" % exc) from exc
        raise


def _stop_refresher(devices):
    """Opening Find My starts pyicloud's refresher thread (every 5 minutes,
    for the life of the process); one per poll piled up in the service.
    Stopped once everything is read: reading a device after this would
    start a new one."""
    stop = getattr(devices, "stop_event", None)
    if stop is not None:
        stop.set()


_LIVE = {"svc": None, "devices": None, "since": 0.0, "dir": None}
_LIVE_MAX_AGE = 6 * 3600


def _live_find_my(apple_id, client_id, d):
    """Find My devices from a session kept between rounds: the first call
    opens it, later ones ask Find My for fresh locations on the same
    session. Any failure drops it, and the next round opens a new one."""
    svc, devices = _LIVE["svc"], _LIVE["devices"]
    if (svc is not None and devices is not None and _LIVE["dir"] == d
            and time.time() - _LIVE["since"] < _LIVE_MAX_AGE):
        try:
            devices.refresh(locate=True)
            return devices
        except Exception as exc:
            _LIVE.update(svc=None, devices=None)
            if _wants_password(exc):
                if getattr(svc, "_rig_password_used", False):
                    from app import icloud_secret
                    icloud_secret.mark_refused(d, "Find My refresh: %s" % str(exc)[:200])
                raise SessionExpired("Find My wants the Apple ID password again (%s)"
                                     % exc) from exc
            # anything else: fall through to a fresh session below
    try:
        svc = _default_factory(apple_id, client_id, d)
    except Exception as exc:
        raise SessionExpired(str(exc)) from exc
    if getattr(svc, "requires_2fa", False):
        raise SessionExpired("session requires a new login")
    devices = _open_find_my(svc)
    _LIVE.update(svc=svc, devices=devices, since=time.time(), dir=d)
    return devices


def fetch_icloud(session_dir=None, service_factory=None, on_device=None): 
    """Reads apple_id and client_id from <session_dir>/account.json (default
    .secrets/icloud) and opens the session with
    service_factory(apple_id, client_id, session_dir). The iPhone is the
    device whose status()["deviceDisplayName"] starts with "iPhone"; with
    several, the freshest reading wins. ts is in SECONDS (Find My gives
    milliseconds). No account.json, a session that wants a new login
    (requires_2fa), or the factory raising -> SessionExpired.
    """
    d = session_dir or ".secrets/icloud"
    acct_path = os.path.join(d, "account.json")
    if not os.path.exists(acct_path):
        raise SessionExpired("no account.json in " + d)
    with open(acct_path, encoding="utf-8") as f:
        acct = json.load(f)
    apple_id = acct["apple_id"]
    client_id = acct["client_id"]
    if service_factory is None:
        # ONE session, refreshed, instead of a fresh sign-in every round: at a
        # fix every two minutes that would be 720 new iCloud sessions a day
        devices = _live_find_my(apple_id, client_id, d)
    else:
        try:
            svc = service_factory(apple_id, client_id, d)
        except Exception as exc:
            raise SessionExpired(str(exc)) from exc
        if getattr(svc, "requires_2fa", False):
            raise SessionExpired("session requires a new login")
        devices = _open_find_my(svc)
    best = None
    try:
        for dev in devices:
            try:
                st = dev.status()
            except Exception:
                continue
            name = st.get("name")
            loc = getattr(dev, "location", None)
            if loc:
                ts = loc.get("timeStamp")
                if ts is not None and on_device is not None:
                    try:
                        on_device(name, loc["latitude"], loc["longitude"], ts / 1000.0)
                    except Exception:
                        pass
            if not str(st.get("deviceDisplayName", "")).startswith("iPhone"):
                continue
            if not loc:
                continue
            ts = loc.get("timeStamp")
            if ts is None:
                continue
            if best is None or ts > best[0]:
                best = (ts, loc)
    finally:
        _stop_refresher(devices)
    if best is None:
        return None
    loc = best[1]
    return {
        "lat": loc["latitude"],
        "lon": loc["longitude"],
        "ts": best[0] / 1000.0,
        "accuracy": loc.get("horizontalAccuracy"),
    }


def find_devices(session_dir=None, service_factory=None, geocode=None, now=None, state_dir=None):
    """Return one dict per Find My device, in Find My's order."""
    d = session_dir or ".secrets/icloud"
    acct_path = os.path.join(d, "account.json")
    if not os.path.exists(acct_path):
        raise SessionExpired("no account.json in " + d)
    with open(acct_path, encoding="utf-8") as f:
        acct = json.load(f)
    apple_id = acct["apple_id"]
    client_id = acct["client_id"]
    factory = service_factory or _default_factory
    try:
        svc = factory(apple_id, client_id, d)
    except Exception as exc:
        raise SessionExpired(str(exc)) from exc
    if getattr(svc, "requires_2fa", False):
        raise SessionExpired("session requires a new login")
    if now is None:
        now = time.time()
    devices = _open_find_my(svc)
    read = []
    try:
        for dev in devices:
            try:
                read.append((dev.status(), getattr(dev, "location", None)))
            except Exception:
                continue
    finally:
        _stop_refresher(devices)
    out = []
    for st, loc in read:
        name = st.get("name")
        kind = st.get("deviceDisplayName")
        online = st.get("deviceStatus") == "200"
        battery = st.get("batteryLevel")
        if battery is not None:
            battery = int(round(battery * 100))
        place = None
        fix_age_min = None
        map_link = None
        if loc and loc.get("latitude") is not None and loc.get("longitude") is not None:
            lat = loc["latitude"]
            lon = loc["longitude"]
            if geocode is not None:
                geo = geocode(lat, lon)
                if geo is not None:
                    city = geo.get("city")
                    country = geo.get("country")
                    place = ", ".join(p for p in (city, country) if p)
            ts = loc.get("timeStamp")
            if ts is not None:
                fix_age_min = int((now - ts / 1000.0) // 60)
            map_link = "https://maps.apple.com/?ll={lat},{lon}&q={q}".format(
                lat=lat, lon=lon, q=urllib.parse.quote(str(name), safe="")
            )
        last_seen_place = None
        last_seen_age_min = None
        last_seen_map_link = None
        if map_link is None:
            kept = device_last_seen(name, state_dir)
            if kept is not None:
                klat = kept.get("lat")
                klon = kept.get("lon")
                kts = kept.get("ts")
                if klat is not None and klon is not None:
                    if geocode is not None:
                        geo = geocode(klat, klon)
                        if geo is not None:
                            city = geo.get("city")
                            country = geo.get("country")
                            last_seen_place = ", ".join(p for p in (city, country) if p)
                    if kts is not None:
                        last_seen_age_min = int((now - kts) // 60)
                    last_seen_map_link = "https://maps.apple.com/?ll={lat},{lon}&q={q}".format(
                        lat=klat, lon=klon, q=urllib.parse.quote(str(name), safe="")
                    )
        out.append({
            "name": name,
            "type": kind,
            "online": online,
            "battery": battery,
            "place": place,
            "fix_age_min": fix_age_min,
            "map_link": map_link,
            "last_seen_place": last_seen_place,
            "last_seen_age_min": last_seen_age_min,
            "last_seen_map_link": last_seen_map_link,
        })
    return out


def _default_fetch(url, headers):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _load_cache(state_dir):
    path = _state_path(state_dir, "location_geocode_cache.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_cache(cache, state_dir):
    _ensure_dir(state_dir)
    path = _state_path(state_dir, "location_geocode_cache.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False)


def nominatim_geocode(lat, lon, fetch=None, state_dir=None):
    """Reverse-geocode via OpenStreetMap Nominatim, city level.

    Only coordinates rounded to 2 decimals (~1 km) leave the rig. The URL
    asks for format=jsonv2, zoom=10 and accept-language=lv; the User-Agent
    names this app. The city is the first of address.city / town / village /
    municipality / county present, the country is address.country. Answers
    are cached by the rounded coordinates in location_geocode_cache.json.
    A failed request returns None, never raises.
    """
    rlat = round(lat, 2)
    rlon = round(lon, 2)
    key = f"{rlat},{rlon}"

    cache = _load_cache(state_dir)
    if key in cache:
        return cache[key]

    url = (
        "https://nominatim.openstreetmap.org/reverse?"
        f"lat={rlat}&lon={rlon}"
        "&format=jsonv2&zoom=10&accept-language=lv"
    )
    headers = {"User-Agent": _USER_AGENT}

    do_fetch = fetch or _default_fetch
    try:
        reply = do_fetch(url, headers)
    except Exception:
        return None

    if not isinstance(reply, dict):
        return None

    address = reply.get("address") or {}
    city = None
    for field in ("city", "town", "village", "municipality", "county"):
        if address.get(field):
            city = address[field]
            break
    country = address.get("country")
    if not city or not country:
        return None

    result = {"city": city, "country": country}
    cache[key] = result
    _save_cache(cache, state_dir)
    return result
