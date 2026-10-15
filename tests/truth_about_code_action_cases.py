# -*- coding: utf-8 -*-
"""_truth_about_code_action: the record says what happened, a model reads the reply.

No model runs here: a stand-in judge answers.

    python tests/truth_about_code_action_cases.py
"""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from app import messaging as M  # noqa: E402

PASS, FAIL = [], []
_CODE_TOOLS = ("antons_apply_fix", "antons_propose_fix", "antons_read_code",
               "antons_restart_service")
APPLIED = [{"tool": "antons_apply_fix", "ok": True, "result": {"applied": True}}]
ROLLED = [{"tool": "antons_apply_fix", "ok": True,
           "result": {"applied": False, "reverted": True}}]
CLIPPED = [{"tool": "antons_apply_fix", "ok": True,
            "result": {"truncated": True, "chars": 7930,
                       "preview": '{"applied": true, "reverted": false'}}]
PROPOSED = [{"tool": "antons_propose_fix", "ok": True, "result": {"ok": True}}]
READ = [{"tool": "antons_read_code", "ok": True, "result": {"text": "..."}}]
RESTARTED = [{"tool": "antons_restart_service", "ok": True,
              "result": {"restarting": True}}]


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("    %-66s %s" % (name, "ok" if ok else "FAIL  " + str(detail)[:90]))


def judged(reply, tool_results, verdict, allowed=_CODE_TOOLS, fail=False):
    """Run the check with a stand-in judge that answers `verdict`."""
    asked = []

    def judge(cfg, messages, options=None, format_schema=None, think=False):
        asked.append(messages[-1]["content"])
        if fail:
            raise RuntimeError("ollama down")
        return json.dumps({"applied": "apply" in verdict,
                           "restarted": "restart" in verdict,
                           "investigated": "investigation" in verdict,
                           "proposed": "proposal" in verdict})

    saved = M._ollama_chat
    M._ollama_chat = judge
    del M._CLAIMED_CODE_ACTION_WITHOUT_ACTION[:]
    try:
        out = M._truth_about_code_action(reply, tool_results, allowed, acfg={})
        flagged = set().union(*M._CLAIMED_CODE_ACTION_WITHOUT_ACTION)
    finally:
        M._ollama_chat = saved
        del M._CLAIMED_CODE_ACTION_WITHOUT_ACTION[:]
    return out, flagged, asked


def case_the_judge_decides_what_the_reply_claims():
    print("1. what the reply claims is the judge's reading, not a word list")
    # no word the old patterns knew, still a claim that code landed
    out, flagged, asked = judged("Gatavs, viss jau ir kodā.", [], ["apply"])
    check("an apply the judge finds is replaced", "kodā" not in out, out)
    check("...with the apply text", "antons_apply_fix" in out, out)
    check("...and the caller hears it was the apply", flagged == {"apply"}, flagged)
    check("the judge was asked once", len(asked) == 1, asked)

    # "pielikts" in a sentence about what happens LATER is not a claim
    later = "Priekšlikums ir gatavs. Kad tas būs pielikts, serviss jāpārstartē."
    out, flagged, _ = judged(later, PROPOSED, [])
    check("a clean verdict keeps a reply the old patterns replaced",
          out == later and not flagged, out)


def case_the_record_wins_both_ways():
    print("")
    print("2. the record wins over the judge in both directions")
    out, flagged, _ = judged("Pielikts. Izmaiņas ir uz diska.", APPLIED, ["apply"])
    check("a kind the record shows as done is never unbacked",
          out == "Pielikts. Izmaiņas ir uz diska." and not flagged, (out, flagged))
    out, flagged, _ = judged("Pārstartēts.", RESTARTED, ["restart"])
    check("same for a restart that was requested", not flagged, flagged)
    out, flagged, _ = judged("Pielikts. Viss strādā.", ROLLED, ["apply"])
    check("a rolled-back apply the judge calls a claim is replaced",
          "Viss strādā" not in out and flagged == {"apply"}, (out, flagged))


def case_what_the_record_backs():
    print("")
    print("3. what counts as done comes from the tool record")
    for name, results, kind in (
            ("a clipped apply result still counts as applied", CLIPPED, "apply"),
            ("an apply that landed ran the tests", APPLIED, "investigation"),
            ("an apply that was rolled back ran the tests too", ROLLED, "investigation"),
            ("a code read is an investigation", READ, "investigation"),
            ("a written proposal backs 'proposal ready'", PROPOSED, "proposal")):
        _, flagged, _ = judged("Kaut kas.", results, [kind])
        check(name, not flagged, flagged)
    _, flagged, _ = judged("Kaut kas.", READ, ["proposal"])
    check("a code read does not back 'proposal ready'", flagged == {"proposal"}, flagged)
    # shown the record, qwen listed its "no" lines back (11/20 on the real texts)
    _, _, asked = judged("Labrīt! Kā iet?", [], [])
    check("the judge reads the reply alone, never the record",
          asked and "Labrīt" in asked[0] and "RECORD" not in asked[0], asked)


def case_what_is_left_alone():
    print("")
    print("4. what is never touched")
    text = "Biļete jau ir pielikta klāt, un man ir priekšlikums tev."
    out, flagged, asked = judged(text, [], ["apply"], allowed=("web_search",))
    check("no code tools this turn: no judge call, reply as is",
          out == text and not asked and not flagged, (out, asked))
    out, flagged, _ = judged("Pielikts.", [], ["apply"], fail=True)
    check("the judge failing leaves the reply as written",
          out == "Pielikts." and not flagged, (out, flagged))
    out, flagged, asked = judged("   ", [], ["apply"])
    check("an empty reply is not judged", not asked and not flagged, asked)


def case_what_it_says_instead():
    print("")
    print("5. the replacement names what was missing")
    out, flagged, _ = judged("Izpētīju kodu. Problēma: X.", [], ["investigation"])
    check("an invented investigation gets the read-first text",
          "antons_read_code" in out and flagged == {"investigation"}, out)
    out, flagged, _ = judged("Priekšlikums ir gatavs, un tas ir pielikts.",
                             PROPOSED, ["apply"])
    check("a false apply next to a real proposal gets the apply text",
          "antons_apply_fix" in out and "nebija balstīts" not in out, out)
    restored = [
        {"tool": "antons_restore_page", "ok": True, "risk": "PRIVILEGED",
         "args": {"slug": "tile-mahjong", "version": "box:bak-20260910T120225"}},
        {"tool": "antons_page_versions", "ok": True, "risk": "READ_ONLY",
         "args": {"slug": "tile-mahjong"}},
        {"tool": "antons_page_versions", "ok": True, "executed": False, "reused": True,
         "risk": "PRIVILEGED", "args": {"slug": "tile-mahjong"}},
        {"tool": "antons_publish", "ok": False, "risk": "PRIVILEGED",
         "args": {"slug": "home"}}]
    out, flagged, _ = judged("Pārbaudīju kodu un atjaunoju spēli.", restored,
                             ["investigation"])
    check("a turn that did things is not called 'I did nothing'",
          "neizdarīju" not in out and "antons_restore_page(" in out, out)
    check("...and only what really ran and changed something is named",
          "antons_page_versions" not in out and "antons_publish" not in out, out)
    built = [{"tool": "antons_build", "ok": True, "risk": "PRIVILEGED",
              "args": {"files": "minesweeper"}, "result": {"ok": True, "started": True}}]
    text = "Sāku labot: mīnas tagad tiek novietotas pēc pirmā pieskāriena."
    out, flagged, _ = judged(text, built, ["apply"])
    check("a change the turn made to a page backs 'it is in place'",
          out == text and not flagged, (out, flagged))
    out, flagged, _ = judged("Pielikts.", PROPOSED + [dict(built[0], executed=False,
                                                              reused=True)], ["apply"])
    check("...a repeated build call that did not run backs nothing",
          flagged == {"apply"}, flagged)
    # 10:34: "Atjaunoju ... un palaidu labošanu vēlreiz ... testi 8/8 OK" after
    # a text pass (which runs the browser test itself) read as an investigation
    retexted = [{"tool": "antons_fix_page_text", "ok": True, "risk": "PRIVILEGED",
                 "args": {"slug": "kurmji"}, "result": {"ok": True, "test": "testi: 8/8 OK"}}]
    text = "Atjaunoju versiju un palaidu labošanu vēlreiz. 19 teksti, testi 8/8 OK."
    out, flagged, _ = judged(text, retexted, ["investigation", "apply"])
    check("a page tool's own test backs 'tests pass'", out == text and not flagged,
          (out, flagged))
    _, flagged, _ = judged("Priekšlikums ir gatavs.", retexted, ["proposal"])
    check("...but not a proposal nobody wrote", flagged == {"proposal"}, flagged)


def main():
    print(__doc__.strip().splitlines()[0])
    print("")
    for fn in (case_the_judge_decides_what_the_reply_claims,
               case_the_record_wins_both_ways,
               case_what_the_record_backs,
               case_what_is_left_alone,
               case_what_it_says_instead):
        try:
            fn()
        except Exception as exc:
            check(fn.__name__, False, "%s: %s" % (type(exc).__name__, exc))
    print("")
    print("%d passed, %d failed" % (len(PASS), len(FAIL)))
    for n in FAIL:
        print("   FAILED: %s" % n)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
