"""No network: the iCloud service is a stand-in.

    python tests/location_icloud_cases.py
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
    def __init__(self, devices, requires_2fa=False):
        self.devices = devices
        self.requires_2fa = requires_2fa


def _reading(lat, lon, ms, acc=9):
    return {"latitude": lat, "longitude": lon, "timeStamp": ms, "horizontalAccuracy": acc}


def _session(account=True):
    d = tempfile.mkdtemp()
    if account:
        with open(os.path.join(d, "account.json"), "w", encoding="utf-8") as fh:
            json.dump({"apple_id": "him@example.com", "client_id": "fixed-client"}, fh)
    return d


def _factory(api, seen=None):
    def make(apple_id, client_id, session_dir):
        if seen is not None:
            seen.append((apple_id, client_id, str(session_dir)))
        if isinstance(api, Exception):
            raise api
        return api
    return make


MAC = Device("MacBook Pro", 'MacBook Pro 15"', _reading(56.95, 24.11, 1790600000000))
PHONE = Device("Studio42", "iPhone Air", _reading(52.23, 21.01, 1790610000000, 9))


def test_his_iphone_is_found_by_type_and_read_in_seconds():
    d = _session()
    seen = []
    got = L.fetch_icloud(session_dir=d, service_factory=_factory(Api([MAC, PHONE]), seen))
    assert got == {"lat": 52.23, "lon": 21.01, "ts": 1790610000.0, "accuracy": 9}, got
    assert seen == [("him@example.com", "fixed-client", d)], seen


def test_another_device_never_stands_in_for_the_phone():
    d = _session()
    no_fix = Device("Studio42", "iPhone Air", None)
    assert L.fetch_icloud(session_dir=d, service_factory=_factory(Api([MAC, no_fix]))) is None


def test_the_freshest_iphone_reading_wins():
    d = _session()
    old = Device("Old phone", "iPhone 12", _reading(56.95, 24.11, 1790000000000))
    got = L.fetch_icloud(session_dir=d, service_factory=_factory(Api([old, PHONE])))
    assert got and got["lat"] == 52.23, got


def _expired(**kwargs):
    try:
        L.fetch_icloud(**kwargs)
    except L.SessionExpired:
        return True
    return False


def test_a_session_that_wants_a_new_login_says_so():
    assert _expired(session_dir=_session(),
                    service_factory=_factory(Api([PHONE], requires_2fa=True)))
    assert _expired(session_dir=_session(),
                    service_factory=_factory(RuntimeError("Invalid email/password combination.")))
    assert _expired(session_dir=_session(account=False),
                    service_factory=_factory(Api([PHONE])))


def test_the_rig_never_has_a_password_to_send():
    import pyicloud
    made = []

    class Recorder:
        def __init__(self, apple_id, password="unset", **kw):
            made.append(dict(kw, apple_id=apple_id, password=password))
            self.authenticated = False

        def authenticate(self, *a, **kw):
            self.authenticated = True

    real = pyicloud.PyiCloudService
    pyicloud.PyiCloudService = Recorder
    try:
        svc = L._default_factory("him@example.com", "fixed-client", "some/dir")
    finally:
        pyicloud.PyiCloudService = real
    assert made and made[0]["password"] is None, made
    assert made[0].get("authenticate") is False, "authenticate=True reads the keyring: %s" % made
    assert svc.authenticated, "the saved session was never reopened"


def test_find_my_wanting_the_password_is_login_needed():
    """Find My asks for the password again every few hours while the trusted
    session stays valid (450 -> pyicloud re-login -> no password). That is
    "run the login again", not a passing error; anything else stays an error."""
    from pyicloud.exceptions import PyiCloudAuthRequiredException, PyiCloudFailedLoginException

    class NeedsPassword:
        requires_2fa = False

        def __init__(self, exc):
            self.exc = exc

        @property
        def devices(self):
            raise self.exc

    for exc in (PyiCloudFailedLoginException("No password set"),
                PyiCloudAuthRequiredException("him@example.com", None)):
        assert _expired(session_dir=_session(), service_factory=_factory(NeedsPassword(exc)))
        try:
            L.find_devices(session_dir=_session(), service_factory=_factory(NeedsPassword(exc)))
            raise AssertionError("find_devices hid %r" % exc)
        except L.SessionExpired:
            pass
    try:
        L.fetch_icloud(session_dir=_session(),
                       service_factory=_factory(NeedsPassword(RuntimeError("iCloud timed out"))))
        raise AssertionError("a timeout vanished")
    except L.SessionExpired:
        raise AssertionError("a timeout is not a login problem") from None
    except RuntimeError:
        pass


def test_the_refresher_is_stopped_after_everything_is_read():
    """Opening Find My starts pyicloud's own refresher thread (every 5
    minutes, forever); one per poll piled up. It is stopped once read, and
    not before: reading a device with it stopped starts a new one."""
    import threading

    class Watched(Device):
        @property
        def location(self):
            assert not devices.stop_event.is_set(), "read after the refresher was stopped"
            return self._loc

        @location.setter
        def location(self, value):
            self._loc = value

    class Manager(list):
        stop_event = threading.Event()

    devices = Manager([Watched("Studio42", "iPhone Air",
                               _reading(52.23, 21.01, 1790610000000))])
    got = L.fetch_icloud(session_dir=_session(), service_factory=_factory(Api(devices)))
    assert got and got["lat"] == 52.23, got
    assert devices.stop_event.is_set(), "fetch_icloud left the refresher running"
    devices.stop_event.clear()
    out = L.find_devices(session_dir=_session(), service_factory=_factory(Api(devices)))
    assert out and out[0]["name"] == "Studio42", out
    assert devices.stop_event.is_set(), "find_devices left the refresher running"


if __name__ == "__main__":
    test_his_iphone_is_found_by_type_and_read_in_seconds()
    test_another_device_never_stands_in_for_the_phone()
    test_the_freshest_iphone_reading_wins()
    test_a_session_that_wants_a_new_login_says_so()
    test_the_rig_never_has_a_password_to_send()
    test_find_my_wanting_the_password_is_login_needed()
    test_the_refresher_is_stopped_after_everything_is_read()
    print("OK")
