# -*- coding: utf-8 -*-
"""A failing suite's evidence keeps the line that says why it failed.

subprocess.run is faked, so no suite actually runs.

    py -3 tests/suite_evidence_cases.py
"""
import io
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import agent_tools as T  # noqa: E402
from app import self_heal as S  # noqa: E402

PASS, FAIL = [], []
EXCEPTION = "ModuleNotFoundError: No module named 'app'"
TRACEBACK = "\n".join([
    "Traceback (most recent call last):",
    '  File "C:\\x\\tests\\ledger_isolation_cases.py", line 46, in test_child',
    "    subprocess.run([sys.executable, '-c', " + repr("import os, sys; " * 80) + "])",
    EXCEPTION,
])


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("    %-60s %s" % (name, "ok" if ok else "FAIL  " + str(detail)[:110]))


class Done:
    returncode = 1
    stdout = TRACEBACK.encode("utf-8")
    stderr = b""


def main():
    print("1. the exception line survives a very long source line")
    out = S._suite_evidence(TRACEBACK)
    check("the exception line is kept", out.endswith(EXCEPTION), out[-120:])
    check("the file and line number are kept", "line 46" in out, out)
    check("no line is longer than the clip",
          all(len(ln) <= S.SUITE_EVIDENCE_LINE_CHARS + 1 for ln in out.splitlines()))
    check("within the total budget", len(out) <= S.SUITE_EVIDENCE_CHARS, len(out))

    print("")
    print("2. lines stay lines, and short output is kept whole")
    short = "1. ok\n2. FAIL  something\n3 passed, 1 failed"
    check("kept whole, newlines included", S._suite_evidence(short) == short, S._suite_evidence(short))
    many = "\n".join("line %d" % i for i in range(100))
    lines = S._suite_evidence(many).splitlines()
    check("at most the last %d lines" % S.SUITE_EVIDENCE_LINES,
          lines == ["line %d" % i for i in range(100 - S.SUITE_EVIDENCE_LINES, 100)], lines[:3])

    print("")
    print("3. run_suite hands that evidence back")
    real = S.subprocess.run
    S.subprocess.run = lambda *args, **kwargs: Done()
    try:
        res = S.run_suite("parse_json_fence_cases")
    finally:
        S.subprocess.run = real
    check("reported as failed", res.get("ok") is False and res.get("exit_code") == 1, res)
    check("its output ends with the exception", str(res.get("output")).endswith(EXCEPTION),
          str(res.get("output"))[-120:])

    print("")
    print("4. the apply result the model reads keeps it too")
    model = T._apply_result_for_model({"applied": False, "reverted": True,
                                       "note": "the suites did not pass", "checks": {},
                                       "suites": [dict(res)]})
    shown = model["failed_suites"][0]["output"]
    check("the exception reaches the model", EXCEPTION in shown, shown[-120:])
    check("capped at 1500 characters", len(shown) <= 1500, len(shown))

    print("")
    print("%d passed, %d failed" % (len(PASS), len(FAIL)))
    for name in FAIL:
        print("   FAILED: %s" % name)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
