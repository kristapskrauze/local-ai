# -*- coding: utf-8 -*-
"""Which digest story does a follow-up question mean — and when is it none?

Runs against a FIXED story set, not the live digest, so it means the same
thing tomorrow.

    py tests\\digest_match_cases.py
"""
import io
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import messaging as M  # noqa: E402

STORIES = [
    {"n": 1, "title": "Divi no Klaipēdas zoodārza izņemtie Amūras tīģeri atraduši jaunas mājas «Siguldas Zoo»", "shown": ""},
    {"n": 2, "title": "Granulu netrūkst, bet lielais pieprasījums ceļ cenu", "shown": ""},
    {"n": 3, "title": "Plāno mainīt NMPD reaģēšanas kārtību un ieviest jaunus valsts apmaksātus pakalpojumus", "shown": ""},
    {"n": 4, "title": "Jaunas detaļas par uzbrukumu ārstam Jēkabpils reģionālajā slimnīcā", "shown": ""},
    {"n": 5, "title": "Šķēpmetējs Gailums ar izcīnīto bronzu Eiropas čempionātā", "shown": ""},
    {"n": 6, "title": "Šī ir vēsturiska diena Latvijas basketbolam! U-16 puiši izcīna zeltu", "shown": ""},
    {"n": 7, "title": "Jauns pētījums atklāj trīs lietas, kas demenci var attālināt par 13 gadiem", "shown": ""},
    {"n": 8, "title": "Laikraksts: ASV mudina „Apple” neiepirkt no Ķīnas mikroshēmas", "shown": ""},
]

# (question, expected story n or None)
CASES = [
    # naming the subject picks that story
    ("pastāsti vairāk par tīģeriem", 1),
    ("Ko nozīmē tas ar zoodārzu?", 1),
    ("Kas tur ar granulām?", 2),
    ("par NMPD pastāsti", 3),
    ("Kas notika ar to ārstu Jēkabpilī?", 4),
    ("pastāsti par Apple un Ķīnu", 8),
    # by number, which is the other supported way
    ("pastāsti vairāk par 7", 7),
    # THE BUG: the question's own idiom must never be what matches
    ("Ir kaut kas jauns par Ukrainu?", None),
    ("Kas jauns par airBaltic?", None),
    ("Kādas šodien ziņas?", None),
    ("pastāsti vairāk", None),
    ("kas jauns?", None),
]


def main():
    original = M._load_digest_state
    M._load_digest_state = lambda: STORIES      # fixed set, not today's digest
    try:
        ok = bad = 0
        for question, want in CASES:
            got = M._match_digest_story(question)
            got_n = (got or {}).get("n")
            passed = got_n == want
            ok += passed
            bad += (not passed)
            print("%-5s %-38s want %-5s got %-5s %s"
                  % ("ok" if passed else "FAIL", question[:36],
                     want if want else "none", got_n if got_n else "none",
                     "" if passed else "<-- " + str((got or {}).get("title"))[:40]))
    finally:
        M._load_digest_state = original
    print("-" * 78)
    print("VERDICT: %s — %d/%d" % ("PASS" if not bad else "FAIL", ok, ok + bad))
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
