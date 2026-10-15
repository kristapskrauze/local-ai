# -*- coding: utf-8 -*-
"""Boundary checks for the Phase 5 local temporal normalizer."""
import io
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import temporal  # noqa: E402


def main():
    checks = []

    def check(label, passed):
        checks.append((label, bool(passed)))

    tz = "Europe/Riga"
    now = datetime(2026, 8, 22, 10, 0, tzinfo=ZoneInfo(tz))

    tomorrow = temporal.normalize_datetime("tomorrow 14:00", now=now, tz_name=tz)
    check("TEMP-001 tomorrow resolves against Europe/Riga now",
          tomorrow.ok and tomorrow.dt.isoformat(timespec="minutes") == "2026-08-23T14:00+03:00")

    friday = temporal.normalize_datetime("friday 09:30", now=now, tz_name=tz)
    check("TEMP-002 weekday rolls forward, not backward",
          friday.ok and friday.dt.isoformat(timespec="minutes") == "2026-08-28T09:30+03:00")

    relative = temporal.normalize_datetime("in 90 minutes", now=now, tz_name=tz)
    check("TEMP-003 relative minutes preserve timezone",
          relative.ok and relative.dt.isoformat(timespec="minutes") == "2026-08-22T11:30+03:00")

    past = temporal.normalize_datetime("today 09:00", now=now, tz_name=tz)
    check("TEMP-004 write-oriented parsing refuses past local times",
          not past.ok and past.error_type == "past_time")

    allowed_past = temporal.normalize_datetime(
        "today 09:00", now=now, tz_name=tz, allow_past=True)
    check("TEMP-005 read-oriented parsing can allow past times",
          allowed_past.ok and allowed_past.dt.isoformat(timespec="minutes") == "2026-08-22T09:00+03:00")

    nonexistent = temporal.normalize_datetime(
        "2026-03-29 03:30",
        now=datetime(2026, 3, 28, 12, 0, tzinfo=ZoneInfo(tz)),
        tz_name=tz,
    )
    check("TEMP-006 DST spring-forward nonexistent time is refused",
          not nonexistent.ok and nonexistent.error_type == "nonexistent_time")

    ambiguous = temporal.normalize_datetime(
        "2026-10-25 03:30",
        now=datetime(2026, 10, 24, 12, 0, tzinfo=ZoneInfo(tz)),
        tz_name=tz,
    )
    check("TEMP-007 DST fall-back ambiguous time is refused",
          not ambiguous.ok and ambiguous.error_type == "ambiguous_time")

    first, second = temporal.normalize_range(
        when="tomorrow 09:00", now=now, tz_name=tz)
    check("TEMP-008 ranges default to one day when only a start is supplied",
          first.ok and second.ok
          and first.dt.isoformat(timespec="minutes") == "2026-08-23T09:00+03:00"
          and second.dt.isoformat(timespec="minutes") == "2026-08-24T09:00+03:00")

    ok = True
    for label, passed in checks:
        ok = ok and passed
        print(("OK   " if passed else "FAIL ") + label)
    print("\n%d checks, VERDICT: %s" % (len(checks), "PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
