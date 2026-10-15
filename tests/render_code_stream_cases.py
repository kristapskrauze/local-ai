# -*- coding: utf-8 -*-
"""py -3 tests/render_code_stream_cases.py
"""
import io
import json
import os
import re
import shutil
import subprocess
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

APP_JS = os.path.join(ROOT, "app", "static", "app.js")
INDEX = os.path.join(ROOT, "app", "static", "index.html")
CSS = os.path.join(ROOT, "app", "static", "styles.css")


def _node():
    for c in (r"C:\Program Files\nodejs\node.exe", "node", "nodejs"):
        if os.path.isabs(c) and os.path.isfile(c):
            return c
        if shutil.which(c):
            return c
    return None


def _run_parse_cases(node):
    src = open(APP_JS, encoding="utf-8").read()
    fn = re.search(r"  function parseToolCall\(line\) \{.*?\n  \}\n", src, re.S)
    glyphs = re.search(r'var CODE_TOOL_GLYPHS = "[^"]*";', src)
    if not fn:
        return None, "parseToolCall not found in app.js"
    cases = [
        ["\u2192 Read app/coder.py [offset=1, limit=20]", {"tool": "Read", "target": "app/coder.py"}],
        ['\u2731 Grep "RUNNER_WSL" 8 matches', {"tool": "Grep", "target": "RUNNER_WSL"}],
        ['\u2731 Grep "lab" in . \u00b7 1 match', {"tool": "Grep", "target": "lab"}],
        ['\u2192 List app/static in project', {"tool": "List", "target": "app/static"}],
        ["\u2192 Edit app/messaging.py", {"tool": "Edit", "target": "app/messaging.py"}],
        ["\u2192 Write app/static/new.js", {"tool": "Write", "target": "app/static/new.js"}],
        ["$ py -3 tests/foo.py", {"tool": "Bash", "target": "py -3 tests/foo.py"}],
        ["> build qwen3.8:latest", None],
        ["**app/coder.py 1-20:** module docstring", None],
        ["235 |   throw makeError()", None],
        ["## Todos", None],
        ["   ", None],
    ]
    harness = (
        (glyphs.group(0) + "\n" if glyphs else "")
        + fn.group(0)
        + "\nconst C=" + json.dumps([[c[0], c[1]] for c in cases]) + ";\n"
        + "let bad=0;\n"
        + "for (const [inp, want] of C){\n"
        + "  const got=parseToolCall(inp);\n"
        + "  if (JSON.stringify(got)!==JSON.stringify(want)){bad++;"
        + "console.log('MISMATCH '+JSON.stringify(inp)+' got '+JSON.stringify(got));}\n"
        + "}\n"
        + "process.exit(bad?1:0);\n"
    )
    res = subprocess.run([node, "-e", harness], capture_output=True, text=True, timeout=30)
    return res.returncode == 0, (res.stdout + res.stderr).strip()


def main():
    checks = []

    def check(label, ok, detail=""):
        checks.append((label, bool(ok)))
        print(("  ok  " if ok else "FAIL  ") + label + (("  " + detail) if detail and not ok else ""))

    app = open(APP_JS, encoding="utf-8").read()
    idx = open(INDEX, encoding="utf-8").read()
    css = open(CSS, encoding="utf-8").read()

    check("index.html has the #code-steps rail", 'id="code-steps"' in idx)
    check("styles.css styles .code-step rows", ".code-step" in css and ".code-steps" in css)
    check("app.js defines parseToolCall + pushCodeStep",
          "function parseToolCall" in app and "function pushCodeStep" in app)
    check("the stream consumer feeds tool calls to the rail",
          re.search(r'o\.type === "out"[\s\S]{0,160}parseToolCall[\s\S]{0,60}pushCodeStep', app) is not None)
    check("a new run resets the rail", "resetCodeSteps()" in app
          and re.search(r'out\.innerHTML = "";\s*\n\s*resetCodeSteps\(\)', app) is not None)
    check("the raw log line is still appended for a tool call",
          re.search(r'if \(step\) pushCodeStep\(step\);\s*\n\s*appendCodeLine\("out", o\.text\);', app) is not None)

    node = _node()
    if node:
        ok, out = _run_parse_cases(node)
        check("parseToolCall maps real opencode stream lines to {tool,target}", ok, out)
    else:
        print("  --  node not found, skipping parseToolCall behaviour cases")

    passed = sum(1 for _, ok in checks if ok)
    print("\n%d/%d passed" % (passed, len(checks)))
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
