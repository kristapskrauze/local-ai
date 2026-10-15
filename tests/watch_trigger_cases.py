r"""Adversarial cases for the "watch" skill trigger.

Written before trusting the regex, because this is exactly where rig_agent has
been bitten twice: "cik ilgi" claimed general-knowledge questions, and the image
trigger claimed pizza. The adjacent category here is TALKING ABOUT films — "vakar
redzēju labu filmu" is conversation, not a request for a recommendation.

Also guards the ordering trap: "services" already claims the word `jellyfin` and
"status" claims a bare "kā iet", so a film question must be matched BEFORE them.

    py tests\watch_trigger_cases.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from app import rig_agent as RA  # noqa: E402

CASES = [
    # --- SHOULD route to "watch"
    ("ko šovakar skatīties?", "watch"),
    ("ko paskatīties vakarā?", "watch"),
    ("iesaki kādu filmu", "watch"),
    ("iesaki kādu seriālu", "watch"),
    ("piedāvā kaut ko skatīties", "watch"),
    ("kaut ko paskatīties šovakar?", "watch"),
    ("what to watch tonight?", "watch"),
    ("suggest a movie", "watch"),
    ("recommend a series", "watch"),
    ("/watch", "watch"),

    # --- must NOT be a recommendation request: talking ABOUT films
    ("vakar redzēju labu filmu", None),
    ("tā filma bija garlaicīga", None),
    ("man patika tas seriāls", None),
    ("skatījos to filmu jau", None),

    # --- must NOT be stolen from the other skills
    ("vai jellyfin strādā?", "services"),
    ("kā iet ar rigu?", "status"),
    # "gpu" is listed before "temps" and reports temperature itself — this is
    # pre-existing behaviour, asserted so a later reorder is noticed
    ("cik grādi gpu?", "gpu"),
    ("cik brīvas vietas uz diska?", "disks"),

    # --- ordinary chat must match nothing at all
    ("kā tev iet?", None),
    ("mīļā, es mājās", None),
    ("ko dari?", None),
]

if __name__ == "__main__":
    bad = 0
    for text, want in CASES:
        got = RA.match(text)
        ok = got == want
        if not ok:
            bad += 1
        print("  %-4s %-40s want=%-9s got=%s"
              % ("ok" if ok else "FAIL", text[:40], want, got))
    print()
    if bad:
        print("VERDICT: FAIL — %d/%d" % (bad, len(CASES)))
        sys.exit(1)
    print("VERDICT: PASS — %d cases" % len(CASES))
