"""The lookup's own reading of what it found reaches the answer lane.

The reading goes along now, labelled as a reading and never as evidence: the
grounding pass still checks the reply against the tool results alone.

No model runs.

    python tests/prepass_reading_cases.py
"""
import os
import sys
import types
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import messaging as M  # noqa: E402

READING = ("Latvijā ir 10 valstspilsētas, bet no tām 7 ir ar savu atsevišķu "
           "pašvaldību: Rīga, Daugavpils, Jelgava, Jūrmala, Liepāja, Rēzekne, Ventspils.")
FOUND = [{"tool": "web_search", "result": {"query": "7 valsts pilsētas",
                                           "results": [{"title": "Valstspilsēta"}]}}]


def _prepass(tool_results, answer):
    run = types.SimpleNamespace(tool_results=tool_results, answer=answer, cancelled=False)
    got = []
    with patch.object(M.qwen_agent, "run_agent_turn", return_value=run):
        findings = M._lookup_prepass({}, "kuras ir tās 7 valsts pilsētas?", ("web_search",),
                                     "iMessage;-;partner", "partner", "qwen", reading_out=got)
    return findings, got


def test_the_reading_is_handed_over_and_kept_out_of_the_evidence():
    findings, got = _prepass(FOUND, READING)
    assert got == [READING], got
    assert "Valstspilsēta" in findings and "savu atsevišķu pašvaldību" not in findings, findings


def test_no_reading_without_a_lookup():
    # prose with no tool behind it is a guess from memory, not a reading
    findings, got = _prepass([], "Tās ir Rīga un vēl sešas.")
    assert findings == "" and got == [], (findings, got)


def test_no_reading_when_it_only_searched_the_chat():
    only_chat = [{"tool": "search_chat_history",
                  "result": {"matches": [{"text": "Kopumā ir 35 novadi un 7 valstspilsētas."}]}}]
    findings, got = _prepass(only_chat, "Latvijas 7 valstspilsētas ir: Rīga, Jūrmala, "
                                        "Daugavpils, Liepāja, Jelgava, Valmiera, Jēkabpils.")
    assert "35 novadi" in findings and got == [], (findings, got)


def test_the_answer_lane_sees_it_as_a_reading():
    seen = {}

    def fake_prepass(*args, reading_out=None, **kwargs):
        if reading_out is not None:
            reading_out.append(READING)
        return "web_search -> {\"results\": [{\"title\": \"Valstspilsēta\"}]}"

    def fake_agent(**kwargs):
        seen["conversation"] = kwargs.get("conversation")
        raise RuntimeError("stop before the model")

    with patch.object(M, "_lookup_prepass", side_effect=fake_prepass), \
            patch.object(M.qwen_agent, "run_agent_turn", side_effect=fake_agent):
        try:
            M._live_assistant_agent_reply(
                {}, [{"role": "system", "content": "persona"},
                     {"role": "user", "content": "kuras ir tās 7 valsts pilsētas?"}],
                "iMessage;-;partner", "kuras ir tās 7 valsts pilsētas?", "",
                sender="partner", trusted_contact_action=True)
        except RuntimeError:
            pass
    notes = [m["content"] for m in seen.get("conversation") or [] if m.get("role") == "system"]
    reading = [n for n in notes if READING in n]
    assert reading, notes
    assert "Valstspilsēta" not in reading[0], reading[0]   # its own note, not the results'


if __name__ == "__main__":
    test_the_reading_is_handed_over_and_kept_out_of_the_evidence()
    test_no_reading_without_a_lookup()
    test_no_reading_when_it_only_searched_the_chat()
    test_the_answer_lane_sees_it_as_a_reading()
    print("OK")
