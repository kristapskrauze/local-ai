# -*- coding: utf-8 -*-
"""Postprocessing may remove a CLAIM. It may not destroy the answer.

Two independent defects made that one message:

And from the same turn's second run, the echo guard removed the two lines a
third line referred to and sent the leftover conclusion on its own:
"Tāpēc nevaru apstiprināt, vai himna skanēja" — 43 chars of 147.

Offline. No model call, no network.

    py -3 tests/postprocess_loss_cases.py
"""
import io
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.people import OWNER
from app import messaging as M  # noqa: E402


def main():
    checks = []

    def check(label, passed):
        checks.append((label, bool(passed)))

    check("LOSS-001 '18. novembris' is one sentence, not two",
          M._lv_sentences("Svētku dienās (18. novembris, 4. maijs) skan himna.")
          == ["Svētku dienās (18. novembris, 4. maijs) skan himna."])
    check("LOSS-001 an abbreviation does not end one either",
          M._lv_sentences("Sāksies plkst. 19:00. Nāc arī!")
          == ["Sāksies plkst. 19:00.", "Nāc arī!"])
    # ...and the inverse: a real boundary after a number is still a boundary,
    # or the answer this whole case is about would come back as one blob
    check("LOSS-001 a clock time still ends a sentence",
          M._lv_sentences("Himna skan 05:58. Ziņas sākas 06:00.")
          == ["Himna skan 05:58.", "Ziņas sākas 06:00."])
    check("LOSS-001 short sentences are not merged",
          M._lv_sentences("Jā. Nē. Varbūt.") == ["Jā.", "Nē.", "Varbūt."])

    raw = ("Parasti Latvijas himnu radio raida tikai īpašos gadījumos, svētku "
           "dienās (18. novembris, 4. maijs) vai pēc īpaši svarīgiem valsts "
           "notikumiem.")
    out = M._texting_length(raw, None, max_sentences=2, max_words=26)
    check("LOSS-002 the cap does not cut inside a bracket",
          out.count("(") == out.count(")"))
    check("LOSS-002 ...and does not end mid-sentence",
          M._complete_sentence(out))
    check("LOSS-002 an over-cap single sentence survives whole",
          out == raw)

    # a truncated generation must not be shipped as the tail of a longer reply
    cut = ("Himna skan Latvijas Radio rītos.\n"
           "Ja vakar to laida, tam bija kāds iemesls, vai arī tas bija ī")
    out2 = M._texting_length(cut, None, max_sentences=6, max_words=110)
    check("LOSS-002 an amputated tail is dropped, not sent",
          "tas bija ī" not in out2 and "Himna skan" in out2)

    fact = ("Latvijas Radio 1 himnu atskaņo katru rītu 05:58, pirms ziņām.\n"
            "Mix FM ir privāta stacija un savu pilnu ēteru nepublicē.")
    kept = M._texting_length(fact, None, max_sentences=6, max_words=110)
    check("LOSS-003 the primary fact survives",
          "05:58" in kept)
    check("LOSS-003 the secondary grounded fact survives too",
          "Mix FM" in kept)
    check("LOSS-003 the line structure survives",
          len(kept.splitlines()) == 2)

    # ---- LOSS-004: removing a claim must not leave a dangling clause -------
    check("LOSS-004 a leftover conclusion is stood up, not sent as-is",
          M._repair_dangling_start("Tāpēc nevaru apstiprināt, vai himna skanēja")
          == "Nevaru apstiprināt, vai himna skanēja")
    check("LOSS-004 a reply that says nothing else is left alone",
          M._repair_dangling_start("Tāpēc") == "Tāpēc")
    check("LOSS-004 only the first line is repaired",
          M._repair_dangling_start("Tāpēc nevaru zināt\nBet varu pajautāt")
          == "Nevaru zināt\nBet varu pajautāt")

    M._LAST_INCOMING["loss008"] = "Omg, kā ēzelītis"
    both = (f"{OWNER} teica ka tev tas ļoti labi sanāca, un domāju viņš ir "
            "pilnībā taisnībā Saldus sapnīšus")
    kept = M._drop_premature_farewell(both, "loss008")
    check("LOSS-008 the compliment survives the farewell guard",
          "ļoti labi sanāca" in kept)
    check("LOSS-008 ...and the send-off is what got removed",
          "sapnīšus" not in kept)
    check("LOSS-008 a reply that IS only a send-off is still dropped",
          M._drop_premature_farewell("Saldus sapnīšus", "loss008").strip() == "")
    check("LOSS-008 a send-off in front of a question keeps the question",
          "kā tev gāja" in M._drop_premature_farewell(
              "Ar labunakti, bet vispirms pasaki kā tev gāja darbā", "loss008"))

    M.polish_trace_reset()
    check("LOSS-005 an empty reply comes back empty, not as whitespace",
          M._polish_reply("   ", None).strip() == "")
    src = M._polish_reply_inner.__doc__ or ""
    check("LOSS-005 the emptied-reply path is still instrumented",
          "polish emptied the reply" in (M._tr_out.__code__.co_consts.__str__()))

    # ---- LOSS-006: the fact cap asks the ROUTE, not a second classifier -----
    consts = " ".join(str(c) for c in M._polish_reply_inner.__code__.co_consts
                      if isinstance(c, str))
    check("LOSS-006 the length cap reuses the routing fact verdict",
          "wants_fact_text" in consts or "wants_fact" in consts)
    # the routing lane records it with the conversation in hand
    gen_consts = " ".join(str(c) for c in M._generate_reply.__code__.co_consts
                          if isinstance(c, str))
    check("LOSS-006 the route records its verdict for the rest of the turn",
          "wants_fact" in gen_consts)

    all_consts = " ".join(
        str(c) for fn in (M._texting_length, M._lv_sentences,
                          M._repair_dangling_start)
        for c in fn.__code__.co_consts if isinstance(c, str))
    check("LOSS-007 no station and no schedule leaked into the guards",
          "05:58" not in all_consts and "Latvijas Radio" not in all_consts)

    ok = True
    for label, passed in checks:
        ok = ok and passed
        print(("OK   " if passed else "FAIL ") + label)
    print("\n%d checks, VERDICT: %s" % (len(checks), "PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
