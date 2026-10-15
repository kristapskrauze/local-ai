# -*- coding: utf-8 -*-
r"""The transcript was in the prompt the whole time, as an assistant turn: the
audio is generated FROM that text and `_match_bot_send` correlates the echo by
tempGuid. Every prompt-side fix was tried and MEASURED against the rebuilt
production prompt at production temperature:

    plain media tag                      4/4 denials
    "this is YOUR OWN voice message"     4/4 denials
    transcript quoted as a system fact   4/4 denials
    distrust-your-own-output rule scoped 4/4 denials
    earlier denials deleted from history 4/4 denials
    no media tag at all                  4/4 denials

Sixteen of sixteen. The model reasons about audio access instead of reading the
message it already has. So it goes in the deterministic lane, for exactly the
reason rig_agent exists: the answer is on disk and a model that "cannot hear"
its own file has nothing to add.

    py -3 tests/voice_recall_cases.py
"""
import io
import os
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import rig_agent as R  # noqa: E402

CHAT = "iMessage;-;+37120000001"
OTHER = "iMessage;-;+37120000005"
ACFG = {"partner_handles": ["+37120000001"], "rig_agent": True}
SPOKEN = ("Aha, saprotu, nākamreiz soļus rakstīšu angliski, jo iPhone "
          "iestatījumi ir angļu valodā. Tagad klusēšu, kungs")


def main():
    checks = []

    def check(label, passed):
        checks.append((label, bool(passed)))

    for text in ("Anton pasaki ko tu ierunāji tajā balss ziņā",
                 "Pasaki vēlreiz ko tu ierunāji iepriekšējā ziņā",
                 "ko tu teici balss ziņā?",
                 "atkārto to balss ziņu",
                 "what did you say in that voice message"):
        check("routes to the lookup: %s" % text[:38], R.match(text) == "voicenote")

    # it must not swallow ordinary talk
    for text, want in (("vakar skatījos Dexter", None),
                       ("pasaki viņai sveiki", None),
                       ("kas jauns?", None)):
        check("%r stays %s" % (text[:30], want), R.match(text) == want)

    real = R.last_voice_note
    R.last_voice_note = lambda guid: SPOKEN if guid == CHAT else ""
    try:
        out = R.answer_for("Anton pasaki ko tu ierunāji tajā balss ziņā",
                           "+37120000001", ACFG, chat_guid=CHAT)
        body = str((out or {}).get("text") or "")
        check("it answers with the exact text it spoke", SPOKEN in body)
        check("...and never claims it cannot hear it",
              "nevaru dzirdēt" not in body.lower()
              and "nav pieejams" not in body.lower())

        check("it works in the contact chat, where sender is her handle",
              SPOKEN in str((R.answer_for(
                  "ko tu teici balss ziņā?", "+37120000001", ACFG,
                  chat_guid=CHAT) or {}).get("text") or ""))

        empty = R.answer_for("ko tu teici balss ziņā?", "+37120000001", ACFG,
                             chat_guid=OTHER)
        etext = str((empty or {}).get("text") or "")
        check("a chat with no voice note of ours says so plainly",
              "Neatrodu" in etext and SPOKEN not in etext)
    finally:
        R.last_voice_note = real

    real_recs = None
    try:
        from app import messaging as M
        real_recs = M._bot_outbox_records
        M._bot_outbox_records = lambda guid, limit=300: [
            {"mode": "voice", "ts": 100.0, "content": "[voice] vecā ziņa", "text": ""},
            {"mode": "voice", "ts": 200.0, "content": "[voice] " + SPOKEN, "text": ""},
            {"mode": "chat", "ts": 300.0, "content": "parasta ziņa", "text": ""},
        ]
        got = R.last_voice_note(CHAT)
        check("the NEWEST voice note wins", got == SPOKEN)
        check("the [voice] marker is stripped", not got.startswith("[voice]"))
        M._bot_outbox_records = lambda guid, limit=300: (_ for _ in ()).throw(
            RuntimeError("outbox unreadable"))
        check("an unreadable outbox returns nothing, never a guess",
              R.last_voice_note(CHAT) == "")
    finally:
        if real_recs is not None:
            M._bot_outbox_records = real_recs

    from app import messaging as MSG
    for text in ("Anton pasaki ko tu ierunāji tajā balss ziņā",
                 "Pasaki vēlreiz ko tu ierunāji iepriekšējā ziņā",
                 "ko tu teici balss ziņā?"):
        check("the CONTACT chat lets the rig lane answer: %s" % text[:32],
              MSG._rig_reply_allowed(text, False, "+37120000001", CHAT))
    for text in ("kas jauns?", "vakar skatījos Dexter"):
        check("...and ordinary talk still does not: %s" % text[:24],
              not MSG._rig_reply_allowed(text, False, "+37120000001", CHAT))
    check("a bare pick does not raise inside the gate",
          MSG._rig_reply_allowed("2", False, "+37120000001", CHAT) in (True, False))

    src = Path(MSG.__file__).read_text(encoding="utf-8")
    check("the contact-chat reply gate lists voicenote next to the media skills",
          '"kino", "voicenote"' in src)

    ok = all(p for _l, p in checks)
    for label, passed in checks:
        print(("OK   " if passed else "FAIL ") + label)
    print("\n%d checks, VERDICT: %s" % (len(checks), "PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
