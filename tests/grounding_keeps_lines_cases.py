"""The grounding pass keeps the reply's lines.

No model runs: a stand-in answers the grounding call; every other model call
in the chain is refused, so those steps leave the text as it is.

    python tests/grounding_keeps_lines_cases.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app.people import PARTNER_DAT
from app import messaging as M  # noqa: E402

PLAN = ("Bomb It 2 ir Bomberman tipa spēle.\n"
        "**Kā spēlē:** ej pa laukumu un liec bumbas.\n"
        "**Mērķis:** uzvarēt pārējos spēlētājus.\n"
        "Izklausās labi?")


def _run(answer_for_draft, fn):
    def stand_in(cfg, messages, options=None, **_kw):
        content = messages[-1]["content"]
        if "ČERNOVIKS:\n" not in content:
            raise RuntimeError("no other model calls in this test")
        return answer_for_draft(content.split("ČERNOVIKS:\n", 1)[1])

    saved = M._ollama_chat
    M._ollama_chat = stand_in
    try:
        return fn()
    finally:
        M._ollama_chat = saved


def test_unchanged_draft_keeps_its_lines():
    out = _run(lambda d: d, lambda: M._grounded_reply({}, PLAN, "web_search -> Bomb It 2", "q"))
    assert out == PLAN, repr(out)


def test_draft_handed_back_as_one_line_keeps_its_lines():
    out = _run(lambda d: " ".join(d.split()),
               lambda: M._grounded_reply({}, PLAN, "web_search -> Bomb It 2", "q"))
    assert out == PLAN, repr(out)


def test_a_real_rewrite_keeps_the_layout_it_came_back_in():
    out = _run(lambda d: d.replace("uzvarēt pārējos spēlētājus", "palikt pēdējam uz laukuma"),
               lambda: M._grounded_reply({}, PLAN, "web_search -> Bomb It 2", "q"))
    assert out.count("\n") == 3 and "palikt pēdējam" in out, repr(out)


def test_the_whole_finishing_chain_keeps_the_lines():
    out = _run(lambda d: d, lambda: M._finalize_agent_reply(
        {}, PLAN, f"Anton uztaisi {PARTNER_DAT} spēli bomb it 2", "", "iMessage;-;+37120000000",
        findings="web_search -> Bomb It 2: blow up your opponents"))
    # the label keeps its own line; its ** become bold glyphs or, with a
    # Latvian letter in it, plain text (_unicode_style) -- either is fine here
    assert out.count("\n") == 3 and out.splitlines()[1].startswith("Kā spēlē:"), repr(out)


if __name__ == "__main__":
    test_unchanged_draft_keeps_its_lines()
    test_draft_handed_back_as_one_line_keeps_its_lines()
    test_a_real_rewrite_keeps_the_layout_it_came_back_in()
    test_the_whole_finishing_chain_keeps_the_lines()
    print("OK")
