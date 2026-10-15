# -*- coding: utf-8 -*-
"""Page text goes through the translator, in place, and nothing else moves.

No model and no translator run: both are stand-ins.

    py -3 tests/page_texts_cases.py
"""
import io
import json
import os
import sys
import unittest

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import page_texts as P  # noqa: E402
from app import agent_tools as A  # noqa: E402

PAGE = """<!doctype html><html lang="lv"><head><meta charset="utf-8">
<title>Ķirzakju Klupens</title>
<meta name="game-desc" content="Klup ķirzakus, kamēr tie izskaujas!">
<style>.btn{color:red} .note:after{content:"Sākums"}</style></head><body>
<button class="btn" id="startBtn"><span class="ico">🔨</span>Sākt spēli</button>
<input id="who" placeholder="Tavs vārds">
<div id="streak"></div>
<script>
var MSG="Brīnišķīgi! Tu esi klupens meistars!";
function hit(el,n){ el.textContent="🔥 x"+n+" SĒRIJA!"; }
document.getElementById('streak').addEventListener('click',function(){}); // don't touch
var Q='It\\'s ok';
</script>
<script type="application/json" id="selftest">[{"name":"start works","js":"return 'Sākt spēli'.length>0"}]</script>
</body></html>"""


def _idx(cands, text):
    return next(c["i"] for c in cands if c["text"] == text)


class PageTextsTests(unittest.TestCase):
    def setUp(self):
        self.cands = P.candidates(PAGE)
        self.texts = [c["text"] for c in self.cands]

    def test_candidates_are_the_page_strings_not_css_or_the_selftest(self):
        for t in ("Ķirzakju Klupens", "Klup ķirzakus, kamēr tie izskaujas!", "Sākt spēli",
                  "Tavs vārds", "Brīnišķīgi! Tu esi klupens meistars!", " SĒRIJA!",
                  "streak", "click", "It's ok"):
            self.assertIn(t, self.texts)
        self.assertNotIn("Sākums", self.texts)          # CSS
        self.assertNotIn("start works", self.texts)     # the selftest block
        self.assertFalse(any("don" in t for t in self.texts), self.texts)  # a comment

    def test_visible_strings_are_replaced_in_place_and_the_code_still_parses(self):
        c = self.cands
        meanings = {_idx(c, "Ķirzakju Klupens"): "Whack the Lizards",
                    _idx(c, "Sākt spēli"): "Start game",
                    _idx(c, "Brīnišķīgi! Tu esi klupens meistars!"):
                        "Amazing! You are a whacking master!",
                    _idx(c, " SĒRIJA!"): "STREAK!",
                    _idx(c, "It's ok"): "It's fine"}
        lv = {"Whack the Lizards": "Sit ķirzakas",
              "Start game": "Sākt spēli <3 & draugi",
              "Amazing! You are a whacking master!": 'Lieliski! Tu esi "sitienu" meistars!',
              "STREAK!": "Sitienu sērija!",
              "It's fine": "Tas ir 'labi'"}
        out, changes = P.retext(PAGE, meanings=meanings, translate=lv.get)
        self.assertIn("<title>Sit ķirzakas</title>", out)
        self.assertIn("Sākt spēli &lt;3 &amp; draugi</button>", out)
        self.assertIn('var MSG="Lieliski! Tu esi \\"sitienu\\" meistars!";', out)
        self.assertIn('+" SITIENU SĒRIJA!"', out)            # edge space + caps kept
        self.assertIn("var Q='Tas ir \\'labi\\'';", out)
        # untouched: code strings, the unmarked meta, the selftest block, CSS
        self.assertIn("getElementById('streak').addEventListener('click'", out)
        self.assertIn('content="Klup ķirzakus, kamēr tie izskaujas!"', out)
        self.assertIn('"js":"return \'Sākt spēli\'.length>0"', out)
        self.assertIn('content:"Sākums"', out)
        self.assertEqual(A._js_syntax_error(out), "")
        self.assertEqual(len(changes), 5)

    def test_no_translation_leaves_the_string_alone(self):
        i = _idx(self.cands, "Sākt spēli")
        out, changes = P.retext(PAGE, meanings={i: "Start game"}, translate=lambda s: "")
        self.assertEqual(out, PAGE)
        self.assertEqual(changes, [])

    def test_a_translation_that_kept_english_is_reworded_and_translated_again(self):
        title = _idx(self.cands, "Ķirzakju Klupens")
        asked = []

        def chat(messages, schema):
            asked.append(messages[0]["content"])
            if messages[0]["content"] == P._ASK_SYS:
                return json.dumps({"texts": [{"i": title, "en": "Hit the lizards"}]})
            if "Pieskaries" in messages[-1]["content"]:
                return json.dumps({"fix": []})           # round 2: clean now
            self.assertIn('"Hit ķirzakas"', messages[-1]["content"])
            return json.dumps({"fix": [{"i": title, "what_is_wrong": "english",
                                        "new_english": "Tap the lizards"}]})

        lv = {"Hit the lizards": "Hit ķirzakas", "Tap the lizards": "Pieskaries ķirzakām"}
        out, changes = P.retext(PAGE, translate=lv.get, chat=chat)
        self.assertIn("<title>Pieskaries ķirzakām</title>", out)
        self.assertEqual(asked, [P._ASK_SYS, P._RECHECK_SYS, P._RECHECK_SYS])
        self.assertEqual(changes[0]["en"], "Tap the lizards")

    def test_a_rewording_that_still_keeps_english_gets_one_more_round(self):
        title = _idx(self.cands, "Ķirzakju Klupens")
        rounds = []

        def chat(messages, schema):
            if messages[0]["content"] == P._ASK_SYS:
                return json.dumps({"texts": [{"i": title, "en": "Good whacking!"}]})
            rounds.append(messages[-1]["content"])
            if len(rounds) == 1:
                return json.dumps({"fix": [{"i": title, "what_is_wrong": "english",
                                            "new_english": "Great smashing!"}]})
            self.assertIn("smashing", messages[-1]["content"])
            return json.dumps({"fix": [{"i": title, "what_is_wrong": "english",
                                        "new_english": "Great job!"}]})

        lv = {"Good whacking!": "Labs whacking!", "Great smashing!": "Lieliski smashing!",
              "Great job!": "Lielisks darbs!"}
        out, _ = P.retext(PAGE, translate=lv.get, chat=chat)
        self.assertIn("<title>Lielisks darbs!</title>", out)
        self.assertEqual(len(rounds), 2)

    def test_the_same_english_handed_back_is_not_a_rewording(self):
        # measured: the model named the bad translation and returned the very
        # English that produced it
        title = _idx(self.cands, "Ķirzakju Klupens")

        def chat(messages, schema):
            if messages[0]["content"] == P._ASK_SYS:
                return json.dumps({"texts": [{"i": title, "en": "Lizard Whacking"}]})
            return json.dumps({"fix": [{"i": title, "what_is_wrong": "english",
                                        "new_english": "lizard whacking"}]})

        calls = []

        def tr(en):
            calls.append(en)
            return {"Lizard Whacking": "Lizard Whacking"}.get(en, "")
        P.retext(PAGE, translate=tr, chat=chat)
        self.assertEqual(calls, ["Lizard Whacking"])

    def test_a_failed_recheck_keeps_the_first_translation(self):
        title = _idx(self.cands, "Ķirzakju Klupens")

        def chat(messages, schema):
            if messages[0]["content"] == P._ASK_SYS:
                return json.dumps({"texts": [{"i": title, "en": "Whack the lizards"}]})
            raise RuntimeError("model down")

        out, _ = P.retext(PAGE, translate={"Whack the lizards": "Dauzi ķirzakas"}.get, chat=chat)
        self.assertIn("<title>Dauzi ķirzakas</title>", out)

    def test_only_listed_strings_count_and_big_pages_are_asked_in_parts(self):
        seen = []

        def chat(messages, schema):
            listing = messages[-1]["content"]
            seen.append(listing)
            first = int(listing.split("\n")[1].split(".")[0])
            return json.dumps({"texts": [{"i": first, "en": "Hello there"},
                                         {"i": 99999, "en": "not a string here"},
                                         {"i": first + 1, "en": "  "}]})
        many = PAGE.replace("<div id=\"streak\"></div>", "".join(
            "<p>Rinda %d</p>" % k for k in range(P._ASK_CHUNK + 20)))
        cands = P.candidates(many)
        got = P.ask_meanings(many, cands, chat=chat)
        self.assertEqual(len(seen), 2)
        self.assertEqual(sorted(got.values()), ["Hello there", "Hello there"])
        self.assertTrue(all(i < len(cands) for i in got))


if __name__ == "__main__":
    unittest.main()
