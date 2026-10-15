"""The model is told WHEN the conversation it sees was written.

No model runs; the history is a stand-in.

    python tests/history_timeline_cases.py
"""
import os
import sys
import time
from datetime import datetime
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import messaging as M  # noqa: E402

CONTACT = {"assistant_chats": [], "reply_persona": "ai", "contact_names": {},
           "contact_notes": {}}


def _messages(acfg, chat, history, now):
    entries = [{"sender": "partner", "text": "Cikos šodien saule norietēs?", "ts": now}]
    with patch.object(M, "_bb_fetch_history", side_effect=RuntimeError("offline")), \
            patch.object(M, "_chat_history_messages", return_value=history):
        return M._build_reply_messages(acfg, {}, entries, chat)


def _stamp(ts):
    return datetime.fromtimestamp(ts).strftime("%d.%m. %H:%M")


def test_the_prompt_says_when_the_conversation_was_written():
    now = time.time()
    old, prev = now - 2 * 86400 - 3600, now - 18 * 3600
    history = [
        {"role": "assistant", "origin": "manual", "content": "Es sēžu ārā",
         "ts": old, "sender": "partner"},
        {"role": "user", "origin": "contact", "content": "Wczasowa 1, Warsaw",
         "ts": prev, "sender": "partner"},
    ]
    msgs = _messages(CONTACT, "iMessage;-;partner", history, now)
    notes = "\n".join(m["content"] for m in msgs if m["role"] == "system")
    assert _stamp(old) in notes and _stamp(prev) in notes, notes[-600:]
    assert "%s (%s) «Es sēžu ārā»" % (_stamp(old), M._rel_time_lv(now - old, now)) in notes, \
        notes[-600:]
    # the turns themselves stay free of times, so there is nothing to copy
    for m in msgs:
        if m["role"] != "system":
            assert _stamp(old) not in m["content"] and _stamp(prev) not in m["content"], m


def test_the_self_chat_gets_it_in_english():
    now = time.time()
    history = [{"role": "user", "origin": "manual", "content": "sveiks",
                "ts": now - 3 * 3600, "sender": "owner"}]
    msgs = _messages({**CONTACT, "assistant_chats": ["iMessage;-;owner"]},
                     "iMessage;-;owner", history, now)
    notes = "\n".join(m["content"] for m in msgs if m["role"] == "system")
    assert "When this was written" in notes and _stamp(now - 3 * 3600) in notes, notes[-600:]


def test_ago_counts_calendar_days_not_hours():
    now = datetime(2026, 9, 28, 17, 48).timestamp()
    assert M._rel_time_lv(now - datetime(2026, 9, 28, 17, 48).timestamp() + 30) == "tikko"
    # 23:34 last night is "vakar", not "šodien agrāk" -- only 18 hours, but a day
    assert M._rel_time_lv(now - datetime(2026, 9, 27, 23, 34).timestamp(), now) == "vakar"
    assert M._rel_time_lv(now - datetime(2026, 9, 26, 13, 14).timestamp(), now) == "pirms 2 dienām"
    assert M._rel_time_lv(now - datetime(2026, 9, 28, 14, 0).timestamp(), now) == "šodien, pirms 3 h"


if __name__ == "__main__":
    test_the_prompt_says_when_the_conversation_was_written()
    test_the_self_chat_gets_it_in_english()
    test_ago_counts_calendar_days_not_hours()
    print("OK")
