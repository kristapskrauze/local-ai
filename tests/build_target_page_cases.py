# -*- coding: utf-8 -*-
"""antons_build changes an existing page only when the call names it.

No model runs; the build thread is never started.

    py -3 tests/build_target_page_cases.py
"""
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import agent_tools as A  # noqa: E402
from app import messaging as M  # noqa: E402
from app import rig_agent  # noqa: E402

OWNER = "owner@example.com"
SELF_CHAT = "iMessage;-;" + OWNER
PARTNER_ID = "+37120000000"
PARTNER_CHAT = "iMessage;-;" + PARTNER_ID
PAGE = ("<!doctype html><html><head><title>Mahjong Pāri</title></head><body>"
        + "<p>" + "x" * 600 + "</p><script>var level = 1;</script></body></html>")


class _Thread:
    started = []

    def __init__(self, target=None, args=(), name="", daemon=None):
        self.args = args

    def start(self):
        _Thread.started.append(self.args)


class BuildTargetTests(unittest.TestCase):
    def setUp(self):
        d = Path(tempfile.mkdtemp(prefix="build_target_"))
        (d / "tile-mahjong.html").write_text(PAGE, encoding="utf-8")
        (d / "2048.html").write_text(PAGE.replace("Mahjong Pāri", "2048"), encoding="utf-8")
        specs = d / "build_specs.json"
        specs.write_text('{"%s": {"slug": "tile-mahjong", "goal": "tile game", '
                         '"ios9": false, "ts": 9999999999}}' % PARTNER_CHAT, encoding="utf-8")
        self.dir = d
        _Thread.started = []
        # the fake thread never runs _run_build, so nothing clears the
        # in-flight mark: one test's build would read as "already building"
        # in the next
        A._BUILDS_IN_FLIGHT.clear()
        self.addCleanup(A._BUILDS_IN_FLIGHT.clear)
        self.ledger = []
        cfg = {"auto_reply": {"partner_handles": [PARTNER_ID], "assistant_chats": [SELF_CHAT]}}
        for p in (patch.object(A, "_BUILD_STATIC", d),
                  patch.object(A, "_BUILD_SPECS_PATH", specs),
                  patch.object(A, "_append_action_ledger", self.ledger.append),
                  patch.object(A.threading, "Thread", _Thread),
                  patch.object(M, "load_config", return_value=cfg),
                  patch.object(M, "_assistant_external_write_allowed", return_value=False),
                  patch.object(rig_agent, "owner_allowed", lambda s, a=None: s == OWNER)):
            p.start()
            self.addCleanup(p.stop)

    def _build(self, sender, chat, owner_authored=False, **args):
        ctx = A.AgentContext(sender=sender, conversation_id=chat,
                             owner_authored=owner_authored)
        return A._tool_antons_build(dict({"goal": "Jauna spēle: kurmju ķeršana, 3x3"}, **args), ctx)

    def test_no_page_named_builds_a_new_page_not_the_latest_one(self):
        out = self._build(OWNER, SELF_CHAT)
        self.assertNotEqual(out["slug"], "tile-mahjong", out)
        self.assertFalse(out["editing"], out)
        self.assertEqual(_Thread.started[-1][-1], "")          # no base page
        self.assertEqual((self.dir / "tile-mahjong.html").read_text(encoding="utf-8"), PAGE)

    def test_her_request_without_a_name_builds_her_a_new_page(self):
        out = self._build(PARTNER_ID, PARTNER_CHAT)
        self.assertTrue(out["started"], out)
        self.assertFalse(out["editing"], out)
        self.assertNotEqual(out["slug"], "tile-mahjong", out)
        self.assertEqual((self.dir / "tile-mahjong.html").read_text(encoding="utf-8"), PAGE)

    def test_someone_else_still_cannot_build(self):
        with self.assertRaises(A.ToolRefused):
            self._build("+37129999999", "iMessage;-;+37129999999")
        self.assertEqual(_Thread.started, [])

    def test_his_request_typed_in_her_chat_counts_as_his(self):
        out = self._build(PARTNER_ID, PARTNER_CHAT, owner_authored=True)
        self.assertFalse(out["editing"], out)
        self.assertNotEqual(out["slug"], "tile-mahjong", out)

    def test_a_named_page_is_matched_the_way_it_is_written(self):
        out = self._build(OWNER, SELF_CHAT, files="Tile-Mahjong.html")
        self.assertEqual(out["slug"], "tile-mahjong", out)
        self.assertTrue(out["editing"], out)
        self.assertEqual(_Thread.started[-1][-1], PAGE)        # edited from the page

    def test_she_can_still_change_her_page_by_name(self):
        out = self._build(PARTNER_ID, PARTNER_CHAT, files="tile-mahjong")
        self.assertTrue(out["editing"], out)
        self.assertEqual(out["slug"], "tile-mahjong")


if __name__ == "__main__":
    unittest.main()
