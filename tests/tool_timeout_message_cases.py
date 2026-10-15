"""A read-only tool that times out is not "still running".

Spec (the owner's), app/agent_tools.py ToolRegistry.execute: a READ_ONLY
tool's timeout says that no result came back (so it may try again
differently, or answer with what it has and say what it could not check);
a tool that writes keeps "still running, do not call it again".

    python tests/tool_timeout_message_cases.py
"""
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import agent_tools as T  # noqa: E402


def _slow(risk):
    reg = T.ToolRegistry()
    reg.register(T.Tool("zz_slow_probe", "slow probe",
                        {"type": "object", "properties": {}},
                        lambda args, ctx: time.sleep(2) or {"ok": True},
                        risk, 0.2))
    return reg.execute("zz_slow_probe", {}, T.AgentContext(allow_external_write=True))


def test_a_read_only_timeout_is_not_still_running():
    out = _slow(T.READ_ONLY)
    msg = str(out.get("message") or "").lower()
    assert out.get("error_type") == "timeout", out
    assert "still running" not in msg and "report the result" not in msg, out
    assert "no result" in msg, out


def test_a_write_timeout_still_says_it_is_running():
    out = _slow(T.EXTERNAL_WRITE)
    assert out.get("error_type") == "timeout", out
    assert "still running" in str(out.get("message") or "").lower(), out


if __name__ == "__main__":
    test_a_read_only_timeout_is_not_still_running()
    test_a_write_timeout_still_says_it_is_running()
    print("OK")
