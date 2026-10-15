# -*- coding: utf-8 -*-
r"""Does a question reach the statute? The MODEL decides, not a word list.

    "Anton kad ir izdevīgākais periods iet prom no darba ar abpusējo
     vienošanos, lai bezdarbnieka pabalstu izmaksātu laicīgāk?"

Adding "bezdarbnieks" and "pabalsts" to the pattern would just move the edge to
the next unlisted word. The gate is a model call now, with the old pattern kept
as the fallback for when the model cannot be reached — so this can only widen
what reaches the statute, never narrow it.

    py -3 tests/law_gate_cases.py            # offline: fallback + wiring
    py -3 tests/law_gate_cases.py --live     # also asks the real model
"""
import io
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import messaging as M  # noqa: E402

LIVE = "--live" in sys.argv

# (question, should the statute be consulted, label)
CASES = [
    ("Anton kad ir izdevīgākais periods iet prom no darba ar abpusējo vienošanos, "
     "lai bezdarbnieka pabalstu izmaksātu laicīgāk?", True, "the live failure"),
    ("Tad ja es aizeju 14tajā tad saņemu 25tajā pabalstu par 11 dienām?", True,
     "its follow-up"),
    ("cik ilgs ir ikgadējais atvaļinājums?", True, "leave length"),
    ("cik ilgi maksā slimības lapu?", True, "sick pay"),
    ("vai darba devējs drīkst atlaist grūtnieci?", True, "dismissal"),
    ("cik liels ir sods par ātruma pārsniegšanu?", True, "fine"),
    ("kāds laiks rīt Rīgā?", False, "weather"),
    ("ko tu ierunāji tajā balss ziņā?", False, "the voice note"),
    ("kā tev iet?", False, "small talk"),
]


def main():
    checks = []

    def check(label, passed):
        checks.append((label, bool(passed)))

    check("the statute lane takes acfg, so it can ask the model",
          "acfg" in M._likumi_material.__code__.co_varnames)
    check("_grounded_material passes it through",
          "acfg=acfg" in open(M.__file__, encoding="utf-8").read())

    real_chat = M._ollama_chat
    M._ollama_chat = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no model"))
    M._LAW_WANTED_CACHE.clear()
    try:
        check("model unreachable -> falls back to the pattern (a match)",
              M._likumi_wanted({}, "vai darba devējs drīkst atlaist grūtnieci?"))
        M._LAW_WANTED_CACHE.clear()
        check("model unreachable -> falls back to the pattern (a miss)",
              not M._likumi_wanted({}, "kāds laiks rīt Rīgā?"))
    finally:
        M._ollama_chat = real_chat
        M._LAW_WANTED_CACHE.clear()

    # a stubbed verdict must be honoured in both directions
    M._ollama_chat = lambda *a, **k: "STATUTE"
    M._LAW_WANTED_CACHE.clear()
    try:
        check("a STATUTE verdict opens the lane for a question the regex missed",
              M._likumi_wanted({}, "kad sāk maksāt bezdarbnieka pabalstu?"))
    finally:
        M._ollama_chat = real_chat
        M._LAW_WANTED_CACHE.clear()
    M._ollama_chat = lambda *a, **k: "OTHER"
    M._LAW_WANTED_CACHE.clear()
    try:
        check("an OTHER verdict keeps chat out of the statute lane",
              not M._likumi_wanted({}, "kā tev iet?"))
    finally:
        M._ollama_chat = real_chat
        M._LAW_WANTED_CACHE.clear()

    if LIVE:
        cfg = M.load_config()
        acfg = cfg.get("auto_reply") or {}
        M._LAW_WANTED_CACHE.clear()
        for question, want, label in CASES:
            got = M._likumi_wanted(acfg, question)
            check("live: %-14s -> %s" % (label, "statute" if want else "not"),
                  got == want)
    else:
        print("(offline run — pass --live to ask the real model)\n")

    ok = all(p for _l, p in checks)
    for label, passed in checks:
        print(("OK   " if passed else "FAIL ") + label)
    print("\n%d checks, VERDICT: %s" % (len(checks), "PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
