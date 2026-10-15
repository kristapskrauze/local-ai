"""Spec (the owner's):
  app/location.py
    poll_forever(fetch=None, geocode=None, state_dir=None, interval=600,
                 sleep=time.sleep, rounds=None)
      Each round: poll_once(fetch, geocode, state_dir) -- fetch defaults to
      fetch_icloud and geocode to nominatim_geocode -- then writes
      <state_dir>/location_status.json:
        {"ok": true, "checked": <ts>}                         a good round
        {"ok": false, "login_needed": true, "checked", "reason"}
                                                 fetch raised SessionExpired
        {"ok": false, "checked", "reason"}       any other error
      One bad round never stops the loop. It polls first and then sleeps
      `interval`, so the first reading comes at start. `rounds` (tests only)
      stops it after that many rounds.
    status(state_dir=None) -> dict | None     location_status.json, or None
  app/main.py
    a startup hook runs poll_forever in a daemon thread.

No network: fetch and geocode are stand-ins.

    python tests/location_poller_cases.py
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import location as L  # noqa: E402

READING = {"lat": 57.15, "lon": 24.85, "ts": 1790610000.0, "accuracy": 9}


def geocode(lat, lon):
    return {"city": "Sigulda", "country": "Latvija"}


def _run(fetch, rounds=1):
    d = tempfile.mkdtemp()
    slept = []
    L.poll_forever(fetch=fetch, geocode=geocode, state_dir=d, interval=600,
                   sleep=slept.append, rounds=rounds)
    return d, slept


def test_a_good_round_records_the_place_and_says_ok():
    d, _ = _run(lambda: dict(READING))
    assert L.latest(state_dir=d)["city"] == "Sigulda"
    st = L.status(state_dir=d)
    assert st["ok"] is True and st["checked"], st


def test_an_expired_login_is_recorded_and_the_loop_goes_on():
    calls = []

    def fetch():
        calls.append(1)
        raise L.SessionExpired("wants a new login")

    d, slept = _run(fetch, rounds=2)
    st = L.status(state_dir=d)
    assert st["ok"] is False and st["login_needed"] is True and st["reason"], st
    assert len(calls) == 2, calls
    assert 600 in slept, slept


def test_any_other_error_is_recorded_and_the_loop_goes_on():
    calls = []

    def fetch():
        calls.append(1)
        raise RuntimeError("iCloud timed out")

    d, _ = _run(fetch, rounds=2)
    st = L.status(state_dir=d)
    assert st["ok"] is False and "timed out" in st["reason"], st
    assert not st.get("login_needed"), st
    assert len(calls) == 2, calls


def test_no_status_before_the_first_round():
    assert L.status(state_dir=tempfile.mkdtemp()) is None


def test_the_service_starts_it():
    src = open(os.path.join(ROOT, "app", "main.py"), encoding="utf-8").read()
    assert "poll_forever" in src and "Thread" in src, "main.py never starts the poller"


if __name__ == "__main__":
    test_a_good_round_records_the_place_and_says_ok()
    test_an_expired_login_is_recorded_and_the_loop_goes_on()
    test_any_other_error_is_recorded_and_the_loop_goes_on()
    test_no_status_before_the_first_round()
    test_the_service_starts_it()
    print("OK")
