# -*- coding: utf-8 -*-
"""No network calls, messages, or live-state writes are made.
"""
import os
import json
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import agent_tools
from app import messaging


class DigestModelSelectionTests(unittest.TestCase):
    def test_digest_default_does_not_force_a_second_large_model(self):
        self.assertEqual(messaging.DEFAULT_CONFIG["daily_digest"]["model"], "")

    @staticmethod
    def _flat():
        return [
            ("lv", "LSM", {"title": "Latvijas ziņa A", "desc": "Pilns apraksts A."}),
            ("lv", "TVNET", {"title": "Latvijas ziņa B", "desc": "Pilns apraksts B."}),
            ("lv", "Delfi", {"title": "Pasaules ziņa C", "desc": "Pilns apraksts C."}),
            ("lv", "LSM", {"title": "Pasaules ziņa D", "desc": "Pilns apraksts D."}),
        ]

    def test_model_owns_sections_and_unique_emojis(self):
        raw = json.dumps({"picks": [
            {"n": 0, "e": "🏛️", "s": "lv"},
            {"n": 1, "e": "⚡", "s": "lv"},
            {"n": 2, "e": "🌊", "s": "w"},
            {"n": 3, "e": "✈️", "s": "w"},
        ]}, ensure_ascii=False)
        with patch.object(messaging, "_ollama_chat", return_value=raw) as model, \
                patch.object(messaging, "_digest_review_selection",
                             return_value=set()):
            picks = messaging._digest_select({}, self._flat(), 2, 2)
        self.assertEqual(picks[2], (2, "🌊", "w"))
        self.assertEqual(len({e for _n, e, _s in picks}), 4)
        self.assertIs(model.call_args.kwargs["format_schema"],
                      messaging._DIGEST_SELECTION_SCHEMA)
        prompt = model.call_args.args[1][0]["content"]
        self.assertIn("APRAKSTS: Pilns apraksts A.", prompt)
        self.assertIn("nepabeigts fragments", prompt)

    def test_a_repeated_emoji_keeps_the_models_selection(self):
        same = json.dumps({"picks": [
            {"n": 0, "e": "🗳️", "s": "lv"},
            {"n": 1, "e": "🗳️", "s": "lv"},
            {"n": 2, "e": "🌍", "s": "w"},
            {"n": 3, "e": "✈️", "s": "w"},
        ]}, ensure_ascii=False)
        with patch.object(messaging, "_ollama_chat", return_value=same) as model, \
                patch.object(messaging, "_digest_review_selection",
                             return_value=set()):
            picks = messaging._digest_select({}, self._flat(), 2, 2)
        self.assertEqual(model.call_count, 1)
        self.assertEqual([n for n, _e, _s in picks], [0, 1, 2, 3])

    def test_a_missing_emoji_is_asked_again_naming_the_story(self):
        bad = json.dumps({"picks": [
            {"n": 0, "e": "politika", "s": "lv"},
            {"n": 1, "e": "⚡", "s": "lv"},
            {"n": 2, "e": "🌊", "s": "w"},
            {"n": 3, "e": "✈️", "s": "w"},
        ]}, ensure_ascii=False)
        good = json.dumps({"picks": [
            {"n": 0, "e": "🏛️", "s": "lv"},
            {"n": 1, "e": "⚡", "s": "lv"},
            {"n": 2, "e": "🌊", "s": "w"},
            {"n": 3, "e": "✈️", "s": "w"},
        ]}, ensure_ascii=False)
        with patch.object(messaging, "_ollama_chat",
                          side_effect=[bad, good]) as model, \
                patch.object(messaging, "_digest_review_selection",
                             return_value=set()):
            picks = messaging._digest_select({}, self._flat(), 2, 2)
        self.assertEqual(model.call_count, 2)
        retry = model.call_args_list[1].args[1][0]["content"]
        self.assertIn("story 0", retry)
        self.assertEqual(picks[0], (0, "🏛️", "lv"))

    def test_no_story_the_model_never_chose_is_ever_shipped(self):
        with patch.object(messaging, "_ollama_chat", return_value="{}"), \
                patch.object(messaging, "_digest_review_selection",
                             return_value=set()):
            with self.assertRaises(messaging.MessagingError):
                messaging._digest_select({}, self._flat(), 2, 2)

    def test_reviewed_picks_ship_without_padding_when_review_keeps_rejecting(self):
        raw = json.dumps({"picks": [
            {"n": 0, "e": "🏛️", "s": "lv"},
            {"n": 1, "e": "⚡", "s": "lv"},
            {"n": 2, "e": "🌊", "s": "w"},
            {"n": 3, "e": "✈️", "s": "w"},
        ]}, ensure_ascii=False)
        with patch.object(messaging, "_ollama_chat", return_value=raw), \
                patch.object(messaging, "_digest_review_selection",
                             return_value={1}):
            picks = messaging._digest_select({}, self._flat(), 2, 2)
        self.assertEqual([n for n, _e, _s in picks], [0, 2, 3])
        self.assertNotIn("▪️", [e for _n, e, _s in picks])

    def test_no_static_topic_or_geography_fallbacks_remain(self):
        for name in ("_digest_fallback_emoji", "_looks_domestic",
                     "_explicitly_world", "_cap_repeated_topics",
                     "_digest_headline_usable"):
            self.assertFalse(hasattr(messaging, name), name)


class DigestArticleTextTests(unittest.TestCase):
    def test_the_page_title_is_not_offered_as_an_article_sentence(self):
        title = "Šodien gaiss iesils līdz +21 grādam: laika prognoze | LA.LV"
        page = {"title": title, "text": title + " Piektdien Latvijā, izklīstot "
                "miglai, spīdēs saule un nelīs, prognozē sinoptiķi. Rīgā laiks "
                "skaidrosies un gaiss iesils līdz +19 grādiem."}
        with patch.object(messaging, "read_link", return_value=page):
            got = messaging._article_candidates("https://example.lv/a")
        self.assertTrue(got)
        self.assertFalse(any("LA.LV" in c or c.startswith("Šodien gaiss") for c in got), got)
        self.assertTrue(got[0].startswith("Piektdien Latvijā"), got)


class DigestGrammarBoundaryTests(unittest.TestCase):
    def test_headline_is_never_shortened_into_an_incomplete_fragment(self):
        title = ('«Tas nebija viegli» smagais gads, kas šķēpmetēju Gailumu '
                 'aizveda līdz medaļai')
        self.assertEqual(messaging._headline_trim(title, 30), title)

    def test_digest_uses_pipe_instead_of_em_dash_for_story_detail(self):
        self.assertEqual(messaging._DIGEST_DETAIL_SEPARATOR, " · ")
        line = "1. 📰 Virsraksts" + messaging._DIGEST_DETAIL_SEPARATOR + "Fakts."
        with patch.object(messaging, "_proofread_candidate", return_value=""):
            self.assertEqual(messaging._digest_proof_pass(line, {}), line)
        self.assertNotIn(" — ", line)

    def test_local_ai_selects_multiple_source_sentences_without_rewriting(self):
        candidates = [
            "Pirmais avota teikums paskaidro notikuma iemeslu.",
            "Otrais avota teikums nosauc svarīgākās sekas.",
            "Trešais avota teikums nav vajadzīgs kopsavilkumā.",
        ]
        with patch.object(messaging, "_ollama_chat",
                          return_value='{"indices":[2,1]}'):
            recap = messaging._pick_article_sentences(
                {"model": "local"}, "Virsraksts", candidates, max_len=180)
        self.assertEqual(recap, candidates[0] + " " + candidates[1])

    def test_one_model_index_is_rejected_instead_of_static_completion(self):
        candidates = [
            "Pirmais avota teikums paskaidro konkrēto notikuma iemeslu.",
            "Otrais avota teikums nosauc svarīgākās sekas sabiedrībai.",
        ]
        with patch.object(messaging, "_ollama_chat",
                          return_value='{"indices":[1]}'):
            recap = messaging._pick_article_sentences(
                {"model": "local"}, "Virsraksts", candidates, max_len=180)
        self.assertEqual(recap, "")

    def test_model_failure_never_falls_back_to_first_scraped_sentence(self):
        body = ("Vairāk lasi šeit par citām ziņām un komentāriem. "
                "Raksta pirmais fakts izskaidro notikuma iemeslu. "
                "Raksta otrais fakts izskaidro notikuma sekas.")
        with patch.object(messaging, "read_link", return_value={"text": body}), \
                patch.object(messaging, "_pick_article_sentences",
                             return_value=None):
            self.assertEqual(messaging._article_gloss(
                "https://example.com/story", "Virsraksts", acfg={"model": "local"}), "")

    def test_article_recap_reads_a_bounded_full_body_not_only_2500_chars(self):
        body = ("Virsraksta kontekstā valdība paskaidroja lēmuma konkrēto iemeslu. "
                "Otrs teikums papildina notikumu ar konkrētām sekām.")
        with patch.object(messaging, "read_link",
                          return_value={"text": body}) as reader, \
                patch.object(messaging, "_pick_article_sentences",
                             return_value="Valdība paziņoja par jauno lēmumu šodien."):
            self.assertEqual(messaging._article_gloss(
                "https://example.com/story", "Virsraksta tēma", max_len=200,
                acfg={"model": "local"}), "Valdība paziņoja par jauno lēmumu šodien.")
        self.assertEqual(reader.call_args.kwargs["max_chars"], 30000)

    def test_weather_reuses_supplied_forecast_rows(self):
        payload = json.dumps({
            "current_condition": [{"temp_C": "20", "FeelsLikeC": "20"}],
            "weather": [{"maxtempC": "21", "mintempC": "12",
                         "hourly": []}],
        }).encode("utf-8")

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return payload

        rows = [{"date": messaging.datetime.now(), "hi": 21,
                 "rain_day": 0, "mm": 0.0}]
        with patch.object(messaging.urllib.request, "urlopen",
                          return_value=Response()), \
                patch.object(messaging, "_forecast_days",
                             side_effect=AssertionError("duplicate forecast")):
            self.assertIn("Rīgā +20°", messaging._digest_weather(
                forecast_rows=rows))

    def test_digest_trace_separates_local_ai_from_external_requests(self):
        messaging._digest_audit_start()
        messaging._digest_audit_inc("selection_model_calls")
        messaging._digest_audit_inc("article_sentence_model_calls", 2)
        messaging._digest_audit_inc("rss_requests", 4)
        trace = messaging._digest_audit_snapshot()
        self.assertEqual(trace["local_ai_chat_calls"], 3)
        self.assertEqual(trace["external_http_calls"], 4)
        self.assertFalse(trace["ai_writes_news_text"])


class PdfReaderRegressionTests(unittest.TestCase):
    def setUp(self):
        messaging._LINK_CACHE.clear()

    def test_link_reader_extracts_pdf_instead_of_rejecting_content_type(self):
        class Response:
            headers = {"Content-Type": "application/pdf"}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, *_args):
                return b"%PDF-fake"

        parsed = {"title": "Platinum noteikumi", "text": "Limits 1 000 000 EUR",
                  "page_count": 19, "pages_read": 19}
        with patch.object(messaging, "_safe_urlopen", return_value=Response()), \
                patch.object(messaging, "_pdf_text_from_bytes",
                             return_value=parsed):
            result = messaging.read_link(
                "https://example.com/platinum.pdf", max_chars=12000)
        self.assertEqual(result["text"], parsed["text"])
        self.assertEqual(result["page_count"], 19)
        self.assertEqual(result["note"], "")

    def test_attachment_document_tool_uses_local_pdf_extraction(self):
        ctx = agent_tools.AgentContext(attachments=({
            "guid": "pdf-guid", "mime": "application/pdf",
            "name": "noteikumi.pdf", "bytes": b"%PDF-fake",
        },))
        parsed = {"title": "Noteikumi", "text": "Bagāža 1000 EUR",
                  "page_count": 3, "pages_read": 3}
        with patch.object(messaging, "_pdf_text_from_bytes",
                          return_value=parsed):
            result = agent_tools._tool_read_document({}, ctx)
        self.assertEqual(result["text"], parsed["text"])
        self.assertEqual(result["source"], "local_pypdf")

    def test_limits_query_keeps_the_whole_pdf_table_together(self):
        table = (
            "Kas ir apdrošināts, Apdrošinājuma summas un Pašriski. "
            "Medicīniskā palīdzība 1 000 000 EUR. Personiskās mantas 2500 EUR. "
            "Ceļojuma atcelšana 3500 EUR. Nomas automašīnas KASKO 40 000 EUR."
        )
        text = ("Garš ievads par noteikumiem. " * 200) + table + (
            " Citi vispārīgie nosacījumi." * 200)
        excerpt = agent_tools._relevant_excerpt(
            text, "Platinum apdrošināšanas limiti bagāža KASKO")
        for amount in ("1 000 000", "2500", "3500", "40 000"):
            self.assertIn(amount, excerpt)


if __name__ == "__main__":
    unittest.main(verbosity=2)
