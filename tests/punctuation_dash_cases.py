# -*- coding: utf-8 -*-
"""The dash-to-comma rule must not rewrite minus signs or ranges.

Runs through the real _polish_reply, not a copy of the pattern, so it fails if
the rule moves or is rewritten. Offline, no model call.

    py tests/punctuation_dash_cases.py
"""
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import messaging as M                   # noqa: E402
from app.messaging import _polish_reply          # noqa: E402

# (input, must appear in the output, why)
CASES = [
    ("24 - 17 = 7 GB paliek brīvi.", "24 - 17",
     "a minus sign between numbers is arithmetic, not punctuation"),
    ("Sacensības notiks 2005 - 2010 gados.", "2005 - 2010",
     "a year range is not punctuation either"),
    ("Temperatūra bija 5 - 10 grādi.", "5 - 10",
     "a numeric range in prose survives"),
    ("Latvija - skaista valsts.", "Latvija,",
     "a dash between words is still tidied into a comma"),
    ("Rīga - galvaspilsēta.", "Rīga,",
     "the prose rule keeps working"),
]


def main():
    failed = 0
    for text, want, why in CASES:
        with patch.object(M, "_guards_on", return_value=True):
            out = _polish_reply(text, None) or ""
        ok = want in out
        failed += 0 if ok else 1
        print("%-4s %-38r -> %-34r  %s"
              % ("OK" if ok else "FAIL", text, out, why))
    print("\n%d case(s), %d failed" % (len(CASES), failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
