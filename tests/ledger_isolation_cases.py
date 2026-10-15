# -*- coding: utf-8 -*-
"""Offline runs must not write into the live .messaging_history ledgers."""
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIVE_TRACE = os.path.join(ROOT, ".messaging_history", "agent_traces.jsonl")


def _child_code(tmp):
    return (
        "import os, sys\n"
        "sys.path.insert(0, %r)\n"
        "from app import agent_tools as T\n"
        "p = str(T.TRACE_PATH)\n"
        "assert p.startswith(%r), p\n"
        "T.TRACE_PATH.parent.mkdir(parents=True, exist_ok=True)\n"
        "with open(T.TRACE_PATH, 'a', encoding='utf-8') as fh:\n"
        "    fh.write('x\\n')\n"
    ) % (ROOT, tmp)


def test_isolated_child():
    tmp = tempfile.mkdtemp(prefix="ledger_iso_")
    before = os.path.getsize(LIVE_TRACE) if os.path.exists(LIVE_TRACE) else 0
    env = dict(os.environ)
    env["ANTON_LEDGER_DIR"] = tmp
    code = _child_code(tmp)
    done = subprocess.run([sys.executable, "-c", code], env=env,
                          capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    after = os.path.getsize(LIVE_TRACE) if os.path.exists(LIVE_TRACE) else 0
    assert after == before
    assert os.path.exists(os.path.join(tmp, "agent_traces.jsonl"))


def test_default_path():
    env = dict(os.environ)
    env.pop("ANTON_LEDGER_DIR", None)
    code = (
        "import os, sys\n"
        "sys.path.insert(0, %r)\n"
        "from app import agent_tools as T\n"
        "assert str(T.TRACE_PATH).endswith('agent_traces.jsonl')\n"
        "assert '.messaging_history' in str(T.TRACE_PATH)\n"
    ) % (ROOT,)
    done = subprocess.run([sys.executable, "-c", code], env=env,
                          capture_output=True, text=True)
    assert done.returncode == 0, done.stderr


def test_run_suite_env():
    from app import self_heal as S
    captured = {}

    class _Done:
        returncode = 0
        stdout = b""
        stderr = b""

    def _fake_run(cmd, **kw):
        captured["env"] = kw.get("env")
        return _Done()

    real = S.subprocess.run
    S.subprocess.run = _fake_run
    try:
        S.run_suite("ledger_isolation_cases")
    finally:
        S.subprocess.run = real
    assert "ANTON_LEDGER_DIR" in captured["env"]


def main():
    test_isolated_child()
    test_default_path()
    test_run_suite_env()
    print("ledger_isolation_cases: all green")


if __name__ == "__main__":
    main()
