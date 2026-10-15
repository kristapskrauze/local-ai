# -*- coding: utf-8 -*-
"""antons_read_code(action=read): a near-duplicate line range must not be free.

TurnToolLedger._read_range_duplicate closes the gap: a new [start,end] that a
prior read on the same path already covers 50%+ of counts as a semantic
duplicate, which the EXISTING should_force_final() machinery already turns
into an early, real answer instead of silent budget exhaustion.

    py -3 tests/read_range_duplicate_cases.py
"""
import io
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import agent_tools as T  # noqa: E402
from app import qwen_agent as Q  # noqa: E402

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("    %-56s %s" % (name, "ok" if ok else "FAIL  " + str(detail)[:100]))


def _read(path, start, end):
    return {"action": "read", "path": path, "start": start, "end": end}


def case_full_subset_is_a_duplicate():
    print("1. a range fully inside an earlier read is a duplicate")
    ledger = Q.TurnToolLedger()
    ledger.record_result("antons_read_code", _read("a.js", 220, 230),
                         {"ok": True, "result": "x"})
    dup = ledger.find_duplicate("antons_read_code", _read("a.js", 224, 226))
    check("caught as a duplicate", dup is not None, dup)
    if dup:
        check("labelled semantic, not exact", dup[1] == "semantic", dup)


def case_different_file_is_not_a_duplicate():
    print("")
    print("2. the SAME line range in a DIFFERENT file is not a duplicate")
    ledger = Q.TurnToolLedger()
    ledger.record_result("antons_read_code", _read("a.js", 220, 230),
                         {"ok": True, "result": "x"})
    dup = ledger.find_duplicate("antons_read_code", _read("b.js", 220, 230))
    check("not flagged", dup is None, dup)


def case_a_genuinely_new_region_is_not_a_duplicate():
    print("")
    print("3. reading somewhere new in the same file is not penalised")
    ledger = Q.TurnToolLedger()
    ledger.record_result("antons_read_code", _read("a.js", 1, 50),
                         {"ok": True, "result": "x"})
    dup = ledger.find_duplicate("antons_read_code", _read("a.js", 900, 950))
    check("not flagged", dup is None, dup)


def case_stuck_loop_forces_final_well_before_call_38():
    print("")
    print("4. the exact failing sequence trips should_force_final early")
    seq = [
        ("app/static/app.js", 220, 230),
        ("tests/code_intel_cases.py", 185, 210),
        ("app/static/app.js", 220, 230),
        ("tests/code_intel_cases.py", 1, 30),
        ("tests/code_intel_cases.py", 210, 240),
        ("app/static/app.js", 222, 228),
        ("tests/code_intel_cases.py", 196, 223),
        ("app/static/app.js", 223, 227),
    ]
    ledger = Q.TurnToolLedger()
    forced_at = None
    for i, (path, start, end) in enumerate(seq, 1):
        args = _read(path, start, end)
        dup = ledger.find_duplicate("antons_read_code", args)
        if dup:
            ledger.reuse_result("antons_read_code", args, dup)
        else:
            ledger.record_result("antons_read_code", args, {"ok": True, "result": "x"})
        if forced_at is None and ledger.should_force_final():
            forced_at = i
    check("forces final well before the 38 calls actually taken",
          forced_at is not None and forced_at <= 10, forced_at)


def response_tool(name, args, call_id="call_00"):
    return {"message": {"role": "assistant", "content": "",
                        "tool_calls": [{"id": call_id,
                                       "function": {"name": name, "arguments": args}}]}}


def response_text(text):
    return {"message": {"role": "assistant", "content": text}}


def scripted(*responses):
    box = {"i": 0}

    def chat_fn(payload):
        i = box["i"]
        box["i"] += 1
        item = responses[i] if i < len(responses) else response_text("script exhausted")
        return item(payload) if callable(item) else item
    chat_fn.box = box
    return chat_fn


def case_end_to_end_produces_a_real_answer_not_silence():
    print("")
    print("5. driven through the real loop, it answers instead of going silent")
    reg = T.ToolRegistry()

    def fake_read(args, ctx):
        return {"path": args.get("path"), "start": args.get("start"),
                "end": args.get("end"), "lines": "..."}
    reg.register(T.Tool(
        "antons_read_code", "fake read",
        {"type": "object",
         "properties": {"action": {"type": "string"}, "path": {"type": "string"},
                        "start": {"type": "integer"}, "end": {"type": "integer"}},
         "required": ["action"]},
        fake_read, T.READ_ONLY, 1.0))

    # the SAME oscillating pattern that actually starved the context window,
    # scripted as if the model just keeps proposing the next overlapping read
    steps = [
        response_tool("antons_read_code", _read("app/static/app.js", 220, 230)),
        response_tool("antons_read_code", _read("tests/code_intel_cases.py", 185, 210)),
        response_tool("antons_read_code", _read("app/static/app.js", 222, 228)),
        response_tool("antons_read_code", _read("tests/code_intel_cases.py", 196, 223)),
        response_tool("antons_read_code", _read("app/static/app.js", 223, 227)),
        response_tool("antons_read_code", _read("tests/code_intel_cases.py", 200, 217)),
        # by here should_force_final() is already True; whatever the model is
        # offered next has no tools attached (flattened, per force_final), so
        # this is the answer it can actually give
        response_text("Found it: app/static/app.js, lines 220-230."),
    ]
    fn = scripted(*steps)
    res = Q.run_agent(
        [{"role": "user", "content": "fix the failing test"}],
        "offline-qwen", registry=reg,
        ctx=T.AgentContext(actor="test", allow_private=True),
        chat_fn=fn, max_tool_steps=64, max_model_calls=48,
        options={"temperature": 0, "num_ctx": 32768})
    check("did not exhaust the whole 48-call budget",
          res.model_calls < 20, res.model_calls)
    check("produced a real answer, not the silent-failure fallback",
          bool(res.answer) and "could not get a usable response" not in res.answer,
          res.answer)


def main():
    print(__doc__.strip().splitlines()[0])
    print("")
    for fn in (case_full_subset_is_a_duplicate,
               case_different_file_is_not_a_duplicate,
               case_a_genuinely_new_region_is_not_a_duplicate,
               case_stuck_loop_forces_final_well_before_call_38,
               case_end_to_end_produces_a_real_answer_not_silence):
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
