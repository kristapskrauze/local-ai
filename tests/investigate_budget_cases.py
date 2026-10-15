# -*- coding: utf-8 -*-
"""Budget constants for investigate must be raised for real investigations."""
import importlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    mod = importlib.import_module("app.investigate")
    assert mod.MAX_TOOL_STEPS == 18, f"MAX_TOOL_STEPS={mod.MAX_TOOL_STEPS}, expected 18"
    assert mod.MAX_MODEL_CALLS == 20, f"MAX_MODEL_CALLS={mod.MAX_MODEL_CALLS}, expected 20"
    print("OK")


if __name__ == "__main__":
    main()
