# -*- coding: utf-8 -*-
r"""Live model: does a standing instruction hold, and does invention stop?

The first is a memory failure, the second an invention. The directive snippet
addresses both: the instruction is injected every turn, and it carries an
explicit "only say what is here or in the conversation" line.

    py -3 tests/qwen_directive_bench.py
"""
from __future__ import annotations

import io
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.people import OWNER, OWNER_ACC, OWNER_GEN, PARTNER
from app import messaging as M  # noqa: E402

OLLAMA = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
MODEL = os.environ.get("QWEN_AGENT_TEST_MODEL", "qwen3.8:latest")

CASES = [
    {"id": "kungs-plain", "turn": "Sveiks, kā iet?",
     "want": "kungs", "why": "the standing instruction must hold on any turn"},
    {"id": "kungs-task", "turn": "Cik brīvas vietas uz diska?",
     "want": "kungs", "why": "including when he asks something ordinary"},
    {"id": "no-invention", "turn": "Kāda ir mana mīļākā filma?",
     "want": None, "why": "it must not invent a favourite film"},
]

INVENTED = ("gudrā blondīne", "gudra blondine", "legally blonde", "mīļākā filma ir",
            "milaka filma ir", "tava mīļākā filma ir")


BLONDE_TURNS = [
    {"role": "user", "content": "Anton, šovakar skatījāmies Gudrā blondīne"},
    {"role": "assistant", "content": "Laba izvēle: Gudrā Blondīne (Legally Blonde, 2001)."},
    {"role": "user", "content": "forši, paldies!"},
]


def ask_turns(system: str, turns: list) -> str:
    body = {
        "model": MODEL,
        "think": False,
        "messages": [{"role": "system", "content": system}] + list(turns),
        "stream": False,
        "options": {"temperature": 0, "num_predict": 160, "num_ctx": 8192},
    }
    req = urllib.request.Request(
        OLLAMA.rstrip("/") + "/api/chat",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as r:
        d = json.loads(r.read().decode("utf-8", "replace"))
    return str((d.get("message") or {}).get("content") or "")


def ask(system: str, turn: str) -> str:
    body = {
        "model": MODEL,
        # think=False, exactly as the live lane sends it. Without it the model
        # spends its budget on reasoning tokens and returns an EMPTY message —
        # which is how the first run of this bench "failed" a case it had never
        # actually answered.
        "think": False,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": turn}],
        "stream": False,
        "options": {"temperature": 0, "num_predict": 160, "num_ctx": 8192},
    }
    req = urllib.request.Request(
        OLLAMA.rstrip("/") + "/api/chat",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as r:
        d = json.loads(r.read().decode("utf-8", "replace"))
    return str((d.get("message") or {}).get("content") or "")


def build_system(with_directives: bool) -> str:
    """The live prompt, not an approximation of it."""
    system = M._AI_PERSONA
    system += "\n\n" + M._live_reply_voice(True)
    system += (f"\n\n## Ar ko tu šobrīd sarakstīes\n{PARTNER} (+37120000001)"
               f"\n\nBET ŠO ZIŅU uzrakstīja {OWNER}, nevis {PARTNER}. Atbildi VIŅAM —"
               f" nerunā ar viņu tā, it kā rakstītu {PARTNER}.")
    if with_directives:
        directives = M._directives_snippet()
        if directives:
            system += (f"\n\n## {OWNER_GEN} pastāvīgie norādījumi — ievēro tos vienmēr\n"
                       + directives
                       + f"\n\nŠie ir {OWNER_GEN.upper()} norādījumi par sevi. Uzrunas formu, ko"
                         " viņš lūdzis, lieto TIKAI tad, kad raksta viņš pats. Ja šo"
                         " ziņu rakstīja kāds cits, uzrunā viņu parasti — nesauc"
                         f" sarunbiedru tā, kā {OWNER} lūdza saukt sevi."
                         f"\n\nPar {OWNER_ACC} stāsti TIKAI to, kas ir šeit vai sarunā."
                         " Neizdomā viņa viedokli, gaumi vai vārdus.")
    return system


def main() -> int:
    checks = []
    print("model=%s\n" % MODEL)
    for with_dir in (False, True):
        label = "WITH directives" if with_dir else "baseline (no directives)"
        system = build_system(with_dir)
        print("=== %s" % label)
        for case in CASES:
            started = time.time()
            try:
                out = " ".join(ask(system, f"{OWNER}: " + case["turn"]).split())
            except Exception as exc:
                print("   %-14s ERROR %s" % (case["id"], str(exc)[:70]))
                continue
            low = out.lower()
            if case["want"]:
                got = case["want"] in low
            else:
                got = not any(bad in low for bad in INVENTED)
            if with_dir:
                checks.append(("%s — %s" % (case["id"], case["why"]), got))
            print("   %-14s %-5s %4.1fs %s" % (
                case["id"], "OK" if got else "FAIL", time.time() - started, out[:110]))
        print()

    print(f"=== the Legally Blonde claim (download confirmation to {PARTNER})")
    for with_dir in (False, True):
        system = build_system(with_dir).replace(
            f"\n\nBET ŠO ZIŅU uzrakstīja {OWNER}, nevis {PARTNER}. Atbildi VIŅAM —"
            f" nerunā ar viņu tā, it kā rakstītu {PARTNER}.", "")
        try:
            out = " ".join(ask_turns(system, BLONDE_TURNS).split())
        except Exception as exc:
            print("   %-24s ERROR %s" % ("with" if with_dir else "baseline",
                                         str(exc)[:60]))
            continue
        low = out.lower()
        invented = any(w in low for w in
                       ("mīļāk", "milak", "viņš teica", "vins teica", "sajūsm",
                        "sajusm", "viņam patīk", "vinam patik"))
        called_her_kungs = "kungs" in low
        if with_dir:
            checks.append((f"blonde-claim — it must not invent what {OWNER} "
                           "said or likes", not invented))
            checks.append((f"blonde-claim — {PARTNER} must not be called 'kungs'",
                           not called_her_kungs))
        print("   %-9s %-5s %s" % ("with" if with_dir else "baseline",
                                   "OK" if not invented else "FAIL", out[:130]))
    print()

    ok = all(p for _l, p in checks)
    for lbl, passed in checks:
        print(("OK   " if passed else "FAIL ") + lbl)
    print("\n%d checks, VERDICT: %s" % (len(checks), "PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
