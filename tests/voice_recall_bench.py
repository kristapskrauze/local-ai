# -*- coding: utf-8 -*-
r"""Nothing was missing. The prompt already held the answer as an assistant turn —
"Aha, saprotu, nākamreiz soļus rakstīšu angliski ... Tagad klusēšu, kungs" —
because the voice note is generated FROM that text and `_match_bot_send`
correlated it by tempGuid. What the turn also carried was a bare
"[media: ... balss.m4a ...]" tag, and the model read its own message as
somebody else's audio file.

    py -3 tests/voice_recall_bench.py
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

from app import messaging as M  # noqa: E402

OLLAMA = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
MODEL = os.environ.get("QWEN_AGENT_TEST_MODEL", "qwen3.8:latest")

SPOKEN = ("Aha, saprotu, nākamreiz soļus rakstīšu angliski, jo iPhone "
          "iestatījumi ir angļu valodā. Tagad klusēšu, kungs")
DENIALS = ("nevaru dzirdēt", "nav pieejams", "nevaru atskaņot", "nedzirdu",
           "nav piekļuves", "cannot hear", "not available to me")


def ask(messages):
    body = {"model": MODEL, "think": False, "messages": messages, "stream": False,
            "options": {"temperature": 0, "num_predict": 200, "num_ctx": 8192}}
    req = urllib.request.Request(
        OLLAMA.rstrip("/") + "/api/chat",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as r:
        d = json.loads(r.read().decode("utf-8", "replace"))
    return " ".join(str((d.get("message") or {}).get("content") or "").split())


CHAT = "iMessage;-;+37120000001"
TRIGGER = "Anton pasaki ko tu ierunāji tajā balss ziņā"


def real_prompt():
    """The prompt production actually built for that turn — 37 turns, not four."""
    cfg = M.load_config()
    acfg = cfg.get("auto_reply") or {}
    pcfg = cfg.get("providers") or cfg.get("bluebubbles") or {}
    entries = []
    inbox = Path(M.INBOX_PATH)
    with inbox.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if TRIGGER in str(rec.get("text") or ""):
                entries = [rec]
    if not entries:
        return None
    return M._build_reply_messages(acfg, pcfg, entries, CHAT)


def build(note: str) -> list:
    """The real prompt, with the own-voice note swapped for `note`."""
    msgs = real_prompt()
    if not msgs:
        raise RuntimeError("could not rebuild the real prompt")
    live_note = M._media_context_note(
        [{"guid": "x", "mime": "audio/x-m4a", "name": "balss.m4a"}],
        0, SPOKEN, "voice", "bot")
    out = []
    for m in msgs:
        c = str(m.get("content") or "")
        if live_note and live_note[:40] in c:
            c = c.replace(live_note, note)
        item = dict(m)
        item["content"] = c
        out.append(item)
    while out and out[-1].get("role") == "assistant":
        out.pop()
    return out


OLD_NOTE = "[media: attachment names/types: balss.m4a; attachment refs: att_75554cbefbd1(audio: balss.m4a)]"


def main() -> int:
    # the note the code produces today, for the real attachment
    atts = [{"guid": "69F4AD4A", "mime": "audio/x-m4a", "name": "balss.m4a"}]
    new_note = M._media_context_note(atts, 0, SPOKEN, "voice", "bot")
    print("model=%s\n" % MODEL)
    print("note now: %s\n" % new_note[:150])

    checks = []
    for label, note in (("BEFORE (bare media tag)", OLD_NOTE),
                        ("AFTER  (own-voice note)", new_note)):
        started = time.time()
        try:
            out = ask(build(note))
        except Exception as exc:
            print("%-24s ERROR %s" % (label, str(exc)[:70]))
            continue
        low = out.lower()
        denied = any(d in low for d in DENIALS)
        recalled = ("angliski" in low or "klusēšu" in low or "klusesu" in low)
        if label.startswith("AFTER"):
            checks.append(("it does not claim it cannot hear its own voice note",
                           not denied))
            checks.append(("it says what it actually said", recalled))
        print("%-24s %4.1fs denied=%-5s recalled=%-5s %s"
              % (label, time.time() - started, denied, recalled, out[:150]))

    print()
    ok = all(p for _l, p in checks)
    for lbl, passed in checks:
        print(("OK   " if passed else "FAIL ") + lbl)
    print("\n%d checks, VERDICT: %s" % (len(checks), "PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
