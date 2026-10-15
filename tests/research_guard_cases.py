# -*- coding: utf-8 -*-
"""The two guards that decide whether a researched answer is fit to send.

  language  "Ir kaut kas jauns par Ukrainu?" is a Latvian sentence that happens
            to contain no āčēģīķļņšūž. want_english tested only for those
            characters, so it answered with four sentences of English.

  on-topic  the same question came back with a study about delaying dementia.
            The retrieval was fine; a news FRONT PAGE was fetched whole and the
            model answered from the "most read" panel. Nothing downstream
            noticed, because every later guard trusts an answer once material
            of any kind exists.

    py tests\\research_guard_cases.py
"""
import io
import os
import re
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import messaging as M  # noqa: E402

# (question, want_english, note)
LANGUAGE = [
    ("Ir kaut kas jauns par Ukrainu?", False, "LV, no diacritics at all"),
    ("Kas jauns par airBaltic?", False, "LV, no diacritics"),
    ("kada laika prognoze sodien?", False, "LV typed fully stripped"),
    ("Anton, kas jauns Latvijā?", False, "LV with diacritics"),
    ("Cik tālu no Rīgas lidz Siguldai?", False, "LV, half stripped"),
    ("What is new about Ukraine?", True, "real English"),
    ("who won the hockey game?", True, "real English"),
    ("how much does fuel cost in Latvia?", True, "real English, names Latvia"),
]

# (question, answer, want_kept, note)
ON_TOPIC = [
    ("Ir kaut kas jauns par Ukrainu?",
     "Jauns pētījums atklāj trīs lietas, kas demenci var attālināt par 13 gadiem.",
     False, "THE BUG — answered a different subject"),
    ("Ir kaut kas jauns par Ukrainu?",
     "Krievija varētu gatavoties jaunai ofensīvai Ukrainā, brīdina Mūrniece.",
     True, "genuinely about Ukraine"),
    ("Kādas šodien ir ziņas?",
     "Ir sporta ziņas, dronu incidenti un airBaltic glābšana.",
     True, "no named subject — broad answer is correct"),
    ("Anton, kas jauns Latvijā?",
     "Jaunākās ziņas Latvijā ir dažādas, Apollo ziņo par traģēdiju.",
     True, "mentions Latvija"),
    ("Kas uzvarēja hokejā?", "Latvija uzvarēja 3:2.",
     True, "no proper noun in the question"),
    ("Cikos veikals ciet svētdien?", "Rimi strādā līdz 22:00.",
     True, "sentence-initial capital is not a subject"),
    ("Cik tālu no Rīgas līdz Siguldai?", "Apmēram 53 km pa autoceļu A2.",
     False, "names two places, answer has neither"),
    ("Cik tālu no Rīgas līdz Siguldai?", "No Rīgas līdz Siguldai ir ap 53 km.",
     True, "names both"),
]


def main():
    acfg = M.load_config().get("auto_reply") or {}
    bad = 0

    print("## answer language")
    for question, want, note in LANGUAGE:
        got = (not M._LV_TEXT_HINT_RE.search(question)) and bool(
            re.search(r"[a-z]", question, re.I))
        ok = got == want
        bad += not ok
        print("  %-5s -> %-2s  %-34s %s"
              % ("ok" if ok else "FAIL", "EN" if got else "LV", note, question[:34]))

    print()
    print("## answer is about what was asked")
    for question, answer, want, note in ON_TOPIC:
        got = M._answer_is_about(answer, question, acfg)
        ok = got == want
        bad += not ok
        print("  %-5s -> %-6s %-40s %s"
              % ("ok" if ok else "FAIL", "keep" if got else "REJECT",
                 note, question[:30]))

    print("-" * 78)
    print("VERDICT: %s" % ("PASS" if not bad else "FAIL — %d" % bad))
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
