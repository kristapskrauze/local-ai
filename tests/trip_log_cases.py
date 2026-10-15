# -*- coding: utf-8 -*-
"""No network, no live files: every store is in a temp dir, tiles, place names
and road routes are stand-ins. Places are made-up names on real-looking
coordinates.

    python tests/trip_log_cases.py
"""
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import location as L  # noqa: E402
from app import trip_log as T  # noqa: E402


def _use_temp():
    d = Path(tempfile.mkdtemp())
    for name, fname in (("WAZE_TRACKS", "t.json"), ("WAZE_BLIPS", "b.json"),
                        ("WAZE_VISITS", "v.json"), ("POINTS", "p.jsonl"),
                        ("PHOTO_POINTS", "ph.json"), ("FILLS", "f.json"),
                        ("GEOCODE", "g.json"), ("GEOJSON", "trip.geojson"),
                        ("OSM_PLACES", "osm.json"), ("STATIC_DIR", "static")):
        setattr(T, name, d / fname)
    return d


def _ts(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc).timestamp()


def _pt(t, lon, lat):
    return "%s:00+00(%s %s)" % (t, lon, lat)


def _track(t0, lon0, lat0, lon1, lat1, minutes=60, n=30):
    h, m = int(t0[11:13]), int(t0[14:16])
    out = []
    for i in range(n + 1):
        mm = h * 60 + m + minutes * i // n
        out.append(_pt("%s %02d:%02d" % (t0[:10], mm // 60, mm % 60),
                       lon0 + (lon1 - lon0) * i / n, lat0 + (lat1 - lat0) * i / n))
    return "|".join(out)


CSV = "\n".join([
    "﻿Drive summary",
    "Date,Source,Destination",
    # driven: a track starts with it and reaches the destination
    "2026-09-20 08:00:10 GMT,,Testland Zzcity (14.40 50.08)",
    # only looked at: no track ever starts near it
    "2026-09-20 12:00:00 GMT,,Testland Farcity (16.40 38.40)",
    "",
    "Location details",
    "Date,Coordinates",
    "2026-09-20 08:00:00,\"%s\"" % _track("2026-09-20 08:00", 15.20, 49.80, 14.40, 50.08),
    "2026-09-21 07:50:00,\"%s\"" % "|".join([_pt("2026-09-21 07:50", 14.41, 50.08),
                                              _pt("2026-09-21 07:51", 14.41, 50.08)]),
    "",
])


def test_only_driven_navigations_enter():
    d = _use_temp()
    p = d / "waze.csv"
    p.write_text(CSV, encoding="utf-8")
    res = T.import_waze_csv(p)
    assert res["tracks"] == 1 and res["blips"] == 1, res
    assert res["visits"] == 1, "a route only looked at became a visit: %s" % res
    v = json.loads(T.WAZE_VISITS.read_text(encoding="utf-8"))[0]
    assert abs(v[1] - 50.08) < 0.01 and abs(v[2] - 14.40) < 0.01, v


def _store(tracks, blips=()):
    T.WAZE_TRACKS.write_text(json.dumps([
        {"src": "waze", "start": t[0][0], "end": t[-1][0], "pts": t} for t in tracks]))
    T.WAZE_BLIPS.write_text(json.dumps(list(blips)))
    T.WAZE_VISITS.write_text("[]")


def _line(t0, a, b, hours=1.0, n=20):
    return [[t0 + hours * 3600 * i / n, a[0] + (b[0] - a[0]) * i / n,
             a[1] + (b[1] - a[1]) * i / n] for i in range(n + 1)]


def test_gaps_are_filled_only_as_far_as_known():
    _use_temp()
    t = _ts("2026-09-20 08:00")
    a1 = _line(t, (50.0, 14.0), (50.0, 14.5))                      # ends at x
    a2 = _line(t + 6 * 3600, (50.0, 14.8), (50.0, 15.2))           # ~21 km later: fill
    a3 = _line(t + 30 * 3600, (48.0, 15.2), (48.0, 15.6))          # ~220 km: far
    _store([a1, a2, a3])
    segs, _ = T.timeline("2026-09-19", "2026-09-30")
    kinds = [s["kind"] for s in segs if s["kind"] != "drive"]
    assert kinds == ["fill", "far"], kinds


def test_a_sighting_pins_a_long_gap_and_the_rest_is_a_stay_at_its_end():
    _use_temp()
    t = _ts("2026-10-04 08:00")
    ride_end = _line(t, (48.9, 2.5), (48.05, 3.64), hours=3)
    blip = [_ts("2026-10-05 07:51"), 47.00, 4.85]                  # Waze opened next morning
    lake = _line(_ts("2026-10-08 09:00"), (45.48, 5.55), (44.0, 7.0), hours=5)
    _store([ride_end, lake], blips=[blip])
    segs, places = T.timeline("2026-10-01", "2026-10-10")
    ev = [s for s in segs if s["kind"] == "evidence"]
    assert ev and any(abs(p[1] - 47.00) < 1e-6 for p in ev[0]["pts"]), segs
    lake_place = next(p for p in places if abs(p["lat"] - 45.48) < 0.05)
    assert lake_place["stay"], "three days at the lake were not a stay: %s" % lake_place


def test_a_day_out_from_the_hotel_is_dotted_not_a_drive():
    _use_temp()
    hotel = (49.00, 2.52)
    arrive = _line(_ts("2026-10-02 15:00"), (49.5, 2.0), hotel, hours=2)
    leave = _line(_ts("2026-10-04 09:00"), (49.001, 2.521), (48.5, 3.0), hours=2)
    _store([arrive, leave])
    city = [[_ts("2026-10-03 11:00") + 600 * i, 48.86 + 0.002 * i, 2.33 + 0.003 * i]
            for i in range(12)]
    T.PHOTO_POINTS.write_text(json.dumps([{"t": p[0], "lat": p[1], "lon": p[2]} for p in city]))
    segs, _ = T.timeline("2026-10-01", "2026-10-05")
    kinds = [s["kind"] for s in segs if s["kind"] != "drive"]
    assert kinds == ["dayout"], kinds
    out = next(s for s in segs if s["kind"] == "dayout")
    assert any(abs(p[1] - 48.86) < 0.03 for p in out["pts"]), out


def test_photos_beside_the_parked_car_are_no_day_out():
    _use_temp()
    hotel = (49.00, 2.52)
    _store([_line(_ts("2026-10-02 15:00"), (49.5, 2.0), hotel, hours=2),
            _line(_ts("2026-10-04 09:00"), hotel, (48.5, 3.0), hours=2)])
    near = [{"t": _ts("2026-10-03 12:00"), "lat": 49.005, "lon": 2.525}]
    T.PHOTO_POINTS.write_text(json.dumps(near))
    segs, _ = T.timeline("2026-10-01", "2026-10-05")
    assert [s["kind"] for s in segs if s["kind"] != "drive"] == [], segs


def test_a_night_inside_one_track_is_a_stay():
    _use_temp()
    t = _ts("2026-10-08 18:00")
    evening = _line(t, (43.8, 7.6), (44.3, 9.3), hours=3)
    morning = _line(t + 13 * 3600, (44.3, 9.31), (43.2, 10.6), hours=3)
    _store([evening + morning])                                    # ONE recorded track
    _, places = T.timeline("2026-10-01", "2026-10-10")
    assert any(p["stay"] and abs(p["lat"] - 44.3) < 0.05 for p in places), places


def test_a_repeated_find_my_fix_is_written_once():
    _use_temp()
    assert T.add_fix(57.0, 24.1, 1000.0, 10) is True
    assert T.add_fix(57.0, 24.1, 1000.0, 10) is False
    assert T.add_fix(57.0, 24.2, 1120.0, 10) is True
    assert len(T.POINTS.read_text(encoding="utf-8").splitlines()) == 2


def test_the_picture_keeps_where_he_slept_private():
    from PIL import Image
    d = _use_temp()
    t = _ts("2026-09-20 08:00")
    home = (56.95, 24.10)
    _store([_line(t, home, (55.0, 23.0), hours=4),
            _line(t + 20 * 3600, (55.0, 23.01), (54.35, 18.65), hours=5)])
    T.PHOTO_POINTS.write_text(json.dumps(
        [{"t": t + 6 * 3600 + 600 * i, "lat": 54.90 - 0.01 * i, "lon": 23.10} for i in range(6)]))
    saved = (T._tile, T._place_name, T._road_path, T._overpass_cell)
    T._tile = lambda style, z, x, y: Image.new("RGB", (256, 256), (240, 240, 236))
    T._place_name = lambda lat, lon, digits=2, lang="en": {
        "name": "Zzvieta%.0f" % (lat * 10), "country": "Testija", "kind": "town"}
    T._road_path = lambda pts, curvy=False: None
    # one made-up city right on the line, one far off it
    T._overpass_cell = lambda ci, cj: [
        {"name": "Midtown", "lat": 54.675, "lon": 20.825, "place": "city", "pop": 250000},
        {"name": "Faraway", "lat": 57.9, "lon": 27.9, "place": "city", "pop": 900000}]
    try:
        out = d / "map.jpg"
        s = T.render(out, "2026-09-19", "2026-09-30")
    finally:
        T._tile, T._place_name, T._road_path, T._overpass_cell = saved
    assert out.exists() and out.stat().st_size > 10000 and s["km"] > 300, s
    assert s["title"].startswith("Eurotrip") and "Midtown" in s["cities"], s
    assert "Faraway" not in s["cities"], s["cities"]
    feats = json.loads(T.GEOJSON.read_text(encoding="utf-8"))["features"]
    lines = [f for f in feats if f["geometry"]["type"] == "LineString"]
    private = [(56.95, 24.10), (55.0, 23.0)]                      # set off, slept
    for f in lines:
        for lon, lat, _t in f["geometry"]["coordinates"]:
            for q in private:
                assert T._km([lat, lon], q) >= T.PRIVATE_KM - 0.05, (lat, lon, q)
    # the zoomable page: written into the (temp) static dir, same trimmed lines
    page = (d / "static" / (s["html_slug"] + ".html")).read_text(encoding="utf-8")
    data = json.loads(page.split("var D = ", 1)[1].split(";\nfunction esc", 1)[0])
    assert data["title"] == s["title"] and data["cities"][0]["n"] == "Midtown", data["cities"]
    for ln in data["lines"]:
        for lat, lon in ln["p"]:
            for q in private:
                assert T._km([lat, lon], q) >= T.PRIVATE_KM - 0.05, (lat, lon, q)


def test_two_minutes_while_moving_ten_when_parked():
    fixes = iter([{"lat": 57.0, "lon": 24.1, "ts": 1.0},
                  {"lat": 57.0, "lon": 24.2, "ts": 2.0},          # ~6 km: moving
                  {"lat": 57.0, "lon": 24.2, "ts": 3.0}])
    slept = []
    L.poll_forever(fetch=lambda: next(fixes), geocode=lambda a, b: {"city": "X", "country": "Y"},
                   state_dir=tempfile.mkdtemp(), interval=600, fast_interval=120,
                   sleep=slept.append, rounds=3)
    assert slept == [600, 120], slept


if __name__ == "__main__":
    test_only_driven_navigations_enter()
    test_gaps_are_filled_only_as_far_as_known()
    test_a_sighting_pins_a_long_gap_and_the_rest_is_a_stay_at_its_end()
    test_a_night_inside_one_track_is_a_stay()
    test_a_repeated_find_my_fix_is_written_once()
    test_the_picture_keeps_where_he_slept_private()
    test_two_minutes_while_moving_ten_when_parked()
    print("OK")
