"""No network: the iCloud service is a stand-in.

    python tests/devices_last_seen_cases.py
"""
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import location as L  # noqa: E402


class Device:
    def __init__(self, name, kind, loc=None):
        self._status = {"name": name, "deviceDisplayName": kind}
        self.location = loc

    def status(self, *args, **kwargs):
        return dict(self._status)


class Api:
    def __init__(self, devices):
        self.devices = devices
        self.requires_2fa = False


def _reading(lat, lon, ms, acc=9):
    return {"latitude": lat, "longitude": lon, "timeStamp": ms, "horizontalAccuracy": acc}


def _session():
    d = tempfile.mkdtemp()
    with open(os.path.join(d, "account.json"), "w", encoding="utf-8") as fh:
        json.dump({"apple_id": "him@example.com", "client_id": "fixed-client"}, fh)
    return d


MAC = Device("MacBook Pro", 'MacBook Pro 15"', _reading(56.95, 24.11, 1790600000000))
PODS = Device("Owner’s AirPods", "AirPods 4", None)
PHONE = Device("Studio42", "iPhone Air", _reading(52.23, 21.01, 1790610000000))


def test_a_reading_is_kept_and_read_back():
    d = tempfile.mkdtemp()
    assert L.device_last_seen("MacBook Pro", state_dir=d) is None
    L.remember_device("MacBook Pro", 56.95, 24.11, 1790600000.0, state_dir=d)
    assert os.path.exists(os.path.join(d, "devices_last_seen.json"))
    got = L.device_last_seen("MacBook Pro", state_dir=d)
    assert got == {"lat": 56.95, "lon": 24.11, "ts": 1790600000.0}, got


def test_an_older_reading_never_replaces_a_newer_one():
    d = tempfile.mkdtemp()
    L.remember_device("MacBook Pro", 52.23, 21.01, 1790610000.0, state_dir=d)
    L.remember_device("MacBook Pro", 56.95, 24.11, 1790600000.0, state_dir=d)
    got = L.device_last_seen("MacBook Pro", state_dir=d)
    assert got["ts"] == 1790610000.0 and got["lat"] == 52.23, got
    L.remember_device("MacBook Pro", 56.95, 24.11, 1790620000.0, state_dir=d)
    got = L.device_last_seen("MacBook Pro", state_dir=d)
    assert got["ts"] == 1790620000.0 and got["lat"] == 56.95, got


def test_each_device_is_kept_apart():
    d = tempfile.mkdtemp()
    L.remember_device("MacBook Pro", 56.95, 24.11, 1790600000.0, state_dir=d)
    L.remember_device("Studio42", 52.23, 21.01, 1790610000.0, state_dir=d)
    assert L.device_last_seen("MacBook Pro", state_dir=d)["lat"] == 56.95
    assert L.device_last_seen("Studio42", state_dir=d)["lat"] == 52.23
    assert L.device_last_seen("Owner’s AirPods", state_dir=d) is None


def test_fetch_icloud_reports_every_device_that_has_a_location():
    got = []

    def on_device(name, lat, lon, ts):
        got.append((name, lat, lon, ts))

    out = L.fetch_icloud(session_dir=_session(),
                         service_factory=lambda a, c, d: Api([MAC, PODS, PHONE]),
                         on_device=on_device)
    assert out == {"lat": 52.23, "lon": 21.01, "ts": 1790610000.0, "accuracy": 9}, out
    assert sorted(got) == sorted([("MacBook Pro", 56.95, 24.11, 1790600000.0),
                                  ("Studio42", 52.23, 21.01, 1790610000.0)]), got


def test_the_live_poller_remembers_every_device():
    d = tempfile.mkdtemp()
    asked = []

    def fake_fetch_icloud(session_dir=None, service_factory=None, on_device=None):
        asked.append(on_device)
        on_device("MacBook Pro", 56.95, 24.11, 1790600000.0)
        on_device("Studio42", 52.23, 21.01, 1790610000.0)
        return {"lat": 52.23, "lon": 21.01, "ts": 1790610000.0, "accuracy": 9}

    real = L.fetch_icloud
    L.fetch_icloud = fake_fetch_icloud
    try:
        L.poll_forever(fetch=None, geocode=lambda lat, lon: {"city": "Varšava", "country": "Polija"},
                       state_dir=d, sleep=lambda s: None, rounds=1)
    finally:
        L.fetch_icloud = real
    assert asked and asked[0] is not None, "the poller never asked for every device"
    assert L.status(state_dir=d)["ok"] is True, L.status(state_dir=d)
    mac = L.device_last_seen("MacBook Pro", state_dir=d)
    assert mac == {"lat": 56.95, "lon": 24.11, "ts": 1790600000.0}, mac
    assert L.device_last_seen("Studio42", state_dir=d)["ts"] == 1790610000.0


def test_the_poller_the_service_starts_remembers_too():
    d = tempfile.mkdtemp()
    asked = []

    def fake_fetch_icloud(session_dir=None, service_factory=None, on_device=None):
        asked.append(on_device)
        if on_device is not None:
            on_device("MacBook Pro", 56.95, 24.11, 1790600000.0)
        return {"lat": 52.23, "lon": 21.01, "ts": 1790610000.0, "accuracy": 9}

    real_fetch, real_dir = L.fetch_icloud, L._STATE_DIR
    L.fetch_icloud, L._STATE_DIR = fake_fetch_icloud, d
    try:
        L.poll_forever(fetch=None, geocode=lambda lat, lon: {"city": "Varšava", "country": "Polija"},
                       sleep=lambda s: None, rounds=1, on_fix=lambda *a: None)
    finally:
        L.fetch_icloud, L._STATE_DIR = real_fetch, real_dir
    assert asked and asked[0] is not None, "the live poller never asked for every device"
    mac = L.device_last_seen("MacBook Pro", state_dir=d)
    assert mac == {"lat": 56.95, "lon": 24.11, "ts": 1790600000.0}, mac


if __name__ == "__main__":
    test_a_reading_is_kept_and_read_back()
    test_an_older_reading_never_replaces_a_newer_one()
    test_each_device_is_kept_apart()
    test_fetch_icloud_reports_every_device_that_has_a_location()
    test_the_live_poller_remembers_every_device()
    test_the_poller_the_service_starts_remembers_too()
    print("OK")
