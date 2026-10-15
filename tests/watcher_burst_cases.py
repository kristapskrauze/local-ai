# -*- coding: utf-8 -*-
"""The watcher judges one author's burst as one message.

    py -3 tests/watcher_burst_cases.py
"""
import io
import os
import sys
import unittest

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import messaging as M  # noqa: E402

CHAT = "iMessage;-;+37120000000"


def _e(text, mine, ts):
    return {"text": text, "is_from_me": mine, "chat_guid": CHAT,
            "sender": "+37120000000", "ts": ts, "message_guid": "g%d" % ts}


class WatcherBurstTests(unittest.TestCase):
    def _gate(self, entries):
        batch, _latest, target = M._incoming_turn_details(entries)
        return M._gate_entry_for_batch(entries, target, batch)

    def test_one_authors_burst_is_one_message(self):
        got = self._gate([_e("Atgādini pieteikties", True, 1789000000),
                          _e("Lūdzu", True, 1789000004)])
        self.assertEqual(got["text"], "Atgādini pieteikties Lūdzu")
        self.assertIs(got["is_from_me"], True)
        self.assertEqual(got["message_guid"], "g1789000004")   # still the newest entry

    def test_a_batch_with_both_sides_keeps_the_newest_message(self):
        got = self._gate([_e("Atgādini pieteikties", True, 1789000000),
                          _e("Labi, atgādināšu", False, 1789000004)])
        self.assertEqual(got["text"], "Labi, atgādināšu")

    def test_a_single_message_is_untouched(self):
        e = _e("Kā iet?", False, 1789000000)
        self.assertIs(self._gate([e]), e)

    def test_an_attachment_only_entry_does_not_count_as_a_second_author(self):
        pic = dict(_e("", False, 1789000002), attachments=[{"mimeType": "image/png"}])
        got = self._gate([_e("Atgādini pieteikties", True, 1789000000), pic,
                          _e("Lūdzu", True, 1789000004)])
        self.assertEqual(got["text"], "Atgādini pieteikties Lūdzu")


if __name__ == "__main__":
    unittest.main()
