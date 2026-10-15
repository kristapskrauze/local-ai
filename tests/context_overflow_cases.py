# -*- coding: utf-8 -*-
"""A coding turn that outgrows the context window must shrink, not fall over.

    py -3 tests/context_overflow_cases.py
"""
import io
import json
import os
import sys
import urllib.error

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import agent_tools as T  # noqa: E402
from app import qwen_agent as Q  # noqa: E402

PASS, FAIL = [], []
OVERFLOW = "no user query found in messages"


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("    %-62s %s" % (name, "ok" if ok else "FAIL  " + str(detail)[:110]))


def http_error(message, body=None):
    fp = io.BytesIO(body) if body is not None else None
    return urllib.error.HTTPError("http://127.0.0.1:11434/api/chat", 500,
                                  message, {}, fp)  # type: ignore[arg-type]


def case_overflow_is_not_the_parser():
    print("1. an overflow 500 is named for what it is")
    exc = http_error(OVERFLOW)
    check("recognised as context overflow", Q._looks_like_context_overflow(exc))
    check("not taken for the tool-call parser crash",
          not Q._looks_like_toolcall_parser_crash(exc))
    check("the real parser crash is still recognised",
          Q._looks_like_toolcall_parser_crash(http_error("EOF")))
    check("an unexplained 500 keeps the old handling",
          Q._looks_like_toolcall_parser_crash(http_error("Internal Server Error")))


def case_ollama_reason_is_surfaced():
    print("")
    print("2. _ollama_chat raises with the reason Ollama gave")
    real = Q.urllib.request.urlopen

    def fake_urlopen(req, timeout=None):
        raise http_error("Internal Server Error",
                         json.dumps({"error": OVERFLOW}).encode("utf-8"))
    Q.urllib.request.urlopen = fake_urlopen
    try:
        Q._ollama_chat("http://127.0.0.1:11434", {"model": "x", "messages": []}, 5)
        check("raises", False, "no exception")
    except urllib.error.HTTPError as exc:
        check("still an HTTPError with code 500", exc.code == 500, exc.code)
        check("its message carries Ollama's reason", OVERFLOW in str(exc), str(exc))
        check("so it is recognised as overflow", Q._looks_like_context_overflow(exc))
    finally:
        Q.urllib.request.urlopen = real


def case_aggressive_compaction_fits():
    print("")
    print("3. aggressive compaction reaches the budget and spares the user")
    msgs = [{"role": "system", "content": "s" * 1000},
            {"role": "user", "content": "fix the overflow please"},
            {"role": "tool", "name": "antons_read_code", "content": "a" * 12000},
            {"role": "tool", "name": "antons_investigate", "content": "b" * 12000},
            {"role": "tool", "name": "antons_read_code", "content": "c" * 12000}]
    gentle = Q._compact_history(msgs, budget=10000)
    check("the default still keeps one result per tool whole",
          any(m["content"] == "b" * 12000 for m in gentle))
    out = Q._compact_history(msgs, budget=10000, aggressive=True)
    check("aggressive fits the budget", Q._content_chars(out) <= 10000,
          Q._content_chars(out))
    check("the user turn is untouched", out[1] == msgs[1])
    check("the system prompt is untouched", out[0] == msgs[0])
    check("the newest result is cut, not dropped",
          out[4]["content"].startswith("c") and "cut to fit" in out[4]["content"],
          out[4]["content"][-80:])


def case_fit_to_window_uses_the_reported_count():
    print("")
    print("4. the next request is sized from the prompt count the model reported")
    msgs = [{"role": "system", "content": "s" * 2000},
            {"role": "user", "content": "fix the overflow please"},
            {"role": "tool", "name": "antons_read_code", "content": "a" * 20000},
            {"role": "tool", "name": "antons_read_code", "content": "b" * 20000}]
    payload = {"messages": msgs, "tools": [{}],
               "options": {"num_ctx": 16000, "num_predict": 500}}
    same, info = Q._fit_to_window(payload, {})
    check("with no count to judge by, nothing changes",
          same is payload and info is None, info)
    fitted, info = Q._fit_to_window(payload, {"prompt_tokens": 15000,
                                              "prompt_chars": 22000})
    check("an over-budget request is trimmed",
          info is not None
          and Q._content_chars(fitted["messages"]) < Q._content_chars(msgs), info)
    if info:
        check("down to the budget it worked out",
              Q._content_chars(fitted["messages"]) <= info["budget_chars"],
              (Q._content_chars(fitted["messages"]), info))
    check("the user turn survives", fitted["messages"][1] == msgs[1])
    check("the caller's payload is not mutated",
          Q._content_chars(payload["messages"]) == Q._content_chars(msgs))
    _roomy, info = Q._fit_to_window(
        {"messages": msgs[:3], "options": payload["options"]},
        {"prompt_tokens": 3000, "prompt_chars": 22000})
    check("a request with room left goes out as it is", info is None, info)


def case_shrink_for_retry():
    print("")
    print("5. after an overflow refusal the retry is smaller, or not attempted")
    msgs = [{"role": "system", "content": "s" * 1000},
            {"role": "user", "content": "fix it"},
            {"role": "tool", "name": "antons_read_code", "content": "a" * 30000}]
    smaller = Q._shrink_for_retry({"messages": msgs, "tools": [{}]})
    check("a retry payload is produced", smaller is not None)
    if smaller:
        check("it is smaller",
              Q._content_chars(smaller["messages"]) < Q._content_chars(msgs))
        check("it keeps its tools", "tools" in smaller)
        check("and the user turn", smaller["messages"][1] == msgs[1])
    check("with no user turn at all there is nothing to retry",
          Q._shrink_for_retry({"messages": [msgs[0], msgs[2]]}) is None)


def _registry():
    reg = T.ToolRegistry()

    def fake_read(args, ctx):
        return {"path": args.get("path"), "text": "x" * 20000}
    reg.register(T.Tool(
        "antons_read_code", "fake read",
        {"type": "object",
         "properties": {"action": {"type": "string"}, "path": {"type": "string"},
                        "start": {"type": "integer"}, "end": {"type": "integer"}},
         "required": ["action"]},
        fake_read, T.READ_ONLY, 1.0))
    return reg


def _read_call():
    return {"message": {"role": "assistant", "content": "", "tool_calls": [
        {"id": "c1", "function": {"name": "antons_read_code", "arguments": {
            "action": "read", "path": "app/big.py", "start": 1, "end": 900}}}]}}


def _seen(payload):
    msgs = payload.get("messages") or []
    tool_text = next((str(m.get("content") or "") for m in msgs
                      if isinstance(m, dict) and m.get("role") == "tool"), "")
    return {"chars": Q._content_chars(msgs), "tools": bool(payload.get("tools")),
            "format": bool(payload.get("format")), "tool_text": tool_text}


def case_loop_retries_instead_of_falling_back():
    print("")
    print("6. through the real loop: an overflow is trimmed and retried natively")
    seen = []

    def chat_fn(payload):
        row = _seen(payload)
        seen.append(row)
        if not row["tool_text"]:
            return _read_call()
        if len([s for s in seen if s["tool_text"]]) == 1:
            raise http_error(OVERFLOW)
        return {"message": {"role": "assistant", "content": "Done after trimming."}}
    res = Q.run_agent(
        [{"role": "user", "content": "read the big file and answer"}],
        "offline-qwen", registry=_registry(),
        ctx=T.AgentContext(actor="test", allow_private=True),
        chat_fn=chat_fn, max_tool_steps=8, max_model_calls=8,
        options={"temperature": 0, "num_ctx": 32768})
    with_tool = [s for s in seen if s["tool_text"]]
    check("answered", res.answer == "Done after trimming.", res.answer)
    check("the refused request was sent again", len(with_tool) >= 2,
          [(s["chars"], s["tools"], s["format"]) for s in seen])
    if len(with_tool) >= 2:
        check("the second attempt was smaller",
              with_tool[1]["chars"] < with_tool[0]["chars"],
              [s["chars"] for s in with_tool])
        check("and still native, with its tools",
              with_tool[1]["tools"] and not with_tool[1]["format"], with_tool[1])
    check("no structured fallback was used", not res.fallback_used, res.fallback_used)


def case_loop_trims_before_it_overflows():
    print("")
    print("7. through the real loop: the reported count trims the next request")
    seen = []

    def chat_fn(payload):
        row = _seen(payload)
        seen.append(row)
        if not row["tool_text"]:
            reply = _read_call()
            reply["prompt_eval_count"] = 7900
            return reply
        return {"message": {"role": "assistant", "content": "ok"}}
    res = Q.run_agent(
        [{"role": "user", "content": "read the big file and answer"}],
        "offline-qwen", registry=_registry(),
        ctx=T.AgentContext(actor="test", allow_private=True),
        chat_fn=chat_fn, max_tool_steps=8, max_model_calls=8,
        options={"temperature": 0, "num_ctx": 8192, "num_predict": 200})
    with_tool = [s for s in seen if s["tool_text"]]
    check("answered", res.answer == "ok", res.answer)
    check("the request after the big read was trimmed before it was sent",
          bool(with_tool) and ("cut to fit" in with_tool[0]["tool_text"]
                               or with_tool[0]["chars"] <= Q._MIN_HISTORY_CHARS + 200),
          [(s["chars"], s["tool_text"][-60:]) for s in with_tool])


def case_flattened_final_request_is_trimmed():
    print("")
    print("8. a flattened final request (tool budget spent) is trimmed as well")
    seen = []

    def chat_fn(payload):
        msgs = payload.get("messages") or []
        flat = next((str(m.get("content") or "") for m in msgs
                     if isinstance(m, dict) and m.get("role") == "user"
                     and str(m.get("content") or "").startswith("Rezultati no rikiem")),
                    "")
        seen.append({"flat": flat, "tools": bool(payload.get("tools"))})
        if len(seen) == 1:
            reply = _read_call()
            reply["prompt_eval_count"] = 7900
            return reply
        return {"message": {"role": "assistant", "content": "ok"}}
    res = Q.run_agent(
        [{"role": "user", "content": "read the big file and answer"}],
        "offline-qwen", registry=_registry(),
        ctx=T.AgentContext(actor="test", allow_private=True),
        chat_fn=chat_fn, max_tool_steps=1, max_model_calls=6,
        options={"temperature": 0, "num_ctx": 8192, "num_predict": 200})
    flats = [s["flat"] for s in seen if s["flat"]]
    check("answered", res.answer == "ok", res.answer)
    check("the tool budget ran out, so the results were flattened", bool(flats),
          [(bool(s["flat"]), s["tools"]) for s in seen])
    if flats:
        check("the flattened results were trimmed to the window first",
              "cut to fit" in flats[0] or len(flats[0]) <= Q._MIN_HISTORY_CHARS,
              len(flats[0]))


def main():
    print(__doc__.strip().splitlines()[0])
    print("")
    for fn in (case_overflow_is_not_the_parser,
               case_ollama_reason_is_surfaced,
               case_aggressive_compaction_fits,
               case_fit_to_window_uses_the_reported_count,
               case_shrink_for_retry,
               case_loop_retries_instead_of_falling_back,
               case_loop_trims_before_it_overflows,
               case_flattened_final_request_is_trimmed):
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
