# -*- coding: utf-8 -*-
"""street_imagery, with the providers replaced by fakes.

Nothing here touches Mapillary or KartaView. Every provider response is a
recorded shape -- including the broken ones -- so the suite says the same
thing on a train as it does on the rig, and a provider outage is a case
rather than a red run. The live smoke test at the bottom is opt-in.

The cases are the ways this breaks in the field: one provider dead and the
other fine, an API answering HTML, a drive that produced forty near-identical
frames, coordinates that are really an unset variable.

    py tests\\street_imagery_cases.py          # mocked, offline
    py tests\\street_imagery_cases.py --live   # also hit the real APIs
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import agent_tools, street_imagery as si   # noqa: E402

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("    %-52s %s" % (name[:52], "ok" if ok else "FAIL  " + str(detail)[:70]))
    return ok


def use_temp_db():
    """Point the cache at a scratch file so the real one is never touched."""
    path = os.path.join(tempfile.mkdtemp(prefix="street_img_"), "cache.sqlite3")
    si.DB_PATH = path
    si.DB_DIR = os.path.dirname(path)
    si._TABLES_READY[0] = False
    return path


def mapillary_payload():
    return {"data": [
        {"id": "111", "computed_geometry": {"coordinates": [24.1052, 56.9496]},
         "compass_angle": 183.4, "captured_at": 1650000000000,
         "sequence": "seq-a", "is_pano": False,
         "thumb_256_url": "https://images.example/111_256.jpg",
         "thumb_1024_url": "https://images.example/111_1024.jpg"},
        {"id": "112", "geometry": {"coordinates": [24.1053, 56.9497]},
         "compass_angle": 12.0, "captured_at": 1650000600000,
         "sequence": "seq-a", "is_pano": True,
         "thumb_256_url": "https://images.example/112_256.jpg"},
    ]}


def kartaview_payload(n=2, lat=56.9496, lon=24.1052):
    rows = []
    for i in range(n):
        rows.append({
            "id": str(900 + i), "sequenceId": "77", "sequenceIndex": str(i),
            "lat": "%.6f" % (lat + i * 0.00002), "lng": "%.6f" % lon,
            "distance": "%.2f" % (i * 2.0), "heading": "90.5",
            "shotDate": "2019-06-13 10:%02d:00.000" % i, "projection": "PLANE",
            "fileurlTh": "https://storage.example/th/%d.jpg" % i,
            "fileurlLTh": "https://storage.example/lth/%d.jpg" % i,
        })
    return {"status": {"httpCode": 200}, "result": {"data": rows}}


class FakeHTTP(object):

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, url, timeout=None, headers=None):
        self.calls.append(url)
        for needle, outcome in self.routes:
            if needle in url:
                if isinstance(outcome, Exception):
                    raise outcome
                if callable(outcome):
                    return outcome(url)
                return outcome
        raise si.ProviderError("no_route")


def with_http(routes):
    fake = FakeHTTP(routes)
    si._get_json = fake
    return fake


def case_coordinate_validation():
    print("\n1. coordinates")
    check("rejects out-of-range latitude", si.valid_coords(91.0, 24.0) is None)
    check("rejects non-numeric", si.valid_coords("abc", 24.0) is None)
    check("rejects null island (an unset variable)", si.valid_coords(0, 0) is None)
    check("accepts Riga", si.valid_coords(56.9496, 24.1052) == (56.9496, 24.1052))
    out = si.search_nearby(999, 999)
    check("search refuses bad coordinates", out["error"] == "invalid_coordinates", out)
    check("refusal carries no images", out["images"] == [])


def case_normalisation():
    print("\n2. provider normalisation")
    m = si.MapillaryProvider()._to_record(mapillary_payload()["data"][0],
                                          56.9496, 24.1052)
    check("mapillary lat/lon un-swapped from GeoJSON",
          abs(m["latitude"] - 56.9496) < 1e-6 and abs(m["longitude"] - 24.1052) < 1e-6, m)
    check("mapillary epoch ms -> ISO", m["captured_at"].startswith("2022-"), m["captured_at"])
    check("mapillary heading kept", m["heading"] == 183.4, m["heading"])
    check("mapillary sequence kept", m["sequence_id"] == "seq-a", m)
    check("mapillary attribution set", "Mapillary" in m["attribution"], m["attribution"])

    k = si.KartaViewProvider()._to_record(kartaview_payload()["result"]["data"][0],
                                          56.9496, 24.1052)
    check("kartaview text date -> ISO", k["captured_at"].startswith("2019-06-13T"), k)
    check("kartaview uses provider distance", k["distance_m"] == 0.0, k["distance_m"])
    check("kartaview panorama flag false for PLANE", k["is_panorama"] is False, k)
    check("both providers share one record shape",
          set(m) == set(k), set(m) ^ set(k))

    bad = si.KartaViewProvider()._to_record({"id": "x", "lat": None, "lng": None})
    check("row without coordinates is dropped", bad is None, bad)


def case_fallback():
    print("\n3. one provider down, the other answers")
    use_temp_db()
    os.environ["RIG_MAPILLARY_TOKEN"] = "test-token"
    with_http([("graph.mapillary.com", si.ProviderError("rate_limited")),
               ("kartaview", kartaview_payload(3))])
    out = si.search_nearby(56.9496, 24.1052, 200, 10, use_cache=False)
    check("request still succeeds", out["ok"] is True, out)
    check("kartaview results returned", len(out["images"]) == 3, len(out["images"]))
    check("mapillary failure reported, not raised",
          out["providers"]["mapillary"]["error"] == "rate_limited", out["providers"])
    check("kartaview reported ok", out["providers"]["kartaview"]["ok"] is True)

    print("\n   both down")
    with_http([("graph.mapillary.com", si.ProviderError("timeout")),
               ("kartaview", si.ProviderError("timeout"))])
    out = si.search_nearby(56.9496, 24.1052, 200, 10, use_cache=False)
    check("clean no-imagery result", out["ok"] is False and out["images"] == [], out)
    check("no exception text leaks to the model",
          all(isinstance(v.get("error"), str) for v in out["providers"].values()),
          out["providers"])

    print("\n   auth failure is distinguishable from empty")
    with_http([("graph.mapillary.com", si.ProviderError("auth_failed")),
               ("kartaview", kartaview_payload(1))])
    out = si.search_nearby(56.9496, 24.1052, 200, 10, use_cache=False)
    check("auth failure named", out["providers"]["mapillary"]["error"] == "auth_failed")
    os.environ.pop("RIG_MAPILLARY_TOKEN", None)


def case_no_token():
    print("\n4. mapillary without a token")
    os.environ.pop("RIG_MAPILLARY_TOKEN", None)
    check("not listed as configured", "mapillary" not in si.configured_providers(),
          si.configured_providers())
    check("provider reports itself unavailable",
          si.MapillaryProvider().available() is False)
    use_temp_db()
    with_http([("kartaview", kartaview_payload(2))])
    out = si.search_nearby(56.9496, 24.1052, 200, 10, use_cache=False)
    check("kartaview alone still answers", len(out["images"]) == 2, out)


def case_malformed():
    print("\n5. providers returning nonsense")
    use_temp_db()
    with_http([("kartaview", {"result": {"data": "not-a-list"}})])
    out = si.search_nearby(56.9496, 24.1052, 200, 10, use_cache=False)
    check("string where rows expected -> no crash", out["images"] == [], out)

    with_http([("kartaview", {"unexpected": "shape"})])
    out = si.search_nearby(56.9496, 24.1052, 200, 10, use_cache=False)
    check("missing result key -> no crash", out["images"] == [], out)

    with_http([("kartaview", {"result": {"data": [{"id": "1"}]}})])
    out = si.search_nearby(56.9496, 24.1052, 200, 10, use_cache=False)
    check("row missing every field is skipped", out["images"] == [], out)


def case_dedup():
    print("\n6. deduplication")
    frames = []
    for i in range(6):
        frames.append(si.make_record("kartaview", "d%d" % i,
                                     56.9496 + i * 0.000018, 24.1052,
                                     heading=90.0, captured_at="2022-01-01T00:00:00Z",
                                     origin_lat=56.9496, origin_lon=24.1052))
    kept = si.deduplicate(frames)
    check("a drive of near-identical frames collapses",
          len(kept) < len(frames), "%d -> %d" % (len(frames), len(kept)))

    turned = [
        si.make_record("kartaview", "t1", 56.9496, 24.1052, heading=0.0,
                       origin_lat=56.9496, origin_lon=24.1052),
        si.make_record("kartaview", "t2", 56.9496, 24.1052, heading=180.0,
                       origin_lat=56.9496, origin_lon=24.1052),
    ]
    check("same spot, opposite directions both kept",
          len(si.deduplicate(turned)) == 2, si.deduplicate(turned))

    far = [
        si.make_record("kartaview", "f1", 56.9496, 24.1052, heading=90.0,
                       origin_lat=56.9496, origin_lon=24.1052),
        si.make_record("kartaview", "f2", 56.9520, 24.1052, heading=90.0,
                       origin_lat=56.9496, origin_lon=24.1052),
    ]
    check("well-separated frames both kept", len(si.deduplicate(far)) == 2)


def case_selection():
    print("\n7. choosing a small useful set")
    pool = []
    for i in range(30):
        pool.append(si.make_record(
            "kartaview", "s%d" % i, 56.9496 + i * 0.0004, 24.1052,
            heading=(i * 37) % 360,
            captured_at="%d-01-01T00:00:00Z" % (2015 + (i % 8)),
            origin_lat=56.9496, origin_lon=24.1052))
    chosen = si.select_best(pool, 56.9496, 24.1052, 4)
    check("honours max_images", len(chosen) <= 4, len(chosen))
    check("returns something", len(chosen) > 0)
    years = [int((r["captured_at"] or "0")[:4]) for r in chosen]
    check("prefers recent imagery over the oldest",
          sum(years) / len(years) > 2017, years)

    facing = si.make_record("kartaview", "face", 56.9490, 24.1052, heading=0.0,
                            captured_at="2022-01-01T00:00:00Z",
                            origin_lat=56.9496, origin_lon=24.1052)
    away = si.make_record("kartaview", "away", 56.9490, 24.1052, heading=180.0,
                          captured_at="2022-01-01T00:00:00Z",
                          origin_lat=56.9496, origin_lon=24.1052)
    picked = si.select_best([away, facing], 56.9496, 24.1052, 1)
    check("prefers the camera pointing at the place",
          picked and picked[0]["id"] == "face", picked)


def case_cache():
    print("\n8. cache")
    use_temp_db()
    fake = with_http([("kartaview", kartaview_payload(3))])
    first = si.search_nearby(56.9496, 24.1052, 200, 10)
    calls_after_first = len(fake.calls)
    check("cache miss goes to the network", calls_after_first >= 1)
    check("results stored", si.cache_stats()["images"] == 3, si.cache_stats())

    with_http([("kartaview", si.ProviderError("timeout"))])
    second = si.search_nearby(56.9496, 24.1052, 200, 10)
    check("cache answers when the provider is down",
          len(second["images"]) == 3, second)
    check("cache hits counted", second["cache_hits"] == 3, second["cache_hits"])
    check("cached rows are marked",
          all(r.get("cached") for r in second["images"]), second["images"][:1])

    outside = si.cache_nearby(57.5, 25.5, 100, 10)
    check("bounding box excludes far-away rows", outside == [], outside)

    si.cache_describe("kartaview", "900", "a wide street with trams")
    check("description stored beside the image",
          si.cache_stats()["described"] == 1, si.cache_stats())
    again = si.cache_nearby(56.9496, 24.1052, 200, 10)
    check("stored description comes back",
          any(r.get("cached_description") for r in again), again[:1])


def case_sequence():
    print("\n9. sequences")
    use_temp_db()
    with_http([("kartaview", kartaview_payload(3))])
    si.search_nearby(56.9496, 24.1052, 200, 10)

    seq_rows = {"result": {"data": [
        {"id": str(900 + i), "sequenceId": "77", "sequenceIndex": str(i),
         "lat": "56.9496", "lng": "24.1052", "heading": "90.0",
         "shotDate": "2019-06-13 10:00:00.000", "projection": "PLANE",
         "fileurlTh": "https://storage.example/th/%d.jpg" % i} for i in range(9)]}}
    with_http([("sequence", seq_rows), ("kartaview", kartaview_payload(3))])
    out = si.get_sequence("900", provider="kartaview", before=2, after=2)
    check("sequence returns a window, not the whole drive",
          out["ok"] and len(out["images"]) <= 5, len(out.get("images") or []))
    check("window centred on the anchor",
          any(r["id"] == "900" for r in out["images"]), out["images"])

    missing = si.get_sequence("does-not-exist")
    check("unknown image gives a clean error",
          missing["ok"] is False and missing["error"] == "unknown_image", missing)

    with_http([("sequence", si.ProviderError("http_500")),
               ("kartaview", kartaview_payload(3))])
    broken = si.get_sequence("900", provider="kartaview")
    check("broken sequence does not raise", broken["ok"] is False, broken)


def case_tool_surface():
    print("\n10. the tool as the model sees it")
    reg = agent_tools.build_default_registry()
    tool = reg.get("street_imagery")
    check("registered", tool is not None)
    check("read-only", tool.risk == agent_tools.READ_ONLY, tool.risk)
    schema = tool.ollama_schema()
    check("exposes an ollama function schema",
          schema.get("type") == "function"
          and schema["function"]["name"] == "street_imagery", schema)
    actions = tool.schema["properties"]["action"]["enum"]
    for want in ("inspect_location", "search_nearby", "get_image", "get_sequence"):
        check("action %s offered" % want, want in actions, actions)
    desc = tool.description.lower()
    check("description says imagery is historical",
          "historical" in desc or "not.*current" in desc, desc[:80])
    check("description steers away from web_search's job",
          "web_search" in desc, desc[:80])
    check("geocode_place registered too", reg.get("geocode_place") is not None)

    ctx = agent_tools.AgentContext()
    bad = reg.execute("street_imagery", {"action": "inspect_location"}, ctx)
    inner = bad.get("result") or {}
    check("no coordinates -> clean refusal, no guessing",
          inner.get("error_type") == "no_location", inner)
    check("refusal is JSON-serialisable", _serialisable(bad))

    rejected = reg.execute("street_imagery", {"action": "teleport"}, ctx)
    check("unknown action rejected by the schema",
          rejected.get("ok") is False, rejected)
    huge = reg.execute("street_imagery",
                       {"action": "inspect_location", "latitude": 56.9,
                        "longitude": 24.1, "max_images": 99}, ctx)
    check("max_images ceiling enforced by the schema",
          huge.get("ok") is False, huge)


def case_result_serialisation():
    print("\n11. result serialisation")
    use_temp_db()
    with_http([("kartaview", kartaview_payload(2))])
    out = si.search_nearby(56.9496, 24.1052, 200, 10, use_cache=False)
    check("search result is JSON-serialisable", _serialisable(out))
    rec = out["images"][0]
    for field in ("id", "provider", "latitude", "longitude", "distance_m",
                  "heading", "captured_at", "sequence_id", "thumbnail",
                  "image_reference", "attribution"):
        check("record carries %s" % field, field in rec, sorted(rec))
    check("provenance survives to the model",
          rec["provider"] == "kartaview" and rec["attribution"], rec)


def case_secrets():
    print("\n12. token never escapes")
    os.environ["RIG_MAPILLARY_TOKEN"] = "super-secret-value"
    use_temp_db()
    fake = with_http([("graph.mapillary.com", mapillary_payload()),
                      ("kartaview", kartaview_payload(1))])
    out = si.search_nearby(56.9496, 24.1052, 40, 5, use_cache=False)
    blob = repr(out)
    check("token absent from the tool result", "super-secret-value" not in blob)
    check("token absent from request URLs",
          not any("super-secret-value" in u for u in fake.calls), fake.calls)
    check("mapillary results still normalised", len(out["images"]) >= 2, out["images"])
    os.environ.pop("RIG_MAPILLARY_TOKEN", None)


def case_radius_limits():
    print("\n13. provider radius ceilings")
    use_temp_db()
    captured = []

    def record(url):
        captured.append(url)
        return kartaview_payload(1)

    with_http([("kartaview", record)])
    si.search_nearby(56.9496, 24.1052, 5000, 5, use_cache=False)
    check("radius clamped to what the API accepts",
          "radius=500" in captured[0], captured[0])

    os.environ["RIG_MAPILLARY_TOKEN"] = "t"
    captured2 = []

    def record2(url):
        captured2.append(url)
        return mapillary_payload()

    with_http([("graph.mapillary.com", record2), ("kartaview", kartaview_payload(1))])
    si.search_nearby(56.9496, 24.1052, 30, 5, use_cache=False)
    check("small radius uses the point search", "radius=30" in captured2[0], captured2[0])
    captured2[:] = []
    si.search_nearby(56.9496, 24.1052, 400, 5, use_cache=False)
    check("wide radius switches to a bounding box",
          "bbox=" in captured2[0], captured2[0])
    os.environ.pop("RIG_MAPILLARY_TOKEN", None)


def _serialisable(value):
    import json
    try:
        json.dumps(value)
        return True
    except (TypeError, ValueError):
        return False


def case_live():
    print("\n14. LIVE providers (opt-in)")
    use_temp_db()
    out = si.search_nearby(56.9496, 24.1052, radius_m=500, limit=8)
    check("live search returned imagery", out["ok"], out.get("providers"))
    if out["images"]:
        rec = out["images"][0]
        check("live record has coordinates and a date",
              rec["latitude"] and rec["captured_at"], rec)
        try:
            blob = si.fetch_image_bytes(rec["thumbnail"])
            check("thumbnail downloads", len(blob) > 500, len(blob))
        except si.ProviderError as exc:
            check("thumbnail downloads", False, exc)


def main():
    print(__doc__.strip().splitlines()[0])
    started = time.time()
    real_get_json = si._get_json
    cases = [case_coordinate_validation, case_normalisation, case_fallback,
             case_no_token, case_malformed, case_dedup, case_selection,
             case_cache, case_sequence, case_tool_surface,
             case_result_serialisation, case_secrets, case_radius_limits]
    for fn in cases:
        try:
            fn()
        except Exception as exc:
            check(fn.__name__, False, "%s: %s" % (type(exc).__name__, exc))
    si._get_json = real_get_json
    if "--live" in sys.argv:
        try:
            case_live()
        except Exception as exc:
            check("case_live", False, "%s: %s" % (type(exc).__name__, exc))

    print("\n%d passed, %d failed  (%.1fs)"
          % (len(PASS), len(FAIL), time.time() - started))
    for name in FAIL:
        print("   FAILED: %s" % name)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
