# -*- coding: utf-8 -*-
"""A step cut off mid-reasoning gets one wider retry, not the same cap again.

Runs the real loop offline through chat_fn; the trace, LLM log and pulse are
swapped out so nothing reaches the live ledgers.

    py -3 tests/thinking_cut_retry_cases.py
"""
import io
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import agent_tools as T  # noqa: E402
from app import qwen_agent as Q  # noqa: E402

PASS, FAIL = [], []
GAVE_UP = "I could not get a usable response from the model."


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("    %-62s %s" % (name, "ok" if ok else "FAIL  " + str(detail)[:110]))


def _registry():
    reg = T.ToolRegistry()

    def fake_read(args, ctx):
        return {"path": args.get("path"), "text": "x" * 200}
    reg.register(T.Tool(
        "antons_read_code", "fake read",
        {"type": "object",
         "properties": {"action": {"type": "string"}, "path": {"type": "string"}},
         "required": ["action"]},
        fake_read, T.READ_ONLY, 1.0))
    return reg


def _cut(payload, thinking="Let me work through _agent_turn_options. " * 200):
    """What Ollama returns when the reasoning uses the whole budget."""
    predict = int((payload.get("options") or {}).get("num_predict") or 0)
    return {"message": {"role": "assistant", "content": "", "thinking": thinking},
            "done_reason": "length", "eval_count": predict}


def _answer(text):
    return {"message": {"role": "assistant", "content": text},
            "done_reason": "stop", "eval_count": 120}


def _run(chat_fn, options, think=True):
    events = []
    saved = (Q._trace, Q._record_llm, T.pulse_set)

    def fake_trace(trace_id, event, *args, **fields):
        events.append(dict(fields, event=event))

    def no_op(*args, **kwargs):
        return None
    Q._trace = fake_trace
    Q._record_llm = no_op
    T.pulse_set = no_op
    try:
        res = Q.run_agent(
            [{"role": "user", "content": "find where the reasoning level is set"}],
            "offline-qwen", registry=_registry(),
            ctx=T.AgentContext(actor="test", allow_private=True),
            chat_fn=chat_fn, max_tool_steps=4, max_model_calls=6,
            options=options, think=think)
    finally:
        Q._trace, Q._record_llm, T.pulse_set = saved
    return res, events


def _budgets(seen):
    return [int((p.get("options") or {}).get("num_predict") or 0) for p in seen]


def case_cut_reasoning_gets_one_wider_retry():
    print("1. a step cut off mid-reasoning is retried once with more room")
    seen = []

    def chat_fn(payload):
        seen.append(payload)
        return _cut(payload) if len(seen) == 1 else _answer("Atradu: rinda 17115.")
    res, events = _run(chat_fn, {"temperature": 0, "num_ctx": 32768,
                                 "num_predict": 3500})
    check("answered from the retry", res.answer == "Atradu: rinda 17115.", res.answer)
    check("two model calls", len(seen) == 2, len(seen))
    check("the retry went out with 8192 instead of 3500",
          _budgets(seen) == [3500, 8192], _budgets(seen))
    retry = [e for e in events if e.get("reason") == "thinking_cut_retry"]
    check("the trace names the retry for what it is", len(retry) == 1, retry)
    if retry:
        check("with both budgets in it",
              retry[0].get("previous_num_predict") == 3500
              and retry[0].get("retry_num_predict") == 8192, retry[0])


def case_widens_only_once():
    print("")
    print("2. a turn widens at most once, then gives up as before")
    seen = []

    def chat_fn(payload):
        seen.append(payload)
        return _cut(payload)
    res, _events = _run(chat_fn, {"temperature": 0, "num_ctx": 32768,
                                  "num_predict": 3500})
    check("gave up with the usual message", res.answer.startswith(GAVE_UP), res.answer)
    check("no third call", len(seen) == 2, len(seen))
    check("never above 8192", max(_budgets(seen)) == 8192, _budgets(seen))


def case_no_thinking_keeps_the_old_retry():
    print("")
    print("3. with thinking off an empty cut keeps the old retry and budget")
    seen = []

    def chat_fn(payload):
        seen.append(payload)
        if len(seen) == 1:
            return _cut(payload, thinking="")
        return _answer("ok")
    res, events = _run(chat_fn, {"temperature": 0, "num_ctx": 32768,
                                 "num_predict": 900}, think=False)
    check("answered", res.answer == "ok", res.answer)
    check("the budget did not change", _budgets(seen) == [900, 900], _budgets(seen))
    check("no thinking retry was traced",
          not any(e.get("reason") == "thinking_cut_retry" for e in events))


def case_cut_without_reasoning_is_not_widened():
    print("")
    print("4. an empty cut with no reasoning in it is not widened")
    seen = []

    def chat_fn(payload):
        seen.append(payload)
        if len(seen) == 1:
            return _cut(payload, thinking="")
        return _answer("ok")
    res, _events = _run(chat_fn, {"temperature": 0, "num_ctx": 32768,
                                  "num_predict": 3500})
    check("answered", res.answer == "ok", res.answer)
    check("the budget did not change", _budgets(seen) == [3500, 3500], _budgets(seen))


def case_retry_budget_bounds():
    print("")
    print("5. the wider budget: never above half the window, never smaller")
    raw = _cut({"options": {"num_predict": 3500}})
    check("32k window: 8192",
          Q._thinking_retry_predict(raw, {"num_predict": 3500, "num_ctx": 32768}) == 8192)
    check("8k window: half of it",
          Q._thinking_retry_predict(raw, {"num_predict": 3500, "num_ctx": 8192}) == 4096)
    check("no window known: 8192",
          Q._thinking_retry_predict(raw, {"num_predict": 3500}) == 8192)
    check("a window too small to widen into: no retry",
          Q._thinking_retry_predict(raw, {"num_predict": 3500, "num_ctx": 4096}) == 0)
    at_ceiling = _cut({"options": {"num_predict": 8192}})
    check("already at the ceiling: no retry",
          Q._thinking_retry_predict(at_ceiling,
                                    {"num_predict": 8192, "num_ctx": 32768}) == 0)
    stopped = _answer("")
    stopped["message"]["thinking"] = "done thinking"
    check("a normal stop is not a cut",
          Q._thinking_retry_predict(stopped, {"num_predict": 3500}) == 0)
    talking = _cut({"options": {"num_predict": 3500}})
    talking["message"]["content"] = "Atradu"
    check("a cut that already has content is the answer-cut case, not this",
          Q._thinking_retry_predict(talking, {"num_predict": 3500}) == 0)


def case_widened_budget_makes_room():
    print("")
    print("6. the wider budget is reserved, so the history gives up the room")
    msgs = [{"role": "system", "content": "s" * 2000},
            {"role": "user", "content": "fix it"},
            {"role": "tool", "name": "antons_read_code", "content": "a" * 20000},
            {"role": "tool", "name": "antons_read_code", "content": "b" * 20000},
            {"role": "tool", "name": "antons_read_code", "content": "c" * 18000}]
    state = {"prompt_tokens": 26000, "prompt_chars": 60000}
    _same, info = Q._fit_to_window(
        {"messages": msgs, "tools": [{}],
         "options": {"num_ctx": 32768, "num_predict": 3500}}, state)
    check("at 3500 the request fits as it is", info is None, info)
    fitted, info = Q._fit_to_window(
        {"messages": msgs, "tools": [{}],
         "options": {"num_ctx": 32768, "num_predict": 8192}}, state)
    check("at 8192 it is trimmed to make room", info is not None, info)
    if info:
        check("down to the budget",
              Q._content_chars(fitted["messages"]) <= info["budget_chars"], info)
        check("and the user turn survives", fitted["messages"][1] == msgs[1])


def main():
    print(__doc__.strip().splitlines()[0])
    print("")
    for fn in (case_cut_reasoning_gets_one_wider_retry,
               case_widens_only_once,
               case_no_thinking_keeps_the_old_retry,
               case_cut_without_reasoning_is_not_widened,
               case_retry_budget_bounds,
               case_widened_budget_makes_room):
        try:
            fn()
        except Exception as exc:
            check(fn.__name__, False, "%s: %s" % (type(exc).__name__, exc))
    print("")
    print("%d passed, %d failed" % (len(PASS), len(FAIL)))
    for name in FAIL:
        print("   FAILED: %s" % name)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
