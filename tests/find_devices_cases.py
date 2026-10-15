"""No network: the iCloud service and the geocoder are stand-ins.

    python tests/find_devices_cases.py
"""
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import location as L  # noqa: E402

NOW = 1790610600.0


class Device:
    def __init__(self, name, kind, status, battery, loc=None):
        self._status = {"name": name, "deviceDisplayName": kind,
                        "deviceStatus": status, "batteryLevel": battery}
        self.location = loc

    def status(self, *args, **kwargs):
        return dict(self._status)


class Api:
    def __init__(self, devices):
        self.devices = devices
        self.requires_2fa = False


def _session():
    d = tempfile.mkdtemp()
    with open(os.path.join(d, "account.json"), "w", encoding="utf-8") as fh:
        json.dump({"apple_id": "him@example.com", "client_id": "fixed-client"}, fh)
    return d


DEVICES = [
    Device("MacBook Pro", 'MacBook Pro 15"', "200", 0.95723956823349),
    Device("Owner’s AirPods", "AirPods 4", "201", 0.0),
    Device("Studio42", "iPhone Air", "200", 0.87,
           {"latitude": 52.23, "longitude": 21.01, "timeStamp": 1790610000000,
            "horizontalAccuracy": 9}),
]


def _geocode(lat, lon):
    if round(lat) == 57:
        return {"city": "Rīga", "country": "Latvija"}
    return {"city": "Varšava", "country": "Polija"}


def _find(state_dir=None):
    # always a folder of its own: the live devices_last_seen.json never leaks in
    return L.find_devices(session_dir=_session(),
                          service_factory=lambda a, c, d: Api(DEVICES),
                          geocode=_geocode,
                          now=NOW,
                          state_dir=state_dir or tempfile.mkdtemp())


NO_LAST_SEEN = {"last_seen_place": None, "last_seen_age_min": None, "last_seen_map_link": None}


def test_every_device_is_listed_in_order():
    got = _find()
    assert [g["name"] for g in got] == ["MacBook Pro", "Owner’s AirPods", "Studio42"], got


def test_a_device_without_a_fix_says_what_is_known():
    mac = _find()[0]
    assert mac == dict({"name": "MacBook Pro", "type": 'MacBook Pro 15"', "online": True,
                        "battery": 96, "place": None, "fix_age_min": None,
                        "map_link": None}, **NO_LAST_SEEN), mac
    pods = _find()[1]
    assert pods["online"] is False and pods["battery"] == 0, pods


def test_a_device_with_a_fix_has_a_place_an_age_and_a_link():
    phone = _find()[2]
    assert phone["place"] == "Varšava, Polija", phone
    assert phone["fix_age_min"] == 10, phone
    assert phone["map_link"].startswith("https://maps.apple.com/?ll=52.23,21.01&q="), phone
    assert "lat" not in phone and "lon" not in phone and "latitude" not in phone, phone


def test_a_session_that_wants_a_new_login_says_so():
    try:
        L.find_devices(session_dir=_session(),
                       service_factory=lambda a, c, d: (_ for _ in ()).throw(RuntimeError("401")))
    except L.SessionExpired:
        return
    raise AssertionError("no SessionExpired")


def test_a_device_with_no_location_now_says_where_it_was_last_seen():
    d = tempfile.mkdtemp()
    L.remember_device("MacBook Pro", 56.95, 24.11, NOW - 3 * 3600, state_dir=d)
    mac = _find(state_dir=d)[0]
    # nothing current is made up from the kept reading
    assert mac["place"] is None and mac["fix_age_min"] is None and mac["map_link"] is None, mac
    assert mac["last_seen_place"] == "Rīga, Latvija", mac
    assert mac["last_seen_age_min"] == 180, mac
    assert mac["last_seen_map_link"].startswith("https://maps.apple.com/?ll=56.95,24.11&q="), mac
    assert not {"lat", "lon", "latitude", "longitude"} & set(mac), mac


def test_a_device_with_a_location_now_needs_no_last_seen():
    d = tempfile.mkdtemp()
    L.remember_device("Studio42", 56.95, 24.11, NOW - 86400, state_dir=d)
    phone = _find(state_dir=d)[2]
    assert phone["place"] == "Varšava, Polija", phone
    assert {k: phone[k] for k in NO_LAST_SEEN} == NO_LAST_SEEN, phone


def test_a_device_never_kept_has_no_last_seen():
    pods = _find()[1]
    assert {k: pods[k] for k in NO_LAST_SEEN} == NO_LAST_SEEN, pods


def test_a_device_located_now_has_no_last_seen_even_without_a_city():
    """The city lookup can fail (Nominatim down) for a device Find My has
    located right now: it still HAS a location, so the kept older reading
    must not be offered next to it as where it was last seen."""
    d = tempfile.mkdtemp()
    L.remember_device("Studio42", 56.95, 24.11, NOW - 86400, state_dir=d)
    phone = L.find_devices(session_dir=_session(),
                           service_factory=lambda a, c, s: Api(DEVICES),
                           geocode=lambda lat, lon: None, now=NOW, state_dir=d)[2]
    assert phone["map_link"] and phone["fix_age_min"] == 10 and phone["place"] is None, phone
    assert {k: phone[k] for k in NO_LAST_SEEN} == NO_LAST_SEEN, phone


if __name__ == "__main__":
    test_every_device_is_listed_in_order()
    test_a_device_without_a_fix_says_what_is_known()
    test_a_device_with_a_fix_has_a_place_an_age_and_a_link()
    test_a_session_that_wants_a_new_login_says_so()
    test_a_device_with_no_location_now_says_where_it_was_last_seen()
    test_a_device_with_a_location_now_needs_no_last_seen()
    test_a_device_never_kept_has_no_last_seen()
    test_a_device_located_now_has_no_last_seen_even_without_a_city()
    print("OK")
