# -*- coding: utf-8 -*-
"""Offline regressions for model-owned semantic routing."""
import copy
import os
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.people import OWNER, PARTNER, PARTNER_ACC
from app import messaging as M  # noqa: E402

CHAT = "iMessage;-;+37120000001"
PARTNER_ID = "+37120000001"


def _acfg():
    return {
        "enabled": True,
        "assistant_enabled": True,
        "reply_persona": "ai",
        "agentic_routing": "active",
        "assistant_chats": [],
        "allowed_senders": [PARTNER_ID],
        "contact_names": {PARTNER_ID: PARTNER},
        "owner_name": OWNER,
    }


def _entry(text, *, from_me=True, guid="routing-case"):
    return {
        "provider": "bluebubbles",
        "event_type": "new-message",
        "chat_guid": CHAT,
        "sender": PARTNER_ID,
        "message_guid": guid,
        "text": text,
        "is_from_me": from_me,
        "attachments": [],
        "ts": 1786627000.0,
    }


def _verdict(action, subject, confidence=0.97, contact="", addressed=False):
    source = M._WATCHER_SOURCE_FOR[subject] if action == "reply" else "none"
    value = {
        "action": action,
        "subject": subject,
        "source": source,
        "confidence": confidence,
        "reason": "offline model verdict",
        "addressed_to_assistant": addressed,
    }
    if contact:
        value["contact"] = contact
    return value


class ModelOwnedRoutingTests(unittest.TestCase):
    def setUp(self):
        self.pending = copy.deepcopy(M._PENDING_SCHEDULE)
        M._PENDING_SCHEDULE.clear()
        self.log = patch.object(M, "_log_watcher_decision")
        self.log.start()

    def tearDown(self):
        self.log.stop()
        M._PENDING_SCHEDULE.clear()
        M._PENDING_SCHEDULE.update(self.pending)

    def test_every_semantic_shape_calls_the_same_local_model_router(self):
        cases = (
            ("Anton, labs darbs", "conversation"),
            ("Atcel rītdienas randiņu", "calendar"),
            ("Kāds rīt būs laiks?", "public"),
            ("Cik grādu GPU?", "conversation"),
        )
        for index, (text, subject) in enumerate(cases):
            with self.subTest(text=text), patch.object(
                    M, "_watcher_llm_verdict",
                    return_value=_verdict("reply", subject)) as model:
                verdict = M._watcher_decision(
                    _acfg(), _entry(text, guid="shape-%d" % index), history=[])
            model.assert_called_once()
            self.assertEqual(verdict["action"], "reply")
            self.assertEqual(verdict["subject"], subject)

    def test_model_silence_is_honored_even_for_direct_praise(self):
        silent = _verdict("silent", "conversation")
        with patch.object(M, "_watcher_llm_verdict", return_value=silent):
            reason, verdict = M._semantic_gate_decision(
                _acfg(), _entry("Malacis, Anton"), history=[])
        self.assertIsNotNone(reason)
        self.assertEqual(verdict["action"], "silent")

    def test_direct_address_metadata_comes_from_model_not_name_regex(self):
        direct = _verdict("reply", "conversation", addressed=True)
        with patch.object(M, "_watcher_llm_verdict", return_value=direct):
            verdict = M._watcher_decision(
                _acfg(), _entry("Could you answer this one?"), history=[])
        self.assertTrue(verdict["addressed_to_assistant"])

    def test_low_confidence_model_reply_is_not_reinterpreted_by_threshold(self):
        low = _verdict("reply", "conversation", confidence=0.08)
        with patch.object(M, "_watcher_llm_verdict", return_value=low):
            reason, verdict = M._semantic_gate_decision(
                _acfg(), _entry("Anton, ko tu domā?"), history=[])
        self.assertIsNone(reason)
        self.assertEqual(verdict["confidence"], 0.08)

    def test_malformed_or_failed_model_fails_silent_without_phrase_fallback(self):
        for value in (None, {}, {"action": "reply"}):
            with self.subTest(value=value), patch.object(
                    M, "_watcher_llm_verdict", return_value=value):
                verdict = M._watcher_decision(
                    _acfg(), _entry("Anton, salabo ziņas"), history=[])
            self.assertEqual(
                (verdict["action"], verdict["subject"], verdict["source"]),
                ("silent", "unknown", "none"))
        with patch.object(M, "_watcher_llm_verdict",
                          side_effect=TimeoutError("offline")):
            verdict = M._watcher_decision(
                _acfg(), _entry("Anton, salabo ziņas"), history=[])
        self.assertEqual(verdict["action"], "silent")

    def test_private_contact_requires_exact_model_selected_catalog_name(self):
        valid = _verdict("reply", "person", contact=PARTNER)
        with patch.object(M, "_watcher_llm_verdict", return_value=valid):
            verdict = M._watcher_decision(
                _acfg(), _entry(f"Ko tu zini par {PARTNER_ACC}?"), history=[])
        self.assertEqual(verdict["source"], "hermes_chat")
        self.assertEqual(verdict["contact"], PARTNER)

        invalid = _verdict("reply", "person", contact="Nepazīstams")
        with patch.object(M, "_watcher_llm_verdict", return_value=invalid):
            verdict = M._watcher_decision(
                _acfg(), _entry("Ko tu zini par viņu?"), history=[])
        self.assertEqual(
            (verdict["subject"], verdict["source"]),
            ("conversation", "chat"))

    def test_continuation_needs_real_pending_state_but_model_owns_meaning(self):
        continuation = _verdict("reply", "continuation")
        with patch.object(M, "_watcher_llm_verdict", return_value=continuation):
            no_state = M._watcher_decision(
                _acfg(), _entry("19:00"), history=[])
        self.assertEqual((no_state["subject"], no_state["source"]),
                         ("conversation", "chat"))

        M._PENDING_SCHEDULE[CHAT] = {
            "text": "Ieplāno boulingu", "at": time.time()}
        with patch.object(M, "_watcher_llm_verdict", return_value=continuation):
            with_state = M._watcher_decision(
                _acfg(), _entry("19:00"), history=[])
        self.assertEqual((with_state["subject"], with_state["source"]),
                         ("continuation", "pending"))

    def test_transcript_supplies_contacts_pending_state_and_resources(self):
        M._PENDING_SCHEDULE[CHAT] = {
            "text": "Ieplāno boulingu", "at": time.time()}
        transcript = M._watcher_transcript(
            _acfg(), _entry("19:00"), history=[])
        self.assertIn(PARTNER, transcript)
        self.assertIn("NEPABEIGTS KALENDĀRA PIEPRASĪJUMS", transcript)
        self.assertIn("PIEEJAMIE AVOTI", transcript)

    def test_static_semantic_classifier_and_legacy_fallback_are_absent(self):
        for name in ("_WATCHER_EXAMPLES", "_watcher_model_verdict",
                     "_watcher_embed", "_legacy_semantic_gate",
                     "_direct_bot_praise_reply"):
            self.assertFalse(hasattr(M, name), name)


class StandingObligationTests(unittest.TestCase):

    MID = "1FBF1786-C5A5-426E-86B9-C6FFD2529ADE"

    def _with(self, message_ids):
        req = {"request_id": "R0bbe88fe", "thread_id": CHAT,
               "message_ids": list(message_ids), "state": "PENDING"}
        return patch.object(M.obligations, "current", return_value=req)

    def test_request_opened_by_this_turn_is_not_standing(self):
        with self._with([self.MID]):
            self.assertIsNone(
                M._standing_obligation(CHAT, [{"message_guid": self.MID}]))

    def test_request_from_an_earlier_turn_is_standing(self):
        with self._with(["OLDER-GUID"]):
            self.assertIsNotNone(
                M._standing_obligation(CHAT, [{"message_guid": self.MID}]))

    def test_merged_request_holding_an_earlier_message_is_standing(self):
        with self._with(["OLDER-GUID", self.MID]):
            self.assertIsNotNone(
                M._standing_obligation(CHAT, [{"message_guid": self.MID}]))

    def test_merged_request_wholly_inside_this_turn_is_not_standing(self):
        with self._with([self.MID, "B2"]):
            self.assertIsNone(M._standing_obligation(
                CHAT, [{"message_guid": self.MID}, {"message_guid": "B2"}]))

    def test_no_open_request_is_not_standing(self):
        with patch.object(M.obligations, "current", return_value=None):
            self.assertIsNone(
                M._standing_obligation(CHAT, [{"message_guid": self.MID}]))




if __name__ == "__main__":
    unittest.main(verbosity=2)
