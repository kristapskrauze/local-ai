# -*- coding: utf-8 -*-
"""Guard the per-observation clip limit in app/investigate.py."""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.investigate import MAX_OBSERVATION_CHARS


def test_max_observation_chars_is_8000():
    assert MAX_OBSERVATION_CHARS == 8000, (
        "MAX_OBSERVATION_CHARS is %r, expected 8000" % (MAX_OBSERVATION_CHARS,)
    )


if __name__ == "__main__":
    test_max_observation_chars_is_8000()
    print("OK")
