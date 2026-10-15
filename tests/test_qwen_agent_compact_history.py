# -*- coding: utf-8 -*-
"""Tests for _compact_history per-tool retention."""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.qwen_agent import _compact_history


def test_newest_per_tool_kept_whole():
    """The newest tool result for each distinct tool name must be kept whole,
    even if it is not among the last three tool results overall."""
    # Build a history where antons_read_code appears at indices 1 and 5,
    # and other tools fill indices 2, 3, 4. The last three tool indices
    # are 3, 4, 5. Without per-tool retention, index 1 (antons_read_code)
    # would be elided. With per-tool retention, index 5 is the newest
    # antons_read_code and is kept; index 1 is older and may be elided.
    # But the key assertion: the NEWEST antons_read_code (index 5) is kept.
    # To make the test meaningful, put antons_read_code at index 1 and
    # ensure it is NOT in the last three, while another tool's newest IS.
    # Actually, let's construct: tools at indices 0,1,2,3,4,5
    # index 0: antons_read_code (old)
    # index 1: antons_read_code (newer, but not newest overall)
    # index 2: antons_investigate
    # index 3: antons_run_tests
    # index 4: antons_read_code (newest overall AND newest per-tool)
    # index 5: antons_investigate (newest overall)
    # Last three tool indices: 3, 4, 5
    # Per-tool newest: antons_read_code -> 4, antons_investigate -> 5, antons_run_tests -> 3
    # So keep_full = {3,4,5} union {4,5,3} = {3,4,5}
    # That doesn't distinguish. Let me make antons_read_code NOT in last three.
    # index 0: antons_read_code
    # index 1: antons_investigate
    # index 2: antons_run_tests
    # index 3: antons_investigate
    # index 4: antons_run_tests
    # index 5: antons_investigate
    # Last three: 3,4,5
    # Per-tool newest: antons_read_code -> 0, antons_investigate -> 5, antons_run_tests -> 4
    # keep_full = {3,4,5} union {0,5,4} = {0,3,4,5}
    # So index 0 (antons_read_code) is kept whole. Without per-tool, it would be elided.
    big = "x" * 500
    msgs = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "tool", "name": "antons_read_code", "content": big},
        {"role": "tool", "name": "antons_investigate", "content": big},
        {"role": "tool", "name": "antons_run_tests", "content": big},
        {"role": "tool", "name": "antons_investigate", "content": big},
        {"role": "tool", "name": "antons_run_tests", "content": big},
        {"role": "tool", "name": "antons_investigate", "content": big},
        {"role": "assistant", "content": "I have gathered enough information."},
    ]
    # Force compaction by using a tiny budget
    out = _compact_history(msgs, budget=100)
    # The newest antons_read_code is at index 1. It must be kept whole.
    assert out[1]["content"] == big, (
        "Newest antons_read_code result was elided; per-tool retention failed. "
        "Got: %r" % out[1]["content"][:80]
    )


def test_older_per_tool_elided():
    """Older results for the same tool should still be elided to save space."""
    big = "x" * 500
    msgs = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "tool", "name": "antons_read_code", "content": big},
        {"role": "tool", "name": "antons_read_code", "content": big},
        {"role": "tool", "name": "antons_investigate", "content": big},
        {"role": "tool", "name": "antons_investigate", "content": big},
        {"role": "tool", "name": "antons_investigate", "content": big},
        {"role": "assistant", "content": "Done."},
    ]
    out = _compact_history(msgs, budget=100)
    # Newest antons_read_code is index 2, must be kept whole
    assert out[2]["content"] == big
    # Older antons_read_code at index 1 should be elided
    assert "elided" in out[1]["content"].lower() or len(out[1]["content"]) < len(big), (
        "Older antons_read_code result should be elided. Got: %r" % out[1]["content"][:80]
    )


if __name__ == "__main__":
    test_newest_per_tool_kept_whole()
    test_older_per_tool_elided()
    print("OK")
