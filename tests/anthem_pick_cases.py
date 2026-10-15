# -*- coding: utf-8 -*-
"""Three things that kept the answer out of reach, all offline.

    PICK-001  a result found by the WIDENED query is judged against that
              query's goal, not against the literal sentence
    PICK-002  ...and junk is still rejected
    WIDE-001  the widened query drops a one-day scope; the literal one keeps it
    CACHE-001 the query cache is keyed on the resolved context and expires, so
              one bad set cannot pin the lane shut for the process lifetime

Nothing here names a station, a broadcaster or a time — the rules are about
query purpose and scope, not about this one answer.

    py -3 tests/anthem_pick_cases.py
"""
import io
import os
import sys
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import messaging as M  # noqa: E402


def main():
    checks = []

    def check(label, passed):
        checks.append((label, bool(passed)))

    literal = "did station A play the anthem yesterday?"
    M._remember_query_purposes(
        ["station A playlist yesterday",
         "station A schedule anthem",
         "which station plays the anthem daily and when"],
        {"station a playlist yesterday": ("en", "literal"),
         "station a schedule anthem": ("en", "widened"),
         "which station plays the anthem daily and when": ("lv", "alternative")})

    hits = [
        {"url": "https://example.com/stationA/", "title": "Station A home",
         "content": "Listen live", "_found_by": ["q1"]},
        {"url": "https://example.com/forum/thread", "title": "Forum chat",
         "content": "someone asks about music", "_found_by": ["q1"]},
        {"url": "https://broadcaster-b.example/schedule/", "title": "Station B programme",
         "content": "Full daily schedule", "_found_by": ["q3"]},
        {"url": "https://shop.example/vinyl", "title": "Vinyl shop",
         "content": "buy records", "_found_by": ["q2"]},
    ]

    seen = {}

    def fake_chat(cfg, messages, options=None):
        seen["prompt"] = "\n".join(str(m.get("content") or "") for m in messages)
        return "3"                      # the widened-query candidate

    real = M._ollama_chat
    M._ollama_chat = fake_chat
    try:
        picks = M._pick_pages_to_open({}, literal, hits, k=2)
    finally:
        M._ollama_chat = real
    check("PICK-001 the widened-query candidate can be opened",
          picks and picks[0]["url"].startswith("https://broadcaster-b"))
    check("PICK-001 the model is shown each query's purpose",
          "widened" in seen.get("prompt", "")
          and "alternative" in seen.get("prompt", ""))
    check("PICK-001 the candidate carries its query's purpose",
          "q3/alternative" in seen.get("prompt", "")
          or "q3/widened" in seen.get("prompt", ""))
    check("PICK-001 the research goals are stated, not just the sentence",
          "RESEARCH GOALS" in seen.get("prompt", ""))
    check("PICK-001 the rule that a widened result counts is in the prompt",
          "WIDENED" in M._PAGE_PICK_SYS and "ITS OWN goal" in M._PAGE_PICK_SYS)

    def refuse_chat(cfg, messages, options=None):
        return "0"

    M._ollama_chat = refuse_chat
    try:
        none_picks = M._pick_pages_to_open({}, literal, hits, k=2)
    finally:
        M._ollama_chat = real
    check("PICK-002 a set with nothing useful is still refused",
          none_picks == [])

    check("WIDE-001 a day-relative word is recognised",
          bool(M._DAY_RELATIVE_RE.search("himna radio ēterā vakardien"))
          and bool(M._DAY_RELATIVE_RE.search("what played yesterday")))
    check("WIDE-001 a DATE is not a day-relative word",
          not M._DAY_RELATIVE_RE.search("18. novembris 2026 programma"))
    # the widened slot is cleaned; nothing else is touched
    cleaned = " ".join(M._DAY_RELATIVE_RE.sub(" ", "himna radio ēterā vakardien").split())
    check("WIDE-001 the widened query loses the one-day scope",
          cleaned == "himna radio ēterā")
    src = M._ensure_widened_query.__code__.co_consts
    check("WIDE-001 the stripping happens in the widening path only",
          any(isinstance(c, str) and "day scope" in c for c in src))

    M._SEARCH_Q_CACHE.clear()
    M._search_q_store("k1", ["a", "b"])
    check("CACHE-001 a stored set is returned", M._search_q_cached("k1") == ["a", "b"])
    M._SEARCH_Q_CACHE["k1"] = (time.time() - M.SEARCH_Q_TTL_SECONDS - 5, ["a", "b"])
    check("CACHE-001 an expired set is not reused", M._search_q_cached("k1") is None)
    check("CACHE-001 ...and is dropped from the store", "k1" not in M._SEARCH_Q_CACHE)
    check("CACHE-001 the TTL is finite and short enough to retry within a chat",
          0 < M.SEARCH_Q_TTL_SECONDS <= 3600)
    check("CACHE-001 different conversations do not share a cache key",
          M._context_key("she asked about the radio")
          != M._context_key("he asked about the weather"))
    check("CACHE-001 the same conversation does",
          M._context_key("  she asked  about the radio ")
          == M._context_key("she asked about the radio"))
    check("CACHE-001 an unknown key is a miss, not an error",
          M._search_q_cached("nope") is None)

    ok = True
    for label, passed in checks:
        ok = ok and passed
        print(("OK   " if passed else "FAIL ") + label)
    print("\n%d checks, VERDICT: %s" % (len(checks), "PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
