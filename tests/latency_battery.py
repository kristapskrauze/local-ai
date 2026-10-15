# -*- coding: utf-8 -*-
"""Where the seconds actually go. Measured, not estimated.

Runs representative messages through the REAL reply pipeline in dry-run mode
(`generate_test_reply` — nothing is sent to anybody) against a scratch chat
guid, then reports from the traces the pipeline itself recorded:

  * end-to-end and stage breakdown per lane
  * how many Qwen calls one answer costs, and what each was for
  * TTFT vs generation, so "20 s before the first token" is never confused
    with "15 s spent generating a long answer"
  * prompt sizes, model reloads, and generations that hit the output ceiling

    py -3 tests/latency_battery.py              # run the battery, then report
    py -3 tests/latency_battery.py --report     # report on existing traces
    py -3 tests/latency_battery.py --live       # report on LIVE chat traces only

Nothing here changes behaviour. It exists so that latency work is decided by
measurement, and so a regression shows up as a number instead of a feeling.
"""
import argparse
import io
import os
import statistics
import sys
import time
from collections import Counter, defaultdict

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import messaging as M     # noqa: E402
from app import telemetry          # noqa: E402

SCRATCH = "iMessage;-;latency-battery-scratch"
OWNER = "owner@example.com"

# (label, message). Chosen to hit a different lane each time; the lane the
# pipeline actually picked is read back from the trace, never assumed.
CASES = [
    ("chat", "Čau, kā iet?"),
    ("chat", "Nu jā, vakar biju pie mammas, bija forši"),
    ("chat", "Ko domā par to jauno telefonu?"),
    ("chat-lv-long", "Pastāsti, kā tev šodien gāja ar to darbu, interesē detaļas"),
    ("tool", "cik grādi gpu?"),
    ("tool", "cik vietas uz diska?"),
    ("stable-fact", "Kad Latvija ieviesa eiro?"),
    ("stable-fact", "Kas bija Rainis?"),
    ("stable-fact", "Kad Latvija iestājās ES?"),
    ("followup", "Pastāsti sīkāk"),
    ("followup", "Kas vēl interesants par to?"),
]


def run_battery(repeat=1):
    print("running %d case(s) through the live pipeline (dry run, no sends)\n"
          % (len(CASES) * repeat))
    for _ in range(repeat):
        for label, text in CASES:
            started = time.time()
            try:
                out = M.generate_test_reply(text=text, sender=OWNER,
                                            chat_guid=SCRATCH)
                reply = str((out or {}).get("reply") or "")
                mode = str((out or {}).get("mode") or "")
            except Exception as exc:
                reply, mode = "", "ERROR %s" % str(exc)[:60]
            print("  %-12s %-42s %6.1fs  mode=%-16s %d chars"
                  % (label, text[:42], time.time() - started, mode[:16], len(reply)))


def _pct(values, p):
    if not values:
        return 0.0
    values = sorted(values)
    idx = min(len(values) - 1, int(round((p / 100.0) * (len(values) - 1))))
    return values[idx]


def _median(values):
    return round(statistics.median(values), 2) if values else 0.0


def lane_of(trace):
    """The lane a trace belongs to: prefer the recorded mode, else the kind."""
    meta = trace.get("meta") or {}
    for key in ("mode", "lane"):
        if meta.get(key):
            return str(meta[key])
    llm = trace.get("llm") or []
    if not llm:
        return "no-llm"
    purposes = {c.get("purpose") for c in llm}
    if "research_reply_body" in purposes or "deep_reply_one" in purposes:
        return "research"
    if trace.get("kind") == "test_reply":
        return "chat(dry)"
    return str(trace.get("kind") or "chat")


def report(traces):
    if not traces:
        print("no traces yet — run the battery, or wait for live messages")
        return
    print("\n=== E2E by lane (%d traces) ===" % len(traces))
    print("| lane | n | median E2E | P90 E2E | median TTFT | median generation "
          "| median wait-before-work | median LLM calls |")
    print("|---|--:|--:|--:|--:|--:|--:|--:|")
    by_lane = defaultdict(list)
    for t in traces:
        by_lane[lane_of(t)].append(t)
    for lane, rows in sorted(by_lane.items(), key=lambda kv: -len(kv[1])):
        e2e = [float(r.get("e2e_seconds") or 0) for r in rows]
        ttft = [float(c.get("ttft_seconds") or 0) for r in rows
                for c in (r.get("llm") or []) if c.get("ttft_seconds")]
        gen = [float(c.get("generation_seconds") or 0) for r in rows
               for c in (r.get("llm") or []) if c.get("generation_seconds")]
        wait = [float(r.get("wait_before_work_seconds") or 0) for r in rows]
        calls = [int(r.get("llm_calls") or 0) for r in rows]
        print("| %s | %d | %.1fs | %.1fs | %.2fs | %.2fs | %.1fs | %d |"
              % (lane, len(rows), _median(e2e), _pct(e2e, 90), _median(ttft),
                 _median(gen), _median(wait), _median(calls) or 0))

    print("\n=== stage breakdown (median seconds, only stages that cost time) ===")
    stage_totals = defaultdict(list)
    for t in traces:
        for name, row in (t.get("stages") or {}).items():
            stage_totals[name].append(float(row.get("seconds") or 0))
    for name, values in sorted(stage_totals.items(),
                               key=lambda kv: -_median(kv[1])):
        if _median(values) < 0.01 and max(values) < 0.5:
            continue
        print("  %-22s median %6.2fs   p90 %6.2fs   n=%d"
              % (name, _median(values), _pct(values, 90), len(values)))

    print("\n=== Qwen calls per answer ===")
    calls = [int(t.get("llm_calls") or 0) for t in traces]
    print("  median %s, p90 %s, max %s"
          % (_median(calls), _pct(calls, 90), max(calls or [0])))
    purposes = Counter()
    purpose_secs = defaultdict(list)
    for t in traces:
        for c in (t.get("llm") or []):
            purposes[c.get("purpose")] += 1
            purpose_secs[c.get("purpose")].append(float(c.get("seconds") or 0))
    print("  what those calls were for:")
    for purpose, n in purposes.most_common(15):
        print("    %-28s %4d calls, median %5.2fs, total %6.1fs"
              % (purpose, n, _median(purpose_secs[purpose]),
                 sum(purpose_secs[purpose])))

    print("\n=== model residency ===")
    reloads = [c for t in traces for c in (t.get("llm") or [])
               if c.get("model_was_resident") is False]
    total_calls = sum(len(t.get("llm") or []) for t in traces)
    print("  calls: %d | reloads: %d (%.1f%%) | reload seconds total: %.1f"
          % (total_calls, len(reloads),
             100.0 * len(reloads) / (total_calls or 1),
             sum(float(c.get("load_seconds") or 0) for c in reloads)))
    print("  resident now: %s" % telemetry.ollama_ps().get("models"))

    print("\n=== prompt size (tokens the model actually evaluated) ===")
    ptoks = [int(c.get("prompt_tokens") or 0) for t in traces
             for c in (t.get("llm") or []) if c.get("prompt_tokens")]
    otoks = [int(c.get("output_tokens") or 0) for t in traces
             for c in (t.get("llm") or []) if c.get("output_tokens")]
    if ptoks:
        print("  input tokens : median %d, p90 %d, max %d"
              % (_median(ptoks), _pct(ptoks, 90), max(ptoks)))
        print("  output tokens: median %d, p90 %d, max %d"
              % (_median(otoks or [0]), _pct(otoks or [0], 90), max(otoks or [0])))

    print("\n=== truncation: generations that hit the output ceiling ===")
    gens = [c for t in traces for c in (t.get("llm") or [])]
    ceiling = [c for c in gens if c.get("hit_output_ceiling")]
    print("  %d/%d generations (%.1f%%) stopped because they ran out of room"
          % (len(ceiling), len(gens), 100.0 * len(ceiling) / (len(gens) or 1)))
    for c in ceiling[:8]:
        print("    %-24s num_predict=%s output=%s reason=%s"
              % (c.get("purpose"), c.get("num_predict"), c.get("output_tokens"),
                 c.get("done_reason")))

    print("\n=== post-processing content loss (raw Qwen vs sent) ===")
    losses = []
    for t in traces:
        meta = t.get("meta") or {}
        raw, final = meta.get("raw_reply_chars"), meta.get("final_reply_chars")
        if raw and final is not None and int(raw) > 0:
            losses.append((int(raw), int(final), t.get("trace_id")))
    if not losses:
        print("  no polished replies in this sample")
    else:
        pcts = [100.0 * (raw - final) / raw for raw, final, _ in losses]
        print("  median removed %.1f%%, p90 %.1f%% (n=%d)"
              % (_median(pcts), _pct(pcts, 90), len(pcts)))
        big = [(raw, final, tid) for (raw, final, tid), pct in zip(losses, pcts)
               if pct > 20]
        print("  %d reply(s) lost >20%% of the generated text" % len(big))
        for raw, final, tid in big[:8]:
            print("    trace %s: %d -> %d chars" % (tid, raw, final))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true", help="skip the battery")
    ap.add_argument("--live", action="store_true",
                    help="only traces from real chats (kind=chat)")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--limit", type=int, default=400)
    args = ap.parse_args()
    if not args.report:
        run_battery(repeat=args.repeat)
    traces = telemetry.read(limit=args.limit)
    if args.live:
        traces = [t for t in traces if t.get("kind") == "chat"]
    report(traces)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
