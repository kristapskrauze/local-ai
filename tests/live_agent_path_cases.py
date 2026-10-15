# -*- coding: utf-8 -*-
"""BlueBubbles assistant chats use the central Qwen agent core.

These are offline path tests. The model call is mocked, but the messaging
pipeline must choose the central agent lane rather than the old research/deep
intent branches for the personal assistant chat.

    py -3 tests/live_agent_path_cases.py
"""
import io
import os
import sys
import types
import unittest
from unittest.mock import patch


sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import messaging as M  # noqa: E402
from app import main as API  # noqa: E402


GUID = "iMessage;-;assistant-live-agent-test"


class LiveAgentPathTests(unittest.TestCase):
    def _acfg(self):
        return {
            "assistant_chats": [GUID],
            "assistant_enabled": True,
            "assistant_agent_tools": "active",
            "assistant_model": "qwen-test",
            "model": "qwen-test",
            "provider": "debug",
            "history_source": "local",
            "history_limit": 4,
            "grammar_fix": "off",
            "read_links": False,
            "web_access": True,
            "num_ctx": 4096,
        }

    def _entry(self, text):
        return {
            "provider": "bluebubbles",
            "event_type": "new-message",
            "chat_guid": GUID,
            "sender": "+37100000000",
            "text": text,
            "is_from_me": True,
            "message_guid": "m-agent-1",
            "ts": 1,
            "attachments": [],
        }

    def test_assistant_generation_uses_central_agent_before_legacy_research(self):
        seen = {}

        def fake_agent(**kwargs):
            seen.update(kwargs)
            return types.SimpleNamespace(
                answer="The central agent answered.",
                tool_steps=1,
                model_calls=2,
                trace_id="trace-live",
                loop_limit_reached=False,
                cancelled=False,
                tool_results=[{"tool": "web_search", "ok": True}],
                metrics=lambda: {"trace_id": "trace-live"},
            )

        with patch.object(M, "_build_reply_messages", return_value=[
            {"role": "system", "content": "You are ANTONS."},
            {"role": "user", "content": "Can you check the newest Qwen release?"},
        ]), patch.object(M.qwen_agent, "run_agent_turn", side_effect=fake_agent), \
                patch.object(M, "_research_reply",
                             side_effect=AssertionError("legacy research branch ran")), \
                patch.object(M, "_deep_reply",
                             side_effect=AssertionError("legacy deep branch ran")), \
                patch.object(M, "_polish_reply", side_effect=lambda text, _guid=None: text):
            reply, mode = M._generate_reply(
                self._acfg(), {}, [self._entry("Can you check the newest Qwen release?")],
                GUID)

        self.assertEqual(reply, "The central agent answered.")
        self.assertEqual(mode, "assistant-agent")
        self.assertEqual(seen["model"], "qwen-test")
        self.assertEqual(seen["interface_context"].interface, "bluebubbles_assistant")
        self.assertEqual(seen["interface_context"].conversation_id, GUID)
        self.assertIn("web_search", seen["tool_policy"].allowed_tools)
        self.assertIn("search_chat_history", seen["tool_policy"].allowed_tools)
        self.assertNotIn("download_file", seen["tool_policy"].allowed_tools)
        self.assertTrue(callable(seen["cancellation_token"]))

    def test_download_permission_does_not_unlock_goal_writes(self):
        download_only = set(API._agent_allowed_tools(
            allow_downloads=True, allow_goal_actions=False) or ())
        self.assertIn("download_file", download_only)
        self.assertNotIn("add_goal", download_only)
        self.assertNotIn("complete_goal", download_only)

        goals_only = set(API._agent_allowed_tools(
            allow_downloads=False, allow_goal_actions=True) or ())
        self.assertIn("add_goal", goals_only)
        self.assertNotIn("download_file", goals_only)

    def test_self_chat_false_tool_denial_is_retried(self):
        calls = []

        def fake_agent(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                return types.SimpleNamespace(
                    answer=("Es nevaru atrast šo flīzi, jo man nav pieejami "
                            "rīki `repo_read` vai `repo_search`."),
                    tool_steps=0,
                    model_calls=1,
                    trace_id="trace-denial",
                    loop_limit_reached=False,
                    cancelled=False,
                    tool_results=[],
                    metrics=lambda: {"trace_id": "trace-denial"},
                )
            return types.SimpleNamespace(
                answer="Atradu ar antons_read_code: Downloads flīze ir app/static/app.js.",
                tool_steps=1,
                model_calls=1,
                trace_id="trace-retry",
                loop_limit_reached=False,
                cancelled=False,
                tool_results=[{"tool": "antons_read_code", "ok": True}],
                metrics=lambda: {"trace_id": "trace-retry"},
            )

        # whether the reply is a false "I can't" is a model's reading since
        # a0582b1 -- answer that check as the model would; every other model
        # call stays unreachable here, as it always was
        def fake_judge(cfg, messages, options=None, format_schema=None, think=False):
            if messages and messages[0].get("content") == M._TOOL_DENIAL_SYS:
                return '{"false_denial": true}'
            raise RuntimeError("no model in this test")

        with patch.object(M, "_lookup_prepass", return_value=""), \
                patch.object(M, "_build_reply_messages", return_value=[
                    {"role": "system", "content": "You are ANTONS."},
                    {"role": "user", "content":
                     "kodā atrod vietu kur tiek definēta Downloads flīze"},
                ]), patch.object(M.qwen_agent, "run_agent_turn",
                                  side_effect=fake_agent), \
                patch.object(M, "_ollama_chat", side_effect=fake_judge), \
                patch.object(M, "_polish_reply", side_effect=lambda text, _guid=None: text):
            reply, mode = M._generate_reply(
                self._acfg(), {}, [self._entry(
                    "kodā atrod vietu kur tiek definēta Downloads flīze")],
                GUID)

        self.assertEqual(mode, "assistant-agent")
        self.assertIn("antons_read_code", reply)
        self.assertEqual(len(calls), 2)
        retry_messages = calls[1]["conversation"]
        self.assertTrue(any("repo_read" in str(m.get("content") or "")
                            and "antons_read_code" in str(m.get("content") or "")
                            for m in retry_messages
                            if isinstance(m, dict)))
        self.assertIn("antons_read_code", calls[1]["tool_policy"].allowed_tools)


if __name__ == "__main__":
    unittest.main()
