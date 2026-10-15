# -*- coding: utf-8 -*-
"""Assert the antons_build description carries the one-message approval rule."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    path = os.path.join(ROOT, "app", "agent_tools.py")
    with open(path, encoding="utf-8") as f:
        src = f.read()

    phrases = [
        "one message",
        "what it will build",
        "which page it will touch",
        "what will change",
        "never ask for a",
        "read more or ask a real question",
        "instead of asking",
    ]
    missing = [p for p in phrases if p not in src]
    if missing:
        print("FAIL: missing phrases in antons_build description:")
        for p in missing:
            print("  -", p)
        return 1
    print("OK: all phrases present")
    return 0


if __name__ == "__main__":
    sys.exit(main())
