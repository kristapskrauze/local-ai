# -*- coding: utf-8 -*-
"""A query about the SUBJECT finds pages about the subject.

    QUERY-ARTIFACT-001  a "when is it normally on?" question plans a
                        schedule/programme-shaped query alongside the literal
                        and widened ones
    QUERY-ARTIFACT-002  a "what was played?" question plans a playlist-shaped
                        one instead — the rule is about which document holds
                        the answer, not about always adding "schedule"

The contract checks are offline and always run. The two behaviour checks call
the real query writer once each (small, temperature 0.2) and SKIP when Ollama
is not reachable, so this stays runnable off the rig.

    py -3 tests/query_artifact_cases.py
"""
import io
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import messaging as M  # noqa: E402

SCHEDULE_WORDS = ("programm", "saraksts", "grafik", "schedule", "timetable",
                  "listing", "kalendār", "calendar", "afiša", "repertuār")
PLAYLIST_WORDS = ("playlist", "atskaņot", "atskaņo", "skanēj", "spēlēj",
                  "played", "history", "vēsture", "ieraksti", "tracks",
                  "dziesm", "songs")


def _writer_prompt():
    return " ".join(str(c) for c in M._search_queries_for.__code__.co_consts
                    if isinstance(c, str))


def _ollama_up():
    try:
        import urllib.request
        urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=3).read(1)
        return True
    except Exception:
        return False


def main():
    checks = []

    def check(label, passed):
        checks.append((label, bool(passed)))

    prompt = _writer_prompt()

    check("CONTRACT the writer is asked for a source_artifact purpose",
          "source_artifact" in prompt)
    check("CONTRACT it explains the artifact is the DOCUMENT holding the answer",
          "KIND OF DOCUMENT" in prompt or "kind of document" in prompt.lower())
    check("CONTRACT the document type is chosen from the question, not fixed",
          "Pick the document type from the question" in prompt)
    check("CONTRACT several document types are named, not just schedules",
          all(w in prompt.lower() for w in ("schedule", "playlist", "timetable",
                                            "calendar")))
    # The prompt may quote a real name as a LANGUAGE example (it predates this
    # rule); what must never be in it is the answer or the source we hope to
    # find, or the writer would be reciting rather than searching.
    check("CONTRACT the answer and its source are not baked into the prompt",
          not any(w in prompt.lower() for w in ("05:58", "lr1", "lsm.lv",
                                                "mix fm", "programma himna")))

    # the widened query must not eat the artifact query's slot
    def _fake_wide(cfg, messages, options=None):
        return "kura stacija to raida katru dienu"
    _real = M._ollama_chat
    M._ollama_chat = _fake_wide
    try:
        # every query still names the subject, so widening genuinely fires;
        # when one of them already looks past the name it declines, which is
        # its own correct behaviour and not what this case is about
        plan = ["Station A X vakar", "Station A X laiks",
                "Station A programma saraksts"]
        widened = M._ensure_widened_query({}, plan, "Vai Station A vakar raidīja X?")
    finally:
        M._ollama_chat = _real
    check("CONTRACT widening does not overwrite the artifact query",
          "Station A programma saraksts" in widened)
    check("CONTRACT ...and the widened query is still added",
          any("katru dienu" in q for q in widened))
    check("CONTRACT the set stays bounded", len(widened) <= 4)

    if not _ollama_up():
        check("QUERY-ARTIFACT-001 skipped (ollama unreachable)", True)
        check("QUERY-ARTIFACT-002 skipped (ollama unreachable)", True)
    else:
        acfg = dict(M.load_config().get("auto_reply") or {})
        M._SEARCH_Q_CACHE.clear()
        q1 = M._search_queries_for(
            acfg, "Vai stacija A vakar raidīja X, un ja nē, kad X parasti skan?")
        blob1 = " ".join(q1).lower()
        check("QUERY-ARTIFACT-001 the plan has several distinct queries",
              len(set(q1)) >= 3)
        check("QUERY-ARTIFACT-001 one of them asks for a schedule-shaped document",
              any(w in blob1 for w in SCHEDULE_WORDS))
        print("      plan-001: %s" % " | ".join(q1))

        M._SEARCH_Q_CACHE.clear()
        q2 = M._search_queries_for(
            acfg, "Kādas dziesmas stacija A atskaņoja vakar?")
        blob2 = " ".join(q2).lower()
        check("QUERY-ARTIFACT-002 the plan has several distinct queries",
              len(set(q2)) >= 3)
        check("QUERY-ARTIFACT-002 one of them asks for a play-history document",
              any(w in blob2 for w in PLAYLIST_WORDS))
        print("      plan-002: %s" % " | ".join(q2))

    plan = ["Station A X vakar", "Station A ētera saraksts vakardien",
            "Station A broadcast history X", "kura stacija X raida katru dienu"]
    declared = {"station a x vakar": ("lv", "literal"),
                "station a ētera saraksts vakardien": ("lv", "source_artifact"),
                "station a broadcast history x": ("en", "widened")}
    out = M._ensure_widened_artifact_query(
        plan, declared, "Vai Station A vakar raidīja X?")
    added = [q for q in out if q not in plan]
    combined = added[0] if added else ""
    check("QUERY-WIDENED-ARTIFACT-001 a widened_artifact query is added",
          bool(combined))
    check("QUERY-WIDENED-ARTIFACT-001 it carries the artifact document type",
          any(w in combined.lower() for w in SCHEDULE_WORDS))
    check("QUERY-WIDENED-ARTIFACT-001 it drops the dead literal subject",
          "station a" not in combined.lower())
    check("QUERY-WIDENED-ARTIFACT-001 it keeps the widened scope",
          "katru dienu" in combined.lower() or "stacija" in combined.lower())
    check("QUERY-WIDENED-ARTIFACT-001 it does not inherit the one-day scope",
          not M._DAY_RELATIVE_RE.search(combined))
    check("QUERY-WIDENED-ARTIFACT-001 the plan stays bounded", len(out) <= 5)

    plan2 = ["station A playlist yesterday", "station A atskaņotās dziesmas"]
    declared2 = {"station a playlist yesterday": ("en", "literal"),
                 "station a atskaņotās dziesmas": ("lv", "source_artifact")}
    out2 = M._ensure_widened_artifact_query(
        plan2, declared2, "Kādas dziesmas station A atskaņoja vakar?")
    check("QUERY-WIDENED-ARTIFACT-002 no widened scope, no invented query",
          out2 == plan2)
    check("QUERY-WIDENED-ARTIFACT-002 the artifact query is still the playlist",
          any(w in " ".join(out2).lower() for w in PLAYLIST_WORDS))
    # ...and the writer already satisfying the invariant is left alone
    plan3 = ["A vakar", "kura stacija to raida", "stacijas programma saraksts"]
    declared3 = {"a vakar": ("lv", "literal"),
                 "kura stacija to raida": ("lv", "widened"),
                 "stacijas programma saraksts": ("lv", "widened_artifact")}
    check("QUERY-WIDENED-ARTIFACT-002 a satisfied plan is not extended",
          M._ensure_widened_artifact_query(plan3, declared3, "Vai A vakar bija?")
          == plan3)

    ok = True
    for label, passed in checks:
        ok = ok and passed
        print(("OK   " if passed else "FAIL ") + label)
    print("\n%d checks, VERDICT: %s" % (len(checks), "PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
