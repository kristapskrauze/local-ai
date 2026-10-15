# -*- coding: utf-8 -*-
"""The greeting guard must delete bare greetings and keep short answers.

  * ja / ok / nu live in _GREETING_ONLY_RE because they are strippable filler
    when they OPEN a sentence, but the emptiness test built on that regex then
    read an affirmation as a greeting.
  * the test demanded >= 2 words left after stripping, which deleted EVERY
    one-word reply -- "Ne." and "Riga." contain no greeting at all.

Pure string logic, so this runs offline in milliseconds. No model, no service.

    py tests/greeting_guard_cases.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.messaging import _says_nothing          # noqa: E402

# (reply lines, should_be_dropped, why)
CASES = [
    (["Jā."],        False, "an affirmation answers the question it was asked"),
    (["Jā"],         False, "same without the full stop"),
    (["Nē."],        False, "a negation is an answer too"),
    (["Ok"],         False, "an acknowledgement is an answer"),
    (["Protams"],    False, "'of course' answers a yes/no question"),
    (["Rīgā."],      False, "one content word is still content"),
    (["Piektdien"],  False, "a one-word answer to 'when' is an answer"),

    (["Labvakar"],           True, "a bare greeting answers nothing"),
    (["Labvakar!"],          True, "punctuation does not make it content"),
    (["Čau"],                True, "bare greeting"),
    (["Sveiki"],             True, "bare greeting"),
    (["Labrīt 🌙"],          True, "greeting plus an emoji is still a greeting"),
    (["Labvakar", "Čau"],    True, "every line a greeting"),

    (["Nu jā, tā ir taisnība, es arī tā domāju"], False,
     "the filler opens a real sentence, which must survive"),
    (["Labvakar, kā tev iet?"], False,
     "a greeting followed by an actual question is not a bare greeting"),
    (["Labvakar", "Rīgā līst"], False,
     "one greeting line does not condemn a reply that also says something"),
]


def main():
    failed = 0
    for lines, want_drop, why in CASES:
        got = bool(_says_nothing(lines))
        ok = got == want_drop
        failed += 0 if ok else 1
        print("%-4s %-44r drop=%-5s want=%-5s  %s"
              % ("OK" if ok else "FAIL", " / ".join(lines), got, want_drop, why))
    print("\n%d case(s), %d failed" % (len(CASES), failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
