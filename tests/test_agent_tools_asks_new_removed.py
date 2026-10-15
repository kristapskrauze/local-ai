# -*- coding: utf-8 -*-
"""Assert the old keyword gate in antons_build is gone."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = (ROOT / "app" / "agent_tools.py").read_text(encoding="utf-8")


def test_asks_new_gate_removed():
    assert "_asks_new" not in SRC, "the _asks_new keyword gate is still present"
    assert "and not _asks_new" not in SRC
    assert "_wants_existing = bool(_pslug)" in SRC
    assert "_wants_existing = bool(_pslug) and not _asks_new" not in SRC


if __name__ == "__main__":
    test_asks_new_gate_removed()
    print("ok")
