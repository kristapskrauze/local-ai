# -*- coding: utf-8 -*-
"""Build routing is Anton's decision, not a word list.

Offline. The model call is faked and reports the turn as cancelled, so the
lane returns right after the prompt is captured: nothing is finalized,
repaired, sent or recorded.

    py -3 tests/build_routing_cases.py
"""
import io
import os
import sys
import types
import unittest
from unittest.mock import patch


sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.people import PARTNER_DAT
from app import agent_tools as T  # noqa: E402
from app import messaging as M  # noqa: E402


SELF = "iMessage;-;build-routing-self-test"
CONTACT = "iMessage;-;+37100000077"
PARTNER = "+37100000077"
OWNER = "owner-build-routing@example.com"
WORDED = ("Add this to my goals, as a self-hosted ecosystem. The setup I would "
          "build: make the homelab around Home Assistant. Wearable biometrics: "
          "this is harder.")
PLAIN = "Kā tev šodien iet?"
HISTORY = [
    {"role": "user", "content": "Vai rīt līs?"},
    {"role": "assistant", "content": "Rīt Rīgā sola nelielu lietu."},
]
SPEC = {"slug": "demo-page", "goal": "A small page with a counter.", "ios9": False}


def _acfg(**extra):
    cfg = {
        "assistant_chats": [SELF],
        "assistant_enabled": True,
        "assistant_agent_tools": "active",
        "assistant_agent_live_tools": ["web_search", "search_chat_history",
                                       "antons_read_code", "antons_build"],
        "assistant_model": "qwen-test",
        "model": "qwen-test",
        "agent_thinking": False,
    }
    cfg.update(extra)
    return cfg


def _capture(latest, history=HISTORY, spec=None, prepass=None, **kwargs):
    """Run the self-chat agent lane up to the model call; return its arguments."""
    seen = {}

    def fake_agent(**agent_kwargs):
        seen.update(agent_kwargs)
        return types.SimpleNamespace(
            answer="", tool_steps=0, model_calls=0, trace_id="t-build-routing",
            loop_limit_reached=False, cancelled=True, tool_results=[],
            metrics=lambda: {})

    messages = ([{"role": "system", "content": "You are ANTONS."}]
                + [dict(m) for m in history]
                + [{"role": "user", "content": latest}])
    prepass_patch = (patch.object(M, "_lookup_prepass", side_effect=prepass) if prepass
                     else patch.object(M, "_lookup_prepass", return_value=""))
    with patch.object(M.qwen_agent, "run_agent_turn", side_effect=fake_agent), \
            prepass_patch, \
            patch.object(M, "_assistant_agent_enabled", return_value=True), \
            patch.object(M, "_recent_sent_replies", return_value=[]), \
            patch.object(T, "build_spec_for", return_value=dict(spec or {})):
        M._live_assistant_agent_reply(_acfg(), messages, SELF, latest, latest,
                                      owner_authored=True, **kwargs)
    return seen


def _notes(seen):
    return [m.get("content") for m in seen["conversation"] if m.get("role") == "system"]


def _talk(seen):
    return [{"role": m.get("role"), "content": m.get("content")}
            for m in seen["conversation"] if m.get("role") != "system"]


def _route(text, spec=None, verdict="NO", sender=PARTNER, owner=False, thinks=False):
    asked = []

    def fake_chat(cfg, messages, options=None, format_schema=None, think=False):
        asked.append(messages)
        return verdict

    with patch.object(M, "_ollama_chat", side_effect=fake_chat), \
            patch.object(M, "_assistant_agent_enabled", return_value=True), \
            patch.object(M, "_assistant_external_write_allowed", return_value=False), \
            patch.object(M, "_agent_lane_is_live", return_value=False), \
            patch.object(M, "_contact_turn_needs_thinking", return_value=thinks), \
            patch.object(M.rig_agent, "owner_allowed", return_value=owner), \
            patch.object(T, "build_spec_for", return_value=dict(spec or {})):
        why = M._trusted_contact_action_agent_needed(
            _acfg(partner_handles=[PARTNER]), text, CONTACT, sender)
    return why, asked


class SelfChatPromptTests(unittest.TestCase):
    def test_build_words_change_nothing_in_the_prompt(self):
        worded, plain = _capture(WORDED), _capture(PLAIN)
        self.assertIn("antons_build", worded["tool_policy"].allowed_tools)
        self.assertEqual(_notes(worded), _notes(plain))
        self.assertEqual(_talk(worded), HISTORY + [{"role": "user", "content": WORDED}])
        self.assertEqual(_talk(plain), HISTORY + [{"role": "user", "content": PLAIN}])

    def test_a_page_built_here_is_offered_the_same_way_whatever_the_words(self):
        worded, plain = _capture(WORDED, spec=SPEC), _capture(PLAIN, spec=SPEC)
        self.assertEqual(_notes(worded), _notes(plain))
        self.assertTrue(any('files: "demo-page"' in note for note in _notes(plain)))

    def test_a_message_pasted_again_drops_the_stale_copy_and_its_answer(self):
        history = [{"role": "user", "content": WORDED},
                   {"role": "assistant", "content": "Vai sākt?"}] + HISTORY
        seen = _capture(WORDED, history=history)
        self.assertEqual(_talk(seen), HISTORY + [{"role": "user", "content": WORDED}])
        self.assertTrue(any("tikko atsūtīja TIEŠI TO PAŠU" in note for note in _notes(seen)))

    def test_no_earlier_copy_is_not_a_repaste(self):
        seen = _capture(WORDED)
        self.assertFalse(any("tikko atsūtīja TIEŠI TO PAŠU" in note for note in _notes(seen)))


class ContactRoutingTests(unittest.TestCase):
    def test_the_model_saying_a_partner_message_is_about_the_page_routes_it(self):
        for verdict in ("YES", "yes.", "Jā"):
            with self.subTest(verdict=verdict):
                why, asked = _route("šodien bija forša diena", spec=SPEC, verdict=verdict)
                self.assertEqual(why, "answer")
                self.assertEqual(len(asked), 1)
                self.assertIn("demo-page", str(asked[0]))

    def test_build_words_do_not_route_what_the_model_says_is_not_about_it(self):
        why, asked = _route("make it harder, build a new game and add levels",
                            spec=SPEC, verdict="NO")
        self.assertEqual(why, "")
        self.assertEqual(len(asked), 1)

    def test_no_page_in_this_chat_means_no_question_about_one(self):
        why, asked = _route("make it harder", spec=None, verdict="YES")
        self.assertEqual(why, "")
        self.assertEqual(asked, [])

    def test_owner_build_words_go_through_the_think_classifier(self):
        self.assertEqual(_route(f"uztaisi {PARTNER_DAT} spēli", sender=OWNER, owner=True,
                                thinks=False)[0], "")
        self.assertEqual(_route(f"uztaisi {PARTNER_DAT} spēli", sender=OWNER, owner=True,
                                thinks=True)[0], "answer")


class AuthorshipTests(unittest.TestCase):

    def _lane_kwargs(self, is_from_me):
        seen = {}

        def fake_lane(*args, **kwargs):
            seen.update(kwargs)
            return ("stub", "assistant-agent")

        entry = {"text": "Uztaisi man jaunu spēli", "sender": OWNER,
                 "is_from_me": is_from_me, "attachments": [], "ts": 1789400000.0,
                 "message_guid": "authorship-1"}
        with patch.object(M, "_statute_first_reply", return_value=""), \
                patch.object(M, "_sun_reply", return_value=None), \
                patch.object(M, "_release_comfy_for_llm"), \
                patch.object(M, "_assistant_agent_enabled", return_value=True), \
                patch.object(M, "_build_reply_messages", return_value=[
                    {"role": "system", "content": "You are ANTONS."},
                    {"role": "user", "content": entry["text"]}]), \
                patch.object(M, "_live_assistant_agent_reply", side_effect=fake_lane):
            M._generate_reply(_acfg(), {}, [entry], SELF)
        return seen

    def test_the_self_chat_lane_is_told_who_wrote_the_turn(self):
        self.assertIs(self._lane_kwargs(True).get("owner_authored"), True)
        self.assertIs(self._lane_kwargs(False).get("owner_authored"), False)

    def test_an_owner_coding_turn_skips_the_lookup_prepass(self):
        seen = _capture(WORDED, prepass=AssertionError("pre-pass ran on an owner coding turn"))
        self.assertIn("conversation", seen)


if __name__ == "__main__":
    unittest.main()
