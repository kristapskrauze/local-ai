# -*- coding: utf-8 -*-
"""Temp store, fake sender, fake location. No network, nothing sent.

    py -3 tests/reminders_cases.py
"""
import io
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import agent_tools as T  # noqa: E402
from app import messaging as M  # noqa: E402
from app import reminders as R  # noqa: E402

CHAT = "iMessage;-;+37120000000"


class _Store(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        p = mock.patch.object(R, "STORE_PATH", Path(self.tmp.name) / "reminders.json")
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)
        self.sent = []

    def send(self, chat, text):
        self.sent.append((chat, text))
        return True


class DeliveryTests(_Store):
    def test_a_due_reminder_is_sent_once_to_its_chat(self):
        now = datetime(2026, 10, 1, 7, 0, tzinfo=timezone.utc)
        R.add(CHAT, "Depo: melna krāsa, urbis", now - timedelta(seconds=30), "Europe/Riga")
        R.deliver_due(self.send, now=now)
        R.deliver_due(self.send, now=now + timedelta(minutes=1))
        self.assertEqual(self.sent, [(CHAT, "⏰ Depo: melna krāsa, urbis")])
        self.assertEqual(R.pending(), [])

    def test_a_future_reminder_waits(self):
        now = datetime(2026, 10, 1, 7, 0, tzinfo=timezone.utc)
        R.add(CHAT, "vēlāk", now + timedelta(hours=1), "Europe/Riga")
        R.deliver_due(self.send, now=now)
        self.assertEqual(self.sent, [])
        self.assertEqual(len(R.pending(CHAT)), 1)

    def test_a_late_reminder_says_how_late(self):
        now = datetime(2026, 10, 1, 7, 0, tzinfo=timezone.utc)
        R.add(CHAT, "pases", now - timedelta(minutes=40), "Europe/Riga")
        R.deliver_due(self.send, now=now)
        self.assertEqual(self.sent, [(CHAT, "⏰ pases (nokavēju par 40 min)")])

    def test_a_reminder_half_a_day_late_is_missed_not_sent(self):
        now = datetime(2026, 10, 1, 7, 0, tzinfo=timezone.utc)
        R.add(CHAT, "vakardienas", now - timedelta(hours=13), "Europe/Riga")
        done = R.deliver_due(self.send, now=now)
        self.assertEqual(self.sent, [])
        self.assertIn(("missed", R._load()[0]["id"]), done)

    def test_a_failed_send_is_retried_then_given_up(self):
        now = datetime(2026, 10, 1, 7, 0, tzinfo=timezone.utc)
        R.add(CHAT, "x", now - timedelta(seconds=5), "Europe/Riga")
        for _ in range(5):
            R.deliver_due(lambda c, t: False, now=now)
        self.assertEqual(R._load()[0]["status"], "failed")
        self.assertEqual(R._load()[0]["attempts"], 5)

    def test_one_left_mid_send_by_a_crash_is_not_sent_twice(self):
        now = datetime(2026, 10, 1, 7, 0, tzinfo=timezone.utc)
        R.add(CHAT, "x", now - timedelta(seconds=5), "Europe/Riga")
        rows = R._load()
        rows[0]["status"] = "sending"
        R._save(rows)
        R._recover_sending()
        R.deliver_due(self.send, now=now)
        self.assertEqual(self.sent, [])
        self.assertEqual(R._load()[0]["status"], "unknown")

    def test_cancel(self):
        now = datetime(2026, 10, 1, 7, 0, tzinfo=timezone.utc)
        row = R.add(CHAT, "x", now + timedelta(hours=1), "Europe/Riga")
        self.assertEqual(R.cancel(row["id"])["status"], "cancelled")
        R.deliver_due(self.send, now=now + timedelta(hours=2))
        self.assertEqual(self.sent, [])


class TimezoneTests(unittest.TestCase):
    def test_his_country_gives_the_zone_when_find_my_is_fresh(self):
        entry = {"lat": 53.87, "lon": 10.69, "ts": time.time() - 600, "city": "Lübeck"}
        with mock.patch("app.location.latest", return_value=entry), \
                mock.patch.object(R, "_country_cache", {}):
            tz, src = R.local_timezone(fetch=lambda url: {"address": {"country_code": "de"}})
        self.assertEqual(tz, "Europe/Berlin")
        self.assertIn("Lübeck", src)

    def test_a_stale_reading_falls_back_to_the_configured_zone(self):
        entry = {"lat": 57.15, "lon": 24.87, "ts": time.time() - 2 * 86400, "city": "Sigulda"}
        with mock.patch("app.location.latest", return_value=entry), \
                mock.patch.object(R, "_configured_tz", return_value="Europe/Riga"):
            tz, src = R.local_timezone(fetch=lambda url: {"address": {"country_code": "de"}})
        self.assertEqual((tz, src), ("Europe/Riga", "configured"))

    def test_only_a_rounded_point_is_sent_to_the_geocoder(self):
        seen = []
        with mock.patch.object(R, "_country_cache", {}):
            R.country_code_at(53.86551, 10.68661,
                              fetch=lambda url: seen.append(url) or {"address": {"country_code": "de"}})
        self.assertIn("lat=53.9", seen[0])
        self.assertIn("lon=10.7", seen[0])


class ToolTests(_Store):
    def setUp(self):
        super().setUp()
        p = mock.patch.object(R, "local_timezone", return_value=("Europe/Berlin", "location:Lübeck"))
        p.start()
        self.addCleanup(p.stop)

    def ctx(self, owner=True):
        return T.AgentContext(actor="test", conversation_id=CHAT, sender="+37120000000",
                              owner_authored=owner, allow_external_write=True)

    def test_his_reminder_is_stored_in_his_local_time(self):
        out = T._tool_set_reminder({"text": "Depo", "when": "rīt 9:00"}, self.ctx())
        self.assertTrue(out["ok"], out)
        tomorrow = (datetime.now(ZoneInfo("Europe/Berlin")) + timedelta(days=1)).date()
        self.assertTrue(out["reminder"]["due_local"].startswith(tomorrow.isoformat() + "T09:00"))
        self.assertEqual(out["timezone"], "Europe/Berlin")
        self.assertEqual(R.pending(CHAT)[0]["text"], "Depo")

    def test_a_contacts_turn_cannot_set_one(self):
        out = T._tool_set_reminder({"text": "x", "when": "rīt 9:00"}, self.ctx(owner=False))
        self.assertEqual(out["error_type"], "authorization_required")
        self.assertEqual(R.pending(), [])

    def test_no_time_asks_instead_of_guessing(self):
        out = T._tool_set_reminder({"text": "Depo", "when": "kaut kad"}, self.ctx())
        self.assertFalse(out["ok"])
        self.assertEqual(R.pending(), [])

    def test_the_same_reminder_twice_is_one(self):
        T._tool_set_reminder({"text": "Depo", "when": "rīt 9:00"}, self.ctx())
        again = T._tool_set_reminder({"text": "Depo", "when": "rīt 9:00"}, self.ctx())
        self.assertTrue(again.get("idempotent_replay"))
        self.assertEqual(len(R.pending(CHAT)), 1)

    def test_list_and_cancel_are_his(self):
        T._tool_set_reminder({"text": "Depo", "when": "rīt 9:00"}, self.ctx())
        listed = T._tool_list_reminders({}, self.ctx())
        self.assertEqual(listed["count"], 1)
        self.assertFalse(T._tool_list_reminders({}, self.ctx(owner=False))["ok"])
        rid = listed["reminders"][0]["id"]
        self.assertFalse(T._tool_cancel_reminder({"reminder_id": rid}, self.ctx(owner=False))["ok"])
        self.assertTrue(T._tool_cancel_reminder({"reminder_id": rid}, self.ctx())["ok"])
        self.assertEqual(R.pending(), [])

    def test_a_reminder_backs_a_promise_to_remind(self):
        rec = [{"tool": "set_reminder", "ok": True, "result": {"ok": True}}]
        self.assertTrue(M._save_actually_happened(rec))

    def test_offered_in_his_chat_and_in_contact_chats(self):
        for name in ("set_reminder", "list_reminders", "cancel_reminder"):
            self.assertIn(name, M._LIVE_ASSISTANT_AGENT_TOOLS)
            self.assertIn(name, M._LIVE_TRUSTED_CONTACT_ACTION_TOOLS)
            self.assertIsNotNone(T.default_registry().get(name))


if __name__ == "__main__":
    unittest.main()
