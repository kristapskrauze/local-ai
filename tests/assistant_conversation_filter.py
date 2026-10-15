import os


def _read_messaging():
    path = os.path.join(os.path.dirname(__file__), "..", "app", "messaging.py")
    with open(path, encoding="utf-8") as f:
        return f.read()


def test_operator_meta_kept_in_assistant_mode():
    src = _read_messaging()
    assert "not assistant_mode and _OPERATOR_META_RE.search(content)" in src, \
        "operator-meta filter must be skipped when assistant_mode is True"
    assert "if content and _OPERATOR_META_RE.search(content):" not in src, \
        "old unconditional operator-meta filter must be removed"


def test_operator_meta_dropped_in_normal_mode():
    src = _read_messaging()
    assert "not assistant_mode and _OPERATOR_META_RE.search(content)" in src, \
        "operator-meta filter must still apply when assistant_mode is False"


if __name__ == "__main__":
    test_operator_meta_kept_in_assistant_mode()
    test_operator_meta_dropped_in_normal_mode()
    print("OK")
