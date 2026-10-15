# -*- coding: utf-8 -*-
"""1. antons_apply_fix takes no arguments, so after a rolled-back apply and a
   corrected proposal the second apply looked like an exact repeat, and the
   turn ledger refused to run it.
2. The apply result reached the model with the diff first and the one line
   that says what failed last, so the clip cut that line off; its `reason` key
   held only the caller label and was read as the cause of the rollback.

    py -3 tests/apply_loop_plumbing_cases.py
"""
import io
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import agent_tools as T  # noqa: E402
from app import qwen_agent as Q  # noqa: E402

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("    %-62s %s" % (name, "ok" if ok else "FAIL  " + str(detail)[:100]))


def _apply(applied):
    return {"ok": True, "tool": "antons_apply_fix", "risk": T.PRIVILEGED,
            "result": {"applied": applied, "reverted": not applied}}


def _propose(ok=True):
    return {"ok": True, "tool": "antons_propose_fix", "risk": T.PRIVILEGED,
            "result": {"ok": ok, "state": "PROPOSED" if ok else "FAILED"}}


def case_reapply_after_new_proposal():
    print("1. a corrected proposal makes the next apply a new action")
    ledger = Q.TurnToolLedger()
    ledger.record_result("antons_apply_fix", {}, _apply(False))
    check("a bare repeat with nothing new is still refused",
          ledger.find_action_duplicate("antons_apply_fix", {}) is not None)
    ledger.record_result("antons_propose_fix", {"goal": "fix it properly"},
                         _propose(True))
    check("after a successful new proposal it may run again",
          ledger.find_action_duplicate("antons_apply_fix", {}) is None)
    ledger.record_result("antons_apply_fix", {}, _apply(False))
    check("once that apply ran, a third bare repeat is refused again",
          ledger.find_action_duplicate("antons_apply_fix", {}) is not None)


def case_failed_proposal_does_not_reopen():
    print("")
    print("2. a proposal that produced nothing does not count as new")
    ledger = Q.TurnToolLedger()
    ledger.record_result("antons_apply_fix", {}, _apply(False))
    ledger.record_result("antons_propose_fix", {"goal": "x"}, _propose(False))
    check("still refused",
          ledger.find_action_duplicate("antons_apply_fix", {}) is not None)


def case_other_actions_unchanged():
    print("")
    print("3. other side-effect actions keep their exact-repeat refusal")
    ledger = Q.TurnToolLedger()
    args = {"target": "chat", "text": "hi"}
    ledger.record_result("send_message", args,
                         {"ok": True, "risk": T.EXTERNAL_WRITE,
                          "result": {"sent": True}})
    ledger.record_result("antons_propose_fix", {"goal": "x"}, _propose(True))
    check("a repeated send_message is still a duplicate",
          ledger.find_action_duplicate("send_message", args) is not None)


def case_apply_result_leads_with_the_failure():
    print("")
    print("4. the model sees what failed first, and no caller label")
    raw = {
        "applied": False, "reverted": True,
        "reason": "antons_apply_fix via iMessage;-;someone",
        "files": ["app/qwen_agent.py"],
        "diff": "x" * 20000,
        "checks": {"ok": False, "failed_stage": "ruff", "stages": [
            {"stage": "ast", "ok": True, "findings": []},
            {"stage": "ruff", "ok": False, "findings": [
                {"path": "app/qwen_agent.py", "line": 1431, "code": "F841",
                 "message": "Local variable `tool_names` is assigned to but never used",
                 "baseline": 0, "now": 1, "added": 1}]}]},
        "suites": [],
        "note": ("ruff failed on the changed files, so they were put back: "
                 "app/qwen_agent.py:1431 Local variable `tool_names` is "
                 "assigned to but never used"),
    }
    out = T._apply_result_for_model(raw)
    keys = list(out)
    check("applied, reverted and outcome come first",
          keys[:3] == ["applied", "reverted", "outcome"], keys)
    check("the caller label is gone", "reason" not in out, keys)
    check("the failing stage is named", out["failed_stage"] == "ruff",
          out["failed_stage"])
    first = (out["failures"] or [{}])[0]
    check("the finding says where and which rule",
          first.get("where") == "app/qwen_agent.py:1431"
          and first.get("code") == "F841", out["failures"])
    check("the diff is clipped short", len(out["diff"]) <= 1500, len(out["diff"]))
    clipped = T._limit_result(out)
    check("after the model-side clip the failure is still there",
          "tool_names" in str(clipped), str(clipped)[:200])

    suite_fail = T._apply_result_for_model({
        "applied": False, "reverted": True, "files": ["tests/x.py"], "diff": "",
        "checks": {"ok": True, "failed_stage": "", "stages": []},
        "suites": [{"suite": "tests/x.py", "ok": False, "exit_code": 1,
                    "output": "AssertionError: expected 78400"}],
        "note": "the suites did not pass, so the files were put back"})
    check("a red suite is reported as the tests stage with its output",
          suite_fail["failed_stage"] == "tests"
          and "78400" in suite_fail["failed_suites"][0]["output"], suite_fail)


def main():
    print(__doc__.strip().splitlines()[0])
    print("")
    for fn in (case_reapply_after_new_proposal,
               case_failed_proposal_does_not_reopen,
               case_other_actions_unchanged,
               case_apply_result_leads_with_the_failure):
        try:
            fn()
        except Exception as exc:
            check(fn.__name__, False, "%s: %s" % (type(exc).__name__, exc))
    print("")
    print("%d passed, %d failed" % (len(PASS), len(FAIL)))
    for name in FAIL:
        print("   FAILED: %s" % name)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
