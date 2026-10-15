import sys

sys.path.insert(0, "app")


def test_retry_message_is_flat():
    src = open("app/propose_patch.py").read()
    # The old tool-call shape must be gone from the retry message.
    assert '"action": "PROPOSE_PATCH"' not in src, "retry message still shows action key"
    assert '"args": {' not in src, "retry message still shows args wrapper"
    # The flat shape must be present: top-level files key, no wrapper.
    assert '{"files": [{"path": "...", "why": "..."}]' in src, "flat files key missing"
    # Sanity: the message still instructs a single fenced json object.
    assert "ONE json object" in src


if __name__ == "__main__":
    test_retry_message_is_flat()
    print("OK")
