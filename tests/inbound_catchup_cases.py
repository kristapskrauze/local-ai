# -*- coding: utf-8 -*-
"""The catch-up stores what the BlueBubbles webhook never sent.

Fake BlueBubbles, temp inbox/outbox, placeholder handle -- nothing live.

    py -3 tests/inbound_catchup_cases.py
"""
import io
import json
import os
import sys
import tempfile
import time
import unittest
from collections import OrderedDict
from pathlib import Path
from unittest import mock

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import messaging as M  # noqa: E402
from app import rig_agent  # noqa: E402

HANDLE = "+37120000000"
CHAT = "iMessage;-;" + HANDLE
NOW = 1790000000.0
CFG = {"auto_reply": {"provider": "bluebubbles_http", "allowed_senders": [HANDLE],
                      "assistant_chats": ["iMessage;-;owner@example.com"]},
       "providers": {"bluebubbles_http": {"enabled": True,
                                          "server_url": "http://bb.invalid"}}}


def _msg(guid, text, age, mine=False, chats=True, handle=True):
    m = {"guid": guid, "text": text, "isFromMe": mine,
         "dateCreated": int((NOW - age) * 1000), "attachments": []}
    if chats:
        m["chats"] = [{"guid": CHAT}]
    if handle:
        m["handle"] = {"address": HANDLE}
    return m


class CatchupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.inbox = d / "inbox.jsonl"
        self.inbox.write_text("", encoding="utf-8")
        self.dispatched = []
        self.query_body = {"status": 200, "data": []}
        self.query_ok = True
        self.per_chat = {}
        patches = [
            mock.patch.object(M, "INBOX_PATH", self.inbox),
            mock.patch.object(M, "OUTBOX_PATH", d / "outbox.jsonl"),
            mock.patch.object(M, "load_config", lambda: CFG),
            mock.patch.object(M, "_bb_post", self._fake_post),
            mock.patch.object(M, "_bb_get", self._fake_get),
            mock.patch.object(M, "dispatch_recorded_inbound", self._fake_dispatch),
            mock.patch.object(M, "_inbound_seen", OrderedDict()),
            mock.patch.object(M, "_inbound_seeded", []),
            mock.patch.object(M, "_inbox_guids", set()),
            mock.patch.object(M, "_inbox_guids_seeded", []),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)

    def _fake_post(self, pcfg, path, payload, timeout=None):
        self.assertEqual(path, "/api/v1/message/query")
        self.last_query = payload
        if not self.query_ok:
            return {"ok": False, "status_code": 404, "body": "not found"}
        return {"ok": True, "status_code": 200, "body": self.query_body}

    def _fake_get(self, pcfg, path, params=None, timeout=None, binary=False):
        for chat, msgs in self.per_chat.items():
            if path == "/api/v1/chat/%s/message" % M.urllib.parse.quote(chat, safe=""):
                return {"ok": True, "status_code": 200, "body": {"data": msgs}}
        return {"ok": True, "status_code": 200, "body": {"data": []}}

    def _fake_dispatch(self, result):
        if result.get("duplicate"):
            return False
        self.dispatched.append(result["entry"]["message_guid"])
        return True

    def _rows(self):
        return [json.loads(line) for line in self.inbox.read_text(encoding="utf-8").splitlines()
                if line.strip()]

    def test_a_missed_message_is_stored_and_answered(self):
        self.query_body["data"] = [_msg("G-HERS", "Odi nebūs", 20)]
        res = M.inbound_catchup_once(now=NOW)
        rows = self._rows()
        self.assertEqual([r["message_guid"] for r in rows], ["G-HERS"])
        self.assertEqual(rows[0]["via"], "catchup")
        self.assertEqual(rows[0]["chat_guid"], CHAT)
        self.assertEqual(rows[0]["sender"], HANDLE)
        self.assertEqual(rows[0]["event_type"], "new-message")
        self.assertEqual(self.dispatched, ["G-HERS"])
        self.assertEqual(res["mode"], "query")
        self.assertEqual(res["answered"], 1)

    def test_a_message_the_webhook_delivered_is_not_stored_twice(self):
        webhook = {"type": "new-message", "data": _msg("G-ONE", "Labi", 30)}
        M.record_inbound(webhook)
        self.query_body["data"] = [_msg("G-ONE", "Labi", 30), _msg("G-TWO", "Oky", 10)]
        M.inbound_catchup_once(now=NOW)
        self.assertEqual([r["message_guid"] for r in self._rows()], ["G-ONE", "G-TWO"])
        self.assertEqual(self.dispatched, ["G-TWO"])

    def test_a_late_webhook_after_the_catchup_is_a_duplicate(self):
        self.query_body["data"] = [_msg("G-LATE", "Tas nekas", 15)]
        M.inbound_catchup_once(now=NOW)
        res = M.record_inbound({"type": "new-message", "data": _msg("G-LATE", "Tas nekas", 15)})
        self.assertTrue(res.get("duplicate"))
        self.assertEqual(len(self._rows()), 1)

    def test_an_old_message_is_stored_but_not_answered(self):
        self.query_body["data"] = [_msg("G-OLD", "Sumuks, bet dārgs", 2 * 3600)]
        res = M.inbound_catchup_once(lookback_seconds=3 * 3600, now=NOW)
        self.assertEqual([r["message_guid"] for r in self._rows()], ["G-OLD"])
        self.assertEqual(self.dispatched, [])
        self.assertEqual(res["answered"], 0)

    def test_answer_false_never_hands_anything_to_the_pipeline(self):
        self.query_body["data"] = [_msg("G-NEW", "Nice", 5)]
        M.inbound_catchup_once(answer=False, now=NOW)
        self.assertEqual(len(self._rows()), 1)
        self.assertEqual(self.dispatched, [])

    def test_messages_before_the_window_are_ignored_even_if_returned(self):
        self.query_body["data"] = [_msg("G-ANCIENT", "sen", 5 * 3600), _msg("G-NOW", "tagad", 5)]
        M.inbound_catchup_once(lookback_seconds=15 * 60, now=NOW)
        self.assertEqual([r["message_guid"] for r in self._rows()], ["G-NOW"])
        self.assertEqual(self.last_query["after"], int((NOW - 15 * 60) * 1000))

    def test_stored_oldest_first(self):
        self.query_body["data"] = [_msg("G-B", "otrā", 10), _msg("G-A", "pirmā", 40)]
        M.inbound_catchup_once(now=NOW)
        self.assertEqual([r["message_guid"] for r in self._rows()], ["G-A", "G-B"])

    def test_per_chat_fallback_fills_chat_and_sender(self):
        self.query_ok = False
        self.per_chat = {CHAT: [_msg("G-PC", "Viss notiek", 12, chats=False, handle=False)]}
        res = M.inbound_catchup_once(now=NOW)
        rows = self._rows()
        self.assertEqual(res["mode"], "per-chat")
        self.assertEqual(rows[0]["chat_guid"], CHAT)
        self.assertEqual(rows[0]["sender"], HANDLE)
        self.assertEqual(self.dispatched, ["G-PC"])

    def test_the_webhook_path_is_unchanged(self):
        res = M.record_inbound({"type": "new-message", "data": _msg("G-WH", "Sveiks", 3)})
        self.assertNotIn("via", res["entry"])
        self.assertFalse(res.get("duplicate"))

    def test_provider_off_does_nothing(self):
        off = json.loads(json.dumps(CFG))
        off["providers"]["bluebubbles_http"]["enabled"] = False
        with mock.patch.object(M, "load_config", lambda: off):
            res = M.inbound_catchup_once(now=NOW)
        self.assertIn("skipped", res)
        self.assertEqual(self._rows(), [])


class RecentContextTests(unittest.TestCase):
    def test_newest_lines_by_message_time_in_order(self):
        # inbox() is newest-first by FILE order; the last-written row here is
        # an older message the catch-up stored late
        rows = [{"sender": HANDLE, "text": "late-stored old", "ts": NOW - 500},
                {"sender": HANDLE, "text": "third", "ts": NOW - 10},
                {"sender": HANDLE, "text": "second", "ts": NOW - 20},
                {"sender": HANDLE, "text": "first", "ts": NOW - 30}]
        with mock.patch.object(M, "inbox", lambda limit=50: rows):
            got = rig_agent.recent_context(HANDLE, limit=3)
        self.assertEqual(got.splitlines(), ["first", "second", "third"])


if __name__ == "__main__":
    unittest.main()
