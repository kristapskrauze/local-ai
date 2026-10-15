"""A second apply call must not turn a landed change into "nothing applied".

No model, network or file write: apply_edits is a stand-in.

    python tests/apply_twice_cases.py
"""
import os
import sys
import time
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from app import agent_tools as T  # noqa: E402
from app import messaging as M  # noqa: E402
from app import rig_agent, self_heal  # noqa: E402

OWNER = "owner@example.com"
CHAT = "iMessage;-;" + OWNER + ";apply-twice-test"
PROPOSAL = {"proposal": {"edits": [{"path": "app/x.py", "find": "a", "replace": "b"}],
                         "files": [{"path": "app/x.py", "why": "test"}],
                         "test": {"suite": "tests/x.py"}}}
LANDED = {"applied": True, "reverted": False,
          "note": "the change is on disk; the service still runs the old code "
                  "until it is restarted",
          "checks": {"stages": []}, "suites": [{"suite": "tests/x.py", "ok": True}]}


def ctx():
    return T.AgentContext(actor="test", conversation_id=CHAT, sender=OWNER,
                          allow_privileged=True)


class ApplyTwiceTests(unittest.TestCase):
    def setUp(self):
        getattr(T, "_LAST_APPLIED", {}).pop(CHAT, None)
        M._LAST_PROPOSAL.pop(CHAT, None)

    def _patches(self, apply_result):
        return [patch.object(rig_agent, "owner_allowed", return_value=True),
                patch.object(M, "load_config", return_value={"auto_reply": {}}),
                patch.object(M, "_auto_accept_state", return_value=(False, None)),
                patch.object(M, "pending_proposal_approved", return_value=True),
                patch.object(M, "clear_proposal_approval"),
                patch.object(self_heal, "apply_edits", return_value=apply_result)]

    def _run(self, calls, apply_result=LANDED, pending=True):
        if pending:
            M._LAST_PROPOSAL[CHAT] = (time.time(), dict(PROPOSAL))
        ps = self._patches(apply_result)
        for p in ps:
            p.start()
        try:
            out = [T._tool_antons_apply_fix({}, ctx()) for _ in range(calls)]
            applied_calls = self_heal.apply_edits.call_count
        finally:
            for p in ps:
                p.stop()
        return out, applied_calls

    def test_a_second_call_reports_the_landed_change(self):
        (first, second), applied_calls = self._run(2)
        self.assertTrue(first.get("applied"))
        self.assertEqual(applied_calls, 1)
        text = str(second)
        self.assertTrue(second.get("already_applied"), second)
        self.assertTrue(second.get("applied"), second)
        self.assertNotIn("no pending proposal", text)
        self.assertIn("app/x.py", text)

    def test_with_nothing_applied_the_old_answer_stands(self):
        (only,), _n = self._run(1, pending=False)
        self.assertFalse(only.get("ok"))
        self.assertIn("no pending proposal", str(only))

    def test_an_old_apply_is_not_reported_as_this_one(self):
        (first,), _n = self._run(1)
        self.assertTrue(first.get("applied"))
        # push the record past its lifetime
        ts, info = T._LAST_APPLIED[CHAT]
        T._LAST_APPLIED[CHAT] = (ts - 10 * 3600, info)
        (second,), _n = self._run(1, pending=False)
        self.assertIn("no pending proposal", str(second))


if __name__ == "__main__":
    result = unittest.main(verbosity=2, exit=False).result
    if result.wasSuccessful():
        print("OK")
    sys.exit(0 if result.wasSuccessful() else 1)
