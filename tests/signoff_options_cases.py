"""The ready-made farewell lines that fit a given day: the options the
sign-off chooser is offered next to the model's own lines.

M._digest_signoff_options(hi, rain, mm, now) returns a list with no
duplicates: the weather lines whose condition holds today (the same
_CLOSERS_WEATHER rules as _digest_signoff), then the weekday's lines
(_CLOSERS_WEEKDAY), then the general ones (_DIGEST_CLOSERS).

No model or network call is made.

    python tests/signoff_options_cases.py
"""
import os
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from app import messaging as M  # noqa: E402

FRIDAY = datetime(2026, 9, 18, 9, 0)
SUNDAY = datetime(2026, 9, 27, 9, 0)
UMBRELLA = "Paņem lietussargu, šodien līs! ☔"


def test_a_rainy_friday():
    opts = M._digest_signoff_options(hi=16, rain=60, mm=3.0, now=FRIDAY)
    assert "Lai jauka piektdiena! Nedēļa galā 🍻" in opts, opts
    assert "Piektdiena! 🎉" in opts, opts
    assert UMBRELLA in opts, opts
    for line in M._DIGEST_CLOSERS:
        assert line in opts, (line, opts)


def test_no_rain_means_no_umbrella():
    opts = M._digest_signoff_options(hi=16, rain=0, mm=0.0, now=FRIDAY)
    assert UMBRELLA not in opts, opts


def test_a_hot_day_gets_the_heat_line():
    opts = M._digest_signoff_options(hi=30, rain=0, mm=0.0, now=FRIDAY)
    assert "Nesakarsti! 🥵" in opts, opts


def test_a_sunday_gets_sunday_lines_not_fridays():
    opts = M._digest_signoff_options(hi=15, rain=0, mm=0.0, now=SUNDAY)
    assert "Mierīgu svētdienu ☕" in opts, opts
    assert "Lai jauka piektdiena! Nedēļa galā 🍻" not in opts, opts


def test_no_duplicates():
    opts = M._digest_signoff_options(hi=16, rain=60, mm=3.0, now=FRIDAY)
    assert len(opts) == len(set(opts)), opts


if __name__ == "__main__":
    test_a_rainy_friday()
    test_no_rain_means_no_umbrella()
    test_a_hot_day_gets_the_heat_line()
    test_a_sunday_gets_sunday_lines_not_fridays()
    test_no_duplicates()
    print("OK")
