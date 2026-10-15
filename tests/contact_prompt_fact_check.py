"""Assert the contact-conversation system prompt contains the fact-check rule."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def test_contact_prompt_contains_fact_check_instruction():
    """The contact (non-assistant) system prompt must instruct the model to
    verify checkable facts with web_search/research before answering, and to
    omit unverifiable numbers."""
    from app import messaging

    # Minimal config: no assistant_chats, so the contact branch is taken.
    acfg = {
        "assistant_chats": [],
        "reply_persona": "owner",
        "contact_names": {},
        "contact_notes": {},
    }
    pcfg = {}
    entries = [{"sender": "partner", "text": "Kāds ir iPhone 15 Pro procesors?"}]
    chat_guid = "iMessage;-;partner"

    messages = messaging._build_reply_messages(acfg, pcfg, entries, chat_guid)

    # The system message is the first one.
    sys_msgs = [m for m in messages if m.get("role") == "system"]
    assert sys_msgs, "expected at least one system message"
    prompt = sys_msgs[0].get("content", "")

    # The fact-check instruction must be present.
    assert "Faktu pārbaude" in prompt, (
        "contact prompt missing 'Faktu pārbaude' section header"
    )
    assert "web_search" in prompt, (
        "contact prompt does not mention web_search for fact verification"
    )
    assert "skaitļi" in prompt, (
        "contact prompt does not mention verifying numbers (skaitļi)"
    )
    assert "pārbaudi" in prompt or "pārbaudīt" in prompt, (
        "contact prompt does not instruct to verify (pārbaudīt) facts"
    )
    assert "izlaiž" in prompt, (
        "contact prompt does not instruct to omit unverifiable data"
    )


def test_assistant_prompt_not_affected():
    """The assistant (self-chat) prompt should NOT get the contact fact-check
    rule appended in the else branch — it has its own tool instructions."""
    from app import messaging

    acfg = {
        "assistant_chats": ["iMessage;-;owner"],
        "reply_persona": "ai",
        "contact_names": {},
        "contact_notes": {},
    }
    pcfg = {}
    entries = [{"sender": "owner", "text": "hello"}]
    chat_guid = "iMessage;-;owner"

    messages = messaging._build_reply_messages(acfg, pcfg, entries, chat_guid)
    sys_msgs = [m for m in messages if m.get("role") == "system"]
    assert sys_msgs, "expected at least one system message"
    prompt = sys_msgs[0].get("content", "")

    # The assistant prompt should NOT contain the contact-specific fact-check
    # section (it is only appended in the else branch).
    assert "Faktu pārbaude — pirms atbildes" not in prompt, (
        "assistant prompt should not contain the contact fact-check section"
    )


if __name__ == "__main__":
    test_contact_prompt_contains_fact_check_instruction()
    test_assistant_prompt_not_affected()
    print("OK")
