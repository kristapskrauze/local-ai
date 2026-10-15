# -*- coding: utf-8 -*-
r"""Bezdarbnieka pabalsts: the model reads the numbers, Python does the sum.

    7. pants   stāžs 1-9 -> 50%, 10-19 -> 55%, 20-29 -> 60%, 30+ -> 65%
    9. p. 3.d. months 1-2 full, 3-4 75%, 5-6 50%, 7-8 45%

    py -3 tests/unemployment_calc_cases.py
"""
import io
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import messaging as M  # noqa: E402


def main():
    checks = []

    def check(label, passed):
        checks.append((label, bool(passed)))

    for years, want in ((1, 50), (9, 50), (10, 55), (19, 55),
                        (20, 60), (29, 60), (30, 65), (44, 65)):
        check("stāžs %2d gadi -> %d%%" % (years, want),
              M._ub_share_for(years) == want)

    out = M._unemployment_estimate(1500, 12, "lv")
    check("1500 EUR / 12 gadi -> 55%", "55%" in out)
    check("...monthly base is 825 EUR", "825 EUR" in out)
    for step in ("619", "412", "371"):
        check("...the %s EUR step is shown" % step, step in out)
    check("...the eight-month total is stated", "4455" in out)
    check("...it is labelled an estimate, not a promise", "aplēse" in out)
    check("...and it links the statute", "likumi.lv/ta/id/14595" in out)

    # a different record moves the whole schedule
    out30 = M._unemployment_estimate(1000, 31, "lv")
    check("1000 EUR / 31 gadi -> 65% = 650 EUR", "65%" in out30 and "650 EUR" in out30)

    check("no wage, no sum", M._unemployment_estimate(0, 12) == "")
    check("junk in, nothing out", M._unemployment_estimate("alga", 12) == "")
    check("a missing record is not guessed", M._unemployment_estimate(1500, None) == "")

    share = M._unemployment_share_reply(25, "lv")
    check("stāžs alone still answers: 25 gadi -> 60%", "60%" in share)
    check("...and shows the whole band table", "1-9 gadi 50%" in share)
    check("...and the step-down schedule", "7.-8. mēnesī 45%" in share)

    unknown = M._statute_unknown_reply("lv")
    check("an unsourced law question refuses instead of inventing",
          "likumi.lv" in unknown and "neminēšu" in unknown)

    ok = all(p for _l, p in checks)
    for label, passed in checks:
        print(("OK   " if passed else "FAIL ") + label)
    print("\n%d checks, VERDICT: %s" % (len(checks), "PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
