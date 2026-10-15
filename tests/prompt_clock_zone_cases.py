"""The clock in the prompt says which time zone it is.

    python tests/prompt_clock_zone_cases.py
"""
import os
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import messaging  # noqa: E402


def _zone():
    minutes = int((datetime.now().astimezone().utcoffset()).total_seconds() // 60)
    return "UTC%s%d" % ("+" if minutes >= 0 else "-", abs(minutes) // 60)


def _system_prompt(acfg, chat):
    entries = [{"sender": "partner", "text": "Cikos šodien saule norietēs?"}]
    messages = messaging._build_reply_messages(acfg, {}, entries, chat)
    return next(m["content"] for m in messages if m.get("role") == "system")


def _warsaw_now():
    # EU clocks change on the same instant, so Warsaw is always an hour behind
    from datetime import timedelta
    return (datetime.now() - timedelta(hours=1)).strftime("%H:%M")


def test_a_contact_chat_clock_names_its_zone():
    prompt = _system_prompt({"assistant_chats": [], "reply_persona": "owner",
                             "contact_names": {}, "contact_notes": {}},
                            "iMessage;-;partner")
    assert "Rīgas laiks, %s" % _zone() in prompt, prompt[-1500:]
    # named, the zone was still not converted ("~40 minūtes" to an 18:22
    # sunset at 16:54 in Warsaw), so the local times are read out too
    assert "%s Varšavā" % _warsaw_now() in prompt, prompt[-1500:]


def test_the_assistant_chat_clock_names_its_zone():
    prompt = _system_prompt({"assistant_chats": ["iMessage;-;owner"],
                             "reply_persona": "ai", "contact_names": {},
                             "contact_notes": {}}, "iMessage;-;owner")
    assert "Riga time, %s" % _zone() in prompt, prompt[-1500:]
    assert "%s in Warsaw" % _warsaw_now() in prompt, prompt[-1500:]


if __name__ == "__main__":
    test_a_contact_chat_clock_names_its_zone()
    test_the_assistant_chat_clock_names_its_zone()
    print("OK")
