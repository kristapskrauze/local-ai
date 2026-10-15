import sys

sys.path.insert(0, "app")


def test_tapback_relabel_present():
    with open("app/messaging.py", encoding="utf-8") as f:
        src = f.read()
    anchor = '        text = str(item.get("text") or "")'
    assert anchor in src, "anchor line missing from app/messaging.py"
    idx = src.index(anchor) + len(anchor)
    tail = src[idx:idx + 200]
    assert "if _TAPBACK_TEXT_RE.match(text.strip()):" in tail, "relabelling guard not directly after anchor"
    assert 'text = "[reakcija] " + text.strip()' in tail, "relabelling assignment not directly after guard"


if __name__ == "__main__":
    test_tapback_relabel_present()
    print("ok")
