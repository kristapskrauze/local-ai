"""The model answers first, in every chat. Word lanes do not pre-empt it.

No model, network or live-store call is made.

    python tests/model_answers_first_cases.py
"""
import os
import sys
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from app import messaging as M  # noqa: E402

OWNER = "owner@example.com"
SELF = "iMessage;-;" + OWNER
PARTNER = "+37100000000"
CONTACT = "iMessage;-;" + PARTNER

WORDED = {
    "wall: instructions": "Instrukcijā vienīgie piemēri ir tas, ko nerakstīt.",
    "wall: code": "Uzraksti python funkciju, kas pārbauda atvadu rindu.",
    "wall: password": "Kāda ir wifi parole mājās?",
    "bible": "Svētdienas rītā iedod man kādu pantu.",
    "clock": "Cik pulkstenis?",
    "sun": "Cikos šodien lec saule Rīgā?",
}


def _acfg():
    return {"assistant_chats": [SELF], "assistant_agent_tools": "active"}


def _entry(chat, text):
    return {"text": text, "sender": OWNER if chat == SELF else PARTNER,
            "is_from_me": chat == SELF, "attachments": [],
            "ts": 1789400000.0, "message_guid": "maf-1"}


def _messages(text):
    return [{"role": "system", "content": "persona"},
            {"role": "user", "content": text}]


def _first_lane(chat, text):
    """Run the shared reply function with the agent stubbed; report the order
    in which lanes were reached and what the agent was handed."""
    order, seen = [], {}

    def fake_agent(*args, **kwargs):
        order.append("agent")
        seen.update(kwargs)
        return ("MODEL", "assistant-agent")

    def statute(*_a, **_k):
        order.append("statute")
        return ""

    def bible(*_a, **_k):
        order.append("bible")
        return "VERSE"

    with patch.object(M, "_statute_first_reply", side_effect=statute), \
            patch.object(M, "_bible_direct_reply", side_effect=bible), \
            patch.object(M, "_release_comfy_for_llm"), \
            patch.object(M, "_build_reply_messages", return_value=_messages(text)), \
            patch.object(M, "_live_assistant_agent_reply", side_effect=fake_agent):
        out = M._generate_reply(_acfg(), {}, [_entry(chat, text)], chat)
    return out, order, seen


class _ReachedTheLegacyModelPath(Exception):
    pass


def _fallback(chat, text):
    """The agent is off: only the legacy lanes run. Reaching the point where
    they need the LLM proves no word lane answered on the way there."""
    with patch.object(M, "_assistant_agent_enabled", return_value=False), \
            patch.object(M, "_statute_first_reply", return_value=""), \
            patch.object(M, "_bible_direct_reply", return_value="VERSE"), \
            patch.object(M, "_build_reply_messages", return_value=_messages(text)), \
            patch.object(M, "_release_comfy_for_llm",
                         side_effect=_ReachedTheLegacyModelPath):
        return M._generate_reply(_acfg(), {}, [_entry(chat, text)], chat)


class ModelAnswersFirstTests(unittest.TestCase):
    def test_his_chat_gives_every_worded_turn_to_the_model_first(self):
        for name, text in WORDED.items():
            with self.subTest(name):
                out, order, seen = _first_lane(SELF, text)
                self.assertEqual(out, ("MODEL", "assistant-agent"))
                self.assertEqual(order[:1], ["agent"], order)
                self.assertIs(seen.get("trusted_contact_action"), False)
                self.assertIs(seen.get("owner_authored"), True)

    def test_a_contact_chat_gives_every_worded_turn_to_the_model_first(self):
        for name, text in WORDED.items():
            with self.subTest(name):
                out, order, seen = _first_lane(CONTACT, text)
                self.assertEqual(out, ("MODEL", "assistant-agent"))
                self.assertEqual(order[:1], ["agent"], order)
                self.assertIs(seen.get("trusted_contact_action"), True)
                self.assertIs(seen.get("owner_authored"), False)
                self.assertIs(seen.get("accept_without_action"), True)

    def test_the_canned_walls_are_gone(self):
        self.assertFalse(hasattr(M, "_canned_guard"))
        self.assertFalse(hasattr(M, "_GUARD_RULES"))

    def test_with_the_agent_off_no_wall_or_verse_answers(self):
        for chat in (SELF, CONTACT):
            for name in ("wall: instructions", "wall: code", "bible"):
                with self.subTest(chat=chat, name=name):
                    with self.assertRaises(_ReachedTheLegacyModelPath):
                        _fallback(chat, WORDED[name])

    def test_when_the_agent_cannot_answer_no_word_lane_decides_either(self):
        lanes = []

        def lane(name, value):
            def f(*_a, **_k):
                lanes.append(name)
                return value
            return f

        for chat in (SELF, CONTACT):
            for text in ("Cik pulkstenis?", "Cikos šodien lec saule Rīgā?",
                         "Ko darba likums saka par virsstundām?",
                         "Kas jauns Latvijā šodien?"):
                with self.subTest(chat=chat, text=text):
                    del lanes[:]
                    with patch.object(M, "_live_assistant_agent_reply", return_value=None), \
                            patch.object(M, "_release_comfy_for_llm"), \
                            patch.object(M, "_build_reply_messages", return_value=_messages(text)), \
                            patch.object(M, "_statute_first_reply", side_effect=lane("statute", "LAW")), \
                            patch.object(M, "_sun_reply", side_effect=lane("sun", "SUN")), \
                            patch.object(M, "_research_intent", side_effect=lane("research", ("x", ""))), \
                            patch.object(M, "_deep_reply", side_effect=lane("deep", "DEEP")), \
                            patch.object(M, "_personal_facts", side_effect=lane("facts", "BLOCKED")), \
                            patch.object(M, "_wants_fact", return_value=True), \
                            patch.object(M, "_ollama_chat", side_effect=_ReachedTheLegacyModelPath):
                        with self.assertRaises(_ReachedTheLegacyModelPath):
                            M._generate_reply(_acfg(), {}, [_entry(chat, text)], chat)
                    self.assertEqual(lanes, [], lanes)


class TestEndpointTests(unittest.TestCase):
    """The preview must take the lane the live path takes. Live, with agentic
    routing active, the model owns every reply turn, so the rig word router
    ("kino", "balss ziņa", "novelc") must not answer first in the preview."""

    def _run(self, chat, text, agentic="active"):
        cfg = {"auto_reply": dict(_acfg(), agentic_routing=agentic,
                                  assistant_enabled=True, enabled=True),
               "providers": {}}
        with patch.object(M, "load_config", return_value=cfg), \
                patch.object(M, "_self_inquiry_reply", return_value=None), \
                patch.object(M, "_rig_agent_reply",
                             return_value={"text": "RIG", "skill": "kino"}) as rig, \
                patch.object(M, "_generate_reply",
                             return_value=("MODEL", "assistant-agent")) as gen, \
                patch.object(M, "_identity_confused_reply", return_value=False):
            out = M._generate_test_reply_inner(
                text=text, sender=OWNER if chat == SELF else PARTNER,
                chat_guid=chat, from_me=chat == SELF)
        return out, rig, gen

    def test_worded_rig_lookups_go_to_the_model_in_both_chats(self):
        for chat in (SELF, CONTACT):
            for text in ("Kas šodien rāda kino?", "Ko tu ierunāji tajā balss ziņā?"):
                with self.subTest(chat=chat, text=text):
                    out, rig, gen = self._run(chat, text)
                    rig.assert_not_called()
                    self.assertEqual(out["reply"], "MODEL")

    def test_an_explicit_command_still_runs_directly(self):
        out, rig, _gen = self._run(SELF, "/kino")
        rig.assert_called_once()
        self.assertEqual(out["reply"], "RIG")

    def test_the_self_inquiry_lane_does_not_answer_before_the_model(self):
        cfg = {"auto_reply": dict(_acfg(), agentic_routing="active",
                                  assistant_enabled=True, enabled=True),
               "providers": {}}
        text = "Anton, servisa žurnālā redzu, ka web_search nolūzt; palielini limitu."
        with patch.object(M, "load_config", return_value=cfg), \
                patch.object(M, "_self_inquiry_reply",
                             return_value="PROMISE") as inquiry, \
                patch.object(M, "_generate_reply",
                             return_value=("MODEL", "assistant-agent")), \
                patch.object(M, "_identity_confused_reply", return_value=False):
            out = M._generate_test_reply_inner(text=text, sender=OWNER,
                                               chat_guid=SELF, from_me=True)
        inquiry.assert_not_called()
        self.assertEqual(out["reply"], "MODEL")


class LivePathTests(unittest.TestCase):
    def test_a_reply_verdict_without_a_subject_is_the_models_turn(self):
        cfg = {"auto_reply": {"provider": "bluebubbles_http",
                              "max_replies_per_hour": 20,
                              "assistant_chats": [SELF],
                              "agentic_routing": "active",
                              "assistant_agent_tools": "active",
                              "image_gen": True},
               "providers": {"bluebubbles_http": {}}}
        text = "Uzraksti man dzejolīti par rudeni"
        entry = dict(_entry(CONTACT, text), chat_guid=CONTACT)
        route = M._watcher_verdict("reply", "unknown", "none", 0.9,
                                   "she asked for a poem")
        chat = CONTACT + ";model-owned-unknown-subject"
        M._chat_gen_state.pop(chat, None)
        calls = []

        def fake_agent(*_a, **kwargs):
            calls.append(kwargs)
            return ("MODEL", "assistant-agent")

        with patch.object(M, "load_config", return_value=cfg), \
                patch.object(M, "_bb_mark_read"), \
                patch.object(M, "_recent_auto_reply_count", return_value=0), \
                patch.object(M, "_mode_command", return_value=None), \
                patch.object(M.obligations, "begin_generation", return_value=0), \
                patch.object(M.obligations, "resolve"), \
                patch.object(M, "_build_reply_messages", return_value=_messages(text)), \
                patch.object(M, "_live_assistant_agent_reply", side_effect=fake_agent), \
                patch.object(M, "_schedule_reply",
                             side_effect=AssertionError("schedule word lane ran")), \
                patch.object(M, "_maybe_react_to_incoming",
                             side_effect=AssertionError("tapback word lane ran")), \
                patch.object(M, "_image_request",
                             side_effect=AssertionError("image word lane ran")), \
                patch.object(M, "_turn_is_stale", return_value=False), \
                patch.object(M, "_owner_replied_after", return_value=False), \
                patch.object(M, "_append_jsonl"), \
                patch.object(M, "_send_reply_parts",
                             return_value={"ok": True}) as send:
            M._reply_to_batch(chat, [entry], route=route)
        self.assertEqual(len(calls), 1)
        self.assertIs(calls[0].get("trusted_contact_action"), True)
        send.assert_called_once()
        self.assertEqual(send.call_args.args[3].get("execution"), "model-owned")

    def test_an_open_request_that_overrides_silence_reaches_the_model_as_a_reply(self):
        cfg = {"auto_reply": {"provider": "bluebubbles_http",
                              "assistant_chats": [SELF],
                              "agentic_routing": "active"},
               "providers": {"bluebubbles_http": {}}}
        entry = dict(_entry(CONTACT, "Nu tad?"), chat_guid=CONTACT,
                     _arrived_at=1789400001.0)
        silent = M._watcher_verdict("silent", "conversation", "chat", 0.6,
                                    "short filler")
        handed = {}

        def fake_batch(chat_guid, entries, route=None, route_history=None):
            handed["route"] = route

        with patch.object(M, "load_config", return_value=cfg), \
                patch.object(M.obligations, "current",
                             return_value={"request_id": "R1"}), \
                patch.object(M, "_owner_replied_after", return_value=False), \
                patch.object(M, "_with_open_request_entries",
                             side_effect=lambda _c, e: e), \
                patch.object(M, "_watcher_history", return_value=[]), \
                patch.object(M, "_semantic_gate_decision",
                             return_value=("agentic watcher stayed silent: x", silent)), \
                patch.object(M, "_standing_obligation",
                             return_value={"request_id": "R1"}), \
                patch.object(M, "_append_jsonl"), \
                patch.object(M, "_reply_to_batch", side_effect=fake_batch):
            M._fire_auto_reply_inner(CONTACT, {"entries": [entry]})
        self.assertEqual((handed.get("route") or {}).get("action"), "reply")


if __name__ == "__main__":
    result = unittest.main(verbosity=2, exit=False).result
    if result.wasSuccessful():
        print("OK")
    sys.exit(0 if result.wasSuccessful() else 1)
