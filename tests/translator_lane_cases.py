# -*- coding: utf-8 -*-
"""The translator lane renders Anton's FINAL answer, shape intact, or not at all.

    python tests/translator_lane_cases.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import messaging as M  # noqa: E402

LV = ("Priekšlikums gatavs, kungs.\n"
      "**Maiņa:** `app/agent_tools.py` — tikai apraksts.\n"
      "- saite: http://198.51.100.7:8183/bomb-it-2.html\n"
      "Saki \"jā\", un es to pielikšu.")
EN = ("The proposal is ready, sir.\n"
      "**Change:** `app/agent_tools.py` — only the description.\n"
      "- link: http://198.51.100.7:8183/bomb-it-2.html\n"
      "Say \"yes\" and I will apply it.")
ON = {"reply_pipeline": "translate"}


def _with(en_answer, nmt=None):
    """Run the lane with the model answering `en_answer` and a stand-in
    translator (default: prefixes each piece, keeps placeholders)."""
    saved = (M._ollama_chat, M._nmt_lv, M._finalize_agent_reply)
    seen = []
    M._ollama_chat = lambda acfg, messages, options=None, **k: en_answer
    M._nmt_lv = nmt or (lambda text, provider="", acfg=None: seen.append(text) or "LV:" + text)
    M._finalize_agent_reply = lambda acfg, reply, *a, **k: reply
    try:
        return M._render_reply_through_translator(ON, LV), seen
    finally:
        M._ollama_chat, M._nmt_lv, M._finalize_agent_reply = saved


def test_off_unless_switched_on():
    assert M._render_reply_through_translator({}, LV) is None
    assert M._render_reply_through_translator({"reply_pipeline": ""}, LV) is None


def test_the_shape_survives_and_labels_are_translated_apart():
    out, seen = _with(EN)
    lines = out.split("\n")
    assert len(lines) == 4, out
    assert lines[0] == "LV:The proposal is ready, sir.", lines[0]
    # the label is translated on its own, the code goes back verbatim
    assert lines[1].startswith("**LV:Change:** "), lines[1]
    assert "`app/agent_tools.py`" in lines[1], lines[1]
    # the list marker stays, the link is never sent to the translator
    assert lines[2].startswith("- LV:link: ") and lines[2].endswith(
        "http://198.51.100.7:8183/bomb-it-2.html"), lines[2]
    assert not any("http" in s or "agent_tools.py" in s for s in seen), seen


def test_any_doubt_keeps_the_original():
    # lines merged in English
    assert _with(EN.replace("\n- link", " - link"))[0] is None
    # a link the English dropped
    assert _with(EN.replace("http://198.51.100.7:8183/bomb-it-2.html", "the link"))[0] is None
    # the translator lost a held token
    assert _with(EN, nmt=lambda text, provider="", acfg=None: "LV")[0] is None
    # the translator returned nothing
    assert _with(EN, nmt=lambda text, provider="", acfg=None: None)[0] is None


if __name__ == "__main__":
    test_off_unless_switched_on()
    test_the_shape_survives_and_labels_are_translated_apart()
    test_any_doubt_keeps_the_original()
    print("OK")
