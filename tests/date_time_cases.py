# -*- coding: utf-8 -*-
"""Does it know what day and time it is?

Every scheduling mistake this bot has made came from one of three things, and
none of them is the model being stupid about calendars:

So the checks below freeze the clock at a known Wednesday and ask the temporal
engine — the one thing every lane goes through, from calendar_search to the
plan store — to place today, tomorrow, the day after, a named weekday and an
explicit date. A frozen clock is the whole point: an assertion computed the
same way the code computes it would pass no matter how wrong both were.

Offline. No model, no network, no wall clock except the one check that says the
wall clock is what the bot reads.

    py -3 tests/date_time_cases.py
"""
import io
import os
import sys
from datetime import datetime, timedelta

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from zoneinfo import ZoneInfo                                    # noqa: E402
from app import temporal as T                                    # noqa: E402

RIGA = ZoneInfo("Europe/Riga")
# Morning, so that "šodien 15:00" is still ahead: at 21:40 a 15:00 booking is
# in the past and correctly refused, which is a different case (DTM-004).
NOW = datetime(2026, 8, 26, 9, 15, tzinfo=RIGA)       # a Wednesday morning


def _at(text, allow_past=False):
    """Resolve `text` against the frozen clock. Returns 'YYYY-MM-DD HH:MM' or ''."""
    res = T.normalize_datetime(text, now=NOW, tz_name="Europe/Riga",
                               allow_past=allow_past)
    return res.dt.strftime("%Y-%m-%d %H:%M") if res.ok and res.dt else ""


def main():
    checks = []

    def check(label, passed):
        checks.append((label, bool(passed)))

    wall = datetime.now(RIGA)
    live = T.now_in("Europe/Riga")
    check("DTM-001 the current time is the machine's, not UTC or a guess",
          abs((live - wall).total_seconds()) < 60)
    check("DTM-001 the default timezone is Riga, where they live",
          T.timezone_name("") == "Europe/Riga")

    check("DTM-002 'šodien 15:00' is today", _at("šodien 15:00") == "2026-08-26 15:00")
    check("DTM-002 'sodien' without diacritics still today",
          _at("sodien 15:00") == "2026-08-26 15:00")
    check("DTM-002 'today at 3pm' is today, in the afternoon",
          _at("today at 3pm") == "2026-08-26 15:00")

    check("DTM-003 'rīt 9:00' is tomorrow", _at("rīt 9:00") == "2026-08-27 09:00")
    check("DTM-003 'rit' without diacritics is tomorrow too",
          _at("rit 9:00") == "2026-08-27 09:00")
    check("DTM-003 'tomorrow 09:00' agrees with 'rīt'",
          _at("tomorrow 09:00") == _at("rīt 9:00"))
    check("DTM-003 'rīt no rīta' takes the morning default",
          _at("rīt no rīta") == "2026-08-27 09:00")
    check("DTM-003 'rīt vakarā' takes the evening default",
          _at("rīt vakarā") == "2026-08-27 19:00")

    check("DTM-004 'parīt 18:00' is two days out",
          _at("parīt 18:00") == "2026-08-28 18:00")
    check("DTM-004 'parīt' is not read as 'rīt' with a prefix",
          _at("parīt 18:00") != _at("rīt 18:00"))
    check("DTM-004 'vakar' is refused as a future time",
          _at("vakar 10:00") == "")
    check("DTM-004 ...and is resolvable when the past is allowed",
          _at("vakar 10:00", allow_past=True) == "2026-08-25 10:00")

    check("DTM-005 'piektdien 12:00' is the coming Friday",
          _at("piektdien 12:00") == "2026-08-28 12:00")
    check("DTM-005 'nākampiektdien' is the one after that",
          _at("nākampiektdien 12:00") == "2026-09-04 12:00")
    check("DTM-005 'pirmdien 09:00' skips the weekend",
          _at("pirmdien 09:00") == "2026-08-31 09:00")

    check("DTM-006 '15. septembrī 14:00' is this September",
          _at("15. septembrī 14:00") == "2026-09-15 14:00")
    check("DTM-006 '15.09 14:00' is the same day",
          _at("15.09 14:00") == "2026-09-15 14:00")
    check("DTM-006 an ISO datetime is taken as written",
          _at("2026-09-15 14:00") == "2026-09-15 14:00")
    # A date with no year that has already gone by means NEXT year, or every
    # January booking made in autumn lands nine months in the past.
    check("DTM-006 '15. janvārī' with no year rolls forward, never back",
          _at("15. janvārī 10:00") == "2027-01-15 10:00")
    check("DTM-006 '15.01 10:00' rolls forward the same way",
          _at("15.01 10:00") == "2027-01-15 10:00")

    # ---- DTM-007: a day with no clock time is not silently 00:00 -----------
    # It is refused with `missing_time`, which is what lets calendar_search
    # widen it to the whole day instead of booking midnight.
    bare = T.normalize_datetime("rīt", now=NOW, tz_name="Europe/Riga")
    check("DTM-007 a bare day is not invented into a time",
          not bare.ok and bare.error_type == "missing_time")

    # ---- DTM-008: "what have I got tomorrow" covers the whole day ----------
    from app import personal_assistant as PA
    start, end = PA._calendar_search_range({"when": "rīt"}, NOW, "Europe/Riga")
    check("DTM-008 a bare 'rīt' search starts at the top of tomorrow",
          start.ok and start.dt.strftime("%Y-%m-%d %H:%M") == "2026-08-27 00:00")
    check("DTM-008 ...and ends after tomorrow, not at midday",
          end.ok and end.dt > start.dt + timedelta(hours=23))
    plain_start, plain_end = PA._calendar_search_range({}, NOW, "Europe/Riga")
    check("DTM-008 with nothing asked, the window is now plus a week",
          plain_start.dt == NOW
          and plain_end.dt.strftime("%Y-%m-%d") == "2026-09-02")

    check("DTM-009 nothing resolved against the wall clock by accident",
          _at("šodien 15:00").startswith("2026-08-26"))

    print("date / time awareness")
    print("=" * 72)
    for label, ok in checks:
        print("  %s  %s" % ("PASS" if ok else "FAIL", label))
    bad = [l for l, ok in checks if not ok]
    print("=" * 72)
    print("%d/%d passed" % (len(checks) - len(bad), len(checks)))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
