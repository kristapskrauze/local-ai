"""The agent's fallback line says what the turn did, not "No action was taken".

    python tests/agent_error_text_cases.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import qwen_agent as Q  # noqa: E402

PROPOSED = {"tool": "antons_propose_fix", "ok": True, "risk": "PRIVILEGED"}
READ = {"tool": "antons_read_code", "ok": True, "risk": "READ_ONLY"}
FAILED = {"tool": "antons_propose_fix", "ok": False, "risk": "PRIVILEGED"}


def test_a_turn_that_proposed_says_so():
    text = Q._model_error_text("turn_timeout", "agent turn exceeded 1500.0s", [READ, PROPOSED])
    assert "No action was taken" not in text and "antons_propose_fix" in text, text
    assert "ran out of time" in text, text


def test_reads_and_failures_are_no_action():
    text = Q._model_error_text("RuntimeError", "boom", [READ, FAILED])
    assert text.endswith("No action was taken."), text
    assert Q._model_error_text("TimeoutError", "", None).endswith("No action was taken.")


if __name__ == "__main__":
    test_a_turn_that_proposed_says_so()
    test_reads_and_failures_are_no_action()
    print("OK")
