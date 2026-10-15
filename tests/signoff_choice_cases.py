"""The digest sign-off: the model writes three lines, then CHOOSES the one to
send from its own three plus the ready-made lines that fit the day.

Contract (this file is the spec):
  * three calls write candidate lines (no JSON schema on those calls);
  * one call chooses: its user message holds the day's facts and the options,
    one per line, numbered "1. ...", "2. ..."; it answers JSON {"choice": n};
  * the options are the three written lines plus the ready-made lines that fit
    today: the weather lines whose condition holds, the weekday's lines, and
    the general ones;
  * the chosen option is returned; an unreadable or out-of-range choice
    returns "" (the caller then falls back to the ready-made line as before).

No model or network call is made.

    python tests/signoff_choice_cases.py
"""
import json
import os
import re
import sys
import unittest
from datetime import datetime
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from app import messaging as M  # noqa: E402

FRIDAY = datetime(2026, 9, 18, 9, 0)


def run(written, choose, hi=16, rain=0, mm=0.0, now=FRIDAY):
    """written: the lines the stand-in model writes, in turn.
    choose(options) -> the raw answer to the choosing call."""
    seen = {"write": 0, "options": [], "choice_user": ""}

    def fake(acfg, messages, options=None, format_schema=None, think=False):
        if format_schema is None:
            line = written[seen["write"] % len(written)]
            seen["write"] += 1
            return line
        user = next(m["content"] for m in messages if m.get("role") == "user")
        seen["choice_user"] = user
        seen["options"] = [ln.split(". ", 1)[1] for ln in user.splitlines()
                           if re.match(r"^\d+\. ", ln)]
        return choose(seen["options"])

    with patch.object(M, "_ollama_chat", side_effect=fake):
        out = M._digest_signoff_model({}, hi=hi, rain=rain, mm=mm, now=now)
    return out, seen


def pick(text):
    return lambda opts: json.dumps({"choice": opts.index(text) + 1})


class SignoffChoiceTests(unittest.TestCase):
    def test_it_writes_three_and_ships_the_one_it_chose(self):
        written = ["Jauku piektdienu, draug! 🍻", "Lai lietus neizbīdītu! 🌧️",
                   "Lai laba piektdiena! ☀️"]
        out, seen = run(written, pick("Jauku piektdienu, draug! 🍻"))
        self.assertEqual(seen["write"], 3)
        for line in written:
            self.assertIn(line, seen["options"])
        self.assertEqual(out, "Jauku piektdienu, draug! 🍻")

    def test_the_ready_made_lines_for_the_day_are_options(self):
        # a Friday with rain expected: the Friday line, the rain line and the
        # general lines are all offered next to the written ones
        _out, seen = run(["A 🙂", "B 🙂", "C 🙂"], pick("A 🙂"),
                         hi=16, rain=60, mm=3.0)
        self.assertIn("Lai jauka piektdiena! Nedēļa galā 🍻", seen["options"])
        self.assertIn("Paņem lietussargu, šodien līs! ☔", seen["options"])
        self.assertIn("Lai laba diena! ✌️", seen["options"])

    def test_a_weather_line_is_offered_only_when_its_weather_holds(self):
        _out, seen = run(["A 🙂"], pick("A 🙂"), hi=16, rain=0, mm=0.0)
        self.assertNotIn("Paņem lietussargu, šodien līs! ☔", seen["options"])

    def test_the_choosing_call_sees_the_days_facts(self):
        _out, seen = run(["A 🙂"], pick("A 🙂"))
        self.assertIn("piektdiena", seen["choice_user"])
        self.assertIn("18. septembris", seen["choice_user"])

    def test_a_chosen_ready_made_line_ships(self):
        out, _seen = run(["A 🙂", "B 🙂", "C 🙂"], pick("Lai laba diena! ✌️"))
        self.assertEqual(out, "Lai laba diena! ✌️")

    def test_an_unreadable_or_out_of_range_choice_falls_back(self):
        for answer in ("trešā", json.dumps({"choice": 99}),
                       json.dumps({"choice": 0})):
            with self.subTest(answer=answer):
                out, _seen = run(["A 🙂"], lambda opts, a=answer: a)
                self.assertEqual(out, "")

    def test_every_curated_closer_carries_an_emoji(self):
        for line in M._DIGEST_CLOSERS:
            self.assertTrue(M._EMOJI_RE.search(line), line)


if __name__ == "__main__":
    result = unittest.main(verbosity=2, exit=False).result
    if result.wasSuccessful():
        print("OK")
    sys.exit(0 if result.wasSuccessful() else 1)
