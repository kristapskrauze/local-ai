# -*- coding: utf-8 -*-
import os
import sys
import unittest


REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from app import lv_engine, lv_grammar, lv_validate  # noqa: E402


class LatvianEngineTests(unittest.TestCase):
    def test_validator_negative_patterns_are_detected_and_fixed(self):
        cases = [
            ("Mēs aizgāja mājās.", "Mēs aizgājām mājās."),
            ("Viņi bija ļoti priecīgs.", "Viņi bija ļoti priecīgi."),
            ("Man patīk braukt ar mašīna.", "Man patīk braukt ar mašīnu."),
            ("Es vakar redzēju viņš veikalā.", "Es vakar redzēju viņu veikalā."),
            ("Es runāju ar mans draugs.", "Es runāju ar savu draugu."),
            ("Es piezvanīju uz savu draugu.", "Es piezvanīju savam draugam."),
        ]
        for bad, preferred in cases:
            with self.subTest(bad=bad):
                issues = lv_grammar.contextual_diagnostics(bad)
                self.assertTrue(issues, bad)
                result = lv_engine.edit_reply(bad, log=False)
                self.assertTrue(result["accepted"], result)
                self.assertEqual(result["final"], preferred)
                verdict = lv_validate.validate_latvian_reply(result["final"])
                self.assertTrue(verdict["pass"], verdict)

    def test_reflexive_possessive_is_detection_only_when_ambiguous(self):
        text = "Viņa aizgāja pie viņas mammas."
        issues = lv_grammar.contextual_diagnostics(text)
        self.assertTrue(issues, issues)
        self.assertFalse(issues[0]["auto_fix"], issues)
        result = lv_engine.edit_reply(text, log=False)
        self.assertFalse(result["accepted"], result)
        self.assertEqual(result["final"], text)

        explicit_other_person = "Viņa aizgāja pie viņas mammas, nevis pie savas."
        self.assertFalse(lv_grammar.contextual_diagnostics(explicit_other_person))
        self.assertEqual(
            lv_engine.edit_reply(explicit_other_person, log=False)["final"],
            explicit_other_person,
        )

    def test_prompt_style_multi_issue_sentence(self):
        text = "Es vakar aizgāju pie draugs un nopirku jauns telefons."
        result = lv_engine.edit_reply(text, log=False)
        self.assertTrue(result["accepted"], result)
        self.assertEqual(
            result["final"],
            "Es vakar aizgāju pie drauga un nopirku jaunu telefonu.",
        )
        self.assertGreaterEqual(len(result["rules_used"]), 2)

    def test_good_informal_and_code_switch_text_is_not_changed(self):
        texts = [
            "Nu labi, tad darām tā.",
            "Es deployoju jauno build, bet pipeline atkal nokrita.",
            "Tas API response ir pilnīgs garbage, godīgi.",
            "Man vajag quick sanity check, nevis full research.",
        ]
        for text in texts:
            with self.subTest(text=text):
                verdict = lv_validate.validate_latvian_reply(text)
                self.assertTrue(verdict["pass"], verdict)
                result = lv_engine.edit_reply(text, log=False)
                self.assertFalse(result["accepted"], result)
                self.assertEqual(result["final"], text)

    def test_lowercase_sentence_keeps_lowercase_subject(self):
        result = lv_engine.edit_reply("mēs aizgāja mājās.", log=False)
        self.assertTrue(result["accepted"], result)
        self.assertEqual(result["final"], "mēs aizgājām mājās.")

    def test_rule_suggestions_are_trusted_when_dictionary_is_missing_forms(self):
        cases = [
            ("Mēs neaizgāja mājās.", "Mēs neaizgājām mājās."),
            ("Viņas bija ļoti dusmīgs.", "Viņas bija ļoti dusmīgas."),
            ("Viņas bija ļoti noguris.", "Viņas bija ļoti nogurušas."),
        ]
        for bad, good in cases:
            with self.subTest(bad=bad):
                result = lv_engine.edit_reply(bad, log=False)
                self.assertTrue(result["accepted"], result)
                self.assertEqual(result["final"], good)

    def test_coverage_summary_is_structured(self):
        summary = lv_grammar.coverage_summary()
        self.assertGreaterEqual(summary["rule_count"], 8)
        self.assertIn("case.preposition", summary["categories"])
        self.assertTrue(summary["ontology_loaded"])


if __name__ == "__main__":
    unittest.main()
