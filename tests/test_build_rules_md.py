# -*- coding: utf-8 -*-
"""Test that app/build_rules.md exists, has a section per rule, and that
_build_sys returns a prompt containing text from the markdown."""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main() -> None:
    md_path = os.path.join(ROOT, "app", "build_rules.md")
    assert os.path.isfile(md_path), "app/build_rules.md must exist"
    text = open(md_path, encoding="utf-8").read()
    for section in ("base", "data", "selftest", "ios9"):
        pat = re.compile(r"^##\s+" + re.escape(section) + r"\s*$", re.M)
        assert pat.search(text), "missing section: " + section

    sys.path.insert(0, ROOT)
    from app.agent_tools import _build_sys
    prompt = _build_sys("gpu monitor", True)
    assert "LIVE RIG DATA" in prompt, "data section text missing from prompt"
    assert "SELF-TEST BLOCK" in prompt, "selftest section text missing from prompt"
    assert "iOS 9 Safari" in prompt, "ios9 section text missing from prompt"
    print("OK")


if __name__ == "__main__":
    main()
