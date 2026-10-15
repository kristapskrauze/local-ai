# -*- coding: utf-8 -*-
"""python tests/destyle_history_cases.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import messaging as M  # noqa: E402


def test_the_sent_glyphs_go_back_to_markdown():
    sent = M._apply_bold_runs("**Maine:** `app/agent_tools.py` — tikai apraksts.")
    assert sent != "**Maine:** `app/agent_tools.py` — tikai apraksts."
    assert M._destyle(sent) == "**Maine**: `app/agent_tools.py` — tikai apraksts."


def test_destyled_history_restyles_to_the_same_message():
    for md in ("**Tests:** `tests/icloud_reminder_cases.py` iet cauri.",
               "Rīks `icloud_reminder` ir **EXTERNAL_WRITE** tagad.",
               "**50%** no `app/static/app.js`, `x = 2`."):
        sent = M._apply_bold_runs(md)
        assert M._apply_bold_runs(M._destyle(sent)) == sent, (md, M._destyle(sent))


def test_plain_text_and_latvian_are_untouched():
    for text in ("Pielikts, kungs. Tests iet cauri, izmaiņa ir diskā.",
                 "Mērķis: 18:00 Itālijas laikā (UTC+2).", ""):
        assert M._destyle(text) == text


def test_the_garbled_reply_reads_as_code_again():
    garbled = "𝗡𝗼𝘂𝗿𝗲: 𝚘𝚠𝚗𝚎𝚛_𝚊𝚕𝚕𝚘𝚠𝚎𝚍(𝚜𝚎𝚗𝚍𝚎𝚛) → 𝚋𝚞𝚝𝚗 𝚘𝚗𝚘𝚕𝚘"
    assert M._destyle(garbled) == "**Noure**: `owner_allowed(sender)` → `butn onolo`"


if __name__ == "__main__":
    test_the_sent_glyphs_go_back_to_markdown()
    test_destyled_history_restyles_to_the_same_message()
    test_plain_text_and_latvian_are_untouched()
    test_the_garbled_reply_reads_as_code_again()
    print("OK")
