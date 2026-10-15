# -*- coding: utf-8 -*-
"""A reply that says it saved something, with nothing saved, gets one retry.

The agent runs, the claim judge and the reply finishing are faked: no model,
no network, placeholder handle.

    py -3 tests/save_claim_retry_cases.py
"""
import io
import os
import sys
import types
import unittest
from unittest import mock

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import messaging as M  # noqa: E402

CHAT = "iMessage;-;+37120000000"
ASK = "Atgādini ka mums vajag depo"
CAL_OK = {"tool": "calendar_create", "ok": True, "risk": "EXTERNAL_WRITE",
          "args": {"title": "Depo", "start": "rīt 09:00"},
          "result": {"ok": True, "event_id": "e1"}}
CAL_REFUSED = {"tool": "calendar_create", "ok": False, "risk": "EXTERNAL_WRITE",
               "result": {"ok": False, "error_type": "authorization_required"}}
CLAIMS = {"Notēts — depo. 📝", "Ieliku kalendārā rīt 9:00", "Labi, atgādināšu"}


def _run(answer, results=()):
    return types.SimpleNamespace(answer=answer, tool_results=list(results),
                                 cancelled=False, loop_limit_reached=False,
                                 model_calls=1, tool_steps=len(results), trace_id="t")


class SaveClaimRetryTests(unittest.TestCase):
    def turn(self, main, retry=None):
        """Drive one contact-chat turn. `main`/`retry` are (answer, results)."""
        self.retry_notes = []
        calls = {"n": 0}

        def fake_agent(conversation=None, interface_context=None, **_kw):
            if getattr(interface_context, "interface", "") == "bluebubbles_lookup_prepass":
                return _run("NAV")
            calls["n"] += 1
            if calls["n"] == 1:
                return _run(*main)
            self.retry_notes.append(str((conversation or [{}])[-1].get("content") or ""))
            return _run(*(retry or ("", ())))

        patches = [
            mock.patch.object(M.qwen_agent, "run_agent_turn", fake_agent),
            mock.patch.object(M, "_reply_claims_save", lambda a, b, think=False: b in CLAIMS),
            mock.patch.object(M, "_finalize_agent_reply", lambda acfg, ans, *a, **k: ans),
            mock.patch.object(M, "_note_capability_gap", lambda *a, **k: None),
            mock.patch.object(M, "_note_agent_lane", lambda *a, **k: None),
            mock.patch.object(M, "_turn_is_stale", lambda *a, **k: False),
        ]
        for p in patches:
            p.start()
        try:
            out = M._live_assistant_agent_reply(
                {}, [{"role": "user", "content": ASK}], CHAT, ASK, ASK,
                sender="+37120000000", trusted_contact_action=True, owner_authored=True,
                accept_without_action=True)
        finally:
            for p in reversed(patches):
                p.stop()
        self.agent_runs = calls["n"]
        return out[0] if out else None

    def test_a_claim_with_nothing_saved_is_retried_and_the_retry_saves(self):
        got = self.turn(("Notēts — depo. 📝", ()), ("Ieliku kalendārā rīt 9:00", [CAL_OK]))
        self.assertEqual(got, "Ieliku kalendārā rīt 9:00")
        self.assertEqual(self.agent_runs, 2)
        self.assertIn("nekas netika saglabāts", self.retry_notes[0])

    def test_a_retry_that_asks_when_is_kept(self):
        got = self.turn(("Notēts — depo. 📝", ()), ("Kad tev atgādināt?", ()))
        self.assertEqual(got, "Kad tev atgādināt?")

    def test_a_retry_that_still_claims_is_replaced_with_an_honest_line(self):
        got = self.turn(("Notēts — depo. 📝", ()), ("Labi, atgādināšu", [CAL_REFUSED]))
        self.assertEqual(got, "To es vēl nekur neesmu pierakstījis.")

    def test_an_empty_retry_does_not_leave_the_claim_standing(self):
        got = self.turn(("Notēts — depo. 📝", ()), ("", ()))
        self.assertEqual(got, "To es vēl nekur neesmu pierakstījis.")

    def test_a_claim_the_record_backs_goes_out_untouched(self):
        got = self.turn(("Ieliku kalendārā rīt 9:00", [CAL_OK]))
        self.assertEqual(got, "Ieliku kalendārā rīt 9:00")
        self.assertEqual(self.agent_runs, 1)

    def test_a_reply_that_claims_nothing_is_not_retried(self):
        got = self.turn(("Depo ir līdz 21:00 atvērts", ()))
        self.assertEqual(got, "Depo ir līdz 21:00 atvērts")
        self.assertEqual(self.agent_runs, 1)

    def test_the_retry_is_told_what_already_happened(self):
        route = {"tool": "plan_route", "ok": True,
                 "risk": "EXTERNAL_WRITE", "args": {"to": "Depo"}, "result": {"ok": True}}
        self.turn(("Notēts — depo. 📝", [route]), ("Kad tev atgādināt?", ()))
        self.assertIn("plan_route", self.retry_notes[0])
        self.assertIn("Nedari to vēlreiz", self.retry_notes[0])


if __name__ == "__main__":
    unittest.main()
