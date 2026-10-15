# -*- coding: utf-8 -*-
"""Assert the selftest repair section in app/agent_tools.py now loops up to
three attempts and re-runs the browser test inside the loop."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = (ROOT / "app" / "agent_tools.py").read_text(encoding="utf-8")


def _repair_section() -> str:
    start = SRC.index("def _selftest_and_repair(")
    end = SRC.index("def _run_build(", start)
    return SRC[start:end]


def test_repair_section_loops_three_times_and_retests():
    sec = _repair_section()
    # The repair block must be a loop bounded by three attempts.
    assert "for _attempt in range(3):" in sec, "repair section must loop up to three attempts"
    loop_body = sec.split("for _attempt in range(3):", 1)[1].split(
        "# None of the repair passes came back clean", 1)[0]
    assert '_selftest_retrying("repair %d"' in loop_body, \
        "browser test must be re-run inside the repair loop"
    # The new failure text must be fed forward for the next attempt.
    assert "fails = _r2_new or list(res2.get(\"failures\") or [])" in loop_body, \
        "new failure text must be fed forward to the next attempt"


def test_stale_selftest_failures_do_not_revert_a_working_edit():
    sec = _repair_section()
    assert "_base_fail_keys()" in sec, "must diff the edit's failures against the base file's"
    assert "_selftest_failure_key" in sec, "must key failures so stale vs new can be told apart"
    # the accept-over-stale path: base has red checks, edit adds none -> ship it
    assert "kept_over_stale" in sec
    guard = sec.split("_base_keys = _base_fail_keys()", 1)[1]
    assert "if _base_keys and not _new_fails:" in guard, \
        "an edit that introduces no new failures over a stale base must be shipped"
    # the verified-revert must also tolerate the base's pre-existing failures
    assert "tolerate=_base_keys" in sec


def test_playwright_rides_on_every_test_of_the_new_page_but_not_the_revert():
    sec = _repair_section()
    assert "return _with_playwright(r, tag)" in sec
    assert "_pt.repair_context(_pw_spec)" in sec
    revert = sec.split("def _verified_revert(", 1)[1].split("\n    # STEP 0", 1)[0]
    assert "_with_playwright" not in revert and "_selftest_retrying" not in revert


if __name__ == "__main__":
    test_repair_section_loops_three_times_and_retests()
    test_stale_selftest_failures_do_not_revert_a_working_edit()
    test_playwright_rides_on_every_test_of_the_new_page_but_not_the_revert()
    print("OK")
