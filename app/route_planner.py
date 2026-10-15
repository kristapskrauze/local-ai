# -*- coding: utf-8 -*-
"""Route planning with eyes on the road."""
from __future__ import annotations

import base64
import json
import logging
import math
import os
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

LOGGER = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]
VALHALLA = "https://valhalla1.openstreetmap.de"
OVERPASS = "https://overpass-api.de/api/interpreter"
NOMINATIM = "https://nominatim.openstreetmap.org"
UA = "dobby-rig-anton/1.0 (personal route planner)"
TILE_URL = "https://tile.openstreetmap.org/%d/%d/%d.png"
TILE_CACHE = ROOT / ".ai_memory" / "tiles"

MAX_ROUTES = 5
PHOTOS_PER_ROUTE = 4
PRIVATE_START_KM = 10.0
DRAW_SKIP_KM = 3.0
VIGNETTE = {"AT", "CZ", "SK", "HU", "SI", "CH", "BG", "RO", "MD"}
COLORS = [(33, 102, 204), (230, 120, 20), (40, 150, 60), (140, 60, 170), (200, 40, 40)]
LETTERS = "ABCDE"

_job = {"thread": None, "destination": "", "started": 0.0}
_job_lock = threading.Lock()


def _post_json(url, body, timeout=60):
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                 headers={"User-Agent": UA,
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _get_json(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _overpass(query, timeout=120):
    data = urllib.parse.urlencode({"data": query}).encode("utf-8")
    req = urllib.request.Request(OVERPASS, data=data, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def decode_polyline6(encoded):
    coords, i, lat, lon = [], 0, 0, 0
    s = str(encoded or "")
    while i < len(s):
        for which in (0, 1):
            shift = result = 0
            while True:
                b = ord(s[i]) - 63
                i += 1
                result |= (b & 0x1f) << shift
                shift += 5
                if b < 0x20:
                    break
            delta = ~(result >> 1) if result & 1 else result >> 1
            if which == 0:
                lat += delta
            else:
                lon += delta
        coords.append((lat / 1e6, lon / 1e6))
    return coords


def haversine_km(a, b):
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dp, dl = p2 - p1, math.radians(b[1] - a[1])
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(min(1.0, h)))


def cumulative_km(points):
    out, total = [0.0], 0.0
    for a, b in zip(points, points[1:]):
        total += haversine_km(a, b)
        out.append(total)
    return out


def point_at_km(points, cum, km):
    """The shape point nearest to `km` along the route."""
    if not points:
        return None
    lo, hi = 0, len(cum) - 1
    while lo < hi:
        mid = (lo + hi) // 2
        if cum[mid] < km:
            lo = mid + 1
        else:
            hi = mid
    return points[lo]


def resample(points, cum, step_km, start_km=0.0, end_km=None):
    """(km, point) every step_km between start_km and end_km."""
    end_km = cum[-1] if end_km is None else end_km
    out, km = [], start_km
    while km <= end_km + 1e-9:
        out.append((km, point_at_km(points, cum, km)))
        km += step_km
    return out


class _Grid(object):
    """Nearest-distance lookups against a resampled polyline."""

    def __init__(self, points, cell=0.05):
        self.cell = cell
        self.cells = {}
        for p in points:
            self.cells.setdefault(self._key(p), []).append(p)

    def _key(self, p):
        return (int(math.floor(p[0] / self.cell)), int(math.floor(p[1] / self.cell)))

    def nearest_km(self, p, rings=3):
        ky, kx = self._key(p)
        best = None
        for dy in range(-rings, rings + 1):
            for dx in range(-rings, rings + 1):
                for q in self.cells.get((ky + dy, kx + dx), ()):
                    d = haversine_km(p, q)
                    if best is None or d < best:
                        best = d
        return best if best is not None else 999.0


def geocode(place):
    """{lat, lon, name, city, country_code} for a place name, or None."""
    place = " ".join(str(place or "").split())
    if not place:
        return None
    url = NOMINATIM + "/search?" + urllib.parse.urlencode(
        {"q": place, "format": "json", "limit": 1, "addressdetails": 1,
         "accept-language": "lv,en"})
    try:
        rows = _get_json(url, timeout=20)
    except Exception as exc:
        LOGGER.info("geocode failed for %r: %s", place, str(exc)[:120])
        return None
    if not rows:
        return None
    row = rows[0]
    addr = row.get("address") or {}
    city = (addr.get("city") or addr.get("town") or addr.get("village")
            or addr.get("municipality") or addr.get("county") or place)
    return {"lat": float(row["lat"]), "lon": float(row["lon"]),
            "name": row.get("display_name") or place, "city": city,
            "country_code": str(addr.get("country_code") or "").upper()}


def city_center(city, country_code=""):
    found = geocode("%s %s" % (city, country_code) if country_code else city)
    return (found["lat"], found["lon"]) if found else None


PROFILES = (
    ("fastest", {}, 2),
    ("toll_free", {"exclude_tolls": True, "use_tolls": 0.0}, 1),
    ("no_motorway", {"use_highways": 0.0, "exclude_tolls": True}, 1),
    ("country_roads", {"use_highways": 0.3}, 0),
)


def _route_from_trip(trip, profile):
    s = trip.get("summary") or {}
    shape = "".join(leg.get("shape") or "" for leg in (trip.get("legs") or [])[:1])
    pts = decode_polyline6(shape)
    return {"profile": profile, "shape": shape, "points": pts,
            "length_km": float(s.get("length") or 0.0),
            "time_min": float(s.get("time") or 0.0) / 60.0,
            "has_toll": bool(s.get("has_toll")), "has_ferry": bool(s.get("has_ferry")),
            "has_highway": bool(s.get("has_highway"))}


def candidates(origin, dest, vias=()):
    found = []
    locations = ([{"lat": origin[0], "lon": origin[1]}]
                 + [{"lat": v[0], "lon": v[1], "type": "through"} for v in vias]
                 + [{"lat": dest[0], "lon": dest[1]}])
    for profile, opts, alternates in PROFILES:
        body = {"locations": locations,
                "costing": "auto", "costing_options": {"auto": opts},
                "alternates": alternates, "directions_type": "none",
                "units": "kilometers"}
        try:
            data = _post_json(VALHALLA + "/route", body, timeout=60)
        except Exception as exc:
            LOGGER.info("valhalla %s failed: %s", profile, str(exc)[:160])
            continue
        trips = [data.get("trip")] + [a.get("trip") for a in data.get("alternates") or []]
        for trip in trips:
            if trip and (trip.get("legs") or []):
                found.append(_route_from_trip(trip, profile))
    return dedupe(found)


def overlap(a, b):
    """Share of route a (sampled every 2 km) lying within 300 m of route b."""
    grid = _Grid([p for _km, p in resample(b["points"], b["cum"], 0.2)])
    samples = resample(a["points"], a["cum"], 2.0)
    if not samples:
        return 1.0
    near = sum(1 for _km, p in samples if grid.nearest_km(p) <= 0.3)
    return near / float(len(samples))


def dedupe(routes, limit=MAX_ROUTES):
    usable = [r for r in routes if r.get("points") and len(r["points"]) >= 2]
    for r in usable:
        r["cum"] = cumulative_km(r["points"])
    kept = []

    def distinct(r):
        return all(not (overlap(r, k) >= 0.92 and overlap(k, r) >= 0.92) for k in kept)

    for profile, _opts, _alts in PROFILES:
        mine = sorted((r for r in usable if r["profile"] == profile),
                      key=lambda x: x["time_min"])
        if mine and len(kept) < limit and distinct(mine[0]):
            kept.append(mine[0])
    for r in sorted(usable, key=lambda x: x["time_min"]):
        if len(kept) >= limit:
            break
        if r not in kept and distinct(r):
            kept.append(r)
    kept.sort(key=lambda x: x["time_min"])
    return kept


EDGE_ATTRS = ["edge.surface", "edge.road_class", "edge.length", "edge.speed",
              "edge.speed_limit", "edge.lane_count", "edge.density", "edge.curvature",
              "edge.toll", "edge.use", "edge.names", "edge.tunnel", "edge.bridge",
              "edge.begin_shape_index", "edge.end_shape_index",
              "admin.country_code", "node.admin_index"]
ROUGH = ("paved_rough", "compacted", "dirt", "gravel", "path", "impassable")
MOTORWAY = ("motorway", "trunk")


def _num(value, default=0):
    if isinstance(value, str) and value.strip().lower() == "unlimited":
        return 999
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def road_facts(route):
    """Every segment the route drives on, summed up."""
    data = _post_json(VALHALLA + "/trace_attributes", {
        "encoded_polyline": route["shape"], "shape_match": "edge_walk",
        "costing": "auto", "units": "kilometers",
        "filters": {"attributes": EDGE_ATTRS, "action": "include"}}, timeout=120)
    edges = data.get("edges") or []
    admins = data.get("admins") or []
    surface, klass, country, toll_by_country = {}, {}, {}, {}
    km = 0.0
    curvy = rural = ferry = toll = 0.0
    single_lane = fast_limit = 0.0
    stretches, cur = [], None
    for e in edges:
        length = float(e.get("length") or 0.0)
        start = km
        km += length
        s = str(e.get("surface") or "unknown")
        c = str(e.get("road_class") or "unknown")
        surface[s] = surface.get(s, 0.0) + length
        klass[c] = klass.get(c, 0.0) + length
        ai = (e.get("end_node") or {}).get("admin_index")
        cc = (admins[ai].get("country_code") if isinstance(ai, int) and ai < len(admins)
              else "") or "?"
        country[cc] = country.get(cc, 0.0) + length
        if e.get("toll"):
            toll += length
            toll_by_country[cc] = toll_by_country.get(cc, 0.0) + length
        if str(e.get("use") or "") == "ferry":
            ferry += length
        if _num(e.get("curvature")) >= 5:
            curvy += length
        if _num(e.get("density")) <= 4:
            rural += length
        if _num(e.get("lane_count")) == 1 and c not in ("residential", "service_other"):
            single_lane += length
        if _num(e.get("speed_limit")) >= 90:
            fast_limit += length
        if s in ROUGH:
            names = [n for n in (e.get("names") or []) if n]
            if cur and cur["surface_worst"] and abs(cur["end_km"] - start) < 0.05:
                cur["end_km"] = km
                cur["surfaces"][s] = cur["surfaces"].get(s, 0.0) + length
                for n in names:
                    if n not in cur["names"]:
                        cur["names"].append(n)
                cur["shape_index"].append(int(e.get("begin_shape_index") or 0))
            else:
                cur = {"start_km": start, "end_km": km, "surfaces": {s: length},
                       "names": names[:3], "road_class": c, "surface_worst": True,
                       "shape_index": [int(e.get("begin_shape_index") or 0)]}
                stretches.append(cur)
        else:
            cur = None
    total = max(km, 1e-6)
    worst = sorted((st for st in stretches if st["end_km"] - st["start_km"] >= 0.3),
                   key=lambda st: -(st["end_km"] - st["start_km"]))[:6]
    for st in worst:
        idx = st["shape_index"][len(st["shape_index"]) // 2]
        st["mid_point"] = route["points"][min(idx, len(route["points"]) - 1)]
        st["length_km"] = round(st["end_km"] - st["start_km"], 1)
        st["surface"] = max(st["surfaces"].items(), key=lambda kv: kv[1])[0]
        for k in ("surfaces", "shape_index", "surface_worst"):
            st.pop(k, None)
    return {
        "segments": len(edges),
        "km": round(km, 1),
        "surface_km": {k: round(v, 1) for k, v in sorted(surface.items(), key=lambda kv: -kv[1])},
        "class_km": {k: round(v, 1) for k, v in sorted(klass.items(), key=lambda kv: -kv[1])},
        "country_km": {k: round(v, 1) for k, v in sorted(country.items(), key=lambda kv: -kv[1])},
        "toll_km": round(toll, 1),
        "toll_km_by_country": {k: round(v, 1) for k, v in toll_by_country.items()},
        "ferry_km": round(ferry, 1),
        "motorway_share": round(sum(klass.get(c, 0.0) for c in MOTORWAY) / total, 3),
        "rough_km": round(sum(surface.get(s, 0.0) for s in ROUGH), 1),
        "unpaved_km": round(sum(surface.get(s, 0.0) for s in
                                ("compacted", "dirt", "gravel", "path", "impassable")), 1),
        "curvy_share": round(curvy / total, 3),
        "rural_share": round(rural / total, 3),
        "single_lane_km": round(single_lane, 1),
        "fast_limit_share": round(fast_limit / total, 3),
        "worst_stretches": worst,
    }


LAND = {
    "forest": ("landuse=forest", "natural=wood"),
    "fields": ("landuse=farmland", "landuse=meadow", "landuse=grass", "natural=grassland",
               "landuse=orchard", "landuse=vineyard", "natural=heath"),
    "water": ("natural=water", "natural=coastline", "natural=beach", "natural=wetland",
              "waterway=river", "waterway=canal"),
    "protected": ("boundary=protected_area", "boundary=national_park",
                  "leisure=nature_reserve"),
    "town": ("landuse=residential", "landuse=commercial", "landuse=retail"),
    "industry": ("landuse=industrial", "landuse=quarry", "landuse=landfill"),
}


def _overpass_retry(query, timeout=200, waits=(20, 45)):
    for i in range(len(waits) + 1):
        try:
            return _overpass(query, timeout=timeout)
        except urllib.error.HTTPError as exc:
            if exc.code not in (429, 502, 503, 504) or i == len(waits):
                raise
            LOGGER.info("overpass %s, retrying in %ds", exc.code, waits[i])
            time.sleep(waits[i])
    return {}


def surroundings_all(routes, max_points=30):
    gpts, index, per_route = [], {}, []
    for r in routes:
        length = r["cum"][-1]
        step = max(5.0, min(20.0, length / max_points))
        gids = []
        for _km, p in resample(r["points"], r["cum"], step,
                               start_km=min(PRIVATE_START_KM, length / 4)):
            key = (round(p[0] / 0.03), round(p[1] / 0.03))
            if key not in index:
                index[key] = len(gpts)
                gpts.append(p)
            gids.append(index[key])
        per_route.append((gids, step))
    if not gpts:
        return [{} for _r in routes]
    parts = []
    for i, p in enumerate(gpts):
        parts.append(
            'make pt i=%d; out; '
            '(way(around:200,%.5f,%.5f)[~"^(landuse|natural|leisure)$"~"."];'
            'way(around:300,%.5f,%.5f)[waterway~"^(river|canal)$"];'
            'relation(around:200,%.5f,%.5f)[~"^(landuse|natural)$"~"."];'
            'is_in(%.5f,%.5f)->.a%d; area.a%d[boundary~"^(national_park|protected_area)$"];'
            '); out tags;' % (i, p[0], p[1], p[0], p[1], p[0], p[1], p[0], p[1], i, i))
    data = _overpass_retry("[out:json][timeout:180];\n" + "\n".join(parts))
    groups = {}
    named = {}
    cur = None
    for el in data.get("elements") or []:
        tags = el.get("tags") or {}
        if el.get("type") == "pt":
            cur = int(tags.get("i", -1))
            groups.setdefault(cur, set())
            continue
        if cur is None:
            continue
        for key in ("landuse", "natural", "leisure", "waterway", "boundary"):
            if tags.get(key):
                pair = "%s=%s" % (key, tags[key])
                for group, pairs in LAND.items():
                    if pair in pairs:
                        groups[cur].add(group)
                        if group in ("water", "protected") and tags.get("name"):
                            named.setdefault((cur, group), set()).add(tags["name"])
    out = []
    for gids, step in per_route:
        if not gids or not groups:
            out.append({})
            continue
        n = float(len(gids))
        shares = {g: round(sum(1 for i in gids if g in groups.get(i, ())) / n, 3) for g in LAND}
        water = sorted({nm for i in gids for nm in named.get((i, "water"), ())})
        prot = sorted({nm for i in gids for nm in named.get((i, "protected"), ())})
        out.append({"points": len(gids), "step_km": round(step, 1), "shares": shares,
                    "water_names": water[:8], "protected_names": prot[:8]})
    return out


VISION_SCHEMA = {"type": "object", "properties": {
    "is_road_photo": {"type": "boolean"},
    "surface": {"type": "string", "enum": ["excellent", "good", "fair", "poor",
                                           "very_poor", "unpaved", "cannot_tell"]},
    "defects": {"type": "array", "items": {"type": "string", "enum": [
        "potholes", "cracks", "patches", "ruts", "gravel", "roadworks", "narrow",
        "no_shoulder", "none"]}},
    "scenery": {"type": "integer", "minimum": 1, "maximum": 5},
    "landscape": {"type": "string", "enum": [
        "forest", "fields", "water", "coast", "hills", "mountains", "village", "town",
        "city", "industrial", "motorway_corridor", "other"]},
    "note": {"type": "string"}},
    "required": ["is_road_photo", "surface", "defects", "scenery", "landscape", "note"]}
VISION_PROMPT = (
    "Street-level photo taken from a car. Judge ONLY what is visible. surface: the "
    "condition of the road the car drives on (excellent = new smooth asphalt, good = "
    "sound asphalt, fair = worn or patched but fine, poor = broken or potholed, "
    "very_poor = barely drivable, unpaved = gravel or dirt, cannot_tell = the road "
    "is not visible or the photo is too blurry). defects: only what you can SEE, "
    "else ['none']. scenery 1-5: how pleasant the view is for a driver (1 = "
    "industrial or ugly, 3 = ordinary fields or town, 5 = striking landscape, water "
    "or an old town). note: one short sentence. Answer only with JSON.")


def _vision(image_bytes):
    from app import agent_tools
    payload = {"model": "qwen3.8:latest", "stream": False, "think": False,
               "format": VISION_SCHEMA, "keep_alive": agent_tools.KEEP_ALIVE,
               "options": agent_tools._runner_options({"temperature": 0, "num_predict": 220}),
               "messages": [{"role": "user", "content": VISION_PROMPT,
                             "images": [base64.b64encode(image_bytes).decode("ascii")]}]}
    req = urllib.request.Request("http://127.0.0.1:11434/api/chat",
                                 data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as resp:
        raw = json.loads(resp.read().decode("utf-8", "replace"))
    return json.loads(((raw.get("message") or {}).get("content")) or "{}")


def photo_points(route, facts, limit=PHOTOS_PER_ROUTE):
    length = route["cum"][-1]
    chosen = []

    def far_enough(p):
        return all(haversine_km(p, q) >= 15.0 for _k, q, _w in chosen)

    for st in (facts or {}).get("worst_stretches") or []:
        if len(chosen) >= max(1, limit // 2):
            break
        mid = (st["start_km"] + st["end_km"]) / 2.0
        if mid < PRIVATE_START_KM:
            continue
        p = point_at_km(route["points"], route["cum"], mid)
        if p and far_enough(p):
            chosen.append((mid, p, "worst_stretch"))
    slots = limit - len(chosen)
    for i in range(1, slots + 8):
        if len(chosen) >= limit:
            break
        km = length * i / float(slots + 1)
        if km < PRIVATE_START_KM or km > length - 2.0:
            continue
        p = point_at_km(route["points"], route["cum"], km)
        if p and far_enough(p):
            chosen.append((km, p, "along"))
    return [(km, p, why, _bearing_at(route, km)) for km, p, why in sorted(chosen)]


def _bearing_at(route, km):
    from app import street_imagery as si
    a = point_at_km(route["points"], route["cum"], max(0.0, km - 0.3))
    b = point_at_km(route["points"], route["cum"], km + 0.3)
    if not a or not b or a == b:
        return None
    return si.bearing_deg(a[0], a[1], b[0], b[1])


def _best_frame(point, bearing=None):
    from app import street_imagery as si
    found = si.search_nearby(point[0], point[1], 250, 12, providers=["kartaview"])
    images = found.get("images") or []
    if not images:
        return None

    def along(rec):
        h = rec.get("heading")
        if bearing is None or h is None:
            return False
        return min(si.angle_gap(h, bearing), si.angle_gap(h, (bearing + 180.0) % 360.0)) <= 35.0

    images.sort(key=lambda r: (not along(r), (r.get("distance_m") or 999) > 60,
                               -(int((r.get("captured_at") or "0")[:4] or 0)),
                               r.get("distance_m") or 999))
    return images[0]


def inspect_photos(points, cache):
    """KartaView frame near each point, read by the vision model. `cache` maps
    a rounded point to a finished look, so overlapping routes share them."""
    from app import street_imagery as si
    todo = [p for p in points if (round(p[1][0], 2), round(p[1][1], 2)) not in cache]
    with ThreadPoolExecutor(max_workers=6) as pool:
        frames = list(pool.map(lambda p: _best_frame(p[1], p[3]), todo))
    for (km, p, why, _bearing), rec in zip(todo, frames):
        key = (round(p[0], 2), round(p[1], 2))
        if not rec:
            cache[key] = {"photo": False}
            continue
        look = {"photo": True, "image_id": rec.get("id"),
                "captured": (rec.get("captured_at") or "")[:10],
                "distance_m": round(rec.get("distance_m") or 0)}
        try:
            blob = si.fetch_image_bytes(rec.get("image_reference") or rec.get("thumbnail"))
            look.update(_vision(blob))
        except Exception as exc:
            look["error"] = str(exc)[:120]
        if look.get("error") or look.get("surface") not in PHOTO_WORDS:
            look["is_road_photo"] = False
            look["unreadable"] = True
        cache[key] = look
    out = []
    for km, p, why, _bearing in points:
        look = dict(cache.get((round(p[0], 2), round(p[1], 2))) or {"photo": False})
        look.update({"km": round(km), "why": why})
        out.append(look)
    return out


SURFACE_Q = {"paved_smooth": 10, "paved": 8.5, "paved_rough": 5, "compacted": 3.5,
             "gravel": 2.5, "dirt": 1.5, "path": 0.5, "impassable": 0.0}
PHOTO_Q = {"excellent": 10, "good": 8, "fair": 6, "poor": 3, "very_poor": 1,
           "unpaved": 2.5}
PHOTO_WORDS = {"excellent": "ļoti labs", "good": "labs", "fair": "vidējs", "poor": "slikts",
               "very_poor": "ļoti slikts", "unpaved": "grants vai zeme"}


def road_score(facts, photos):
    km = sum(v for s, v in facts["surface_km"].items() if s in SURFACE_Q) or 0.0
    osm = (sum(SURFACE_Q[s] * v for s, v in facts["surface_km"].items() if s in SURFACE_Q)
           / km) if km else 7.0
    seen = [PHOTO_Q[p["surface"]] for p in photos
            if p.get("is_road_photo") and p.get("surface") in PHOTO_Q]
    score = 0.7 * osm + 0.3 * (sum(seen) / len(seen)) if seen else osm
    bad = sum(1 for p in photos if set(p.get("defects") or ()) & {"potholes", "ruts"})
    return round(max(0.0, min(10.0, score - 0.5 * bad)), 1)


def scenery_score(facts, around, photos):
    sh = (around or {}).get("shares") or {}
    score = (4.5 + 2.5 * sh.get("forest", 0) + 3.5 * sh.get("water", 0)
             + 3.0 * sh.get("protected", 0) + 1.0 * sh.get("fields", 0)
             - 3.0 * sh.get("industry", 0) - 1.5 * sh.get("town", 0)
             - 5.0 * facts.get("motorway_share", 0) + 4.0 * facts.get("curvy_share", 0)
             + 1.0 * facts.get("rural_share", 0))
    views = [int(p["scenery"]) for p in photos if p.get("is_road_photo") and p.get("scenery")]
    if views:
        score = 0.6 * score + 0.4 * (2.0 * sum(views) / len(views))
    return round(max(0.0, min(10.0, score)), 1)


def pick(routes):
    """Which route is which. Each route is a dict with time_min, has_toll,
    road_score and scenery_score."""
    fastest = min(routes, key=lambda r: r["time_min"])
    picks = {"fastest": fastest["letter"]}
    free = [r for r in routes if not r["has_toll"]]
    picks["toll_free"] = min(free, key=lambda r: r["time_min"])["letter"] if free else None
    reasonable = [r for r in routes if r["time_min"] <= fastest["time_min"] * 1.6] or routes
    picks["scenic"] = max(reasonable, key=lambda r: (r["scenery_score"], -r["time_min"]))["letter"]
    picks["best_roads"] = max(routes, key=lambda r: (r["road_score"], -r["time_min"]))["letter"]
    for r in routes:
        r["overall"] = round(0.45 * 10.0 * fastest["time_min"] / max(r["time_min"], 1.0)
                             + 0.25 * r["road_score"] + 0.2 * r["scenery_score"]
                             + (1.0 if not r["has_toll"] else 0.0), 2)
    picks["overall"] = max(routes, key=lambda r: r["overall"])["letter"]
    picks["avoid"] = (min(routes, key=lambda r: r["overall"])["letter"]
                      if len(routes) >= 3 else None)
    return picks


def _deviation_waypoints(route, reference, limit=3):
    """Points where `route` leaves `reference` the most: the fewest stops that
    make Google Maps drive THIS route instead of its own fastest."""
    grid = _Grid([p for _km, p in resample(reference["points"], reference["cum"], 0.5)])
    samples = resample(route["points"], route["cum"], 2.0, start_km=PRIVATE_START_KM,
                       end_km=route["cum"][-1] - 5.0)
    runs, cur = [], []
    for km, p in samples:
        d = grid.nearest_km(p, rings=6)
        if d >= 3.0:
            cur.append((d, p))
        elif cur:
            runs.append(cur)
            cur = []
    if cur:
        runs.append(cur)
    runs.sort(key=lambda run: -max(d for d, _p in run))
    return [max(run)[1] for run in runs[:limit]]


def google_maps_url(dest, waypoints=()):
    """No origin: Google Maps starts from where the phone is."""
    params = [("api", "1"), ("destination", "%.5f,%.5f" % dest), ("travelmode", "driving")]
    if waypoints:
        params.append(("waypoints", "|".join("%.5f,%.5f" % p for p in waypoints)))
    return "https://www.google.com/maps/dir/?" + urllib.parse.urlencode(params)


def waze_url(dest):
    return "https://waze.com/ul?" + urllib.parse.urlencode(
        {"ll": "%.5f,%.5f" % dest, "navigate": "yes"})


def _tile_xy(lat, lon, z):
    n = 2 ** z
    x = (lon + 180.0) / 360.0 * n
    y = (1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n
    return x, y


def _tile(z, x, y):
    TILE_CACHE.mkdir(parents=True, exist_ok=True)
    path = TILE_CACHE / ("%d_%d_%d.png" % (z, x, y))
    if path.exists() and time.time() - path.stat().st_mtime < 30 * 86400:
        return path.read_bytes()
    req = urllib.request.Request(TILE_URL % (z, x, y), headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = resp.read()
    path.write_bytes(data)
    time.sleep(0.05)
    return data


def render_map(routes, start_city_point, dest, out_path, size=1100):
    """The routes on an OSM map, lettered and coloured like the report. The
    canvas follows the routes' shape (a square left a wide, flat route in the
    top half of an empty picture)."""
    import io
    from PIL import Image, ImageDraw, ImageFont
    drawn = []
    for r in routes:
        pts = [p for km, p in resample(r["points"], r["cum"], 0.5)
               if km >= DRAW_SKIP_KM]
        drawn.append(pts)
    allp = [p for pts in drawn for p in pts] + [dest] + (
        [start_city_point] if start_city_point else [])
    lat_min = min(p[0] for p in allp)
    lat_max = max(p[0] for p in allp)
    lon_min = min(p[1] for p in allp)
    lon_max = max(p[1] for p in allp)
    z = 12
    while z > 3:
        x0, y0 = _tile_xy(lat_max, lon_min, z)
        x1, y1 = _tile_xy(lat_min, lon_max, z)
        if (x1 - x0) * 256 <= size * 0.8 and (y1 - y0) * 256 <= size * 0.8:
            break
        z -= 1
    x0, y0 = _tile_xy(lat_max, lon_min, z)
    x1, y1 = _tile_xy(lat_min, lon_max, z)
    legend_h = 20 + 34 * len(routes)
    width = int(max(640, min(size, (x1 - x0) * 256 + 240)))
    height = int(max(480, min(size, (y1 - y0) * 256 + 60 + legend_h)))
    cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
    left = cx * 256 - width / 2.0
    # the routes centred in the space BELOW the legend
    top = cy * 256 - (legend_h + height) / 2.0
    img = Image.new("RGB", (width, height), (238, 238, 238))
    for tx in range(int(left // 256), int((left + width) // 256) + 1):
        for ty in range(int(top // 256), int((top + height) // 256) + 1):
            if not (0 <= ty < 2 ** z):
                continue
            try:
                tile = Image.open(io.BytesIO(_tile(z, tx % (2 ** z), ty))).convert("RGB")
                img.paste(tile, (int(tx * 256 - left), int(ty * 256 - top)))
            except Exception:
                pass
    draw = ImageDraw.Draw(img)

    def xy(p):
        px, py = _tile_xy(p[0], p[1], z)
        return (px * 256 - left, py * 256 - top)

    for pts, r in reversed(list(zip(drawn, routes))):
        line = [xy(p) for p in pts]
        if len(line) >= 2:
            draw.line(line, fill=(255, 255, 255), width=9, joint="curve")
            draw.line(line, fill=r["color"], width=5, joint="curve")
    try:
        font = ImageFont.truetype(r"C:\Windows\Fonts\arialbd.ttf", 22)
        small = ImageFont.truetype(r"C:\Windows\Fonts\arial.ttf", 14)
    except Exception:
        font = small = ImageFont.load_default()
    dx, dy = xy(dest)
    draw.ellipse((dx - 9, dy - 9, dx + 9, dy + 9), fill=(200, 30, 30), outline=(255, 255, 255), width=3)
    if start_city_point:
        sx, sy = xy(start_city_point)
        draw.rectangle((sx - 8, sy - 8, sx + 8, sy + 8), fill=(30, 30, 30), outline=(255, 255, 255), width=3)
    for i, r in enumerate(routes):
        y = 14 + i * 34
        draw.rectangle((10, y - 4, 330, y + 28), fill=(255, 255, 255))
        draw.line([(18, y + 12), (58, y + 12)], fill=r["color"], width=6)
        draw.text((66, y), "%s  %s, %d km" % (r["letter"], _hm(r["time_min"]), round(r["length_km"])),
                  fill=(20, 20, 20), font=font)
    draw.rectangle((width - 222, height - 22, width, height), fill=(255, 255, 255))
    draw.text((width - 216, height - 19), "© OpenStreetMap contributors",
              fill=(60, 60, 60), font=small)
    img.save(out_path, "PNG")
    return out_path


def _hm(minutes):
    m = int(round(minutes))
    return "%d:%02d h" % (m // 60, m % 60)


EMOJI = ("🔵", "🟠", "🟢", "🟣", "🔴")
DEFECT_WORDS = {"potholes": "bedres", "cracks": "plaisas", "patches": "ielāpi",
                "ruts": "rises", "gravel": "grants", "roadworks": "ceļa darbi",
                "narrow": "šaurs ceļš", "no_shoulder": "nav nomales"}
PICK_WORDS = (("fastest", "ātrākais"), ("toll_free", "bez nodevām"),
              ("scenic", "skaistākais"), ("best_roads", "labākie ceļi"))


def _tolls_text(f, has_ferry):
    parts = []
    for cc, km in sorted((f.get("toll_km_by_country") or {}).items(), key=lambda kv: -kv[1]):
        if km < 0.5:
            continue
        parts.append("vajag vinjeti (%s)" % cc if cc in VIGNETTE
                     else "maksas ceļš %s %d km" % (cc, round(km)))
    text = ", ".join(parts) if parts else "bez nodevām"
    return text + (", ar prāmi" if has_ferry else "")


def _road_label(st):
    """'V1192 Apriķi-Cīrava-Medze': the road number first, then the name.
    OSM's Latvian road names carry long dashes ("Apriķi — Cīrava — Medze");
    shown as plain hyphens."""
    names = [str(n) for n in (st.get("names") or []) if str(n).strip()]
    refs = [n for n in names if n[:1].isalpha() and n[1:2].isdigit() or n[:2].isalpha()
            and n[2:3].isdigit() and len(n) <= 8]
    words = [n for n in names if n not in refs]
    label = " ".join(refs[:1] + [w.replace(" — ", "-").replace("—", "-")
                                 .replace(" – ", "-") for w in words[:1]])
    return label or str(st.get("road_class") or "ceļš")


def _km(value):
    """Latvian writes a decimal comma: 16,9 km."""
    return ("%.1f" % value).replace(".", ",")


def _surface_text(f):
    if f.get("rough_km", 0) < 1.0:
        return "Segums visur asfalts"
    text = "Negluds segums %d km" % round(f["rough_km"])
    if f.get("unpaved_km", 0) >= 1.0:
        text += ", no tiem grants vai zeme %d km" % round(f["unpaved_km"])
    worst = (f.get("worst_stretches") or [])[:1]
    if worst:
        st = worst[0]
        text += " (garākais: %s, %d. km, %s km)" % (_road_label(st), round(st["start_km"]),
                                                    _km(st["length_km"]))
    return text


def _around_text(around):
    sh = (around or {}).get("shares") or {}
    if not sh:
        return "Apkārtne: nav datu"
    words = []
    if sh.get("forest", 0) >= 0.4:
        words.append("meži")
    if sh.get("fields", 0) >= 0.4:
        words.append("lauki")
    if sh.get("water", 0) >= 0.25:
        names = (around.get("water_names") or [])[:2]
        words.append("ūdeņi" + (" (%s)" % ", ".join(names) if names else ""))
    if sh.get("protected", 0) >= 0.15:
        names = (around.get("protected_names") or [])[:1]
        words.append("dabas teritorijas" + (" (%s)" % names[0] if names else ""))
    if sh.get("industry", 0) >= 0.3:
        words.append("rūpniecība")
    if sh.get("town", 0) >= 0.5:
        words.append("daudz apdzīvotu vietu")
    return "Gar ceļu: " + (", ".join(words) if words else "nekā izteikta")


def _photos_text(photos):
    seen = [p for p in photos if p.get("photo") and p.get("is_road_photo")
            and p.get("surface") in PHOTO_WORDS]
    if not seen:
        return "Foto: nav"
    years = sorted({(p.get("captured") or "")[:4] for p in seen} - {""})
    when = ("-".join((years[0], years[-1])) if len(years) > 1 else (years[0] if years else "?"))
    counts = {}
    for p in seen:
        w = PHOTO_WORDS[p["surface"]]
        counts[w] = counts.get(w, 0) + 1
    text = "Foto %s (%d): segums %s" % (when, len(seen), ", ".join(
        "%s %d" % (w, n) if len(counts) > 1 else w for w, n in counts.items()))
    notes = []
    for p in seen:
        found = [DEFECT_WORDS[d] for d in p.get("defects") or () if d in DEFECT_WORDS]
        if found:
            notes.append("%s pie %d. km" % (", ".join(found), p["km"]))
    if notes:
        text += "; " + "; ".join(notes[:3])
    return text


def format_report(result):
    routes = result["routes"]
    by_letter = {r["letter"]: r for r in routes}
    picks = result["picks"]
    fastest = by_letter[picks["fastest"]]
    lines = ["🗺️ %s → %s: %d varianti (laiki bez sastrēgumiem)" % (
        result["origin_city"] or "starts", result["destination_name"], len(routes))]
    best = picks["overall"]
    why = [word for key, word in PICK_WORDS if picks.get(key) == best]
    lines.append("Ieteicu %s%s." % (best, (": " + ", ".join(why)) if why else ""))
    lines.append("")
    for i, r in enumerate(routes):
        f = r["facts"]
        extra = ""
        if r is not fastest:
            extra = " (+%s)" % _hm(r["time_min"] - fastest["time_min"]).replace(" h", "")
        lines.append("%s %s: %s%s, %d km, %s. Šoseja %d%%. %s. %s. %s." % (
            EMOJI[i], r["letter"], _hm(r["time_min"]), extra, round(r["length_km"]),
            _tolls_text(f, r["has_ferry"]), round(100 * f.get("motorway_share", 0)),
            _surface_text(f), _around_text(r.get("around")), _photos_text(r["photos"])))
    lines.append("")
    named = ["%s %s" % (word.capitalize(), picks[key]) for key, word in PICK_WORDS if picks.get(key)]
    if picks.get("toll_free") is None:
        named.append("bez nodevām nav neviena")
    lines.append(". ".join(named) + ".")
    if picks.get("avoid") and picks["avoid"] != best:
        lines.append("Mazāk ieteicams: %s." % picks["avoid"])
    return "\n".join(lines)


def write_report(result):
    return format_report(result)


def links_block(result):
    names = {"fastest": "Ātrākais", "toll_free": "Bez nodevām", "scenic": "Skaistākais",
             "best_roads": "Labākie ceļi", "overall": "Ieteiktais"}
    lines, done = [], set()
    by_letter = {r["letter"]: r for r in result["routes"]}
    for key in ("overall", "fastest", "toll_free", "scenic", "best_roads"):
        letter = result["picks"].get(key)
        if not letter or letter in done:
            continue
        done.add(letter)
        lines.append("%s (%s): %s" % (names[key], letter, by_letter[letter]["google_url"]))
    lines.append("Waze (tas izvēlas pats savu ātrāko): %s" % result["waze_url"])
    return "\n".join(lines)


def resolve_origin(origin_text="", may_use_his_location=True):
    if origin_text:
        found = geocode(origin_text)
        if not found:
            return None, "", "", "origin_not_found"
        return (found["lat"], found["lon"]), found["city"], found["country_code"], "named"
    if not may_use_his_location:
        return None, "", "", "origin_required"
    from app import reminders
    entry = reminders.fresh_location()
    if not entry:
        return None, "", "", "location_unknown"
    code = reminders.country_code_at(entry["lat"], entry["lon"]).upper()
    return (float(entry["lat"]), float(entry["lon"])), entry.get("city") or "", code, "find_my"


def plan(origin_point, origin_city, origin_cc, dest, vias=(), deadline=None,
         progress=None):
    """Everything but sending. `dest` is a geocode() result, `vias` points."""
    t0 = time.time()
    deadline = deadline or (t0 + 900)

    def note(msg):
        LOGGER.info("route plan: %s (%.0fs)", msg, time.time() - t0)
        if progress:
            progress(msg)

    routes = candidates(origin_point, (dest["lat"], dest["lon"]), vias)
    if not routes:
        raise RuntimeError("no route found between the two places")
    note("%d distinct routes" % len(routes))
    for i, r in enumerate(routes):
        r["letter"] = LETTERS[i]
        r["color"] = COLORS[i]
        r["color_name"] = ("zils", "oranžs", "zaļš", "violets", "sarkans")[i]
        r["facts"] = road_facts(r)
    note("road facts done")
    arounds = _safe(surroundings_all, routes) or [{} for _r in routes]
    for r, a in zip(routes, arounds):
        r["around"] = a or {}
    note("surroundings done")
    cache = {}
    for r in routes:
        if time.time() > deadline - 90:
            r["photos"] = []
            continue
        r["photos"] = inspect_photos(photo_points(r, r["facts"]), cache)
    note("photos done (%d looked at)" % len([v for v in cache.values() if v.get("photo")]))
    for r in routes:
        r["road_score"] = road_score(r["facts"], r["photos"])
        r["scenery_score"] = scenery_score(r["facts"], r["around"], r["photos"])
    picks = pick(routes)
    fastest = next(r for r in routes if r["letter"] == picks["fastest"])
    dest_pt = (dest["lat"], dest["lon"])
    for r in routes:
        wps = [] if r is fastest else _deviation_waypoints(r, fastest)
        r["google_url"] = google_maps_url(dest_pt, wps)
    return {"origin_city": origin_city, "origin_country": origin_cc,
            "destination_name": dest.get("city") or dest.get("name"),
            "destination": dest_pt, "routes": routes, "picks": picks,
            "waze_url": waze_url(dest_pt), "seconds": round(time.time() - t0)}


def _safe(fn, *args):
    try:
        return fn(*args)
    except Exception as exc:
        LOGGER.info("route plan step %s failed: %s", getattr(fn, "__name__", fn), str(exc)[:160])
        return None


def busy():
    with _job_lock:
        t = _job["thread"]
        return bool(t and t.is_alive())


ORIGIN_MESSAGES = {
    "location_unknown": ("his current location is unknown or stale (Find My has no fresh "
                         "reading); ask where he starts from and pass it as origin"),
    "origin_required": ("ask where the drive starts from and pass it as origin"),
    "origin_not_found": "could not find the starting place; ask for a fuller name",
}


def start(chat_guid, destination, origin_text="", via=(), sender=None,
          may_use_his_location=True):
    """Check what can be checked at once, then run the rest in the background
    and send the result to `chat_guid`. `sender(chat_guid, text, image_path)`."""
    if busy():
        return {"ok": False, "error_type": "busy",
                "message": "a route check to %s is already running; its result comes first"
                           % _job["destination"]}
    dest = geocode(destination)
    if not dest:
        return {"ok": False, "error_type": "destination_not_found",
                "message": "could not find that place; ask for a fuller name (city, country)"}
    vias = []
    for place in list(via or ())[:3]:
        found = geocode(place)
        if not found:
            return {"ok": False, "error_type": "via_not_found",
                    "message": "could not find '%s' to drive through" % str(place)[:60]}
        vias.append((found["lat"], found["lon"]))
    origin, city, cc, source = resolve_origin(origin_text, may_use_his_location)
    if not origin:
        return {"ok": False, "error_type": source, "message": ORIGIN_MESSAGES[source]}
    straight = haversine_km(origin, (dest["lat"], dest["lon"]))
    eta = int(min(9, 3 + straight / 250.0))

    def run():
        try:
            result = plan(origin, city, cc, dest, vias=vias)
            text = write_report(result) + "\n\n" + links_block(result)
            image = None
            try:
                start_pt = city_center(city, cc) if city else None
                image = render_map(result["routes"], start_pt, result["destination"],
                                   os.path.join(tempfile.gettempdir(),
                                                "marsruti-%d.png" % int(time.time())))
            except Exception as exc:
                LOGGER.info("route map failed: %s", str(exc)[:160])
            if sender:
                sender(chat_guid, text, image)
        except Exception as exc:
            LOGGER.warning("route plan failed: %s", str(exc)[:200])
            if sender:
                sender(chat_guid, "Maršrutu pārbaude uz %s nesanāca: %s"
                       % (dest.get("city") or destination, str(exc)[:160]), None)

    with _job_lock:
        thread = threading.Thread(target=run, name="route_plan", daemon=True)
        _job.update(thread=thread, destination=dest.get("city") or destination,
                    started=time.time())
        thread.start()
    return {"ok": True, "started": True, "action_state": "EXECUTED",
            "origin": city or "(nosauktā vieta)", "origin_source": source,
            "origin_resolved": ("%s, %s" % (city, cc)) if source == "named" else city,
            "destination": dest.get("city") or dest.get("name"),
            "destination_country": dest.get("country_code"),
            "destination_resolved": dest.get("name"),
            "eta_minutes": eta,
            "message": ("route check started; the result (routes, road quality from the map "
                        "and street photos, a map image and links) will be sent to this chat "
                        "in about %d minutes" % eta)}


def send_to_chat(chat_guid, text, image_path=None):
    """The job's result into the chat: the text, then the map."""
    from app import messaging
    acfg = messaging.load_config().get("auto_reply") or {}
    provider = str(acfg.get("provider") or "bluebubbles_http")
    messaging.send_message(provider=provider, target=chat_guid, text=text,
                           metadata={"auto_reply": True, "mode": "route_plan"},
                           real_send=True)
    if image_path and os.path.exists(image_path):
        pcfg = (messaging.load_config().get("providers") or {}).get(provider) or {}
        try:
            messaging._bb_send_attachment(pcfg, chat_guid, image_path, name="marsruti.png",
                                          transcript="[maršrutu karte]", mode="image")
            messaging._append_jsonl(messaging.OUTBOX_PATH, messaging._entry(
                provider, chat_guid, "[marsruti.png]", "sent", "",
                {"auto_reply": True, "mode": "image"}))
        except Exception as exc:
            LOGGER.info("route map send failed: %s", str(exc)[:160])
        finally:
            try:
                os.remove(image_path)
            except Exception:
                pass
