# -*- coding: utf-8 -*-
"""Greetings and questions about Anton are the model's call, not a word list's.

Offline: every model call and every store write is faked.

    py -3 tests/greeting_and_self_inquiry_cases.py
"""
import io
import os
import sys
import unittest
from unittest.mock import patch


sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import investigate as I  # noqa: E402
from app import messaging as M  # noqa: E402


CONTACT = "iMessage;-;+37100000078"
PARTNER = "+37100000078"
OWNER = "owner-greeting-test@example.com"


class _ReachedTheModelPath(Exception):
    pass


def _acfg(**extra):
    cfg = {"assistant_chats": ["iMessage;-;greeting-self-test"], "enabled": True,
           "assistant_enabled": True, "reply_persona": "ai", "read_links": False,
           "allowed_senders": [PARTNER], "model": "qwen-test",
           "assistant_agent_tools": "off"}
    cfg.update(extra)
    return cfg


class SelfInquiryTests(unittest.TestCase):
    def test_every_owner_direct_message_goes_to_the_supervisor(self):
        for text in ("Anton, kāpēc tu tikko ignorēji Annu?",
                     "salabo šo kļūdu savā kodā",
                     "kas tur notika?",
                     "pastāsti ko mēs šodien izlabojām"):
            with self.subTest(text=text):
                self.assertEqual(
                    I.self_inquiry_signal(text, is_owner=True, is_direct=True), I.MAYBE)

    def test_the_facts_that_are_not_wording_still_refuse(self):
        self.assertEqual(I.self_inquiry_signal(
            "kāpēc tu ignorēji Annu?", is_owner=False, is_direct=True), I.NO)
        self.assertEqual(I.self_inquiry_signal(
            "kāpēc tu ignorēji Annu?", is_owner=True, is_direct=False), I.NO)
        self.assertEqual(I.self_inquiry_signal("   ", is_owner=True, is_direct=True), I.NO)

    def test_the_supervisor_decides_before_any_investigation(self):
        asked = []

        def supervisor(acfg, text, chat_fn=None):
            asked.append(text)
            return False

        def must_not_run(*args, **kwargs):
            raise AssertionError("investigated without the supervisor's vote")

        text = "Anton, kāpēc tu tikko ignorēji Annu?"
        with patch.object(M.rig_agent, "owner_allowed", return_value=True), \
                patch.object(M.rig_agent, "match", return_value=None), \
                patch.object(M.obligations, "is_direct_request", return_value=True), \
                patch.object(I, "supervisor_says_investigate", side_effect=supervisor), \
                patch.object(M.obligations, "start_investigation", side_effect=must_not_run), \
                patch.object(I, "investigate", side_effect=must_not_run):
            out = M._self_inquiry_reply(text, OWNER, _acfg(), CONTACT, owner_authored=True)
        self.assertIsNone(out)
        self.assertEqual(asked, [text])


class GreetingTests(unittest.TestCase):
    def test_the_test_endpoint_hands_a_greeting_to_the_model(self):
        cfg = {"auto_reply": _acfg(), "providers": {}}
        with patch.object(M, "load_config", return_value=cfg), \
                patch.object(M, "_self_inquiry_reply", return_value=None), \
                patch.object(M, "_rig_reply_allowed", return_value=False), \
                patch.object(M, "_generate_reply",
                             return_value=("MODEL REPLY", "chat")) as generate, \
                patch.object(M, "_identity_confused_reply", return_value=False):
            for text in ("Čau!", "Čau, kā iet?", "Sveiks Anton"):
                with self.subTest(text=text):
                    out = M._generate_test_reply_inner(
                        text=text, sender=PARTNER, chat_guid=CONTACT)
                    self.assertEqual((out["reply"], out["mode"]), ("MODEL REPLY", "chat"))
        self.assertEqual(generate.call_count, 3)

    def test_a_bare_greeting_in_an_empty_chat_reaches_the_model_path(self):
        entry = {"text": "Čau", "sender": PARTNER, "is_from_me": False,
                 "attachments": [], "ts": 1789400000.0, "message_guid": "g-1"}
        with patch.object(M, "_statute_first_reply", return_value=""), \
                patch.object(M, "_sun_reply", return_value=None), \
                patch.object(M, "_build_reply_messages", return_value=[
                    {"role": "system", "content": "persona"},
                    {"role": "user", "content": "Čau"}]), \
                patch.object(M, "_polish_reply", side_effect=lambda text, _guid=None: text), \
                patch.object(M, "_release_comfy_for_llm", side_effect=_ReachedTheModelPath):
            with self.assertRaises(_ReachedTheModelPath):
                M._generate_reply(_acfg(), {}, [entry], CONTACT)


if __name__ == "__main__":
    unittest.main()
