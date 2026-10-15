# -*- coding: utf-8 -*-
import csv
import io
import json
import logging
import math
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from app.people import PARTNER_DAT

LOGGER = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]
TRIP_DIR = ROOT / ".messaging_history" / "trip"
WAZE_TRACKS = TRIP_DIR / "waze_tracks.json"
WAZE_BLIPS = TRIP_DIR / "waze_blips.json"
WAZE_VISITS = TRIP_DIR / "waze_visits.json"
POINTS = TRIP_DIR / "points.jsonl"
PHOTO_POINTS = TRIP_DIR / "photo_points.json"
FILLS = TRIP_DIR / "fills.json"
MAPS_DIR = TRIP_DIR / "maps"
GEOJSON = TRIP_DIR / "trip.geojson"
GEOCODE = TRIP_DIR / "geocode.json"
UA = "dobby-rig-anton/1.0 (personal trip map)"
VALHALLA = "https://valhalla1.openstreetmap.de"
NOMINATIM = "https://nominatim.openstreetmap.org/reverse"
ESRI = "https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/%s/MapServer/tile/%d/%d/%d"
ESRI_LAYER = {"light": "World_Light_Gray_Base", "dark": "World_Dark_Gray_Base"}
TILES_DIR = TRIP_DIR / "tiles"
OSM_PLACES = TRIP_DIR / "osm_places.json"   # cities/towns per 1-degree cell
OVERPASS = "https://overpass-api.de/api/interpreter"
TILE = 256
STYLES = ("light", "dark")
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct",
          "Nov", "Dec")
# a big city "on the way": its centre within this far of a driven line
CITY_NEAR_KM = 6.0
TOWN_NEAR_KM = 3.0
TRIP_SINCE = "2026-09-17"      # left Rīga: the first long Waze track of the trip
FILL_MAX_KM = 60.0
GAP_MIN_KM = 3.0
STAY_HOURS = 2.0
PRIVATE_KM = 1.2
CLUSTER_KM = 8.0               # 15 km put Vienna into "Schwechat"
HOME_KM = 40.0
FONT_DIR = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
# sky -> indigo -> fuchsia -> orange, along the trip's time
PALETTE = [(14, 165, 233), (99, 102, 241), (217, 70, 239), (249, 115, 22)]

_job = {"thread": None, "started": 0.0}
_job_lock = threading.Lock()
_io_lock = threading.Lock()


def _km(a, b):
    """Great-circle km between [.., lat, lon] rows (the last two items)."""
    la1, lo1, la2, lo2 = map(math.radians, (a[-2], a[-1], b[-2], b[-1]))
    h = (math.sin((la2 - la1) / 2) ** 2
         + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2)
    return 6371.0 * 2 * math.asin(min(1.0, math.sqrt(h)))


def _path_km(pts):
    return sum(_km(pts[i], pts[i + 1]) for i in range(len(pts) - 1))


def _thin(pts, min_km):
    if len(pts) <= 2:
        return list(pts)
    out = [pts[0]]
    for p in pts[1:-1]:
        if _km(out[-1], p) >= min_km:
            out.append(p)
    out.append(pts[-1])
    return out


def _load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def _save_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _since_ts(since=None):
    s = str(since or TRIP_SINCE)
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").timestamp()
    except ValueError:
        return datetime.strptime(TRIP_SINCE, "%Y-%m-%d").timestamp()


def _until_ts(until=None):
    if not until:
        return time.time() + 86400
    try:
        return datetime.strptime(str(until)[:10], "%Y-%m-%d").timestamp() + 86400
    except ValueError:
        return time.time() + 86400


_WAZE_PT = re.compile(r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\+00\((-?\d+(?:\.\d+)?) (-?\d+(?:\.\d+)?)\)")


def _waze_section(text, name):
    lines = text.splitlines()
    try:
        i = next(k for k, ln in enumerate(lines) if ln.strip() == name)
    except StopIteration:
        return []
    block = []
    for ln in lines[i + 1:]:
        if not ln.strip():
            break
        block.append(ln)
    csv.field_size_limit(1 << 30)
    return list(csv.reader(io.StringIO("\n".join(block))))[1:]


def import_waze_csv(path):
    text = Path(path).read_text(encoding="utf-8-sig", errors="replace")
    tracks, blips = [], []
    for row in _waze_section(text, "Location details"):
        if len(row) < 2:
            continue
        pts = []
        for t, lon, lat in _WAZE_PT.findall(row[1]):   # Waze writes (lon lat)
            ts = datetime.strptime(t, "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=timezone.utc).timestamp()
            pts.append([ts, float(lat), float(lon)])
        pts.sort()
        if not pts:
            continue
        if len(pts) >= 2 and _path_km(pts) >= 1.0:
            tracks.append({"src": "waze", "start": pts[0][0], "end": pts[-1][0],
                           "pts": _thin(pts, 0.1)})
        else:
            blips.append(pts[0])
    visits = []
    for row in _waze_section(text, "Drive summary"):
        if len(row) < 3:
            continue
        m = re.search(r"\((-?\d+(?:\.\d+)?) (-?\d+(?:\.\d+)?)\)\s*$", row[2] or "")
        try:
            t = datetime.strptime(row[0][:19], "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
        if not m:
            continue
        dest = [t, float(m.group(2)), float(m.group(1))]
        tr = next((x for x in tracks if -300 <= x["start"] - t <= 900), None)
        if tr is None:
            continue                                   # only looked at, not driven
        near = min(tr["pts"], key=lambda p: _km(p, dest))
        if _km(near, dest) <= 3.0:
            visits.append([near[0], dest[1], dest[2]])
    with _io_lock:
        have = {round(t["start"]): t for t in _load_json(WAZE_TRACKS, [])}
        for t in tracks:
            have[round(t["start"])] = t
        merged = sorted(have.values(), key=lambda t: t["start"])
        _save_json(WAZE_TRACKS, merged)
        bl = {round(b[0]): b for b in _load_json(WAZE_BLIPS, [])}
        for b in blips:
            bl[round(b[0])] = b
        _save_json(WAZE_BLIPS, sorted(bl.values()))
        vs = {round(v[0]): v for v in _load_json(WAZE_VISITS, [])}
        for v in visits:
            vs[round(v[0])] = v
        _save_json(WAZE_VISITS, sorted(vs.values()))
    return {"tracks": len(tracks), "blips": len(blips), "visits": len(visits),
            "km": round(sum(_path_km(t["pts"]) for t in tracks)), "stored": len(merged)}


def _read_points():
    out = []
    try:
        with open(POINTS, encoding="utf-8") as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                    out.append([float(r["ts"]), float(r["lat"]), float(r["lon"]),
                                float(r.get("acc") or 0)])
                except Exception:
                    continue
    except FileNotFoundError:
        pass
    return out


def _last_point():
    try:
        with open(POINTS, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - 4096))
            tail = fh.read().decode("utf-8", errors="replace").splitlines()
        for line in reversed(tail):
            try:
                r = json.loads(line)
                return [float(r["ts"]), float(r["lat"]), float(r["lon"])]
            except Exception:
                continue
    except FileNotFoundError:
        pass
    return None


def add_fix(lat, lon, ts, accuracy=None):
    """Keep one Find My fix for the trip. A repeat of the same fix (Find My
    answers with the last known one until the phone reports again) is
    skipped. Returns True when a point was written."""
    try:
        lat, lon, ts = float(lat), float(lon), float(ts)
    except (TypeError, ValueError):
        return False
    with _io_lock:
        last = _last_point()
        if last and ts <= last[0]:
            return False
        POINTS.parent.mkdir(parents=True, exist_ok=True)
        with open(POINTS, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": ts, "lat": lat, "lon": lon,
                                 "acc": accuracy}) + "\n")
    return True


def moving_recently(minutes=15, km=0.3, now=None):
    now = time.time() if now is None else now
    recent = [p for p in _read_points()[-40:] if now - p[0] <= minutes * 60]
    if len(recent) < 2:
        return False
    return max(_km(recent[0][:3], p[:3]) for p in recent[1:]) >= km


def _findmy_tracks(since, until):
    pts = [p[:3] for p in _read_points() if since <= p[0] <= until and p[3] <= 250]
    tracks, cur, prev = [], [], None
    for p in pts:
        if prev is not None:
            moving = p[0] - prev[0] <= 45 * 60 and _km(prev, p) >= 0.3
            if moving:
                if not cur:
                    cur = [prev]
                cur.append(p)
            elif cur:
                tracks.append(cur)
                cur = []
        prev = p
    if cur:
        tracks.append(cur)
    return [{"src": "findmy", "start": t[0][0], "end": t[-1][0], "pts": t}
            for t in tracks if _path_km(t) >= 2.0]


def _photo_points(since, until):
    rows = _load_json(PHOTO_POINTS, [])
    return sorted([float(r["t"]), float(r["lat"]), float(r["lon"])]
                  for r in rows if since <= float(r.get("t") or 0) <= until
                  and r.get("lat") is not None)


def _split_pauses(track, max_gap=45 * 60):
    pieces, cur = [], [track["pts"][0]]
    for p in track["pts"][1:]:
        if p[0] - cur[-1][0] > max_gap:
            pieces.append(cur)
            cur = [p]
        else:
            cur.append(p)
    pieces.append(cur)
    return [{"src": track["src"], "start": c[0][0], "end": c[-1][0], "pts": c}
            for c in pieces if len(c) >= 2 and _path_km(c) >= 1.0]


DAYOUT_KM = 3.0      # photos this far from the parked car are a day out


def _densify(p, q, step_km=0.5):
    """p .. q as points about step_km apart (time and position interpolated)."""
    n = max(1, int(_km(p, q) / step_km))
    return [[p[0] + (q[0] - p[0]) * i / n, p[1] + (q[1] - p[1]) * i / n,
             p[2] + (q[2] - p[2]) * i / n] for i in range(n + 1)]


def _day_out(photos, base):
    """The photos of a day out from `base` (where the car stood), in time
    order and thinned to one per ~300 m; [] when none was taken far from it."""
    if not any(_km(base, p) > DAYOUT_KM for p in photos):
        return []
    out = []
    for p in photos:
        if _km(base, p) <= PRIVATE_KM:
            continue
        if not out or _km(out[-1], p) >= 0.3:
            out.append(p)
    return out


def timeline(since=None, until=None):
    """(segments, places) for the trip window.
    segments: {"kind": drive|fill|photos|unknown, "pts": [[ts,lat,lon],..]}
    places:   {"lat","lon","first","last","stay": bool, "role": start|stop|now}
    """
    s, u = _since_ts(since), _until_ts(until)
    # A track with a long pause inside is two drives and a stay: Waze kept
    # recording overnight from Liguria (10-08 19:16 -> 10-09 07:04), which hid
    # the night there.
    waze = [piece for t in _load_json(WAZE_TRACKS, []) if s <= t["start"] <= u
            for piece in _split_pauses(t)]
    fm = [t for t in _findmy_tracks(s, u)
          if not any(t["start"] < w["end"] and w["start"] < t["end"] for w in waze)]
    tracks = sorted(waze + fm, key=lambda t: t["start"])
    if not tracks:
        return [], []
    photos = _photo_points(s, u)
    blips = [b for b in _load_json(WAZE_BLIPS, []) if s <= b[0] <= u]
    evidence = sorted(photos + blips)
    segs, sightings = [], []
    parked, dayouts = [], []      # (where the car stood, until), day-out spans
    for i, tr in enumerate(tracks):
        segs.append({"kind": "drive", "src": tr["src"], "pts": tr["pts"]})
        # a stop of 30-45 min inside a drive is a visit (lunch in Prague, a
        # walk in Monaco); a longer pause already split the track above
        for k in range(len(tr["pts"]) - 1):
            if 30 * 60 <= tr["pts"][k + 1][0] - tr["pts"][k][0] <= 45 * 60:
                sightings.append(("pause", tr["pts"][k]))
        a = tr["pts"][-1]
        sightings.append(("end", a))
        if i + 1 < len(tracks):
            b = tracks[i + 1]["pts"][0]
            d = _km(a, b)
            if d >= GAP_MIN_KM:
                via = [p for p in evidence if a[0] < p[0] < b[0]]
                kind = ("evidence" if via else
                        "fill" if d <= FILL_MAX_KM else "far")
                segs.append({"kind": kind, "pts": [a] + via + [b]})
                if via:
                    legs = [a] + via + [b]
                    for p, q in zip(legs, legs[1:]):
                        travel = _km(p, q) * 1.3 / 60.0 * 3600
                        if (q[0] - p[0]) - travel >= 8 * 3600:
                            sightings.append(("arrive", [p[0] + travel, q[1], q[2]]))
            else:
                out = _day_out([p for p in photos if a[0] < p[0] < b[0]], a)
                if out:
                    segs.append({"kind": "dayout", "pts": (
                        _densify(a, out[0]) + out[1:-1] + _densify(out[-1], b))
                        if len(out) > 1 else (_densify(a, out[0]) + _densify(out[0], b)[1:])})
                    dayouts.append((a[0], b[0]))
                parked.append((a, b[0]))
            sightings.append(("start", b))
    sightings.insert(0, ("trip_start", tracks[0]["pts"][0]))
    for b in blips:
        sightings.append(("blip", b))
    for p in photos:
        sightings.append(("photo", p))
    for v in _load_json(WAZE_VISITS, []):
        if s <= v[0] <= u:
            sightings.append(("visit", v))
    last = _last_point()
    if last and s <= last[0] <= u and last[0] > tracks[-1]["end"]:
        sightings.append(("now", last))
    sightings.sort(key=lambda x: x[1][0])
    # cluster consecutive sightings of the same place
    places = []
    for role, p in sightings:
        cur = places[-1] if places else None
        if cur and _km([cur["lat"], cur["lon"]], p) <= CLUSTER_KM:
            cur["last"] = p[0]
            cur["n"] += 1
            if role == "visit" and "visit" not in cur["roles"]:
                cur["lat"], cur["lon"] = p[1], p[2]
            if role == "now":
                cur["lat"], cur["lon"] = p[1], p[2]
            cur["roles"].add(role)
        else:
            places.append({"lat": p[1], "lon": p[2], "first": p[0], "last": p[0],
                           "n": 1, "roles": {role}})
    for pl in places:
        pl["stay"] = (pl["last"] - pl["first"]) >= STAY_HOURS * 3600
    for a, until in parked:
        if until - a[0] < STAY_HOURS * 3600:
            continue
        home = next((pl for pl in places if pl["first"] <= a[0] <= pl["last"]
                     and _km([pl["lat"], pl["lon"]], a) <= CLUSTER_KM), None)
        if home is not None:
            home["last"] = max(home["last"], until)
            home["stay"] = True
    for t_from, t_to in dayouts:
        for pl in places:
            if t_from < pl["first"] and pl["last"] < t_to and pl["roles"] <= {"photo"}:
                pl["dayout"] = True
                pl["stay"] = False
    for pl in places:
        pl["visit"] = "visit" in pl["roles"]
        pl["pause"] = "pause" in pl["roles"]
        pl["role"] = ("start" if "trip_start" in pl["roles"] else
                      "now" if "now" in pl["roles"] else "stop")
        pl["roles"] = sorted(pl["roles"])
    places[-1]["role"] = "now" if places[-1]["role"] != "start" else "start"
    return segs, places


def _post_json(url, body, timeout=60):
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                 headers={"User-Agent": UA,
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _decode6(encoded):
    coords, index, lat, lon = [], 0, 0, 0
    while index < len(encoded):
        for which in (0, 1):
            shift = result = 0
            while True:
                b = ord(encoded[index]) - 63
                index += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            delta = ~(result >> 1) if result & 1 else result >> 1
            if which == 0:
                lat += delta
            else:
                lon += delta
        coords.append([lat / 1e6, lon / 1e6])
    return coords


def _road_path(pts, curvy=False):
    pts = _thin(pts, 10.0) if len(pts) > 2 else pts
    pts = pts[:20]
    key = "v2|" + ("m" if curvy else "a") + "|" + "|".join(
        "%.4f,%.4f" % (p[1], p[2]) for p in pts)
    cache = _load_json(FILLS, {})
    if key in cache:
        return cache[key]
    body = {"locations": [{"lat": p[1], "lon": p[2]} for p in pts],
            "costing": "motorcycle" if curvy else "auto",
            "directions_options": {"units": "kilometers"}}
    if curvy:
        body["costing_options"] = {"motorcycle": {"use_highways": 0.0, "use_tolls": 0.0}}
    try:
        data = _post_json(VALHALLA + "/route", body, timeout=90)
        line = []
        for leg in data["trip"]["legs"]:
            shape = _decode6(leg["shape"])
            ferry = [0] * len(shape)
            board = None
            for m in leg.get("maneuvers") or []:
                if m.get("type") == 28:
                    board = m.get("begin_shape_index", 0)
                elif m.get("type") == 29 and board is not None:
                    for k in range(board, min(len(shape), m.get("begin_shape_index", 0) + 1)):
                        ferry[k] = 1
                    board = None
            line.extend([[0, la, lo, f] for (la, lo), f in zip(shape, ferry)])
    except Exception as exc:
        LOGGER.info("trip fill route failed: %s", str(exc)[:160])
        return None
    line = _thin(line, 0.15)
    with _io_lock:
        cache = _load_json(FILLS, {})
        cache[key] = line
        _save_json(FILLS, cache)
    return line


_last_lookup = [0.0]
_NAME_FIELDS = ("city", "town", "village", "hamlet", "suburb", "municipality", "county")


def _place_name(lat, lon, digits=2, lang="en"):
    lat, lon = round(lat, digits), round(lon, digits)
    key = "%.*f,%.*f" % (digits, lat, digits, lon)
    if lang != "lv":
        key = "%s:%s" % (lang, key)
    cache = _load_json(GEOCODE, {})
    if key in cache:
        return cache[key]
    wait = 1.1 - (time.time() - _last_lookup[0])
    if wait > 0:
        time.sleep(wait)
    _last_lookup[0] = time.time()
    url = "%s?lat=%.*f&lon=%.*f&format=jsonv2&zoom=13&accept-language=%s" % (
        NOMINATIM, digits, lat, digits, lon, lang)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=20) as resp:
            addr = (json.loads(resp.read().decode("utf-8")) or {}).get("address") or {}
    except Exception as exc:
        LOGGER.info("trip place name failed: %s", str(exc)[:120])
        return {"name": "", "country": "", "kind": ""}
    name, kind = "", ""
    for field in _NAME_FIELDS:
        if addr.get(field):
            name, kind = addr[field], field
            break
    res = {"name": name, "country": addr.get("country") or "", "kind": kind}
    with _io_lock:
        cache = _load_json(GEOCODE, {})
        cache[key] = res
        _save_json(GEOCODE, cache)
    return res


def _name(pl):
    got = _place_name(pl["lat"], pl["lon"], 3 if pl.get("visit") else 2)
    return got.get("name") or "", got.get("country") or "", got.get("kind") or ""


def _date_range(a, b):
    """"17 Sep", "19–24 Sep", "29 Sep – 3 Oct": the map is in English."""
    da, db = datetime.fromtimestamp(a), datetime.fromtimestamp(b)

    def day(d):
        return "%d %s" % (d.day, MONTHS[d.month - 1])
    if da.date() == db.date():
        return day(da)
    if (da.year, da.month) == (db.year, db.month):
        return "%d–%d %s" % (da.day, db.day, MONTHS[db.month - 1])
    return "%s – %s" % (day(da), day(db))


def _is_major(pl):
    if pl.get("role") != "stop" or pl.get("stay"):
        return True
    if pl.get("home"):
        return False
    if pl.get("dayout"):
        return True                   # Paris on the metro day
    if pl.get("visit") and pl.get("kind") not in ("municipality", "county"):
        return True
    return bool(pl.get("pause") and pl.get("kind") in ("city", "town"))


def named_places(places):
    """Places with their city names, consecutive same-city places merged."""
    out = []
    for pl in places:
        city, country, kind = _name(pl)
        if not city:
            continue
        pl = dict(pl, kind=kind)
        if out and out[-1]["city"] == city:
            prev = out[-1]
            prev["last"] = max(prev["last"], pl["last"])
            prev["stay"] = prev["stay"] or pl["stay"] or (
                prev["last"] - prev["first"]) >= STAY_HOURS * 3600
            prev["visit"] = prev.get("visit") or pl.get("visit")
            prev["pause"] = prev.get("pause") or pl.get("pause")
            if pl["role"] == "now":
                prev["role"] = "now"
            continue
        # back in the same town after a day trip (Kraków -> Oświęcim ->
        # Kraków): one place with the whole span, not two labels on one spot
        same = next((o for o in out if o["city"] == city
                     and _km([o["lat"], o["lon"]], [pl["lat"], pl["lon"]]) <= CLUSTER_KM
                     and o["role"] != "start" and pl["role"] != "start"), None)
        if same is not None and pl["first"] - same["last"] <= 3 * 86400:
            same["last"] = max(same["last"], pl["last"])
            same["stay"] = same["stay"] or pl["stay"]
            same["visit"] = same.get("visit") or pl.get("visit")
            same["pause"] = same.get("pause") or pl.get("pause")
            if pl["role"] == "now":
                same["role"] = "now"
            continue
        out.append(dict(pl, city=city, country=country))
    # home ground: errands near where the trip set off (suburbs OSM tags as
    # "city") and passing back through the home city are not the trip
    start = next((o for o in out if o["role"] == "start"), None)
    for o in out:
        o["home"] = bool(start) and o["role"] == "stop" and _km(
            [o["lat"], o["lon"]], [start["lat"], start["lon"]]) <= HOME_KM
    return out


def _pop(tags):
    raw = re.sub(r"[\s,.'’]", "", str(tags.get("population") or ""))
    return int(raw) if raw.isdigit() else 0


def _overpass_cell(ci, cj):
    key = "%d,%d" % (ci, cj)
    cached = _load_json(OSM_PLACES, {}).get(key)
    if cached is not None:
        return cached
    s, w, n, e = ci - 0.02, cj - 0.02, ci + 1.02, cj + 1.02
    q = ('[out:json][timeout:60];(node["place"="city"](%f,%f,%f,%f);'
         'node["place"="town"](%f,%f,%f,%f););out;' % (s, w, n, e, s, w, n, e))
    els = None
    for wait in (0, 10, 30):
        time.sleep(wait or 1)
        try:
            req = urllib.request.Request(
                OVERPASS, data=urllib.parse.urlencode({"data": q}).encode("utf-8"),
                headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=90) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            if body.get("remark") and not body.get("elements"):
                raise ValueError("overpass remark: %s" % str(body["remark"])[:80])
            els = body.get("elements") or []
            break
        except Exception as exc:
            LOGGER.info("overpass cell %s: %s", key, str(exc)[:100])
    if els is None:
        return None
    rows = []
    for el in els:
        t = el.get("tags") or {}
        name = t.get("name:en") or t.get("int_name") or t.get("name")
        if name and "lat" in el:
            rows.append({"name": name, "lat": round(el["lat"], 4),
                         "lon": round(el["lon"], 4), "place": t.get("place"),
                         "pop": _pop(t)})
    with _io_lock:
        cache = _load_json(OSM_PLACES, {})
        cache[key] = rows
        _save_json(OSM_PLACES, cache)
    return rows


def route_cities(lines, labelled):
    pts, last = [], None
    for ln, kind in lines:
        if kind != "solid":
            continue
        for p in ln:
            q = [p[1], p[2]]
            if last is None or _km(last, q) >= 0.5:
                pts.append(q)
                last = q
    if not pts:
        return []
    grid = {}
    for p in pts:
        grid.setdefault((round(p[0] * 10), round(p[1] * 10)), []).append(p)

    def dist(lat, lon):
        gi, gj, best = round(lat * 10), round(lon * 10), 1e9
        for di in (-1, 0, 1):
            for dj in (-2, -1, 0, 1, 2):
                for p in grid.get((gi + di, gj + dj), ()):
                    best = min(best, _km([lat, lon], p))
        return best

    out, seen = [], set()
    for ci, cj in sorted({(math.floor(p[0]), math.floor(p[1])) for p in pts}):
        for r in _overpass_cell(ci, cj) or ():
            key = (r["name"], round(r["lat"], 1), round(r["lon"], 1))
            if key in seen:
                continue
            if r["place"] == "town" and r["pop"] < 20000:
                continue
            if dist(r["lat"], r["lon"]) > (CITY_NEAR_KM if r["place"] == "city"
                                           else TOWN_NEAR_KM):
                continue
            if any(_km([r["lat"], r["lon"]], [pl["lat"], pl["lon"]]) <= CLUSTER_KM
                   for pl in labelled):
                continue
            seen.add(key)
            out.append(dict(r))
    out.sort(key=lambda r: -r["pop"])
    return out


def _country_labels(named, cities):
    """One English country name per country crossed, placed at the middle of
    where the trip touched it."""
    by = {}
    for p in list(named) + list(cities):
        c = p.get("country")
        if c:
            by.setdefault(c, []).append((p["lat"], p["lon"]))
    own_names = {pl.get("city") for pl in named}
    out = []
    for c, v in sorted(by.items()):
        spread = _km([min(a for a, _b in v), min(b for _a, b in v)],
                     [max(a for a, _b in v), max(b for _a, b in v)])
        if c in own_names or spread < 25:
            continue
        out.append({"name": c, "lat": sum(a for a, _b in v) / len(v),
                    "lon": sum(b for _a, b in v) / len(v)})
    return out


def _city_zoom(pop):
    """The zoom level a city's name appears at on the interactive map."""
    return 5 if pop >= 1000000 else 6 if pop >= 300000 else 7 if pop >= 100000 else 8


def _tile_xy(lat, lon, z):
    n = 2 ** z
    x = (lon + 180.0) / 360.0 * n
    y = (1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n
    return x, y


def _base_tile_bytes(style, z, x, y):
    """One label-free basemap tile, cached on disk (the interactive page
    loads the same tiles straight from Esri in the viewer's browser)."""
    layer = ESRI_LAYER.get(style, ESRI_LAYER["light"])
    path = TILES_DIR / layer / str(z) / str(x) / ("%d.img" % y)
    if path.exists():
        return path.read_bytes()
    req = urllib.request.Request(ESRI % (layer, z, y, x), headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = resp.read()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return data


def _tile(style, z, x, y):
    from PIL import Image
    return Image.open(io.BytesIO(_base_tile_bytes(style, z, x, y))).convert("RGB")


def _font(name, size):
    from PIL import ImageFont
    for f in ([name] if isinstance(name, str) else name):
        try:
            return ImageFont.truetype(str(FONT_DIR / f), int(size))
        except Exception:
            continue
    return ImageFont.load_default()


def _color_at(frac):
    frac = max(0.0, min(1.0, frac))
    pos = frac * (len(PALETTE) - 1)
    i = min(int(pos), len(PALETTE) - 2)
    t = pos - i
    a, b = PALETTE[i], PALETTE[i + 1]
    return tuple(int(a[k] + (b[k] - a[k]) * t) for k in range(3))


def _pull_in(a, b, private_pts):
    d = _km(a, b)
    if d <= 2 * PRIVATE_KM or not any(_km(a, q) < PRIVATE_KM for q in private_pts):
        return a
    f = (PRIVATE_KM * 1.2) / d
    return [a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f, a[2] + (b[2] - a[2]) * f]


def _trim_private(line, private_pts):
    """Split `line` where it comes within PRIVATE_KM of a private point."""
    if len(line) == 2:
        a, b = line
        return [[_pull_in(a, b, private_pts), _pull_in(b, a, private_pts)]]
    out, cur = [], []
    for p in line:
        if any(_km(p, q) < PRIVATE_KM for q in private_pts):
            if len(cur) >= 2:
                out.append(cur)
            cur = []
        else:
            cur.append(p)
    if len(cur) >= 2:
        out.append(cur)
    return out


def _fmt_km(km):
    return "{:,}".format(int(round(km))).replace(",", "\u00a0")


def render(out_path, since=None, until=None, title="", style="light", max_side=3000):
    """Draw the trip; returns a summary dict (also written as GeoJSON)."""
    from PIL import Image, ImageDraw, ImageFilter
    segs, places = timeline(since, until)
    if not segs:
        raise ValueError("no recorded drives in that window")
    named = named_places(places)
    t0 = min(p[0] for s in segs if s["kind"] == "drive" for p in s["pts"])
    t1 = max(p[0] for s in segs if s["kind"] == "drive" for p in s["pts"])
    span = max(1.0, t1 - t0)
    t_end = max([t1] + [pl["last"] for pl in places])
    private = [[pl["lat"], pl["lon"]] for pl in places if pl["stay"] or pl["role"] == "start"]

    lines = []          # (points, kind, colour fraction)
    total_km = 0.0
    for s in segs:
        pts = s["pts"]
        if s["kind"] == "drive":
            total_km += _path_km(pts)
            lines.append((pts, "solid"))
        elif s["kind"] == "dayout":
            lines.append((pts, "dotted"))
        else:
            straight = _path_km(pts)
            road = _road_path(pts, curvy=s["kind"] != "fill")
            if road and (s["kind"] == "evidence" or _path_km(road) <= 1.6 * straight):
                ta, tb = pts[0][0], pts[-1][0]
                n = max(1, len(road) - 1)
                road = [[ta + (tb - ta) * i / n, p[1], p[2], p[3] if len(p) > 3 else 0]
                        for i, p in enumerate(road)]
                # driven stretches solid, ferry stretches dashed on the real
                # sea route; only the driven part counts as kilometres
                runs, run = [], [road[0]]
                for p in road[1:]:
                    run.append(p)
                    if p[3] != run[0][3]:
                        runs.append(run)
                        run = [p]
                runs.append(run)
                for r in runs:
                    if len(r) < 2:
                        continue
                    if r[0][3]:
                        lines.append((r, "dashed"))
                    else:
                        lines.append((r, "solid"))
                        total_km += _path_km(r)
            else:
                lines.append(([pts[0], pts[-1]], "dashed"))

    # the big cities on the way and the countries crossed, all in English
    labelled = [pl for pl in named if _is_major(pl)
                or (pl.get("kind") == "city" and not pl.get("home"))]
    try:
        cities = route_cities(lines, labelled)
        for c in cities:
            c["country"] = (_place_name(c["lat"], c["lon"], 1) or {}).get("country") or ""
    except Exception:
        LOGGER.exception("trip map: cities along the route not found")
        cities = []
    country_labels = _country_labels(named, cities)

    lats = [p[1] for ln, _k in lines for p in ln] + [pl["lat"] for pl in named]
    lons = [p[2] for ln, _k in lines for p in ln] + [pl["lon"] for pl in named]
    z = 3
    for zz in range(3, 14):
        x0, y0 = _tile_xy(max(lats), min(lons), zz)
        x1, y1 = _tile_xy(min(lats), max(lons), zz)
        w, h = (x1 - x0) * TILE, (y1 - y0) * TILE
        if max(w, h) * 1.25 <= max_side:
            z = zz
        else:
            break
    x0, y0 = _tile_xy(max(lats), min(lons), z)
    x1, y1 = _tile_xy(min(lats), max(lons), z)
    bw, bh = (x1 - x0) * TILE, (y1 - y0) * TILE
    pad = max(bw, bh) * 0.13 + 60
    W, H = int(bw + 2 * pad), int(bh + 2 * pad)
    if W < H * 0.72:
        W = int(H * 0.72)
    if H < W * 0.62:
        H = int(W * 0.62)
    ox = (x0 + x1) / 2 * TILE - W / 2
    oy = (y0 + y1) / 2 * TILE - H / 2 - 0.03 * H   # room for the title panel

    def px(lat, lon, scale=1.0):
        x, y = _tile_xy(lat, lon, z)
        return ((x * TILE - ox) * scale, (y * TILE - oy) * scale)

    style = style if style in STYLES else "light"
    base = Image.new("RGB", (W, H), (240, 240, 236) if style == "light" else (20, 22, 30))
    tx0, ty0 = int(ox // TILE), int(oy // TILE)
    tx1, ty1 = int((ox + W) // TILE), int((oy + H) // TILE)
    n = 2 ** z
    for ty in range(max(0, ty0), min(n - 1, ty1) + 1):
        for tx in range(tx0, tx1 + 1):
            try:
                img = _tile(style, z, tx % n, ty)
            except Exception as exc:
                LOGGER.info("trip tile %s/%s/%s failed: %s", z, tx, ty, str(exc)[:100])
                continue
            base.paste(img, (int(tx * TILE - ox), int(ty * TILE - oy)))

    SS = 2
    unit = max(W, H) / 1600.0
    f_country = _font(["segoeuib.ttf", "seguisb.ttf"], 30 * unit)

    def _country_word(cl):
        word = "  ".join(cl["name"].upper().split()).replace("", " ").strip()
        return word
    ov = Image.new("RGBA", (W * SS, H * SS), (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)
    dark = style == "dark"
    casing = (20, 22, 30, 200) if dark else (255, 255, 255, 235)
    lw = max(3, int(5.0 * unit)) * SS
    for pts, kind in lines:
        for part in _trim_private(pts, private):
            xy = [px(p[1], p[2], SS) for p in part]
            if kind == "dashed":
                _dashed(d, xy, fill=_color_at((part[0][0] - t0) / span) + (210,),
                        width=max(2, int(lw * 0.55)), dash=int(16 * unit * SS))
                continue
            if kind == "dotted":
                _dotted(d, xy, fill=_color_at((part[0][0] - t0) / span) + (230,),
                        radius=max(2, lw * 0.32), gap=lw * 1.6)
                continue
            d.line(xy, fill=casing, width=lw + int(4 * unit * SS), joint="curve")
    for pts, kind in lines:
        if kind in ("dashed", "dotted"):
            continue
        for part in _trim_private(pts, private):
            xy = [px(p[1], p[2], SS) for p in part]
            for i in range(len(xy) - 1):
                c = _color_at((part[i][0] - t0) / span)
                d.line([xy[i], xy[i + 1]], fill=c + (255,), width=lw)
    ov = ov.resize((W, H), Image.LANCZOS)
    img = base.convert("RGBA")
    img.alpha_composite(ov)

    # markers and labels at 1x, with halos
    d = ImageDraw.Draw(img)
    f_city = _font(["seguisb.ttf", "segoeuib.ttf"], 25 * unit)
    f_city_small = _font(["segoeui.ttf"], 20 * unit)
    f_date = _font(["segoeui.ttf"], 18 * unit)
    halo = (20, 22, 30) if dark else (255, 255, 255)
    ink = (240, 240, 245) if dark else (34, 38, 49)
    sub = (190, 195, 210) if dark else (90, 96, 112)
    taken = []
    order = sorted(range(len(named)), key=lambda i: (
        not _is_major(named[i]), -(named[i]["last"] - named[i]["first"])))
    for i in order:
        pl = named[i]
        x, y = px(round(pl["lat"], 2), round(pl["lon"], 2))
        frac = (pl["first"] - t0) / span
        col = (34, 197, 94) if pl["role"] == "start" else (
            (249, 115, 22) if pl["role"] == "now" else _color_at(frac))
        r = (11 if _is_major(pl) else 6) * unit
        if pl["role"] == "now":
            d.ellipse([x - r * 2.1, y - r * 2.1, x + r * 2.1, y + r * 2.1],
                      fill=col + (70,))
        d.ellipse([x - r - 3 * unit, y - r - 3 * unit, x + r + 3 * unit, y + r + 3 * unit],
                  fill=halo + (255,))
        d.ellipse([x - r, y - r, x + r, y + r], fill=col + (255,))
        taken.append((x - r, y - r, x + r, y + r))
    for i in order:
        pl = named[i]
        x, y = px(round(pl["lat"], 2), round(pl["lon"], 2))
        major = _is_major(pl)
        # a short stop is a dot; it gets a name only when it is a city
        # (Hengelo), not the fuel stop in Volkel -- and never on home ground
        if not major and (pl.get("kind") != "city" or pl.get("home")):
            continue
        fc = f_city if major else f_city_small
        name = pl["city"]
        date = _date_range(pl["first"], pl["last"]) if major else ""
        nb = d.textbbox((0, 0), name, font=fc, stroke_width=int(3 * unit))
        nw, nh = nb[2] - nb[0], nb[3] - nb[1]
        db = d.textbbox((0, 0), date, font=f_date) if date else (0, 0, 0, 0)
        dw, dh = db[2] - db[0], db[3] - db[1]
        bw_, bh_ = max(nw, dw), nh + (dh + 4 * unit if date else 0)
        spots = []
        for gap in (16 * unit, 40 * unit):
            spots += [(x + gap, y - bh_ / 2), (x - gap - bw_, y - bh_ / 2),
                      (x - bw_ / 2, y - gap - bh_), (x - bw_ / 2, y + gap),
                      (x + gap, y - bh_ - gap / 2), (x - gap - bw_, y + gap / 2),
                      (x + gap, y + gap / 2), (x - gap - bw_, y - bh_ - gap / 2)]
        for cx, cy in spots:
            box = (cx - 4, cy - 4, cx + bw_ + 4, cy + bh_ + 4)
            if box[0] < 0 or box[1] < 0 or box[2] > W or box[3] > H:
                continue
            if any(not (box[2] < t[0] or box[0] > t[2] or box[3] < t[1] or box[1] > t[3])
                   for t in taken):
                continue
            d.text((cx, cy - nb[1]), name, font=fc, fill=ink,
                   stroke_width=int(3 * unit), stroke_fill=halo)
            if date:
                d.text((cx, cy + nh + 4 * unit - db[1]), date, font=f_date, fill=sub,
                       stroke_width=int(2 * unit), stroke_fill=halo)
            taken.append(box)
            break

    # the big cities on the way: a small ring and the name, biggest first,
    # only where there is room (the interactive page shows them all by zoom)
    f_town = _font(["segoeui.ttf"], 17 * unit)
    for c in cities:
        if c["pop"] < 100000:
            continue
        x, y = px(c["lat"], c["lon"])
        r = 4 * unit
        nb = d.textbbox((0, 0), c["name"], font=f_town, stroke_width=int(2 * unit))
        nw, nh = nb[2] - nb[0], nb[3] - nb[1]
        for cx, cy in ((x + 9 * unit, y - nh / 2), (x - 9 * unit - nw, y - nh / 2),
                       (x - nw / 2, y - 9 * unit - nh), (x - nw / 2, y + 9 * unit)):
            box = (cx - 3, cy - 3, cx + nw + 3, cy + nh + 3)
            if box[0] < 0 or box[1] < 0 or box[2] > W or box[3] > H:
                continue
            if any(not (box[2] < t[0] or box[0] > t[2] or box[3] < t[1] or box[1] > t[3])
                   for t in taken):
                continue
            d.ellipse([x - r, y - r, x + r, y + r], fill=halo + (255,),
                      outline=sub, width=max(1, int(1.5 * unit)))
            d.text((cx, cy - nb[1]), c["name"], font=f_town, fill=sub,
                   stroke_width=int(2 * unit), stroke_fill=halo)
            taken.append(box)
            taken.append((x - r, y - r, x + r, y + r))
            break

    cell = max(4, int(6 * unit))
    route_cells = set()
    for pts, _kind in lines:
        for part in _trim_private(pts, private):
            for i in range(len(part) - 1):
                (xa, ya), (xb, yb) = px(part[i][1], part[i][2]), px(part[i + 1][1], part[i + 1][2])
                steps = max(1, int(math.hypot(xb - xa, yb - ya) / cell))
                for k in range(steps + 1):
                    route_cells.add((int((xa + (xb - xa) * k / steps) // cell),
                                     int((ya + (yb - ya) * k / steps) // cell)))
    # the title panel (top left) and the legend line (bottom) come later;
    # keep country names off them ("ITALY" sat on the legend)
    taken.append((0, 0, 440 * unit, 130 * unit))
    taken.append((0, H - 64 * unit, W, H))
    clayer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    cd = ImageDraw.Draw(clayer)
    cfill = (150, 156, 170, 200) if dark else (120, 126, 140, 150)
    for cl in country_labels:
        word = _country_word(cl)
        cb = cd.textbbox((0, 0), word, font=f_country)
        cw, ch = cb[2] - cb[0], cb[3] - cb[1]
        x0, y0 = px(cl["lat"], cl["lon"])
        for dx, dy in ((0, 0), (0, -70), (0, 70), (-140, 0), (140, 0), (0, -140),
                       (0, 140), (-110, -90), (110, -90), (-110, 90), (110, 90)):
            bx, by = x0 + dx * unit - cw / 2, y0 + dy * unit - ch / 2
            box = (bx - 6, by - 6, bx + cw + 6, by + ch + 6)
            if box[0] < 0 or box[1] < 0 or box[2] > W or box[3] > H:
                continue
            if any(not (box[2] < t[0] or box[0] > t[2] or box[3] < t[1] or box[1] > t[3])
                   for t in taken):
                continue
            if any((i, j) in route_cells
                   for i in range(int(box[0] // cell), int(box[2] // cell) + 1)
                   for j in range(int(box[1] // cell), int(box[3] // cell) + 1)):
                continue
            cd.text((bx, by - cb[1]), word, font=f_country, fill=cfill)
            taken.append(box)
            break
    img.alpha_composite(clayer)
    d = ImageDraw.Draw(img)

    # title panel
    countries = sorted({pl["country"] for pl in named if pl.get("country")}
                       | {c["country"] for c in cities if c.get("country")})
    days = (datetime.fromtimestamp(t_end).date() - datetime.fromtimestamp(t0).date()).days + 1
    title = title or "Eurotrip %d" % datetime.fromtimestamp(t0).year
    stats = "%s  ·  %s km  ·  %d days  ·  %d countries" % (
        _date_range(t0, t_end), _fmt_km(total_km), days, len(countries))
    f_title = _font(["segoeuib.ttf", "seguisb.ttf"], 50 * unit)
    f_stats = _font(["segoeui.ttf"], 24 * unit)
    f_leg = _font(["segoeui.ttf"], 18 * unit)
    tb = d.textbbox((0, 0), title, font=f_title)
    sb = d.textbbox((0, 0), stats, font=f_stats)
    pw = max(tb[2] - tb[0], sb[2] - sb[0]) + 64 * unit
    ph = (tb[3] - tb[1]) + (sb[3] - sb[1]) + 70 * unit
    m = 36 * unit
    shadow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle(
        [m + 4 * unit, m + 8 * unit, m + pw + 4 * unit, m + ph + 8 * unit],
        radius=int(22 * unit), fill=(0, 0, 0, 70))
    img.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(10 * unit)))
    panel = (30, 33, 43, 236) if dark else (255, 255, 255, 236)
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(layer).rounded_rectangle([m, m, m + pw, m + ph],
                                            radius=int(22 * unit), fill=panel)
    img.alpha_composite(layer)
    d = ImageDraw.Draw(img)
    d.text((m + 32 * unit, m + 24 * unit - tb[1]), title, font=f_title, fill=ink)
    d.text((m + 32 * unit, m + 24 * unit + (tb[3] - tb[1]) + 14 * unit - sb[1]),
           stats, font=f_stats, fill=sub)
    # gradient bar under the stats: the colours run from the first day to the last
    gy = m + ph - 16 * unit
    for k in range(int(pw - 64 * unit)):
        c = _color_at(k / max(1.0, pw - 64 * unit))
        d.line([(m + 32 * unit + k, gy), (m + 32 * unit + k, gy + 5 * unit)], fill=c)
    # legend + attribution
    leg = "— drive    - - - ferry or not recorded    · · · day out (metro / on foot)"
    lb = d.textbbox((0, 0), leg, font=f_leg)
    d.text((m, H - m - (lb[3] - lb[1]) - lb[1]), leg, font=f_leg, fill=sub,
           stroke_width=int(2 * unit), stroke_fill=halo)
    att = "Base map © Esri  ·  © OpenStreetMap contributors"
    ab = d.textbbox((0, 0), att, font=f_leg)
    d.text((W - m - (ab[2] - ab[0]), H - m - (ab[3] - ab[1]) - ab[1]), att, font=f_leg,
           fill=sub, stroke_width=int(2 * unit), stroke_fill=halo)

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.convert("RGB").save(out_path, quality=92, optimize=True)
    summary = {"from": t0, "to": t_end, "km": round(total_km), "days": days,
               "countries": countries, "path": str(out_path), "size": [W, H],
               "title": title,
               "places": [{"city": pl["city"], "country": pl["country"],
                           "dates": _date_range(pl["first"], pl["last"]),
                           "stay": pl["stay"], "role": pl["role"],
                           "major": _is_major(pl)} for pl in named],
               "cities": [c["name"] for c in cities]}
    _write_geojson(lines, named, private)
    try:
        summary["html_slug"] = write_html(
            lines, named, cities, country_labels, private, t0, span, title, stats)
    except Exception:
        LOGGER.exception("trip map: interactive page not written")
    return summary


def _dashed(d, xy, fill, width, dash):
    """Dashes along a whole polyline, the pattern carried across its many
    short pieces (per-piece dashes made a curved ferry route look solid)."""
    period = dash * 1.8
    phase = 0.0                       # distance into the current period
    for i in range(len(xy) - 1):
        (xa, ya), (xb, yb) = xy[i], xy[i + 1]
        length = math.hypot(xb - xa, yb - ya)
        k = 0.0
        while k < length:
            if phase < dash:          # inside a dash
                step = min(dash - phase, length - k)
                d.line([(xa + (xb - xa) * k / length, ya + (yb - ya) * k / length),
                        (xa + (xb - xa) * (k + step) / length,
                         ya + (yb - ya) * (k + step) / length)], fill=fill, width=width)
            else:
                step = min(period - phase, length - k)
            k += step
            phase = (phase + step) % period


def _dotted(d, xy, fill, radius, gap):
    carry = 0.0
    for i in range(len(xy) - 1):
        (xa, ya), (xb, yb) = xy[i], xy[i + 1]
        length = math.hypot(xb - xa, yb - ya)
        k = carry
        while k <= length:
            x, y = xa + (xb - xa) * k / max(length, 1e-9), ya + (yb - ya) * k / max(length, 1e-9)
            d.ellipse([x - radius, y - radius, x + radius, y + radius], fill=fill)
            k += gap
        carry = k - length


def _write_geojson(lines, named, private):
    """The shareable (trimmed) trip for a later interactive map."""
    feats = []
    for pts, kind in lines:
        for part in _trim_private(pts, private):
            feats.append({"type": "Feature", "properties": {"kind": kind},
                          "geometry": {"type": "LineString", "coordinates": [
                              [round(p[2], 5), round(p[1], 5), int(p[0])] for p in part]}})
    for pl in named:
        feats.append({"type": "Feature",
                      "properties": {"city": pl["city"], "country": pl["country"],
                                     "first": int(pl["first"]), "last": int(pl["last"]),
                                     "stay": pl["stay"], "role": pl["role"]},
                      "geometry": {"type": "Point",
                                   "coordinates": [round(pl["lon"], 2), round(pl["lat"], 2)]}})
    try:
        _save_json(GEOJSON, {"type": "FeatureCollection", "features": feats})
    except Exception as exc:
        LOGGER.info("trip geojson not written: %s", str(exc)[:120])


STATIC_DIR = ROOT / "app" / "static"

_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>__TITLE__</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.css">
<style>
:root{--ink:#22262f;--sub:#5a6070;--card:rgba(255,255,255,.94)}
html,body{margin:0;height:100%;background:#e8e8e5;
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
#map{position:fixed;inset:0}
.card{position:fixed;left:12px;top:12px;z-index:1000;background:var(--card);border-radius:16px;
  padding:14px 18px 12px;box-shadow:0 6px 24px rgba(0,0,0,.18);max-width:calc(100vw - 60px)}
.card h1{margin:0;font-size:26px;letter-spacing:.3px;color:var(--ink)}
.card p{margin:4px 0 9px;font-size:13px;color:var(--sub)}
.bar{height:5px;border-radius:3px;background:linear-gradient(90deg,__GRADIENT__)}
.legend{position:fixed;left:12px;bottom:22px;z-index:1000;background:var(--card);border-radius:10px;
  padding:6px 10px;font-size:12px;color:var(--sub);max-width:calc(100vw - 48px)}
.leaflet-tooltip.lbl{background:none;border:none;box-shadow:none;padding:0;color:var(--ink);
  font-weight:600;white-space:nowrap;text-shadow:0 0 3px #fff,0 0 3px #fff,0 0 4px #fff}
.leaflet-tooltip.lbl:before{display:none}
.lbl .d{display:block;font-weight:400;font-size:11px;color:var(--sub)}
.leaflet-tooltip.stay{font-size:14px}
.leaflet-tooltip.city{font-size:12px;font-weight:500;color:#40465a}
.country{color:rgba(78,84,98,.42);font-weight:700;letter-spacing:5px;font-size:15px;
  text-transform:uppercase;white-space:nowrap;text-shadow:0 0 2px #fff;transform:translate(-50%,-50%)}
@media (max-width:520px){.card h1{font-size:21px}.card{padding:10px 14px 9px}}
</style></head><body>
<div id="map"></div>
<div class="card"><h1 id="t"></h1><p id="s"></p><div class="bar"></div></div>
<div class="legend">&#9473; drive &nbsp; &#9477; ferry or not recorded &nbsp; &middot;&middot;&middot; day out (metro / on foot) &nbsp;&middot;&nbsp; zoom in for the cities on the way</div>
<script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js"></script>
<script>
var D = __DATA__;
function esc(s){return String(s).replace(/[&<>"]/g,function(c){
  return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c];});}
document.getElementById("t").textContent = D.title;
document.getElementById("s").textContent = D.stats;
var map = L.map("map", {minZoom: 3, maxZoom: 12, zoomSnap: 0.5, worldCopyJump: true,
                         zoomControl: false});
L.control.zoom({position: "topright"}).addTo(map);
// A view before any layer (lines on a map with no view throw "reading
// 'intersects'"), and one that needs no size: a page opened with no size yet
// (a hidden tab) fitted itself to zoom 12. It fits properly once it has one.
var B = L.latLngBounds(D.bounds);
map.setView(B.getCenter(), 5);
L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Light_Gray_Base/MapServer/tile/{z}/{y}/{x}",
  {maxZoom: 16, attribution: "Base map &copy; Esri &middot; &copy; OpenStreetMap contributors"}).addTo(map);
D.lines.forEach(function (l) {
  if (l.o) L.polyline(l.p, {color: l.c, weight: 4, opacity: .9, dashArray: "0.1 9", lineCap: "round", interactive: false}).addTo(map);
  else if (l.f) L.polyline(l.p, {color: l.c, weight: 3, opacity: .85, dashArray: "6 9", interactive: false}).addTo(map);
  else L.polyline(l.p, {color: "#fff", weight: 8, opacity: .9, interactive: false}).addTo(map);
});
D.lines.forEach(function (l) {
  if (!l.f && !l.o) L.polyline(l.p, {color: l.c, weight: 4.5, opacity: 1, interactive: false}).addTo(map);
});
// country names come from the basemap itself (in English); ours on top of
// them doubled every one ("GERMANY GERMANY")
var tiers = {}, labelled = [];
D.cities.forEach(function (c) {
  var g = tiers[c.z] || (tiers[c.z] = L.layerGroup());
  var m = L.circleMarker([c.lat, c.lon], {radius: 3, color: "#4b5263", weight: 1.5, fillColor: "#fff", fillOpacity: 1})
    .bindTooltip(esc(c.n), {permanent: true, direction: "right", className: "lbl city", offset: [5, 0]})
    .addTo(g);
  labelled.push({layer: m, w: c.pop || 0});
});
D.stays.forEach(function (s) {
  var m = L.circleMarker([s.lat, s.lon], {radius: s.m ? 7 : 4, color: "#fff", weight: 2,
    fillColor: s.col, fillOpacity: 1}).addTo(map);
  var t = esc(s.n) + '<span class="d">' + esc(s.d) + "</span>";
  if (s.m) {
    m.bindTooltip(t, {permanent: true, direction: "right", className: "lbl stay", offset: [9, 0]});
    labelled.push({layer: m, w: 1e12 + (s.w || 0)});     // where he stayed beats any city
  } else m.bindTooltip(t, {direction: "right", className: "lbl", offset: [6, 0]});
});
labelled.sort(function (a, b) { return b.w - a.w; });
// Zoomed out, every label at once is a pile (his phone, 2026-10-11): the
// longest stays and biggest cities keep their room, the rest come back as
// you zoom in.
function declutter() {
  var boxes = [];
  labelled.forEach(function (o) {
    var tt = o.layer.getTooltip(), el = tt && tt.getElement();
    if (!el || !map.hasLayer(o.layer)) return;
    el.style.visibility = "visible";
    var r = el.getBoundingClientRect();
    var hit = boxes.some(function (b) {
      return !(r.right < b.left || r.left > b.right || r.bottom < b.top || r.top > b.bottom);
    });
    if (hit) el.style.visibility = "hidden"; else boxes.push(r);
  });
}
function sync() {
  var z = map.getZoom();
  Object.keys(tiers).forEach(function (k) {
    if (z >= +k) tiers[k].addTo(map); else map.removeLayer(tiers[k]);
  });
  declutter();
}
var fitted = false;
function fit() {
  map.invalidateSize();
  if (map.getSize().x > 0) { map.fitBounds(B, {padding: [40, 40]}); fitted = true; }
}
fit();
if (window.ResizeObserver) new ResizeObserver(function () {
  if (!fitted) fit(); else map.invalidateSize();
}).observe(document.getElementById("map"));
map.on("zoomend", sync);
map.on("moveend", declutter);
sync();
</script></body></html>
"""


def _hex(rgb):
    return "#%02x%02x%02x" % tuple(rgb)


def write_html(lines, named, cities, country_labels, private, t0, span, title, stats):
    out_lines = []
    for pts, kind in lines:
        for part in _trim_private(pts, private):
            thin, last = [], None
            for p in part:
                if last is None or _km([last[1], last[2]], [p[1], p[2]]) >= 0.3:
                    thin.append(p)
                    last = p
            if part and thin[-1] is not part[-1]:
                thin.append(part[-1])
            step = 40
            for i in range(0, max(1, len(thin) - 1), step):
                chunk = thin[i:i + step + 1]
                if len(chunk) < 2:
                    continue
                out_lines.append({
                    "c": _hex(_color_at((chunk[0][0] - t0) / span)),
                    "f": kind == "dashed",
                    "o": kind == "dotted",
                    "p": [[round(p[1], 4), round(p[2], 4)] for p in chunk]})
    stays = []
    for pl in named:
        if pl.get("home") and pl["role"] == "stop":
            continue
        col = (34, 197, 94) if pl["role"] == "start" else (
            (249, 115, 22) if pl["role"] == "now" else _color_at((pl["first"] - t0) / span))
        stays.append({"n": pl["city"], "d": _date_range(pl["first"], pl["last"]),
                      "lat": round(pl["lat"], 2), "lon": round(pl["lon"], 2),
                      "m": _is_major(pl), "col": _hex(col),
                      "w": int(pl["last"] - pl["first"])})
    lats = [p[0] for ln in out_lines for p in ln["p"]] + [s["lat"] for s in stays]
    lons = [p[1] for ln in out_lines for p in ln["p"]] + [s["lon"] for s in stays]
    data = {"title": title, "stats": stats, "lines": out_lines, "stays": stays,
            "cities": [{"n": c["name"], "lat": c["lat"], "lon": c["lon"],
                        "z": _city_zoom(c["pop"])} for c in cities],
            "countries": [{"n": cl["name"], "lat": round(cl["lat"], 3),
                           "lon": round(cl["lon"], 3)} for cl in country_labels],
            "bounds": [[min(lats), min(lons)], [max(lats), max(lons)]]}
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-") or "trip-map"
    blob = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    html = (_PAGE.replace("__TITLE__", title.replace("<", "&lt;"))
            .replace("__GRADIENT__", ",".join(_hex(c) for c in PALETTE))
            .replace("__DATA__", blob))
    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    (STATIC_DIR / (slug + ".html")).write_text(html, encoding="utf-8", newline="\n")
    return slug


def caption(summary, links=None):
    text = ("%s: %s, %s km, %d dienas, %d valstis." % (
        summary.get("title") or "Eurotrip",
        _date_range(summary["from"], summary["to"]), _fmt_km(summary["km"]),
        summary["days"], len(summary["countries"])))
    if isinstance(links, dict) and links:
        text += "\nPietuvināma karte:\n" + "\n".join(
            "  %s: %s" % (who, url) for who, url in links.items())
    elif links:
        text += "\nPietuvināma karte: %s" % links
    return text


def _publish_page(slug, chat_guid):
    if not slug:
        return None
    try:
        from app import agent_tools, messaging
        pub = agent_tools._publish_slug_to_media(slug)
    except Exception:
        LOGGER.exception("trip map page not published")
        return None
    if not pub.get("ok"):
        LOGGER.info("trip map page not published: %s", pub.get("reason"))
        return None
    acfg = messaging.load_config().get("auto_reply") or {}
    if chat_guid in (acfg.get("assistant_chats") or []):
        return pub.get("url_owner") or pub.get("url")
    return {"Tev": pub.get("url_owner") or pub.get("url"), PARTNER_DAT: pub.get("url_partner")}


def send_to_chat(chat_guid, text, image_path):
    """The map into the chat as Anton's own picture, then a one-line caption."""
    from app import messaging
    acfg = messaging.load_config().get("auto_reply") or {}
    provider = str(acfg.get("provider") or "bluebubbles_http")
    pcfg = (messaging.load_config().get("providers") or {}).get(provider) or {}
    messaging._bb_send_attachment(pcfg, chat_guid, image_path, name="celojums.jpg",
                                  transcript="[ceļojuma karte]", mode="image")
    messaging._append_jsonl(messaging.OUTBOX_PATH, messaging._entry(
        provider, chat_guid, "[celojums.jpg]", "sent", "",
        {"auto_reply": True, "mode": "image"}))
    if text:
        messaging.send_message(provider=provider, target=chat_guid, text=text,
                               metadata={"auto_reply": True, "mode": "trip_map"},
                               real_send=True)


def _wait_for_reply(chat_guid, since, timeout=60):
    """Until the turn's own reply to this chat is out (or `timeout`)."""
    from app import messaging
    end = time.time() + timeout
    while time.time() < end:
        try:
            for row in messaging.outbox(limit=30):
                md = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
                if (str(row.get("target") or "") == chat_guid and md.get("auto_reply")
                        and md.get("mode") not in ("image", "trip_map")
                        and messaging._epoch_seconds(row.get("timestamp")) >= since):
                    return True
        except Exception:
            return False
        time.sleep(2)
    return False


def start_map_job(chat_guid, title="", since=None, until=None, style="light",
                  sender=send_to_chat):
    """Draw in the background and send to the chat. One at a time."""
    with _job_lock:
        th = _job.get("thread")
        if th is not None and th.is_alive():
            return {"ok": True, "started": False, "already_drawing": True,
                    "note": "A trip map is already being drawn; it comes into the "
                            "chat when done. Do not start it again."}
        if not _load_json(WAZE_TRACKS, []) and not _read_points():
            return {"ok": False, "error_type": "no_data",
                    "message": "no recorded drives yet"}

        asked = time.time()

        def run():
            stamp = time.strftime("%Y%m%d-%H%M%S")
            out = MAPS_DIR / ("trip-%s.jpg" % stamp)
            try:
                summary = render(out, since, until, title, style)
                links = _publish_page(summary.get("html_slug"), chat_guid)
                _wait_for_reply(chat_guid, asked)
                sender(chat_guid, caption(summary, links), str(out))
                LOGGER.info("trip map sent to %s: %s km, %d places", chat_guid,
                            summary["km"], len(summary["places"]))
            except Exception:
                LOGGER.exception("trip map failed")
                try:
                    from app import messaging
                    acfg = messaging.load_config().get("auto_reply") or {}
                    messaging.send_message(
                        provider=str(acfg.get("provider") or "bluebubbles_http"),
                        target=chat_guid, text="Karti neizdevās uzzīmēt, mēģināšu vēlreiz, ja pateiksi.",
                        metadata={"auto_reply": True, "mode": "trip_map"}, real_send=True)
                except Exception:
                    pass

        th = threading.Thread(target=run, name="trip_map", daemon=True)
        _job.update(thread=th, started=time.time())
        th.start()
    return {"ok": True, "started": True,
            "note": "Drawing the trip map from every recorded drive; the map "
                    "and a one-line summary come into this chat by themselves in "
                    "about a minute. Say only that the map is being drawn -- never "
                    "list cities, dates or kilometres from memory."}
