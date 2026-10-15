"""No model and no network: describe() and status() are stand-ins, and the chat
history is empty.

    python tests/location_context_cases.py
"""
import os
import sys
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app.people import OWNER
from app import location as L  # noqa: E402
from app import messaging as M  # noqa: E402

SELF = "iMessage;-;owner.test@example.com"
PARTNER_ID = "+37100000001"
FRIEND_ID = "+37100000002"
PLACE = "Zztestpils, Zztestija; kopš 28. septembra"
ACFG = {"assistant_chats": [SELF], "partner_handles": [PARTNER_ID], "reply_persona": "ai",
        "contact_names": {}, "contact_notes": {}}


FRESH = {"city": "Zztestpils", "country": "Zztestija"}


def _system(chat, sender, place=PLACE, status=None, latest=None):
    import time
    entries = [{"sender": sender, "text": "Kas jauns?", "is_from_me": chat == SELF}]
    if latest is None:
        latest = dict(FRESH, ts=time.time() - 600)
    with patch.object(M, "_bb_fetch_history", side_effect=RuntimeError("offline")), \
            patch.object(M, "_chat_history_messages", return_value=[]), \
            patch.object(L, "describe", return_value=place), \
            patch.object(L, "latest", return_value=latest), \
            patch.object(L, "status", return_value=status):
        msgs = M._build_reply_messages(dict(ACFG), {}, entries, chat)
    return "\n".join(m["content"] for m in msgs if m.get("role") == "system")


def test_his_own_chat_knows_where_he_is():
    assert "Zztestpils" in _system(SELF, "owner.test@example.com")


def test_partner_chat_knows_too():
    assert "Zztestpils" in _system("iMessage;-;" + PARTNER_ID, PARTNER_ID)


def test_no_other_chat_does():
    assert "Zztestpils" not in _system("iMessage;-;" + FRIEND_ID, FRIEND_ID)


def test_a_lapsed_login_is_said_instead_of_a_stale_place():
    text = _system(SELF, "owner.test@example.com", place="",
                   status={"ok": False, "login_needed": True, "checked": 1790610000.0,
                           "reason": "wants a new login"})
    assert "iCloud" in text, text[-800:]


def test_an_old_reading_is_never_where_he_is_now():
    import time
    old = dict(FRESH, ts=time.time() - 11 * 86400)
    text = _system(SELF, "owner.test@example.com", latest=old,
                   status={"ok": False, "login_needed": True})
    note = text.split(f"## Kur ir {OWNER}", 1)[1][:400]
    assert "nav zināms" in note and "neuzdod par pašreizējo" in note, note
    assert "iCloud" in note, note
    assert "Izmanto to" not in note, "an old place was offered as current: " + note


def test_nothing_known_means_no_note():
    with_note = _system(SELF, "owner.test@example.com")
    without = _system(SELF, "owner.test@example.com", place="", status=None)
    assert "Zztestpils" in with_note and "Zztestpils" not in without
    assert len(without) < len(with_note), "an empty note was still added"


if __name__ == "__main__":
    test_his_own_chat_knows_where_he_is()
    test_partner_chat_knows_too()
    test_no_other_chat_does()
    test_a_lapsed_login_is_said_instead_of_a_stale_place()
    test_an_old_reading_is_never_where_he_is_now()
    test_nothing_known_means_no_note()
    print("OK")
