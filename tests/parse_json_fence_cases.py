# -*- coding: utf-8 -*-
"""investigate._parse_json must read a valid reply that has ``` inside a string.

    py -3 tests/parse_json_fence_cases.py
"""
import io
import json
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import investigate as I  # noqa: E402

PASS, FAIL = [], []
FENCE = "`" * 3


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("    %-66s %s" % (name, "ok" if ok else "FAIL  " + str(detail)[:110]))


def proposal(replace):
    return {"files": [{"path": "app/messaging.py", "why": "new step"}],
            "test": {"suite": "tests/x_cases.py", "case": "c", "asserts": "a"},
            "edits": [{"path": "app/messaging.py", "find": "X = 1", "replace": replace}],
            "risks": ["r"], "plan": ["tests/x_cases.py"]}


def main():
    code = '_CODE_RE = re.compile(r"' + FENCE + '.*?' + FENCE + '|`[^`\\n]*`", re.S)'
    inner = proposal(code)
    raw = json.dumps(inner, indent=2)

    print("1. a valid reply with ``` inside a string value")
    check("read as it stands", I._parse_json(raw) == inner, I._parse_json(raw))
    check("read inside a json fence", I._parse_json(FENCE + "json\n" + raw + "\n" + FENCE) == inner)
    check("read after prose", I._parse_json("Here it is:\n" + raw + "\nDone.") == inner)

    print("")
    print("2. what already worked still works")
    plain = proposal("X = 2")
    check("a plain object", I._parse_json(json.dumps(plain)) == plain)
    check("a fenced object with prose around it",
          I._parse_json("Plan:\n" + FENCE + "json\n" + json.dumps(plain) + "\n" + FENCE + "\nok") == plain)
    check("prose braces before a fenced object: the fenced one",
          I._parse_json("Use {braces} carefully.\n" + FENCE + "json\n" + json.dumps(plain) + "\n" + FENCE) == plain)
    malformed = ('{"action": "ANSWER", "args": {"text": "Es neatbildēju.", '
                 '"cause": "routing decision set to silent", '
                 '"cites": ["nothing for the assistant to add", '
                 '"decision": "silent"]}}')
    salv = I._parse_json(malformed)
    check("a malformed verdict is still salvaged",
          bool(salv) and salv["action"] == "ANSWER"
          and "nothing for the assistant to add" in salv["args"]["cites"], salv)
    check("a malformed tool plan is still not guessed at",
          I._parse_json('{"action": "USE_TOOL", "plan": [{"tool": "x" "args"}]}') is None)
    check("empty and brace-free replies give None",
          I._parse_json("") is None and I._parse_json("no json here") is None)

    print("")
    print("3. a reply cut off after the ``` is not mistaken for a smaller object")
    cut = raw[:raw.index(FENCE, raw.index(FENCE) + 3) + 40]
    check("a truncated proposal gives None", I._parse_json(cut) is None, I._parse_json(cut))

    print("")
    print("%d passed, %d failed" % (len(PASS), len(FAIL)))
    for name in FAIL:
        print("   FAILED: %s" % name)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
