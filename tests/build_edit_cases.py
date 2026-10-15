import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.build_edit import surgical


class FakeChat:
    """Returns a fixed reply regardless of the prompt."""

    def __init__(self, reply):
        self.reply = reply

    def __call__(self, prompt):
        return self.reply


BASE = "<html><body><h1>Title</h1><p>old text</p></body></html>"


def test_surgical_applies_pairs_and_returns_none_on_missing():
    # Clean apply: one pair present in the base.
    reply = '[{"find": "old text", "replace": "new text"}]'
    out = surgical("edit", BASE, "change the text", FakeChat(reply))
    assert out == "<html><body><h1>Title</h1><p>new text</p></body></html>"

    # No usable pairs -> None.
    out = surgical("edit", BASE, "change the text", FakeChat("no json here"))
    assert out is None

    # A find string that is not in the base -> None.
    reply = '[{"find": "does not exist", "replace": "x"}]'
    out = surgical("edit", BASE, "change the text", FakeChat(reply))
    assert out is None


if __name__ == "__main__":
    test_surgical_applies_pairs_and_returns_none_on_missing()
    print("OK")
