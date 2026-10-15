# -*- coding: utf-8 -*-
"""_fix_lv_unknown_words: only words the checker does not know change, and only for a known word.

    py -3 tests/lv_unknown_word_cases.py
"""
import inspect
import io
import json
import os
import re
import sys
import urllib.request

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _blocked(*args, **kwargs):
    raise RuntimeError("network blocked in this test")


urllib.request.urlopen = _blocked
from app import messaging as M  # noqa: E402

PASS, FAIL = [], []
FENCE = "`" * 3
REPLY = ('Atradu to. 𝚊𝚙𝚙/𝚖𝚎𝚜𝚜𝚊𝚐𝚒𝚗𝚐.𝚙𝚢, rinda 17111: python think_level = "high" if _think else '
         'False _𝚝𝚑𝚒𝚗𝚔 ir tas "jā/nē" karogs — 𝚃𝚛𝚞𝚎 darba gājā (labo koda, lasa koda, būvē '
         'lapu), 𝙵𝚊𝚕𝚜𝚎 parastā čatā. Un vērtība, uz ko tas kartojas, ir jau "𝚑𝚒𝚐𝚑". Tātad '
         'pašlaik: darba gājā → 𝚝𝚑𝚒𝚗𝚔_𝚕𝚎𝚟𝚎𝚕="𝚑𝚒𝚐𝚑", parasts čats → 𝚝𝚑𝚒𝚗𝚔_𝚕𝚎𝚟𝚎𝚕=𝙵𝚊𝚕𝚜𝚎. '
         'Vai gribi, lai paceltu uz "𝚖𝚊𝚡", vai domāji citu vietu?')
ACFG = {"model": "qwen3.8:latest", "num_ctx": 32768}


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("    %-66s %s" % (name, "ok" if ok else "FAIL  " + str(detail)[:120]))


class FakeChat:
    def __init__(self, answer):
        self.answer, self.calls = answer, []

    def __call__(self, acfg, messages, options=None, format_schema=None, think=False):
        self.calls.append({"messages": messages, "options": dict(options or {}),
                           "format": format_schema, "think": think})
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def answer(*pairs):
    return json.dumps({"fixes": [{"word": w, "replacement": r} for w, r in pairs]},
                      ensure_ascii=False)


def run(text, fake, latest="", recent=""):
    real = M._ollama_chat
    M._ollama_chat = fake
    try:
        return M._fix_lv_unknown_words(ACFG, text, latest, recent)
    finally:
        M._ollama_chat = real


def asked(fake):
    if not fake.calls:
        return []
    user = fake.calls[0]["messages"][-1]["content"]
    m = re.search(r"Words: (\[.*\])\s*$", user, re.S)
    return json.loads(m.group(1)) if m else []


def main():
    if not M.LV_VOCAB_PATH.exists():
        # the vocabulary is built from the owner's own messages, so it is never in the repo
        print("SKIP: needs config/lv_vocab.txt (private data, not in the repo)")
        return 0
    print("0. it runs where replies are finished, before the word-form step")
    src = inspect.getsource(M._finalize_agent_reply)
    call, morph = src.find("_fix_lv_unknown_words("), src.find("_fix_lv_morphology(")
    check("_finalize_agent_reply calls it", call >= 0)
    check("before the word-form step", 0 <= call < morph, (call, morph))
    guards = src.find("if (out and _guards_on()")
    check("not inside the block that only runs with reply guards on",
          call >= 0 and not (guards >= 0 and guards < call < src.find("_polish_data_reply(", guards)),
          (guards, call))
    check("its own switch lv_word_fix", "lv_word_fix" in src)

    print("")
    print("1. the real reply: only its unknown words are asked, only the answered one changes")
    fake = FakeChat(answer(("gājā", "gājienā"), ("kartojas", None)))
    out = run(REPLY, fake)
    words = asked(fake)
    check("one model call", len(fake.calls) == 1, len(fake.calls))
    check("gājā and kartojas were asked", {"gājā", "kartojas"} <= set(words), words)
    check("no code, glyph or English token was asked",
          not (set(words) & {"think", "level", "messaging", "python", "high", "max", "False", "True"}), words)
    check("every gājā became gājienā", "gājā" not in out and out.count("gājienā") == REPLY.count("gājā"), out[:120])
    check("nothing else changed", out.replace("gājienā", "gājā") == REPLY)
    if fake.calls:
        c = fake.calls[0]
        check("thinking on, num_predict 4096, a JSON schema",
              c["think"] is True and c["options"].get("num_predict") == 4096 and bool(c["format"]), c)

    print("")
    print("2. the original capital letter is kept")
    text = "Priekšlikums neizdevās — kods neparsēja. Pārformulušu un mēģināšu vēlreiz."
    out = run(text, FakeChat(answer(("Pārformulušu", "pārformulēšu"))))
    check("Pārformulušu -> Pārformulēšu", "Pārformulēšu un mēģināšu" in out, out)

    print("")
    print("3. a replacement that is not a known plain word is refused")
    for bad in ("z-bфера", "gājienāxq", "gājienā 2", "ļoti ļoti ļoti gājienā"):
        check("refused %r" % bad, run(REPLY, FakeChat(answer(("gājā", bad)))) == REPLY)

    print("")
    print("4. no answer, a bad answer or a failing model changes nothing")
    for label, fake in (("null", FakeChat(answer(("gājā", None)))),
                        ("empty list", FakeChat('{"fixes": []}')),
                        ("not JSON", FakeChat("gājā -> gājienā")),
                        ("exception", FakeChat(RuntimeError("model down"))),
                        ("a word that was not asked", FakeChat(answer(("karogs", "flag"))))):
        check(label, run(REPLY, fake) == REPLY)

    print("")
    print("5. no unknown word, or no Latvian: the model is not called")
    fake = FakeChat(answer(("x", "y")))
    run("Atradu to: karogs, vērtība un rinda pašlaik.", fake)
    check("known words only: no call", not fake.calls, len(fake.calls))
    fake = FakeChat(answer(("x", "y")))
    run("Plain English with no Latvian letters at all.", fake)
    check("no Latvian letters: no call", not fake.calls, len(fake.calls))

    print("")
    print("6. a word the other side wrote is theirs")
    fake = FakeChat(answer(("gājā", None)))
    run(REPLY, fake, latest="a kā tas kartojas ar to flagu?")
    check("kartojas not asked when they used it", "kartojas" not in asked(fake), asked(fake))

    print("")
    print("7. code and links are never asked about and never edited")
    text = ("Skaties `gājā` failā app/lv_engine.py un renderInbox https://x.lv/gājā — darba gājā.\n"
            + FENCE + "\ngājā = 1\n" + FENCE)
    fake = FakeChat(answer(("gājā", "gājienā"), ("renderInbox", "renderēšana")))
    out = run(text, fake)
    check("identifiers, paths and links not asked",
          not (set(asked(fake)) & {"renderInbox", "lv", "engine", "py", "https", "x"}), asked(fake))
    check("inline code kept", "`gājā`" in out, out)
    check("link kept", "https://x.lv/gājā" in out, out)
    check("fenced block kept", FENCE + "\ngājā = 1\n" + FENCE in out, out)
    check("camelCase kept", "renderInbox" in out, out)
    check("the prose word fixed", "darba gājienā." in out, out)

    print("")
    print("%d passed, %d failed" % (len(PASS), len(FAIL)))
    for name in FAIL:
        print("   FAILED: %s" % name)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
