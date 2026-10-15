"""A calculator tool, so arithmetic is never done in the model's head.

Spec (the owner's), app/agent_tools.py:
  a READ_ONLY tool "calculate", registered in build_default_registry, with
  one argument "expression":
    "144/12 + 7*3"   -> result "33"
    "0.1 + 0.2"      -> result "0.3"          (no float noise)
    "18:22 - 16:57"  -> result "1:25", minutes 85   (HH:MM clock times)
  Only numbers, + - * / and parentheses, and HH:MM times; anything else,
  and division by zero, raises ToolRefused. Never eval.
  Offered wherever the model writes an answer: the self-chat list
  (messaging._LIVE_ASSISTANT_AGENT_TOOLS) and the contact-chat list
  (messaging._LIVE_TRUSTED_CONTACT_ACTION_TOOLS).

    python tests/calculate_tool_cases.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import agent_tools as T  # noqa: E402
from app import messaging as M  # noqa: E402


def _calc(expression):
    tool = T.build_default_registry().get("calculate")
    assert tool is not None, "calculate is not registered"
    return tool.handler({"expression": expression}, T.AgentContext())


def _refused(expression):
    try:
        _calc(expression)
    except T.ToolRefused:
        return True
    return False


def test_plain_arithmetic():
    assert _calc("144/12 + 7*3")["result"] == "33"
    assert _calc("0.1 + 0.2")["result"] == "0.3"
    assert _calc("(2 + 3) * 4")["result"] == "20"


def test_clock_times():
    got = _calc("18:22 - 16:57")
    assert got["result"] == "1:25" and got["minutes"] == 85, got
    got = _calc("16:57 - 18:22")
    assert got["result"] == "-1:25" and got["minutes"] == -85, got


def test_nothing_else_runs():
    assert _refused("__import__('os').system('echo hi')")
    assert _refused("open('x')")
    assert _refused("1/0")


def test_it_is_read_only_and_offered_where_answers_are_written():
    assert T.build_default_registry().get("calculate").risk == T.READ_ONLY
    assert "calculate" in M._LIVE_ASSISTANT_AGENT_TOOLS
    assert "calculate" in M._LIVE_TRUSTED_CONTACT_ACTION_TOOLS


if __name__ == "__main__":
    test_plain_arithmetic()
    test_clock_times()
    test_nothing_else_runs()
    test_it_is_read_only_and_offered_where_answers_are_written()
    print("OK")
