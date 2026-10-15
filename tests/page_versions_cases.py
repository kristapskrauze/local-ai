# -*- coding: utf-8 -*-
"""A page that was built over can be listed and brought back by Anton.

The games box is faked; nothing is published.

    py -3 tests/page_versions_cases.py
"""
import hashlib
import io
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app  # noqa: E402
from app.people import PARTNER_GEN
from app import agent_tools as A  # noqa: E402
from app import messaging as M  # noqa: E402
from app import rig_agent  # noqa: E402

OWNER = "owner@example.com"
PARTNER_ID = "+37120000000"          # a contact's handle (a placeholder number)


def _page(title):
    return ("<!doctype html><html><head><title>%s</title></head><body><p>%s</p>"
            "<script>var ok = true;</script></body></html>" % (title, "x" * 600)).encode("utf-8")


MOLES = _page("Ķirzakju Klupens")
MAHJONG = _page("Mahjong Pāri")
TILES = _page("Mahjong Pāri ")


def _sha(b):
    return hashlib.sha256(b).hexdigest()


class PageVersionsTests(unittest.TestCase):
    def setUp(self):
        d = Path(tempfile.mkdtemp(prefix="page_versions_"))
        (d / "tile-mahjong.html").write_bytes(MOLES)
        (d / "tile-mahjong.html.lastgood").write_bytes(MOLES)
        self.dir = d
        # the games box: what it serves now + two copies publishes replaced
        self.box = {"tile-mahjong.html": MOLES,
                    "tile-mahjong.html.bak-20260910T234416": TILES,
                    "tile-mahjong.html.bak-20260910T120225": MAHJONG}
        self.fetched, self.published, self.ledger = [], [], []

        def run(args, timeout=0):
            rows = ["%s\t%d\t%s\t%s" % (n, len(b), _sha(b),
                                        b.decode().split("<title>")[1].split("<")[0])
                    for n, b in self.box.items()]
            return {"ok": True, "output": "\n".join(rows)}

        def get(remote, local):
            self.fetched.append(remote)
            name = remote.split("/")[-1]
            if name not in self.box:
                return {"ok": False, "output": "no such file"}
            Path(local).write_bytes(self.box[name])
            return {"ok": True, "output": ""}

        fake_main = types.ModuleType("app.main")
        fake_main._run_media_password = run
        fake_main._sftp_from_media = get

        def publish(slug):
            self.published.append(slug)
            return {"ok": True, "url_owner": "http://x.7/%s.html" % slug,
                    "url_partner": "http://x.6/%s.html" % slug}

        cfg = {"auto_reply": {"partner_handles": [PARTNER_ID]}}
        for p in (patch.dict(sys.modules, {"app.main": fake_main}),
                  patch.object(app, "main", fake_main, create=True),
                  patch.object(A, "_BUILD_STATIC", d),
                  patch.object(A, "_publish_slug_to_media", publish),
                  patch.object(A, "_append_action_ledger", self.ledger.append),
                  patch.object(M, "load_config", return_value=cfg),
                  patch.object(rig_agent, "owner_allowed", lambda s, a=None: s == OWNER)):
            p.start()
            self.addCleanup(p.stop)
        self.ctx = A.AgentContext(sender=OWNER)

    def test_versions_list_the_rig_and_the_games_box(self):
        out = A._tool_antons_page_versions({"slug": "tile-mahjong"}, self.ctx)
        by = {v["version"]: v for v in out["versions"]}
        self.assertEqual(by["box:bak-20260910T120225"]["title"], "Mahjong Pāri")
        self.assertEqual(by["box:bak-20260910T120225"]["live_until"], "2026-09-10 12:02")
        self.assertEqual(by["box:current"]["live_until"], "live now")
        self.assertIn("current", by)
        self.assertIn("lastgood", by)

    def test_keep_the_moles_as_their_own_page_then_put_mahjong_back(self):
        out = A._tool_antons_restore_page(
            {"slug": "tile-mahjong", "version": "current", "to_slug": "kurmji"}, self.ctx)
        self.assertTrue(out["ok"], out)
        self.assertEqual((self.dir / "kurmji.html").read_bytes(), MOLES)
        out = A._tool_antons_restore_page(
            {"slug": "tile-mahjong", "version": "box:bak-20260910T120225"}, self.ctx)
        self.assertTrue(out["ok"], out)
        self.assertEqual((self.dir / "tile-mahjong.html").read_bytes(), MAHJONG)
        self.assertEqual(self.published, ["kurmji", "tile-mahjong"])
        self.assertEqual(self.fetched, ["pages/tile-mahjong.html.bak-20260910T120225"])

    def test_a_page_kept_nowhere_else_is_not_replaced(self):
        unsaved = _page(f"{PARTNER_GEN} neizsūtītā versija")
        (self.dir / "tile-mahjong.html").write_bytes(unsaved)
        out = A._tool_antons_restore_page(
            {"slug": "tile-mahjong", "version": "box:bak-20260910T120225"}, self.ctx)
        self.assertFalse(out["ok"], out)
        self.assertIn("to_slug", out["reason"])
        self.assertEqual((self.dir / "tile-mahjong.html").read_bytes(), unsaved)
        self.assertEqual(self.published, [])

    def test_unknown_or_escaping_versions_are_refused(self):
        for v in ("box:../../etc/passwd", "box:bak-x", "rig:../main.py", "whatever"):
            out = A._tool_antons_restore_page({"slug": "tile-mahjong", "version": v}, self.ctx)
            self.assertFalse(out["ok"], (v, out))
        self.assertEqual(self.fetched, [])
        self.assertEqual((self.dir / "tile-mahjong.html").read_bytes(), MOLES)

    def test_the_games_box_comes_first_without_hashes_and_snapshots_are_capped(self):
        for k in range(9):
            (self.dir / ("tile-mahjong.html.snap-%d" % k)).write_bytes(_page("old %d" % k))
        out = A._tool_antons_page_versions({"slug": "tile-mahjong"}, self.ctx)
        vs = [v["version"] for v in out["versions"]]
        self.assertTrue(vs[0].startswith("box:"), vs)
        self.assertEqual([v for v in vs if v.startswith("box:")], vs[:3], vs)
        self.assertFalse(any("sha256" in v for v in out["versions"]), out["versions"][0])
        self.assertEqual(sum(1 for v in vs if v.startswith("rig:")), 6, vs)
        self.assertEqual(out["older_rig_snapshots_not_shown"], 3)

    def test_an_applied_change_to_a_page_on_the_box_is_published(self):
        with patch.object(A, "_page_versions_box", lambda slug: (
                [{"version": "box:current"}] if slug == "home" else [])):
            out = A._publish_applied_pages(["app/static/home.html", "app/messaging.py",
                                            {"path": "app/static/git.html"}])
        self.assertEqual(self.published, ["home"])
        self.assertEqual([p["slug"] for p in out], ["home"])

    def test_only_the_owner_restores(self):
        with self.assertRaises(A.ToolRefused):
            A._tool_antons_restore_page(
                {"slug": "tile-mahjong", "version": "box:bak-20260910T120225"},
                A.AgentContext(sender=PARTNER_ID))


if __name__ == "__main__":
    unittest.main()
