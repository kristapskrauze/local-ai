"""The mechanical part of polishing runs on every lane, guards on or off.

No model or network call is made.

    python tests/polish_mechanical_cases.py
"""
import os
import sys
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from app import messaging as M  # noqa: E402

ONE = "iMessage;-;+37100000000"          # a one-to-one chat
GROUP = "iMessage;+;chat000000000000"    # a group chat keeps "jūs"


def polish(text, guid=ONE):
    with patch.object(M, "_guards_on", return_value=False):
        return str(M._polish_reply(text, guid))


class GuardsOffStillCleansTests(unittest.TestCase):
    def test_the_bots_own_speaker_label_goes(self):
        self.assertEqual(polish("Antons: Jā, karstums ir briesmīgs"),
                         "Jā, karstums ir briesmīgs")
        self.assertEqual(polish("Antonam: Jā, karstums ir briesmīgs"),
                         "Jā, karstums ir briesmīgs")

    def test_a_question_arrow_transcript_is_unwrapped(self):
        self.assertEqual(polish('"Kā iet?" -> "Labi, paldies!"'), "Labi, paldies!")

    def test_markdown_markers_do_not_reach_the_phone(self):
        out = polish("Tas ir **svarīgi** un *ļoti*")
        self.assertNotIn("*", out)
        self.assertIn("svarīgi", out)
        self.assertIn("ļoti", out)

    def test_a_code_fence_is_unwrapped_not_deleted(self):
        # the content survives (as monospace glyphs, by design), the fence goes
        out = polish("```\nnvidia-smi\n```")
        self.assertNotIn("`", out)
        self.assertEqual(len(out.strip()), len("nvidia-smi"))

    def test_formal_plural_is_familiar_in_a_one_person_chat(self):
        self.assertEqual(polish("Turieties, vēl nedaudz"), "Turies, vēl nedaudz")
        self.assertEqual(polish("Pārbaudiet sarakstu"), "Pārbaudi sarakstu")

    def test_a_group_chat_keeps_the_plural(self):
        self.assertEqual(polish("Turieties, vēl nedaudz", GROUP),
                         "Turieties, vēl nedaudz")

    def test_an_ordinary_reply_is_untouched(self):
        text = "Jā, rīt būšu mājās ap sešiem."
        self.assertEqual(polish(text), text)

    def test_the_data_lane_uses_the_same_cleanup(self):
        self.assertEqual(M._polish_data_reply("Antons: **3** sludinājumi", ONE),
                         "3 sludinājumi".replace("3", M._apply_bold_runs("**3**")))


class DeadGateTests(unittest.TestCase):
    def test_the_service_desk_sentence_dropper_is_gone(self):
        self.assertFalse(hasattr(M, "_drop_helper_speak"))
        self.assertFalse(hasattr(M, "_HELPER_SPEAK_RE"))


if __name__ == "__main__":
    result = unittest.main(verbosity=2, exit=False).result
    if result.wasSuccessful():
        print("OK")
    sys.exit(0 if result.wasSuccessful() else 1)
