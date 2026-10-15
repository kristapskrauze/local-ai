# -*- coding: utf-8 -*-
"""search_chat_history: correct speaker attribution, no duplicate rows.

Monkeypatches messaging.inbox/outbox to synthetic rows for the duration of
each case and restores them in a finally, so this never touches the real
history files.

    py -3 tests/search_chat_history_cases.py
"""
import io
import json
import os
import sys
import tempfile
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import agent_tools as AT  # noqa: E402
from app import messaging as M  # noqa: E402

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("    %-56s %s" % (name, "ok" if ok else "FAIL  " + str(detail)[:100]))


SELF_CHAT = "iMessage;-;owner@example.com"
CONTACT_CHAT = "iMessage;-;+37120000001"


def _patched(inbox_rows, outbox_rows):
    orig = (M.inbox, M.outbox, M.INBOX_PATH, M.OUTBOX_PATH)
    d = Path(tempfile.mkdtemp(prefix="chat_history_case_"))
    for name, rows in (("inbox.jsonl", inbox_rows), ("outbox.jsonl", outbox_rows)):
        (d / name).write_text("".join(json.dumps(r, ensure_ascii=False) + "\n"
                                      for r in rows), encoding="utf-8")
    M.inbox = lambda limit=1200: list(inbox_rows)
    M.outbox = lambda limit=1200: list(outbox_rows)
    M.INBOX_PATH, M.OUTBOX_PATH = d / "inbox.jsonl", d / "outbox.jsonl"

    class _Restore:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            M.inbox, M.outbox, M.INBOX_PATH, M.OUTBOX_PATH = orig
    return _Restore()


def case_self_chat_no_duplicate_bot_send():
    print("1. a bot send in the self-chat appears once, correctly labelled")
    inbox_rows = [{
        "text": "Darbs ir pabeigts, kungs.", "chat_guid": SELF_CHAT,
        "timestamp": "2026-09-04T07:42:09.900000+00:00",
        "is_from_me": True, "sender": "owner@example.com",
    }]
    outbox_rows = [{
        "text": "Darbs ir pabeigts, kungs.", "target": SELF_CHAT,
        "timestamp": "2026-09-04T07:42:09.300000+00:00",
        "status": "sent", "id": "out-1",
        "metadata": {"auto_reply": True},
    }]
    ctx = AT.AgentContext(allow_private=True, sender="owner@example.com",
                          conversation_id=SELF_CHAT, interface="bluebubbles_assistant")
    with _patched(inbox_rows, outbox_rows):
        r = AT.default_registry().execute(
            "search_chat_history", {"query": "pabeigts", "scope": "current_conversation"}, ctx)
    check("tool runs clean", r.get("ok"), r)
    matches = (r.get("result") or {}).get("matches") or []
    check("exactly one row, not two", len(matches) == 1, matches)
    if matches:
        check("labelled assistant, not contact or owner",
              matches[0]["speaker"] == "assistant", matches[0])


def case_self_chat_his_own_message_is_owner():
    print("")
    print("2. his own typed message is NOT labelled assistant")
    inbox_rows = [{
        "text": "Tad samaini savu riku budzetu kodā.", "chat_guid": SELF_CHAT,
        "timestamp": "2026-09-03T22:27:02.000000+00:00",
        "is_from_me": True, "sender": "owner@example.com",
    }]
    outbox_rows = []
    ctx = AT.AgentContext(allow_private=True, sender="owner@example.com",
                          conversation_id=SELF_CHAT, interface="bluebubbles_assistant")
    with _patched(inbox_rows, outbox_rows):
        r = AT.default_registry().execute(
            "search_chat_history", {"query": "budzetu", "scope": "current_conversation"}, ctx)
    matches = (r.get("result") or {}).get("matches") or []
    check("found the row", len(matches) == 1, matches)
    if matches:
        check("labelled owner, not assistant",
              matches[0]["speaker"] == "owner", matches[0])


def case_contact_chat_still_correct():
    print("")
    print("3. an ordinary contact chat: contact vs his manual reply")
    inbox_rows = [{
        "text": "Aizsuti to detalu uz servisu.", "chat_guid": CONTACT_CHAT,
        "timestamp": "2026-08-20T10:00:00+00:00",
        "is_from_me": False, "sender": "+37120000001",
    }, {
        "text": "Labi, tur veedu servisu.", "chat_guid": CONTACT_CHAT,
        "timestamp": "2026-08-20T10:05:00+00:00",
        "is_from_me": True, "sender": "+37120000001",
    }]
    outbox_rows = []
    ctx = AT.AgentContext(allow_private=True, sender="owner@example.com",
                          conversation_id=CONTACT_CHAT, interface="bluebubbles_assistant")
    with _patched(inbox_rows, outbox_rows):
        r = AT.default_registry().execute(
            "search_chat_history", {"query": "servisu", "scope": "current_conversation"}, ctx)
    matches = (r.get("result") or {}).get("matches") or []
    by_text = {m["text"]: m["speaker"] for m in matches}
    check("contact's line stays contact",
          by_text.get("Aizsuti to detalu uz servisu.") == "contact", by_text)
    check("his manual reply is owner, not falsely assistant",
          by_text.get("Labi, tur veedu servisu.") == "owner", by_text)


def main():
    print(__doc__.strip().splitlines()[0])
    print("")
    for fn in (case_self_chat_no_duplicate_bot_send,
               case_self_chat_his_own_message_is_owner,
               case_contact_chat_still_correct):
        try:
            fn()
        except Exception as exc:
            check(fn.__name__, False, "%s: %s" % (type(exc).__name__, exc))
    print("")
    print("%d passed, %d failed" % (len(PASS), len(FAIL)))
    for n in FAIL:
        print("   FAILED: %s" % n)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
