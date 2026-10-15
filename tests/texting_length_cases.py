# -*- coding: utf-8 -*-
"""The length cap must shorten encyclopedia answers, not amputate qualifications.

The cap kept two sentences OR 26 words, whichever came first, and the word
half only ever fired on the second sentence -- the first is exempt. So a
single 36-word run-on shipped whole while a 28-word two-sentence answer was
cut to 9 words. It was not limiting length, it was penalising punctuation.

The sentence cap does the actual shaping and stays. Pure string logic, offline.

    py tests/texting_length_cases.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.messaging import _texting_length          # noqa: E402

VRAM = ("Jā, matemātiski tā ir, 24 mīnus 17 ir 7. "
        "Bet reāli atmiņā var būt vēl citas lietas (operētājsistēma, citi "
        "procesi), tāpēc brīvi var būt mazāk par 7 GB.")

ENCYCLOPEDIA = ("Latvija ir valsts Baltijas jūras austrumu krastā. "
                "Tā robežojas ar Igauniju, Lietuvu, Krieviju un Baltkrieviju. "
                "Latvijā ir ap 12 000 upju. "
                "Tur ir arī ap 3 000 ezeru. "
                "Galvaspilsēta ir Rīga.")

BULLETS = ("Redzu šādus variantus:\n"
           "- BMW 320d, 2005, 1900 eiro\n"
           "- Audi A4, 2004, 1750 eiro")

LINK = ("Atradu to lapā, adrese ir Brīvības iela 40 un tur strādā līdz "
        "astoņiem vakarā katru darbadienu. https://example.lv/veikals")


def check(name, text, want_sentences, want_whole=None):
    out = _texting_length(text, None)
    kept = out.strip()
    whole = kept == text.strip()
    sents = kept.count(".") + kept.count("!") + kept.count("?")
    ok = True
    if want_whole is True and not whole:
        ok = False
    if want_whole is False and whole:
        ok = False
    if want_sentences is not None and sents > want_sentences:
        ok = False
    print("%-4s %-22s in=%3dw out=%3dw whole=%-5s  %s"
          % ("OK" if ok else "FAIL", name, len(text.split()),
             len(kept.split()), whole, kept[:58].replace("\n", " | ")))
    return 0 if ok else 1


def main():
    failed = 0
    failed += check("two-sentence answer", VRAM, None, want_whole=True)
    failed += check("five-sentence dump", ENCYCLOPEDIA, 2, want_whole=False)
    failed += check("bullet list", BULLETS, None, want_whole=True)
    failed += check("answer with a link", LINK, None, want_whole=True)

    # the qualification itself must survive, not just "some" of the reply
    out = _texting_length(VRAM, None)
    ok = "mazāk" in out
    failed += 0 if ok else 1
    print("%-4s %-22s the qualifying clause survives the cap"
          % ("OK" if ok else "FAIL", "qualification kept"))

    print("\n%d failed" % failed)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
