# -*- coding: utf-8 -*-
"""Route planning with eyes on the road: the logic, with fake services.

No network: Valhalla, Overpass, KartaView and the vision model are faked.

    py -3 tests/route_planner_cases.py
"""
import io
import os
import sys
import unittest
import urllib.error
from unittest import mock

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import agent_tools as T  # noqa: E402
from app import messaging as M  # noqa: E402
from app import route_planner as RP  # noqa: E402

START = (53.86551, 10.68661)
DEST = (52.37308, 4.89245)


def line(a, b, n=400):
    return [(a[0] + (b[0] - a[0]) * i / n, a[1] + (b[1] - a[1]) * i / n) for i in range(n + 1)]


def route(points, profile="fastest", minutes=300.0, toll=False):
    r = {"profile": profile, "shape": "x", "points": points, "time_min": minutes,
         "length_km": 0.0, "has_toll": toll, "has_ferry": False, "has_highway": True}
    r["cum"] = RP.cumulative_km(points)
    r["length_km"] = r["cum"][-1]
    return r


class GeometryTests(unittest.TestCase):
    def test_polyline6(self):
        # Google's classic example string, read at Valhalla's precision 6:
        # (38.5, -120.2) at 1e5 is (3.85, -12.02) at 1e6
        pts = RP.decode_polyline6("_p~iF~ps|U_ulLnnqC")
        self.assertEqual(len(pts), 2)
        self.assertAlmostEqual(pts[0][0], 3.85, places=5)
        self.assertAlmostEqual(pts[0][1], -12.02, places=5)

    def test_distinct_routes_keep_one_of_each_kind(self):
        a = route(line(START, DEST), "fastest", 300)
        a2 = route(line(START, DEST), "fastest", 305)          # the same road
        b = route(line(START, (52.9, 6.0)) + line((52.9, 6.0), DEST), "no_motorway", 700)
        c = route(line(START, (53.5, 8.0)) + line((53.5, 8.0), DEST), "fastest", 320)
        kept = RP.dedupe([a, a2, b, c], limit=3)
        self.assertIn(b, kept)                  # the slow country-road route survives
        self.assertEqual(len([r for r in kept if r["points"] == a["points"]]), 1)


class RoadFactsTests(unittest.TestCase):
    def test_every_segment_is_summed_by_surface_class_and_country(self):
        pts = line(START, DEST, 10)
        edges = [
            {"length": 100.0, "surface": "paved_smooth", "road_class": "motorway",
             "speed_limit": "unlimited", "end_node": {"admin_index": 0}},
            {"length": 2.0, "surface": "gravel", "road_class": "tertiary", "names": ["Feldweg"],
             "begin_shape_index": 3, "end_node": {"admin_index": 0}},
            {"length": 1.5, "surface": "dirt", "road_class": "tertiary",
             "begin_shape_index": 4, "end_node": {"admin_index": 0}},
            {"length": 50.0, "surface": "paved", "road_class": "motorway", "toll": True,
             "end_node": {"admin_index": 1}},
        ]
        fake = {"edges": edges, "admins": [{"country_code": "DE"}, {"country_code": "CZ"}]}
        with mock.patch.object(RP, "_post_json", return_value=fake):
            f = RP.road_facts({"shape": "x", "points": pts})
        self.assertEqual(f["km"], 153.5)
        self.assertEqual(f["country_km"], {"DE": 103.5, "CZ": 50.0})
        self.assertEqual(f["toll_km_by_country"], {"CZ": 50.0})
        self.assertEqual(f["unpaved_km"], 3.5)
        worst = f["worst_stretches"][0]
        self.assertEqual((worst["length_km"], worst["names"]), (3.5, ["Feldweg"]))
        self.assertAlmostEqual(f["fast_limit_share"], 100.0 / 153.5, places=2)


class SurroundingsTests(unittest.TestCase):
    def test_one_query_for_all_routes_and_a_429_is_retried(self):
        a = route(line(START, DEST))
        answer = {"elements": [
            {"type": "pt", "tags": {"i": "0"}},
            {"type": "way", "tags": {"landuse": "forest"}},
            {"type": "way", "tags": {"natural": "water", "name": "Gooimeer"}},
            {"type": "pt", "tags": {"i": "1"}},
            {"type": "way", "tags": {"landuse": "industrial"}}]}
        calls = []

        def fake(query, timeout=0):
            calls.append(query)
            if len(calls) == 1:
                raise urllib.error.HTTPError("u", 429, "Too Many", {}, None)
            return answer
        with mock.patch.object(RP, "_overpass", side_effect=fake), \
                mock.patch.object(RP.time, "sleep"):
            out = RP.surroundings_all([a, a])
        self.assertEqual(len(calls), 2)
        self.assertEqual(out[0], out[1])
        self.assertGreater(out[0]["shares"]["forest"], 0)
        self.assertIn("Gooimeer", out[0]["water_names"])


class PhotoTests(unittest.TestCase):
    def test_never_near_his_start_and_spread_out(self):
        r = route(line(START, DEST))
        pts = RP.photo_points(r, {"worst_stretches": []}, limit=4)
        self.assertTrue(pts)
        for km, p, _why, _b in pts:
            self.assertGreaterEqual(km, RP.PRIVATE_START_KM)
        for i, (_k1, p1, _w1, _b1) in enumerate(pts):
            for _k2, p2, _w2, _b2 in pts[i + 1:]:
                self.assertGreaterEqual(RP.haversine_km(p1, p2), 15.0)

    def test_a_frame_along_the_road_beats_a_nearer_one_across_it(self):
        near_across = {"id": "x", "distance_m": 5, "heading": 0.0, "captured_at": "2021-01-01"}
        along = {"id": "y", "distance_m": 40, "heading": 92.0, "captured_at": "2019-01-01"}
        with mock.patch("app.street_imagery.search_nearby",
                        return_value={"images": [near_across, along]}):
            self.assertEqual(RP._best_frame((53.0, 9.0), bearing=270.0)["id"], "y")

    def test_an_empty_reading_is_no_reading(self):
        cache = {}
        rec = {"id": "z", "captured_at": "2016-10-08", "distance_m": 20,
               "thumbnail": "https://x/t.jpg", "image_reference": "https://x/l.jpg"}
        with mock.patch.object(RP, "_best_frame", return_value=rec), \
                mock.patch("app.street_imagery.fetch_image_bytes", return_value=b"jpg"), \
                mock.patch.object(RP, "_vision", return_value={}):
            out = RP.inspect_photos([(50.0, (53.0, 9.0), "along", 90.0)], cache)
        self.assertFalse(out[0]["is_road_photo"])
        self.assertEqual(RP._photos_text(out), "Foto: nav")


def scored(letter, minutes, toll, road, scenery, color=0):
    r = route(line(START, DEST), minutes=minutes, toll=toll)
    r.update(letter=letter, road_score=road, scenery_score=scenery, color=RP.COLORS[color],
             facts={"toll_km_by_country": {"CZ": 80.0} if toll else {}, "motorway_share": 0.9,
                    "rough_km": 0.0, "unpaved_km": 0.0, "worst_stretches": []},
             around={"shares": {"forest": 0.8, "fields": 0.9, "water": 0.1}},
             photos=[{"photo": True, "is_road_photo": True, "surface": "good", "km": 120,
                      "captured": "2020-10-22", "defects": ["cracks"], "scenery": 3}])
    return r


class PickAndReportTests(unittest.TestCase):
    def setUp(self):
        self.routes = [scored("A", 300, True, 9.0, 5.0, 0), scored("B", 330, False, 9.5, 6.0, 1),
                       scored("C", 420, False, 8.0, 8.0, 2)]

    def test_picks(self):
        p = RP.pick(self.routes)
        self.assertEqual((p["fastest"], p["toll_free"], p["scenic"], p["best_roads"]),
                         ("A", "B", "C", "B"))

    def test_the_report_says_the_measured_things_and_nothing_private(self):
        picks = RP.pick(self.routes)
        result = {"routes": self.routes, "picks": picks, "origin_city": "Lībeka",
                  "destination_name": "Amsterdama", "destination": DEST,
                  "waze_url": RP.waze_url(DEST)}
        for r in self.routes:
            r["google_url"] = RP.google_maps_url(DEST, [])
        text = RP.format_report(result) + "\n" + RP.links_block(result)
        self.assertIn("Lībeka → Amsterdama", text)
        self.assertIn("vajag vinjeti (CZ)", text)                   # vignette country
        self.assertIn("5:00 h", text)
        self.assertIn("(+0:30)", text)
        self.assertIn("plaisas pie 120. km", text)
        self.assertIn("Foto 2020 (1)", text)
        self.assertNotIn("—", text)
        self.assertNotIn("%.5f" % START[0], text)
        self.assertNotIn("%.3f" % START[0], text)

    def test_links_start_from_the_phone(self):
        url = RP.google_maps_url(DEST, [(52.9, 6.0)])
        self.assertNotIn("origin=", url)
        self.assertIn("waypoints=52.90000%2C6.00000", url)

    def test_no_waypoint_near_his_start(self):
        fast = route(line(START, DEST))
        other = route(line(START, (53.9, 8.0)) + line((53.9, 8.0), DEST), "no_motorway", 600)
        for p in RP._deviation_waypoints(other, fast):
            self.assertGreaterEqual(RP.haversine_km(p, START), RP.PRIVATE_START_KM)


class StartTests(unittest.TestCase):
    def test_someone_else_must_name_a_start(self):
        with mock.patch.object(RP, "geocode", return_value={"lat": 1.0, "lon": 2.0, "city": "X",
                                                            "name": "X", "country_code": "NL"}):
            out = RP.start("chat", "Amsterdam", may_use_his_location=False)
        self.assertEqual(out["error_type"], "origin_required")

    def test_no_fresh_location_asks_for_a_start(self):
        with mock.patch.object(RP, "geocode", return_value={"lat": 1.0, "lon": 2.0, "city": "X",
                                                            "name": "X", "country_code": "NL"}), \
                mock.patch("app.reminders.fresh_location", return_value=None):
            out = RP.start("chat", "Amsterdam")
        self.assertEqual(out["error_type"], "location_unknown")

    def test_an_unknown_place(self):
        with mock.patch.object(RP, "geocode", return_value=None):
            self.assertEqual(RP.start("chat", "Nowhereville")["error_type"],
                             "destination_not_found")

    def test_the_tool_decides_who_may_start_from_his_position(self):
        seen = {}

        def fake_start(chat, dest, origin_text="", via=(), sender=None, may_use_his_location=True):
            seen["may"] = may_use_his_location
            return {"ok": True}
        with mock.patch.object(RP, "start", side_effect=fake_start), \
                mock.patch.object(M, "load_config", return_value={"auto_reply": {
                    "partner_handles": ["+37120000001"]}}):
            T._tool_plan_route({"destination": "Rīga"}, T.AgentContext(
                conversation_id="c", sender="+37120000009"))
            self.assertFalse(seen["may"])                          # a friend
            T._tool_plan_route({"destination": "Rīga"}, T.AgentContext(
                conversation_id="c", sender="+37120000001"))
            self.assertTrue(seen["may"])
            T._tool_plan_route({"destination": "Rīga"}, T.AgentContext(
                conversation_id="c", sender="+37120000009", owner_authored=True))
            self.assertTrue(seen["may"])

    def test_offered_in_his_chat_and_contact_chats(self):
        self.assertIn("plan_route", M._LIVE_ASSISTANT_AGENT_TOOLS)
        self.assertIn("plan_route", M._LIVE_TRUSTED_CONTACT_ACTION_TOOLS)
        self.assertEqual(T.default_registry().get("plan_route").risk, T.EXTERNAL_WRITE)


if __name__ == "__main__":
    unittest.main()
