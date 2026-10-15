# -*- coding: utf-8 -*-
"""What the model reads carries no long dash, so its replies do not either.

No model, no network: the agent run is faked to capture what it is handed.

    py -3 tests/prompt_punctuation_cases.py
"""
import io
import os
import sys
import unittest
from unittest import mock

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import messaging as M  # noqa: E402

DASH = "—"
CHAT = "iMessage;-;+37120000000"


def stray_dashes(text):
    return str(text or "").replace("(%s)" % DASH, "").count(DASH)


class _Captured(Exception):
    pass


def agent_convo(trusted_contact_action, owner_authored, text="Kas jauns?"):
    seen = []

    def fake_run(conversation=None, interface_context=None, **_kw):
        if getattr(interface_context, "interface", "") == "bluebubbles_lookup_prepass":
            raise _Captured()          # the pre-pass: nothing to read here
        seen.extend(conversation or [])
        raise _Captured()

    patches = [mock.patch.object(M.qwen_agent, "run_agent_turn", fake_run),
               mock.patch.object(M, "_lookup_prepass", lambda *a, **k: ""),
               mock.patch.object(M, "_recent_sent_replies", lambda *a, **k: [])]
    for p in patches:
        p.start()
    try:
        M._live_assistant_agent_reply(
            {}, [{"role": "system", "content": "SYSTEM"}, {"role": "user", "content": text}],
            CHAT, text, text, sender="+37120000000",
            trusted_contact_action=trusted_contact_action, owner_authored=owner_authored,
            accept_without_action=True)
    except _Captured:
        pass
    finally:
        for p in reversed(patches):
            p.stop()
    return seen


class PromptPunctuationTests(unittest.TestCase):
    def test_the_contact_persona(self):
        self.assertEqual(stray_dashes(M._AI_PERSONA), 0)
        self.assertIn("(%s)" % DASH, M._AI_PERSONA)      # the rule is there

    def test_the_voice_lines(self):
        for ai, assistant in ((True, False), (False, True), (False, False)):
            self.assertEqual(stray_dashes(M._live_reply_voice(ai, assistant)), 0)

    def test_the_notes_a_contact_chat_turn_adds(self):
        convo = agent_convo(trusted_contact_action=True, owner_authored=False)
        self.assertTrue(convo)
        for m in convo:
            if m.get("role") == "system":
                self.assertEqual(stray_dashes(m.get("content")), 0, m.get("content")[:200])

    def test_the_notes_his_own_turns_add(self):
        for trusted in (True, False):
            convo = agent_convo(trusted_contact_action=trusted, owner_authored=True)
            self.assertTrue(convo)
            for m in convo:
                if m.get("role") == "system":
                    self.assertEqual(stray_dashes(m.get("content")), 0,
                                     m.get("content")[:200])


if __name__ == "__main__":
    unittest.main()
