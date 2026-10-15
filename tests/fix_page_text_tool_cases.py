# -*- coding: utf-8 -*-
"""antons_fix_page_text keeps new page text only when the page still works.

The text pass (app/page_texts.py) replaces what a player sees with the
translator's Latvian. The page is browser-tested before and after; a check that
newly fails puts the old page back. A page on the games box is published; a
rig-only page (git.html) is not.

Browser test, translator and games box are stand-ins.

    py -3 tests/fix_page_text_tool_cases.py
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
from app import build_selftest as BS  # noqa: E402
from app import messaging as M  # noqa: E402
from app import page_texts as P  # noqa: E402
from app import rig_agent  # noqa: E402

OWNER = "owner@example.com"
BEFORE = ("<!doctype html><html><head><title>Ķirzakju Klupens</title></head><body>"
          "<p>%s</p><script>var ok = 1;</script></body></html>" % ("x" * 600))
AFTER = BEFORE.replace("Ķirzakju Klupens", "Dauzi ķirzakas")
CHANGES = [{"was": "Ķirzakju Klupens", "en": "Whack the Lizards", "lv": "Dauzi ķirzakas"}]


class FixPageTextTests(unittest.TestCase):
    def setUp(self):
        d = Path(tempfile.mkdtemp(prefix="fix_text_"))
        (d / "kurmji.html").write_text(BEFORE, encoding="utf-8")
        self.path = d / "kurmji.html"
        self.published, self.ledger = [], []
        self.on_box = True
        for p in (patch.object(A, "_BUILD_STATIC", d),
                  patch.object(A, "_selftest_enabled", return_value=True),
                  patch.object(BS, "available", return_value=True),
                  patch.object(BS, "summary_line", return_value="testi: ok"),
                  patch.object(P, "retext", return_value=(AFTER, CHANGES)),
                  patch.object(A, "_page_versions_box", lambda slug: (
                      [{"version": "box:current"}] if self.on_box else [])),
                  patch.object(A, "_publish_slug_to_media", lambda slug: (
                      self.published.append(slug) or {"ok": True})),
                  patch.object(A, "_append_action_ledger", self.ledger.append),
                  patch.object(M, "load_config", return_value={"auto_reply": {}}),
                  patch.object(rig_agent, "owner_allowed", lambda s, a=None: s == OWNER)):
            p.start()
            self.addCleanup(p.stop)
        self.ctx = A.AgentContext(sender=OWNER)

    def _runs(self, before_fails, after_fails):
        return patch.object(BS, "run_selftest", side_effect=[
            {"failures": before_fails}, {"failures": after_fails}])

    def test_kept_and_published_when_nothing_new_fails(self):
        with self._runs(["check FAILED: old broken check"], ["check FAILED: old broken check"]):
            out = A._tool_antons_fix_page_text({"slug": "kurmji"}, self.ctx)
        self.assertTrue(out["ok"], out)
        self.assertEqual(self.path.read_text(encoding="utf-8"), AFTER)
        self.assertEqual(out["changes"], CHANGES)
        self.assertEqual(self.published, ["kurmji"])

    def test_a_newly_failing_check_puts_the_old_page_back(self):
        with self._runs([], ["check FAILED: title shows Ķirzakju Klupens"]):
            out = A._tool_antons_fix_page_text({"slug": "kurmji"}, self.ctx)
        self.assertFalse(out["ok"], out)
        self.assertTrue(out["reverted"])
        self.assertEqual(self.path.read_text(encoding="utf-8"), BEFORE)
        self.assertEqual(self.published, [])

    def test_a_rig_only_page_is_not_published(self):
        self.on_box = False
        with self._runs([], []):
            out = A._tool_antons_fix_page_text({"slug": "kurmji"}, self.ctx)
        self.assertTrue(out["ok"], out)
        self.assertFalse(out["published"])
        self.assertEqual(self.published, [])

    def test_only_the_owner(self):
        with self.assertRaises(A.ToolRefused):
            A._tool_antons_fix_page_text({"slug": "kurmji"},
                                         A.AgentContext(sender="+37120000000"))


if __name__ == "__main__":
    unittest.main()
