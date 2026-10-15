# -*- coding: utf-8 -*-
"""The rig lookups can be chosen by the model, under the same permission rules.

Offline: the skills and both configs are faked; nothing reads the rig, the
calendar or Jellyfin.

    py -3 tests/rig_lookup_cases.py
"""
import io
import os
import sys
import unittest
from unittest.mock import patch


sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import agent_tools as T  # noqa: E402
from app import messaging as M  # noqa: E402
from app import rig_agent as R  # noqa: E402


OWNER = "owner-rig-lookup@example.com"
PARTNER = "+37100000079"
FRIEND = "+37100000080"
CHAT = "iMessage;-;rig-lookup-test"
RIG_CFG = {"enabled": True, "owners": [OWNER]}
ACFG = {"partner_handles": [PARTNER]}


def _fake(name):
    def skill(text, lang, ctx=None):
        return "%s answer" % name
    return skill


def _patched():
    fakes = {name: _fake(name) for name in T.RIG_LOOKUP_SKILLS}
    return (patch.object(R, "load_config", return_value=dict(RIG_CFG)),
            patch.dict(R._SKILL_BY_NAME, fakes),
            patch.dict(R._SKILL_WANTS_CTX, {name: True for name in fakes}),
            patch.object(R, "_watch_allowed", return_value=False))


class AnswerSkillTests(unittest.TestCase):
    def setUp(self):
        self._patches = _patched()
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()

    def test_the_owner_gets_a_private_lookup_by_name(self):
        out = R.answer_skill("gpu", "", OWNER, ACFG, chat_guid=CHAT)
        self.assertEqual((out or {}).get("text"), "gpu answer")

    def test_a_private_lookup_is_not_for_anyone_else(self):
        self.assertIsNone(R.answer_skill("disks", "", FRIEND, ACFG, chat_guid=CHAT))
        self.assertIsNone(R.answer_skill("calendar", "", PARTNER, ACFG, chat_guid=CHAT))

    def test_voicenote_needs_no_owner_and_kino_follows_the_owner_gate(self):
        # voicenote repeats our own message into the same chat, so anyone may
        # ask. kino falls through to the owner gate in answer_for, as it always
        # did, although _rig_reply_allowed's comment calls the feed public; the
        # refactor keeps the behaviour, not the comment.
        self.assertEqual((R.answer_skill("voicenote", "", FRIEND, ACFG, chat_guid=CHAT) or {})
                         .get("text"), "voicenote answer")
        self.assertIsNone(R.answer_skill("kino", "", FRIEND, ACFG))
        self.assertEqual((R.answer_skill("kino", "", OWNER, ACFG) or {}).get("text"),
                         "kino answer")

    def test_watch_still_needs_a_jellyfin_account(self):
        self.assertIsNone(R.answer_skill("watch", "", FRIEND, ACFG))

    def test_a_name_that_is_not_a_skill_answers_nothing(self):
        self.assertIsNone(R.answer_skill("rm -rf", "", OWNER, ACFG))

    def test_answer_for_still_picks_by_words_and_answers_the_same(self):
        out = R.answer_for("cik grādi gpu?", OWNER, ACFG, chat_guid=CHAT)
        self.assertEqual((out or {}).get("text"), "gpu answer")


class RigLookupToolTests(unittest.TestCase):
    def setUp(self):
        self._patches = _patched() + (
            patch.object(M, "load_config", return_value={"auto_reply": dict(ACFG)}),)
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()

    def _call(self, skill, sender):
        ctx = T.AgentContext(sender=sender, conversation_id=CHAT, turn_text="kā iet rigam?")
        return T._tool_rig_lookup({"skill": skill}, ctx)

    def test_the_model_picks_a_lookup_and_the_owner_gets_it(self):
        out = self._call("temps", OWNER)
        self.assertEqual((out["ok"], out["text"]), (True, "temps answer"))

    def test_not_permitted_is_reported_not_invented(self):
        self.assertEqual(self._call("models", FRIEND)["error_type"], "not_available")

    def test_an_unknown_lookup_is_refused(self):
        with self.assertRaises(T.ToolRefused):
            self._call("shell", OWNER)

    def test_every_offered_lookup_is_a_real_skill_and_the_self_chat_has_the_tool(self):
        self.assertTrue(set(T.RIG_LOOKUP_SKILLS) <= set(R._SKILL_BY_NAME))
        self.assertIn("rig_lookup", M._LIVE_ASSISTANT_AGENT_TOOLS)
        self.assertNotIn("rig_lookup", M._LIVE_TRUSTED_CONTACT_ACTION_TOOLS)


class SelfChatRoutingTests(unittest.TestCase):

    SELF = "iMessage;-;rig-lookup-self-test"

    def _preview(self, text):
        acfg = {"assistant_chats": [self.SELF], "assistant_enabled": False,
                "enabled": False, "allowed_senders": [OWNER], "model": "qwen-test"}
        router = []

        def fake_router(text, sender, acfg, chat_guid=None):
            router.append(text)
            return {"skill": "gpu", "text": "router answer", "lang": "lv"}

        with patch.object(M, "load_config",
                          return_value={"auto_reply": acfg, "providers": {}}), \
                patch.object(M, "_self_inquiry_reply", return_value=None), \
                patch.object(M, "_rig_agent_reply", side_effect=fake_router), \
                patch.object(M, "_generate_reply",
                             return_value=("ANTON", "assistant-agent")), \
                patch.object(M, "_identity_confused_reply", return_value=False):
            out = M._generate_test_reply_inner(text=text, sender=OWNER,
                                               chat_guid=self.SELF, from_me=True)
        return out, router

    def test_a_plain_rig_question_in_his_chat_goes_to_anton(self):
        for text in ("cik grādi ir GPU?", "cik brīvas vietas diskā?",
                     "kādi modeļi ielādēti?"):
            with self.subTest(text=text):
                out, router = self._preview(text)
                self.assertEqual((out["reply"], out["mode"]), ("ANTON", "assistant-agent"))
                self.assertEqual(router, [])

    def test_an_explicit_command_still_runs_directly(self):
        out, router = self._preview("/gpu")
        self.assertEqual(out["mode"], "rig:gpu")
        self.assertEqual(router, ["/gpu"])

    def test_voicenote_stays_on_the_direct_path(self):
        _out, router = self._preview("ko tu teici balss ziņā?")
        self.assertEqual(router, ["ko tu teici balss ziņā?"])


if __name__ == "__main__":
    unittest.main()
