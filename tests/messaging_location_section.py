"""The location section of the prompt points recommendations at where the owner is now."""
import os


def test_recommendations_follow_current_location():
    path = os.path.join(os.path.dirname(__file__), "..", "app", "messaging.py")
    with open(path, encoding="utf-8") as f:
        text = f.read()
    idx = text.find("## Kur ir {OWNER}")
    assert idx != -1, "'## Kur ir {OWNER}' section not found in app/messaging.py"
    section = text[idx:idx + 1500]
    assert "are for where he is now (%s), not" in section, section[:600]
    assert "exist there" in section, section[:600]
    # no named wrong chain in the prompt for the model to repeat
    assert "Rimi" not in section, section[:600]


if __name__ == "__main__":
    test_recommendations_follow_current_location()
    print("OK")
