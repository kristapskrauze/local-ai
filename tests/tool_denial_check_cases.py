"""A self-chat reply that wrongly says "I can't" is caught by reading, not by words.

No model runs: a stand-in answers.

    python tests/tool_denial_check_cases.py
"""
import json
import os
import sys
import types
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import messaging as M  # noqa: E402

TOOLS = ("antons_read_code", "antons_propose_fix", "web_search")


def _run(answer, results=()):
    return types.SimpleNamespace(answer=answer, tool_results=list(results))


def _check(answer, verdict=None, results=(), tools=TOOLS, contact=False, fail=False,
           latest="kāpēc manas ziņas konsolē izskatās tāpat kā tavas?"):
    asked = []

    def judge(cfg, messages, options=None, format_schema=None, think=False):
        asked.append(messages[-1]["content"])
        if fail:
            raise RuntimeError("ollama down")
        return json.dumps({"false_denial": bool(verdict)})

    with patch.object(M, "_ollama_chat", side_effect=judge):
        got = M._self_tool_denial_retry_needed(_run(answer, results), latest, tools,
                                               trusted_contact_action=contact, acfg={})
    return got, asked


def test_a_denial_the_old_words_missed_is_caught():
    got, asked = _check("Iekopē man app.js saturu, tad varēšu palīdzēt.", verdict=True)
    assert got is True and len(asked) == 1, (got, asked)
    assert "antons_read_code" in asked[0] and "app.js" in asked[0] and "konsolē" in asked[0]


def test_an_answer_the_judge_clears_is_left_alone():
    got, _ = _check("renderInbox zīmē is_from_me tikai kā etiķeti.", verdict=False)
    assert got is False


def test_contact_chats_and_toolless_turns_are_not_asked():
    got, asked = _check("Nevaru to izdarīt.", verdict=True, contact=True)
    assert got is False and not asked
    got, asked = _check("Nevaru to izdarīt.", verdict=True, tools=("web_search",))
    assert got is False and not asked


def test_a_self_tool_that_already_ran_settles_it():
    ran = [{"tool": "antons_read_code", "ok": True, "result": {"lines": "..."}}]
    got, asked = _check("Nevaru atrast to failu.", verdict=True, results=ran)
    assert got is False and not asked


def test_a_failed_check_means_no_retry():
    got, _ = _check("Man nav rīka koda lasīšanai.", fail=True)
    assert got is False


if __name__ == "__main__":
    test_a_denial_the_old_words_missed_is_caught()
    test_an_answer_the_judge_clears_is_left_alone()
    test_contact_chats_and_toolless_turns_are_not_asked()
    test_a_self_tool_that_already_ran_settles_it()
    test_a_failed_check_means_no_retry()
    print("OK")
