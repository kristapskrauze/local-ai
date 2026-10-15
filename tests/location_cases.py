"""No network: the geocoder and the fetcher are stand-ins.

    python tests/location_cases.py
"""
import json
import os
import sys
import tempfile
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from app import location as L  # noqa: E402

T0 = datetime(2026, 10, 3, 9, 0).timestamp()
RIGA = (56.9496, 24.1052)
KRAKOW = (50.0614, 19.9366)


def geocode(lat, lon):
    return ({"city": "Rīga", "country": "Latvija"} if lat > 55
            else {"city": "Krakova", "country": "Polija"})


def test_first_reading():
    d = tempfile.mkdtemp()
    got = L.record(RIGA[0], RIGA[1], T0, accuracy=35, geocode=geocode, state_dir=d)
    assert got["city"] == "Rīga" and got["country"] == "Latvija", got
    assert got["since"] == T0, got
    assert not got.get("previous_city"), got
    assert L.latest(state_dir=d)["city"] == "Rīga"


def test_same_city_keeps_since():
    d = tempfile.mkdtemp()
    L.record(RIGA[0], RIGA[1], T0, geocode=geocode, state_dir=d)
    got = L.record(RIGA[0] + 0.01, RIGA[1], T0 + 3600, geocode=geocode, state_dir=d)
    assert got["since"] == T0, got
    assert got["ts"] == T0 + 3600, got


def test_a_move_resets_since_and_remembers_where_he_was():
    d = tempfile.mkdtemp()
    L.record(RIGA[0], RIGA[1], T0, geocode=geocode, state_dir=d)
    got = L.record(KRAKOW[0], KRAKOW[1], T0 + 86400, geocode=geocode, state_dir=d)
    assert got["city"] == "Krakova" and got["since"] == T0 + 86400, got
    assert got["previous_city"] == "Rīga" and got["previous_country"] == "Latvija", got
    lines = open(os.path.join(d, "location.jsonl"), encoding="utf-8").read().splitlines()
    assert len(lines) == 2 and json.loads(lines[-1])["city"] == "Krakova", lines


def test_describe_names_the_place_never_coordinates():
    d = tempfile.mkdtemp()
    L.record(RIGA[0], RIGA[1], T0, geocode=geocode, state_dir=d)
    L.record(KRAKOW[0], KRAKOW[1], T0 + 86400, geocode=geocode, state_dir=d)
    text = L.describe(now=T0 + 86400 + 1800, state_dir=d)
    assert "Krakova" in text and "Polija" in text, text
    assert "Rīga" in text, text
    for coord in ("50.06", "19.93", "56.94", "24.10"):
        assert coord not in text, text
    # "since" is a date a person reads, never a raw timestamp
    import re
    assert not re.search(r"\d{9,}", text), text
    assert "kopš 4. oktobra" in text, text


def test_describe_is_empty_when_nothing_is_known():
    assert L.describe(now=T0, state_dir=tempfile.mkdtemp()) == ""


def test_poll_once_records_what_the_fetcher_returns():
    d = tempfile.mkdtemp()
    got = L.poll_once(lambda: {"lat": KRAKOW[0], "lon": KRAKOW[1], "ts": T0, "accuracy": 20},
                      geocode=geocode, state_dir=d)
    assert got and got["city"] == "Krakova", got
    assert L.poll_once(lambda: None, geocode=geocode, state_dir=tempfile.mkdtemp()) is None


def _nominatim_reply(**address):
    return {"address": dict(address), "lat": "0", "lon": "0"}


def test_nominatim_is_asked_for_a_city_with_rounded_coordinates_only():
    from urllib.parse import parse_qs, urlparse
    asked = []

    def fetch(url, headers):
        asked.append((url, dict(headers or {})))
        return _nominatim_reply(city="Rīga", country="Latvija")

    got = L.nominatim_geocode(RIGA[0], RIGA[1], fetch=fetch, state_dir=tempfile.mkdtemp())
    assert got == {"city": "Rīga", "country": "Latvija"}, got
    assert len(asked) == 1, asked
    url, headers = asked[0]
    q = parse_qs(urlparse(url).query)
    assert "nominatim.openstreetmap.org" in url, url
    assert q.get("format") == ["jsonv2"] and q.get("zoom") == ["10"], url
    assert q.get("accept-language") == ["lv"], url
    assert abs(float(q["lat"][0]) - 56.95) < 1e-9, url
    assert abs(float(q["lon"][0]) - 24.11) < 1e-9, url
    assert "56.9496" not in url and "24.1052" not in url, url
    agent = {k.lower(): v for k, v in headers.items()}.get("user-agent", "")
    assert agent and not agent.lower().startswith("python"), headers


def test_nominatim_town_or_village_counts_as_the_city():
    def fetch(url, headers):
        return _nominatim_reply(village="Zakopane", county="Tatru apriņķis",
                                country="Polija")

    got = L.nominatim_geocode(49.2992, 19.9496, fetch=fetch, state_dir=tempfile.mkdtemp())
    assert got == {"city": "Zakopane", "country": "Polija"}, got


def test_nominatim_asks_once_for_the_same_place():
    d = tempfile.mkdtemp()
    calls = []

    def fetch(url, headers):
        calls.append(url)
        return _nominatim_reply(city="Rīga", country="Latvija")

    L.nominatim_geocode(56.9496, 24.1052, fetch=fetch, state_dir=d)
    got = L.nominatim_geocode(56.9512, 24.1071, fetch=fetch, state_dir=d)  # same ~1 km
    assert got == {"city": "Rīga", "country": "Latvija"}, got
    assert len(calls) == 1, calls
    assert os.path.exists(os.path.join(d, "location_geocode_cache.json"))


def test_nominatim_failing_returns_none():
    def fetch(url, headers):
        raise OSError("no route to host")

    assert L.nominatim_geocode(RIGA[0], RIGA[1], fetch=fetch,
                               state_dir=tempfile.mkdtemp()) is None


def test_no_answer_from_the_geocoder_keeps_the_last_known_place():
    d = tempfile.mkdtemp()
    L.record(RIGA[0], RIGA[1], T0, geocode=geocode, state_dir=d)
    got = L.record(KRAKOW[0], KRAKOW[1], T0 + 600, geocode=lambda lat, lon: None,
                   state_dir=d)
    assert got is None, got
    assert L.latest(state_dir=d)["city"] == "Rīga"
    lines = open(os.path.join(d, "location.jsonl"), encoding="utf-8").read().splitlines()
    assert len(lines) == 1, lines


if __name__ == "__main__":
    test_first_reading()
    test_same_city_keeps_since()
    test_a_move_resets_since_and_remembers_where_he_was()
    test_describe_names_the_place_never_coordinates()
    test_describe_is_empty_when_nothing_is_known()
    test_poll_once_records_what_the_fetcher_returns()
    test_nominatim_is_asked_for_a_city_with_rounded_coordinates_only()
    test_nominatim_town_or_village_counts_as_the_city()
    test_nominatim_asks_once_for_the_same_place()
    test_nominatim_failing_returns_none()
    test_no_answer_from_the_geocoder_keeps_the_last_known_place()
    print("OK")
