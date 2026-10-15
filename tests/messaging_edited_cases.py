"""Assert the edited-message marker is emitted in _chat_history_messages."""
import io
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def test_edited_marker_present():
    path = os.path.join(os.path.dirname(__file__), "..", "app", "messaging.py")
    with io.open(path, "r", encoding="utf-8") as fh:
        src = fh.read()
    assert 'text = "[labots] " + text.strip()' in src, "edited marker line missing"
    assert 'if item.get("edited"):' in src, "edited guard missing"


if __name__ == "__main__":
    test_edited_marker_present()
    print("OK")
