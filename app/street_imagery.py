# -*- coding: utf-8 -*-
"""Street-level imagery: what a place actually looks like.

The map knows what is supposed to be at an address. This knows what it looks
like when you stand there -- whether the entrance is obvious, whether cars
park on the pavement, whether the junction has lights. Two public providers
answer that, and neither is reliable enough to depend on alone, so both are
normalised into one record shape and a failure of either is survivable.

  KartaView (api.kartaview.org 2.0). No token at all. /photo/ takes
  lat/lng/radius and returns rows carrying distance, heading, sequenceId,
  sequenceIndex, shotDate and four image sizes; radius over ~500 m is a 400.
  /sequence/{id}/photos returns the whole drive, which is how "look further
  along the road" works. Note the trailing path: /2.0/sequence/{id}/photos
  answers, /2.0/sequence/{id}/photos/ is a 404.

Riga has coverage in both, but it is thin and old -- the frames around the
centre are from 2016 and 2017. That is the reason captured_at is carried
through every layer and into the tool result: an answer built on a 2016
photograph is still useful, but only if the reader is told it is 2016.

Both providers' imagery is CC-BY-SA. ATTRIBUTION is returned with results so
a reply that leans on them can say where the picture came from.
"""

import json
import logging
import math
import os
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

LOGGER = logging.getLogger(__name__)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_DIR = os.path.join(PROJECT_ROOT, ".ai_memory")
DB_PATH = os.path.join(DB_DIR, "street_imagery.sqlite3")

USER_AGENT = "rig-assistant/1.0 (local personal assistant)"

# Mapillary's own radius search stops at 50 m and its bounding box must stay
# under 0.01 degrees square; 0.005 degrees of latitude is ~555 m, so this is
# the widest circle both providers can actually serve.
MAX_RADIUS_M = 500
DEFAULT_RADIUS_M = 75

# Frames on a drive are metres apart and nearly identical. Two images closer
# than this AND pointing the same way are the same picture for our purposes.
MIN_SEPARATION_M = 12.0
MIN_HEADING_DIFF = 40.0

ATTRIBUTION = {
    "mapillary": "Imagery (c) Mapillary contributors, CC BY-SA 4.0",
    "kartaview": "Imagery (c) KartaView contributors, CC BY-SA 4.0",
    "local": "Local imagery",
}

_LOCK = threading.RLock()
_TABLES_READY = [False]


def _env_int(name, default, low, high):
    try:
        return max(low, min(high, int(str(os.environ.get(name, "")).strip())))
    except Exception:
        return default


def timeout_seconds():
    return _env_int("RIG_STREET_IMAGERY_TIMEOUT", 8, 2, 30)


def mapillary_token():
    """The token, from the environment only.

    Never returned in a tool result, never logged, never handed to the model:
    only the provider client below ever reads it.
    """
    return str(os.environ.get("RIG_MAPILLARY_TOKEN", "") or "").strip()


def configured_providers():
    """Which providers can actually be called right now."""
    names = ["kartaview"]                 # needs no credentials
    if mapillary_token():
        names.insert(0, "mapillary")      # better coverage when available
    return names


def haversine_m(lat1, lon1, lat2, lon2):
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = (math.sin(dp / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2)
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def bearing_deg(lat1, lon1, lat2, lon2):
    """Compass bearing from point 1 to point 2."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def angle_gap(a, b):
    """Smallest angle between two compass headings, 0-180."""
    if a is None or b is None:
        return None
    return abs((float(a) - float(b) + 180.0) % 360.0 - 180.0)


def valid_coords(lat, lon):
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return None
    if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
        return None
    # 0,0 is in the Atlantic and is what an unset variable looks like.
    if abs(lat) < 1e-9 and abs(lon) < 1e-9:
        return None
    return lat, lon


def _bbox(lat, lon, radius_m):
    dlat = radius_m / 111320.0
    dlon = radius_m / max(1.0, 111320.0 * math.cos(math.radians(lat)))
    return lon - dlon, lat - dlat, lon + dlon, lat + dlat


def _iso_from_epoch_ms(value):
    try:
        secs = float(value) / 1000.0
        if secs <= 0:
            return ""
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(secs))
    except (TypeError, ValueError):
        return ""


def _iso_from_text(value):
    text = str(value or "").strip()
    if not text:
        return ""
    text = text.split(".")[0].replace(" ", "T")
    return text + "Z" if len(text) == 19 else text


def _float_or_none(value):
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(out) or math.isinf(out) else out


def make_record(provider, image_id, lat, lon, heading=None, captured_at="",
                sequence_id="", sequence_index=None, thumbnail="",
                image_reference="", is_panorama=False, origin_lat=None,
                origin_lon=None, distance_m=None):
    """One image, in the shape every provider is flattened into.

    provider + image_id is the identity, and it is the cache key: the same
    photograph from two providers is two records, which is correct, because
    their metadata and licences differ.
    """
    coords = valid_coords(lat, lon)
    if not coords:
        return None
    lat, lon = coords
    if distance_m is None and origin_lat is not None and origin_lon is not None:
        distance_m = haversine_m(origin_lat, origin_lon, lat, lon)
    return {
        "id": str(image_id),
        "provider": provider,
        "latitude": round(lat, 7),
        "longitude": round(lon, 7),
        "distance_m": None if distance_m is None else round(float(distance_m), 1),
        "heading": None if heading is None else round(float(heading) % 360.0, 1),
        "captured_at": captured_at or "",
        "sequence_id": str(sequence_id or ""),
        "sequence_index": sequence_index,
        "is_panorama": bool(is_panorama),
        "thumbnail": thumbnail or "",
        "image_reference": image_reference or thumbnail or "",
        "attribution": ATTRIBUTION.get(provider, provider),
    }


class ProviderError(Exception):
    """A provider could not answer. Never escapes to the model as an exception."""


def _get_json(url, timeout=None, headers=None):
    req = urllib.request.Request(
        url, headers=dict({"User-Agent": USER_AGENT}, **(headers or {})))
    try:
        with urllib.request.urlopen(req, timeout=timeout or timeout_seconds()) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        # 429 and 401 are worth telling apart: one is temporary, the other is
        # a misconfiguration that will never fix itself.
        if exc.code == 429:
            raise ProviderError("rate_limited") from exc
        if exc.code in (401, 403):
            raise ProviderError("auth_failed") from exc
        raise ProviderError("http_%d" % exc.code) from exc
    except Exception as exc:
        raise ProviderError(type(exc).__name__.lower()) from exc
    try:
        return json.loads(raw)
    except ValueError as exc:
        # An API that answers with an HTML error page must not become context.
        raise ProviderError("malformed_response") from exc


class StreetImageryProvider(object):
    """Common shape. Each method returns normalised records or raises ProviderError."""

    name = ""

    def available(self):
        return True

    def search_nearby(self, lat, lon, radius_m, limit):
        raise NotImplementedError

    def get_sequence(self, record, before, after):
        raise NotImplementedError


class MapillaryProvider(StreetImageryProvider):
    """graph.mapillary.com, API v4. Needs a token; degrades to unavailable."""

    name = "mapillary"
    ROOT = "https://graph.mapillary.com"
    FIELDS = ("id,computed_geometry,geometry,compass_angle,captured_at,"
              "sequence,is_pano,thumb_256_url,thumb_1024_url")
    RADIUS_SEARCH_MAX_M = 50      # documented ceiling for lat/lng/radius

    def available(self):
        return bool(mapillary_token())

    def _call(self, path, params):
        token = mapillary_token()
        if not token:
            raise ProviderError("not_configured")
        url = "%s/%s?%s" % (self.ROOT, path.lstrip("/"),
                            urllib.parse.urlencode(params))
        # The token goes in the header, not the query string, so it cannot
        # end up in a logged or cached URL.
        return _get_json(url, headers={"Authorization": "OAuth %s" % token})

    def _to_record(self, item, lat, lon):
        geo = item.get("computed_geometry") or item.get("geometry") or {}
        coords = (geo.get("coordinates") or [None, None])
        seq = item.get("sequence")
        return make_record(
            self.name, item.get("id"),
            lat=coords[1], lon=coords[0],
            heading=_float_or_none(item.get("compass_angle")),
            captured_at=_iso_from_epoch_ms(item.get("captured_at")),
            sequence_id=seq if isinstance(seq, str) else (seq or {}).get("id", ""),
            thumbnail=item.get("thumb_256_url") or "",
            image_reference=item.get("thumb_1024_url") or item.get("thumb_256_url") or "",
            is_panorama=bool(item.get("is_pano")),
            origin_lat=lat, origin_lon=lon)

    def search_nearby(self, lat, lon, radius_m, limit):
        if radius_m <= self.RADIUS_SEARCH_MAX_M:
            params = {"fields": self.FIELDS, "lat": lat, "lng": lon,
                      "radius": int(radius_m), "limit": int(limit)}
        else:
            # Wider than the point search allows, so use a bounding box --
            # which the API requires to stay under 0.01 degrees square.
            west, south, east, north = _bbox(lat, lon, radius_m)
            params = {"fields": self.FIELDS,
                      "bbox": "%f,%f,%f,%f" % (west, south, east, north),
                      "limit": int(limit)}
        data = self._call("images", params)
        out = []
        for item in (data.get("data") or []):
            rec = self._to_record(item, lat, lon)
            if rec:
                out.append(rec)
        return out

    def get_sequence(self, record, before, after):
        seq = record.get("sequence_id")
        if not seq:
            raise ProviderError("no_sequence")
        data = self._call("images", {"fields": self.FIELDS,
                                     "sequence_ids": seq,
                                     "limit": 200})
        rows = []
        for item in (data.get("data") or []):
            rec = self._to_record(item, record["latitude"], record["longitude"])
            if rec:
                rows.append(rec)
        # Mapillary has no index within a sequence, so order by capture time
        # and cut the window around the anchor frame.
        rows.sort(key=lambda r: r.get("captured_at") or "")
        return _window_around(rows, record, before, after)


class KartaViewProvider(StreetImageryProvider):
    """api.kartaview.org 2.0. Keyless, and the default provider here."""

    name = "kartaview"
    ROOT = "https://api.kartaview.org/2.0"
    MAX_RADIUS_M = 500            # 999 answers 400

    def _to_record(self, item, lat=None, lon=None):
        return make_record(
            self.name, item.get("id"),
            lat=_float_or_none(item.get("lat")),
            lon=_float_or_none(item.get("lng")),
            heading=_float_or_none(item.get("heading")),
            captured_at=_iso_from_text(item.get("shotDate")
                                       or item.get("dateAdded")),
            sequence_id=item.get("sequenceId") or "",
            sequence_index=_float_or_none(item.get("sequenceIndex")),
            thumbnail=item.get("fileurlTh") or "",
            image_reference=item.get("fileurlLTh") or item.get("fileurlTh") or "",
            is_panorama=str(item.get("projection") or "").upper() != "PLANE",
            origin_lat=lat, origin_lon=lon,
            distance_m=_float_or_none(item.get("distance")))

    def search_nearby(self, lat, lon, radius_m, limit):
        params = {"lat": lat, "lng": lon,
                  "radius": int(min(radius_m, self.MAX_RADIUS_M)),
                  "itemsPerPage": int(limit)}
        data = _get_json("%s/photo/?%s" % (self.ROOT, urllib.parse.urlencode(params)))
        rows = ((data.get("result") or {}).get("data")) or []
        out = []
        for item in rows:
            rec = self._to_record(item, lat, lon)
            if rec:
                out.append(rec)
        return out

    PAGE_SIZE = 100

    def get_sequence(self, record, before, after):
        seq = record.get("sequence_id")
        if not seq:
            raise ProviderError("no_sequence")
        # Trailing slash here is a 404; the path without it answers.
        base = "%s/sequence/%s/photos" % (self.ROOT, urllib.parse.quote(str(seq)))

        index = record.get("sequence_index")
        pages = [None]
        if index is not None:
            start = max(0, int(index) - max(0, int(before)))
            end = int(index) + max(0, int(after))
            first_page = start // self.PAGE_SIZE + 1
            last_page = end // self.PAGE_SIZE + 1
            # At most two, so a window straddling a page boundary still works
            # without walking the whole drive.
            pages = [first_page] if first_page == last_page else [first_page, last_page]

        rows = []
        for page in pages:
            url = base
            if page is not None:
                url = "%s?%s" % (base, urllib.parse.urlencode(
                    {"itemsPerPage": self.PAGE_SIZE, "page": page}))
            data = _get_json(url)
            rows.extend(((data.get("result") or {}).get("data")) or [])

        recs = [r for r in (self._to_record(i) for i in rows) if r]
        seen, unique = set(), []
        for rec in recs:
            if rec["id"] not in seen:
                seen.add(rec["id"])
                unique.append(rec)
        unique.sort(key=lambda r: (r.get("sequence_index") is None,
                                   r.get("sequence_index") or 0))
        return _window_around(unique, record, before, after)


PROVIDER_CLASSES = {"mapillary": MapillaryProvider, "kartaview": KartaViewProvider}


def get_provider(name):
    cls = PROVIDER_CLASSES.get(str(name or "").strip().lower())
    return cls() if cls else None


def _window_around(rows, anchor, before, after):
    """The frames either side of one image, so the model can look up the road.

    The anchor is usually in the list and is found by id. It is not always:
    KartaView returns the first hundred frames of a drive, and a drive can be
    longer than that, so an image from late in one comes back absent from its
    own sequence page. Falling back to the middle of the list was wrong in a
    way that did not look wrong -- it returned five real frames from the same
    real drive, taken somewhere else entirely, possibly a kilometre away.

    So when the id is missing the pivot is the geometrically nearest frame to
    the anchor instead, which is the frame the caller meant.
    """
    if not rows:
        return []
    anchor_id = str(anchor.get("id") if isinstance(anchor, dict) else anchor)
    ids = [r["id"] for r in rows]
    try:
        pivot = ids.index(anchor_id)
    except ValueError:
        pivot = 0
        if isinstance(anchor, dict) and anchor.get("latitude") is not None:
            gaps = [haversine_m(anchor["latitude"], anchor["longitude"],
                                r["latitude"], r["longitude"]) for r in rows]
            pivot = gaps.index(min(gaps))
        else:
            pivot = len(rows) // 2
    start = max(0, pivot - max(0, int(before)))
    end = min(len(rows), pivot + max(0, int(after)) + 1)
    window = rows[start:end]
    if isinstance(anchor, dict) and anchor.get("latitude") is not None:
        for rec in window:
            rec["distance_m"] = round(haversine_m(
                anchor["latitude"], anchor["longitude"],
                rec["latitude"], rec["longitude"]), 1)
    return window


def _connect():
    os.makedirs(DB_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _ensure_tables(conn):
    if _TABLES_READY[0]:
        return
    conn.execute("""
        CREATE TABLE IF NOT EXISTS images (
            provider        TEXT NOT NULL,
            image_id        TEXT NOT NULL,
            latitude        REAL NOT NULL,
            longitude       REAL NOT NULL,
            heading         REAL,
            captured_at     TEXT,
            sequence_id     TEXT,
            sequence_index  REAL,
            thumbnail       TEXT,
            image_reference TEXT,
            is_panorama     INTEGER DEFAULT 0,
            description     TEXT,
            embedding       BLOB,
            first_seen      REAL,
            last_access     REAL,
            PRIMARY KEY (provider, image_id)
        )""")
    # Nearby lookup is a bounding-box scan over these two columns. At this
    # size that is the whole spatial story -- no geohash, no R*Tree. If the
    # cache ever becomes a Latvia-wide dataset, this is the seam to change.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_images_latlon "
                 "ON images(latitude, longitude)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_images_sequence "
                 "ON images(provider, sequence_id)")
    conn.commit()
    _TABLES_READY[0] = True


def cache_put(records):
    """Remember what was seen. Accumulates through use; nothing is pre-fetched."""
    rows = [r for r in (records or []) if r and r.get("id")]
    if not rows:
        return 0
    now = time.time()
    with _LOCK:
        conn = _connect()
        try:
            _ensure_tables(conn)
            for rec in rows:
                conn.execute("""
                    INSERT INTO images (provider, image_id, latitude, longitude,
                        heading, captured_at, sequence_id, sequence_index,
                        thumbnail, image_reference, is_panorama, first_seen,
                        last_access)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(provider, image_id) DO UPDATE SET
                        latitude=excluded.latitude,
                        longitude=excluded.longitude,
                        heading=excluded.heading,
                        captured_at=excluded.captured_at,
                        sequence_id=excluded.sequence_id,
                        sequence_index=excluded.sequence_index,
                        thumbnail=excluded.thumbnail,
                        image_reference=excluded.image_reference,
                        last_access=excluded.last_access
                    """, (rec["provider"], rec["id"], rec["latitude"],
                          rec["longitude"], rec.get("heading"),
                          rec.get("captured_at") or "",
                          rec.get("sequence_id") or "",
                          rec.get("sequence_index"),
                          rec.get("thumbnail") or "",
                          rec.get("image_reference") or "",
                          1 if rec.get("is_panorama") else 0, now, now))
            conn.commit()
        finally:
            conn.close()
    return len(rows)


def cache_nearby(lat, lon, radius_m, limit, providers=None):
    """Cached images near a point, without scanning the table."""
    west, south, east, north = _bbox(lat, lon, radius_m)
    with _LOCK:
        conn = _connect()
        try:
            _ensure_tables(conn)
            sql = ("SELECT * FROM images WHERE latitude BETWEEN ? AND ? "
                   "AND longitude BETWEEN ? AND ?")
            args = [south, north, west, east]
            if providers:
                sql += " AND provider IN (%s)" % ",".join("?" * len(providers))
                args.extend(providers)
            rows = conn.execute(sql, args).fetchall()
        finally:
            conn.close()
    out = []
    for row in rows:
        rec = make_record(row["provider"], row["image_id"], row["latitude"],
                          row["longitude"], heading=row["heading"],
                          captured_at=row["captured_at"],
                          sequence_id=row["sequence_id"],
                          sequence_index=row["sequence_index"],
                          thumbnail=row["thumbnail"],
                          image_reference=row["image_reference"],
                          is_panorama=bool(row["is_panorama"]),
                          origin_lat=lat, origin_lon=lon)
        if rec and (rec["distance_m"] is None or rec["distance_m"] <= radius_m):
            rec["cached"] = True
            if row["description"]:
                rec["cached_description"] = row["description"]
            out.append(rec)
    out.sort(key=lambda r: r.get("distance_m") if r.get("distance_m") is not None else 1e9)
    return out[:limit]


def cache_describe(provider, image_id, description):
    """Keep a visual description next to the image it describes."""
    if not description:
        return
    with _LOCK:
        conn = _connect()
        try:
            _ensure_tables(conn)
            conn.execute("UPDATE images SET description=?, last_access=? "
                         "WHERE provider=? AND image_id=?",
                         (str(description)[:4000], time.time(),
                          provider, str(image_id)))
            conn.commit()
        finally:
            conn.close()


def cache_stats():
    with _LOCK:
        conn = _connect()
        try:
            _ensure_tables(conn)
            total = conn.execute("SELECT COUNT(*) AS n FROM images").fetchone()["n"]
            described = conn.execute(
                "SELECT COUNT(*) AS n FROM images WHERE description IS NOT NULL "
                "AND description != ''").fetchone()["n"]
            by_provider = {r["provider"]: r["n"] for r in conn.execute(
                "SELECT provider, COUNT(*) AS n FROM images GROUP BY provider")}
        finally:
            conn.close()
    return {"images": total, "described": described, "by_provider": by_provider,
            "database": DB_PATH}


def deduplicate(records, min_separation_m=MIN_SEPARATION_M,
                min_heading_diff=MIN_HEADING_DIFF):
    """Drop frames that show the same thing as one already kept.

    A drive produces an image every few metres. Five frames two metres apart
    down the same street are one answer, not five, and they cost five VLM
    calls. A frame survives if it is far enough from everything kept, or if
    it points somewhere meaningfully different from the near ones.
    """
    kept = []
    for rec in records:
        duplicate = False
        for other in kept:
            gap_m = haversine_m(rec["latitude"], rec["longitude"],
                                other["latitude"], other["longitude"])
            if gap_m > min_separation_m:
                continue
            turn = angle_gap(rec.get("heading"), other.get("heading"))
            if turn is None or turn < min_heading_diff:
                duplicate = True
                break
        if not duplicate:
            kept.append(rec)
    return kept


def _recency_key(rec):
    return rec.get("captured_at") or ""


def select_best(records, lat, lon, max_images, prefer_facing=True,
                radius_m=DEFAULT_RADIUS_M):
    """A small, useful spread: close, recent, and pointing at the place.

    Sorting purely by distance returns the same doorway five times from five
    metres apart. Sorting purely by recency returns the newest drive, which
    may have gone the other way down the street. The score below mixes the
    three, then deduplicate() enforces the spread.

    Distance is scored as a FRACTION OF THE RADIUS, not in metres, and that
    detail is the whole behaviour. Scored in metres it swamps everything else
    -- over a 500 m search the distance term reaches 500 while age tops out
    around 30, so the pick is the nearest frames and nothing but, which on a
    street photographed since 2015 means the oldest photographs of it. Scaled
    against the radius, all three terms land in the same range: distance still
    decides between a doorway and one 400 m up the road, and among frames that
    are all close by, recency and where the camera points decide instead.
    """
    span = max(1.0, float(radius_m or DEFAULT_RADIUS_M))
    scored = []
    for rec in records:
        dist = rec.get("distance_m")
        if dist is None:
            dist = haversine_m(lat, lon, rec["latitude"], rec["longitude"])
        score = min(float(dist) / span, 2.0) * 50.0
        if prefer_facing and rec.get("heading") is not None:
            to_target = bearing_deg(rec["latitude"], rec["longitude"], lat, lon)
            off = angle_gap(rec["heading"], to_target) or 0.0
            # A frame facing away is not useless -- it shows the other side of
            # the street -- so this is a penalty, not a filter.
            score += (off / 180.0) * 40.0
        year = (rec.get("captured_at") or "")[:4]
        if year.isdigit():
            # Capped, so a 2011 frame that is the only coverage still wins
            # over nothing at all.
            score += min(max(0, 2026 - int(year)), 12) * 3.0
        scored.append((score, rec))
    scored.sort(key=lambda pair: pair[0])
    return deduplicate([rec for _, rec in scored])[:max_images]


def search_nearby(latitude, longitude, radius_m=DEFAULT_RADIUS_M, limit=8,
                  providers=None, use_cache=True):
    """Ask every configured provider at once and merge what comes back.

    Provider failures are isolated by construction: each returns either rows
    or a reason, and the reasons travel beside the results instead of
    replacing them. Mapillary being rate limited is not an error the user
    should ever see if KartaView answered.
    """
    coords = valid_coords(latitude, longitude)
    if not coords:
        return {"ok": False, "error": "invalid_coordinates",
                "message": "latitude/longitude are not a real position",
                "images": [], "providers": {}}
    lat, lon = coords
    radius_m = max(5, min(int(radius_m or DEFAULT_RADIUS_M), MAX_RADIUS_M))
    limit = max(1, min(int(limit or 8), 50))
    names = [n for n in (providers or configured_providers())
             if n in PROVIDER_CLASSES]

    results, status = [], {}
    started = time.time()

    def _one(name):
        provider = get_provider(name)
        if provider is None or not provider.available():
            return name, None, "not_configured"
        t0 = time.time()
        try:
            rows = provider.search_nearby(lat, lon, radius_m, limit)
            return name, rows, "ok:%.2f" % (time.time() - t0)
        except ProviderError as exc:
            return name, None, str(exc)
        except Exception as exc:                       # never kill the request
            LOGGER.warning("street_imagery provider=%s unexpected=%s",
                           name, type(exc).__name__)
            return name, None, "provider_error"

    if names:
        with ThreadPoolExecutor(max_workers=max(1, len(names))) as pool:
            for name, rows, note in pool.map(_one, names):
                if rows:
                    results.extend(rows)
                    status[name] = {"ok": True, "results": len(rows)}
                else:
                    reason = note.split(":")[0]
                    status[name] = {"ok": reason == "ok", "results": 0,
                                    "error": None if reason == "ok" else reason}

    cache_hits = 0
    if use_cache:
        known = {(r["provider"], r["id"]) for r in results}
        for rec in cache_nearby(lat, lon, radius_m, limit, names or None):
            if (rec["provider"], rec["id"]) not in known:
                results.append(rec)
                cache_hits += 1

    if results:
        cache_put([r for r in results if not r.get("cached")])

    results.sort(key=lambda r: r.get("distance_m")
                 if r.get("distance_m") is not None else 1e9)
    LOGGER.info("tool=street_imagery action=search_nearby lat=%.5f lon=%.5f "
                "radius=%d results=%d cache_hits=%d api_latency=%.2f providers=%s",
                lat, lon, radius_m, len(results), cache_hits,
                time.time() - started,
                ",".join("%s=%s" % (k, "ok" if v.get("ok") else v.get("error"))
                         for k, v in sorted(status.items())))
    return {"ok": bool(results), "location": {"latitude": lat, "longitude": lon},
            "radius_m": radius_m, "images": results[:limit],
            "providers": status, "cache_hits": cache_hits,
            "attribution": sorted({r["attribution"] for r in results[:limit]})}


def get_sequence(image_id, provider=None, before=2, after=2):
    """Frames either side of one image, i.e. further along the same road."""
    record = None
    with _LOCK:
        conn = _connect()
        try:
            _ensure_tables(conn)
            sql = "SELECT * FROM images WHERE image_id=?"
            args = [str(image_id)]
            if provider:
                sql += " AND provider=?"
                args.append(provider)
            row = conn.execute(sql, args).fetchone()
        finally:
            conn.close()
    if row:
        record = make_record(row["provider"], row["image_id"], row["latitude"],
                             row["longitude"], heading=row["heading"],
                             captured_at=row["captured_at"],
                             sequence_id=row["sequence_id"],
                             sequence_index=row["sequence_index"],
                             thumbnail=row["thumbnail"],
                             image_reference=row["image_reference"])
    if record is None:
        return {"ok": False, "error": "unknown_image",
                "message": "that image is not in the local cache; run a nearby "
                           "search first so its sequence is known",
                "images": []}
    prov = get_provider(record["provider"])
    if prov is None or not prov.available():
        return {"ok": False, "error": "provider_unavailable",
                "message": "the provider that holds this image is not available",
                "images": []}
    try:
        rows = prov.get_sequence(record, before, after)
    except ProviderError as exc:
        return {"ok": False, "error": str(exc), "images": [],
                "message": "could not read the surrounding frames"}
    except Exception:
        return {"ok": False, "error": "provider_error", "images": [],
                "message": "could not read the surrounding frames"}
    cache_put(rows)
    return {"ok": bool(rows), "anchor": record, "images": rows,
            "attribution": sorted({r["attribution"] for r in rows}) if rows else []}


def fetch_image_bytes(url, max_bytes=4_000_000, timeout=None):
    """Download one image. Thumbnails by default -- this is for a VLM, not a print."""
    if not str(url or "").startswith("https://"):
        raise ProviderError("bad_image_url")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout or timeout_seconds()) as resp:
            ctype = str(resp.headers.get("Content-Type") or "")
            if not ctype.startswith("image/"):
                raise ProviderError("not_an_image")
            return resp.read(max_bytes)
    except ProviderError:
        raise
    except Exception as exc:
        raise ProviderError("image_unavailable") from exc
