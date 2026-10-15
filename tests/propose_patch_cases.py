# -*- coding: utf-8 -*-
"""Regression: _SYS prompt must forbid unused imports in new test files."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_sys_prompt_forbids_unused_imports():
    from app.propose_patch import _SYS
    assert "import only what it actually uses" in _SYS, (
        "_SYS must state that a new test file must import only what it actually uses"
    )
    assert "unused import fails the lint check and reverts the whole proposal" in _SYS, (
        "_SYS must explain that an unused import fails the lint check and reverts the whole proposal"
    )
    assert "reads a source file as text usually needs nothing beyond os" in _SYS, (
        "_SYS must note that a test reading a source file as text usually needs nothing beyond os"
    )


if __name__ == "__main__":
    test_sys_prompt_forbids_unused_imports()
    print("OK")
