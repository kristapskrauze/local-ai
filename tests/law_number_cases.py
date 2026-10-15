# -*- coding: utf-8 -*-
r"""A legal quantity must come from the statute, or it does not ship.

    "Abpusēja vienošanās ir 113. panta 2. daļa ... (108. pants)"
    "VSAA nav jāveic 30 dienu novērošanas periods"
    "nav 7 dienu pārejas perioda"          <- a different made-up number
    "80% no vidējās algas"

None of it exists. Retrieval had been fixed by then — "ne agrāk kā divus
mēnešus" was sitting in its material — so this is a GROUNDING failure, not a
lookup one, and a wrong number in a legal answer is worse than "nezinu".

Same contract as _drop_unvouched_years: sentence-level, and it only runs on a
turn that actually retrieved statute text, so with nothing to vouch against
nothing is dropped.

    py -3 tests/law_number_cases.py
"""
import io
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import messaging as M  # noqa: E402

# the real wording from "Par apdrošināšanu bezdarba gadījumam"
STATUTE = (
    "bet ne agrāk kā divus mēnešus pēc bezdarbnieka statusa iegūšanas dienas. "
    "Bezdarbnieka pabalsta izmaksas ilgums ir astoņi mēneši 12 mēnešu periodā. "
    "nākamos divus mēnešus — 75 procentu apmērā no piešķirtā bezdarbnieka pabalsta")


def main():
    checks = []

    def check(label, passed):
        checks.append((label, bool(passed)))

    real = getattr(M._INFLIGHT_INCOMING, "law_text", "")

    M._INFLIGHT_INCOMING.law_text = ""
    invented = "Nav 7 dienu pārejas perioda, kungs"
    check("with nothing retrieved it drops nothing",
          M._drop_unvouched_law_numbers(invented) == invented)

    M._INFLIGHT_INCOMING.law_text = STATUTE
    try:
        out = M._drop_unvouched_law_numbers(
            "Pabalstu maksā astoņi mēneši\nNav 7 dienu pārejas perioda")
        check("an invented '7 dienu' period is dropped", "7 dienu" not in out)
        check("...and the grounded line survives", "astoņi mēneši" in out)

        out = M._drop_unvouched_law_numbers("Ne agrāk kā divus mēnešus pēc statusa")
        check("a number the statute spells in WORDS is vouched",
              "divus mēnešus" in out)

        out = M._drop_unvouched_law_numbers("Tas ir 75 procenti no pabalsta")
        check("a percentage the statute states survives", "75" in out)

        out = M._drop_unvouched_law_numbers("Tas ir 80% no vidējās algas")
        check("an invented 80% is dropped", "80" not in out)

        out = M._drop_unvouched_law_numbers("Abpusēja vienošanās ir 113. panta 2. daļa")
        check("an invented article number is dropped", "113" not in out)

        # a reply that would be emptied entirely is left to the regeneration
        # path rather than sent as nothing
        only_bad = "Nav 30 dienu novērošanas perioda"
        check("a wholly invented answer is emptied, which triggers a regenerate",
              M._drop_unvouched_law_numbers(only_bad).strip() == "")

        # ordinary chat carrying a number must not be touched
        M._INFLIGHT_INCOMING.law_text = STATUTE
        chat = "Velku 2 filmas, kungs"
        check("a non-legal number is not policed when it is in the statute",
              M._drop_unvouched_law_numbers(chat) == chat)
    finally:
        M._INFLIGHT_INCOMING.law_text = real

    ok = all(p for _l, p in checks)
    for label, passed in checks:
        print(("OK   " if passed else "FAIL ") + label)
    print("\n%d checks, VERDICT: %s" % (len(checks), "PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
