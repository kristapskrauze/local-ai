# -*- coding: utf-8 -*-
"""Useful evidence found by a WIDENING query must survive to the model.

  * the pool is in QUERY order, so `good[:limit]` and the picker's listing were
    filled entirely by query 1 — the early-break bug one layer later;
  * a duplicate URL found by two queries lost the fact that two queries found
    it, which is a relevance signal.

These tests pin the MECHANISM on fixtures. They never touch the network, and
they deliberately do not assert the anthem fact itself — teaching the router
"05:58" would be the bug this whole exercise exists to avoid.

    py -3 tests/search_rank_cases.py
"""
import io
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import messaging as M  # noqa: E402


def hit(title, url, content, found_by):
    return {"title": title, "url": url, "content": content, "_found_by": list(found_by)}


# what the three queries actually returned for the anthem case, in shape
MIXFM = [
    hit("Mix FM 102.7 playlist - OnlineRadioBox", "https://onlineradiobox.com/lv/mix1027/playlist/",
        "Mix FM 102.7 playlist ; 16:52 LOUD LUXURY - Love You For Life", ["q1"]),
    hit("Mix FM 102,7 Riga: Listen Live", "https://pcradio.app/radio/mix-fm-1027-riga",
        "Listen to Mix FM 102,7 online for free. Broadcast from Riga, Latvia.", ["q1"]),
    hit("Played tracks - Radio Mix FM", "https://mixfm.lv/played-tracks",
        "Believer IMAGINE DRAGONS ; Here We Go Again Oliver Tree", ["q1"]),
    hit("Programmas - Radio Mix FM", "https://mixfm.lv/programs",
        "APPLE MUSIC DANCE TOP 20 SPOTIFY DANCE TOP 20", ["q1"]),
    hit("Mix FM 102.7 - RadioLy", "https://radioly.com/mix-fm-1027",
        "Listen to Mix FM 102.7 live online from Riga, Latvia.", ["q1"]),
]
LSM = hit("Raidījumu arhīvs - Latvijas Radio 1 - LSM", "https://lr1.lsm.lv/lv/lr/arhivs/",
          "05:56 LR1 frekvenču pieteikums ; 05:58 Latvijas himna ; 06:00 Ziņas", ["q3"])
JUNK = hit("Student Handbook for University of Latvia", "https://www.lu.lv/en/studies/",
           "broadcast on the national radio station 95.8 FM", ["q2"])


def main():
    checks = []

    def check(label, passed):
        checks.append((label, bool(passed)))

    # ---- SEARCH-RANK-001: five redundant literal hits vs one authoritative ----
    pool = MIXFM + [JUNK, LSM]
    ordered = M._interleave_by_query(pool)
    idx = [h["url"] for h in ordered].index(LSM["url"])
    check("SEARCH-RANK-001 the widened-query result survives the top-5 cut",
          idx < 5)
    check("SEARCH-RANK-001 ...and is visible to the page picker (first 9)",
          idx < 9)
    check("SEARCH-RANK-001 the literal query still leads",
          ordered[0]["_found_by"] == ["q1"])
    check("SEARCH-RANK-001 order WITHIN a query is preserved",
          [h["url"] for h in ordered if h["_found_by"] == ["q1"]]
          == [h["url"] for h in MIXFM])

    terms = ["latvijas", "radio", "himna", "programma"]
    check("SEARCH-RANK-002 junk from a widening query still fails _on_topic",
          not M._on_topic(JUNK, terms))
    check("SEARCH-RANK-002 the real LSM hit passes _on_topic",
          M._on_topic(LSM, terms))

    both = hit("Latvijas Radio 1 programma", "https://lr1.lsm.lv/lv/lr1/programma/",
               "Latvijas himna 05:58 ; Ziņas 06:00", ["q2", "q3"])
    ordered = M._interleave_by_query([both] + MIXFM)
    check("SEARCH-RANK-003 a URL found by two queries appears once",
          sum(1 for h in ordered if h["url"] == both["url"]) == 1)
    check("SEARCH-RANK-003 ...and keeps both query ids",
          set(both["_found_by"]) == {"q2", "q3"})

    # ---- SEARCH-RANK-004: unopenable/unreadable page -> next candidate ------
    widget = ("titre=LEONY - Remedy&vote=up&cover=https://cover.radioking.io/83de"
              "&titre=DUA LIPA&vote=up&cover=https://cover.radioking.io/aa1")
    check("SEARCH-RANK-004 a widget dump is not accepted as material",
          not M._readable_prose(widget))
    check("SEARCH-RANK-004 a real schedule page IS accepted",
          M._readable_prose(
              "Latvijas Radio 1 programma tuvākajām dienām. Uzzini, kādi "
              "raidījumi plānoti ēterā un kurš tos vadīs. Rīta ēterā skan "
              "himna, pēc tās ziņas. Programma tiek atjaunota katru dienu."))

    # ---- SEARCH-RANK-005: a Latvian source must not be rejected for language -
    check("SEARCH-RANK-005 a Latvian query is no longer filtered out of the "
          "query set", _latvian_query_survives())
    check("SEARCH-RANK-005 a Latvian-language result passes the keep filter",
          M._on_topic(LSM, ["latvijas", "himna", "radio"]))

    from app import telemetry
    real = telemetry.current
    captured = {}

    class FakeTrace(object):
        meta = captured

    telemetry.current = lambda: FakeTrace()
    try:
        M._research_note("https://lr1.lsm.lv/x", "POOLED", "q3", LSM)
        M._research_note("https://mixfm.lv/x", "DUPLICATE", "q1", MIXFM[0])
    finally:
        telemetry.current = real
    rows = captured.get("research_candidates") or []
    check("every candidate records a state and the query that found it",
          len(rows) == 2 and rows[0]["state"] == "POOLED"
          and rows[0]["found_by"] == "q3" and rows[1]["state"] == "DUPLICATE")

    ok = True
    for label, passed in checks:
        ok = ok and passed
        print(("OK   " if passed else "FAIL ") + label)
    print("\n%d checks, VERDICT: %s" % (len(checks), "PASS" if ok else "FAIL"))
    return 0 if ok else 1


def _latvian_query_survives():
    src = M._search_queries_for.__code__.co_consts
    flat = " ".join(str(c) for c in src if isinstance(c, str))
    return "āčēģīķļņšūž" not in flat


if __name__ == "__main__":
    raise SystemExit(main())
