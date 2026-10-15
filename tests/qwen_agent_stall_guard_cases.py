# -*- coding: utf-8 -*-
"""The vibe-coding stall guard in qwen_agent.run_agent.

    py -3 tests/qwen_agent_stall_guard_cases.py
"""
import io
import json
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import agent_tools as T  # noqa: E402
from app import qwen_agent as Q  # noqa: E402


def response_tool(*calls):
    tool_calls = []
    for i, call in enumerate(calls):
        name, args = call
        tool_calls.append({
            "id": "call_%02d" % i,
            "function": {"name": name, "arguments": args},
        })
    return {"message": {"role": "assistant", "content": "", "tool_calls": tool_calls}}


def response_text(text):
    return {"message": {"role": "assistant", "content": text}}


def _is_judge(payload):
    """The stall check's own call: a format-schema question about the reply."""
    return "status_update" in json.dumps(payload.get("format") or {})


def scripted(*responses, verdicts=()):
    """Scripted turn replies; the stall check's verdicts come from `verdicts`
    (default: judged a status update), an Exception entry makes that check fail."""
    box = {"i": 0, "payloads": [], "judged": [], "v": list(verdicts)}

    def chat_fn(payload):
        if _is_judge(payload):
            box["judged"].append(payload)
            v = box["v"].pop(0) if box["v"] else True
            if isinstance(v, Exception):
                raise v
            return response_text(json.dumps({"status_update": bool(v)}))
        box["payloads"].append(payload)
        i = box["i"]
        box["i"] += 1
        if i < len(responses):
            return responses[i]
        return response_text("script exhausted")

    chat_fn.box = box
    return chat_fn


def coding_registry():
    """Fake antons_* tools: the read one records, the act one flips a flag."""
    reg = T.ToolRegistry()
    seen = {"reads": 0, "proposed": False}

    def read_handler(args, ctx):
        seen["reads"] += 1
        return {"ok": True, "lines": "def renderInbox() { ... }"}

    def propose_handler(args, ctx):
        seen["proposed"] = True
        return {"ok": True, "proposal": "add .inbox-item.mine styling",
                "goal": args.get("goal")}

    reg.register(T.Tool(
        "antons_read_code", "fake read code",
        {"type": "object", "properties": {
            "action": {"type": "string"}, "path": {"type": "string"},
            "name": {"type": "string"}, "query": {"type": "string"},
        }, "required": ["action"]},
        read_handler, T.READ_ONLY, 5.0, True))
    reg.register(T.Tool(
        "antons_propose_fix", "fake propose fix",
        {"type": "object", "properties": {
            "goal": {"type": "string", "minLength": 4, "maxLength": 500},
        }, "required": ["goal"]},
        propose_handler, T.PRIVILEGED, 5.0, True))
    reg.box = seen
    return reg


DEFER = ("Es redzu, ka CSS ir definets, bet man vajag parbaudit, ka elementi "
         "tiek renderti. Es meklesu app.js tagad un pievienosu atskirigus "
         "stilus, kad lietas ir skaidrakas.")


def run_coding(fn, reg, allowed, ctx=None):
    return Q.run_agent(
        [{"role": "user", "content": "console personal sadala tavas un manas "
          "zinas izskatas vienadi, salabo lai redz kuras ir kuras"}],
        "offline-qwen",
        registry=reg,
        ctx=ctx or T.AgentContext(actor="bluebubbles_assistant",
                                  allow_private=True, allow_privileged=True,
                                  conversation_id="iMessage;-;self"),
        chat_fn=fn,
        tool_names=allowed,
        max_tool_steps=20,
        max_model_calls=12,
        options={"temperature": 0, "num_ctx": 2048},
    )


def main():
    checks = []

    def check(label, passed):
        checks.append((label, bool(passed)))
        print(("  ok  " if passed else "FAIL  ") + label)

    # STALL-001: read -> defer. The nudge fires; the model then proposes and
    # the deferral text is not what comes back.
    reg = coding_registry()
    fn = scripted(
        response_tool(("antons_read_code", {"action": "read", "path": "app/static/app.js"})),
        response_text(DEFER),
        response_tool(("antons_propose_fix", {"goal": "give sent vs received "
                       "inbox rows distinct styling in renderInbox"})),
        response_text("Priekslikums gatavs: .inbox-item.mine fons un ligzda."),
    )
    res = run_coding(fn, reg, ("antons_read_code", "antons_propose_fix"))
    nudged = any("This turn is your only execution window" in m.get("content", "")
                 for p in fn.box["payloads"] for m in p.get("messages", []))
    check("STALL-001 nudge injected after read-then-defer", nudged)
    check("STALL-001 model reached antons_propose_fix", reg.box["proposed"])
    check("STALL-001 deferral text is not the final answer", res.answer != DEFER)

    # STALL-002: one nudge only. If the model keeps deferring, the loop
    # accepts the second deferral rather than nudging forever.
    reg = coding_registry()
    fn = scripted(
        response_tool(("antons_read_code", {"action": "read", "path": "x"})),
        response_text(DEFER),
        response_text(DEFER + " (v2)"),
        response_text(DEFER + " (v3)"),
    )
    res = run_coding(fn, reg, ("antons_read_code", "antons_propose_fix"))
    nudge_count = sum(
        m.get("content", "").count("This turn is your only execution window")
        for p in fn.box["payloads"] for m in p.get("messages", []))
    # every later payload carries the one injected system message in history,
    # so what we check is that it was injected exactly once, not re-appended.
    injected_once = all(
        sum(1 for m in p.get("messages", [])
            if "This turn is your only execution window" in m.get("content", "")) <= 1
        for p in fn.box["payloads"])
    check("STALL-002 nudge injected at most once", injected_once and nudge_count >= 1)
    check("STALL-002 loop terminates after the single nudge",
          res.answer.startswith("Es redzu") and res.model_calls <= 6)

    # STALL-003: no act tool in policy -> not a coding turn, no nudge, the
    # prose answer is returned untouched (a plain "explain yourself" turn).
    reg = coding_registry()
    fn = scripted(
        response_tool(("antons_read_code", {"action": "read", "path": "x"})),
        response_text("renderInbox zime is_from_me tikai ka teksta etiketi."),
    )
    res = run_coding(fn, reg, ("antons_read_code",))
    nudged = any("This turn is your only execution window" in m.get("content", "")
                 for p in fn.box["payloads"] for m in p.get("messages", []))
    check("STALL-003 read-only policy is never nudged", not nudged)
    check("STALL-003 prose answer returned as-is",
          res.answer == "renderInbox zime is_from_me tikai ka teksta etiketi.")
    check("STALL-003 no stall check is even asked", not fn.box["judged"])

    # STALL-004: model that proposes on the first move is never nudged.
    reg = coding_registry()
    fn = scripted(
        response_tool(("antons_propose_fix", {"goal": "style inbox rows"})),
        response_text("Priekslikums gatavs."),
    )
    res = run_coding(fn, reg, ("antons_read_code", "antons_propose_fix"))
    nudged = any("This turn is your only execution window" in m.get("content", "")
                 for p in fn.box["payloads"] for m in p.get("messages", []))
    check("STALL-004 a turn that proposes immediately is not nudged", not nudged)
    check("STALL-004 proposal path still works", reg.box["proposed"])

    plan = ("Izlasīju home.html: tur ir 3 kartes un selftest, kas prasa tieši 3. "
            "Plāns: 1. Jauna spēle app/static/breakout.html -- dēlītis ar pirkstu, "
            "bumbiņa, 5 līmeņi, dzīvības un labākais rezultāts. 2. home.html -- "
            "pievienoju tower defense un jauno spēli, selftest uz 5 kartēm. "
            "3. Publicēju abas. Saki, ja der.")
    reg = coding_registry()
    fn = scripted(
        response_tool(("antons_read_code", {"action": "read", "path": "app/static/home.html"})),
        response_text(plan),
        verdicts=(False,),
    )
    res = run_coding(fn, reg, ("antons_read_code", "antons_propose_fix"))
    nudged = any("This turn is your only execution window" in m.get("content", "")
                 for p in fn.box["payloads"] for m in p.get("messages", []))
    check("STALL-005 the model is asked whether the reply is a stall", len(fn.box["judged"]) == 1)
    check("STALL-005 a real plan without a question mark is not nudged", not nudged)
    check("STALL-005 the plan is the answer", res.answer == plan)

    # STALL-006: if a nudge is followed by empty replies, the answer written
    # before the nudge is returned, never "I could not get a usable response".
    reg = coding_registry()
    fn = scripted(
        response_tool(("antons_read_code", {"action": "read", "path": "x"})),
        response_text(plan),
        response_text(""),
        response_text(""),
        verdicts=(True,),
    )
    res = run_coding(fn, reg, ("antons_read_code", "antons_propose_fix"))
    check("STALL-006 empty after a nudge keeps the earlier answer", res.answer == plan)

    # STALL-007: the stall check itself failing means no nudge -- the reply
    # goes out as written (the nudge is a quality nudge, not a safety gate).
    reg = coding_registry()
    fn = scripted(
        response_tool(("antons_read_code", {"action": "read", "path": "x"})),
        response_text(plan),
        verdicts=(RuntimeError("ollama down"),),
    )
    res = run_coding(fn, reg, ("antons_read_code", "antons_propose_fix"))
    check("STALL-007 a failed stall check does not nudge", res.answer == plan)

    # EOF-001: the native tool-call parser crashes (HTTP 500 error=EOF). The
    # turn must NOT die -- it drops native tools and drives tool selection
    # through the structured (format-schema) path, and the tool still runs.
    reg = coding_registry()
    box = {"i": 0, "payloads": [], "sent_tools": []}

    def eof_chat(payload):
        box["payloads"].append(payload)
        box["sent_tools"].append("tools" in payload)
        i = box["i"]
        box["i"] += 1
        if i == 0:
            raise RuntimeError("ollama chat failed: HTTP 500: "
                               "qwen3.5 tool call parsing failed error=EOF")
        if i == 1:
            # structured-mode reply: one JSON object with a tool_calls entry
            return {"message": {"role": "assistant", "content": json.dumps({
                "tool_calls": [{"name": "antons_propose_fix",
                                "arguments": {"goal": "add the four git endpoints "
                                              "to app/main.py"}}]})}}
        return response_text("Priekslikums gatavs.")

    eof_chat.box = box
    res = run_coding(eof_chat, reg, ("antons_read_code", "antons_propose_fix"))
    check("EOF-001 turn survives the parser crash", "error" not in (res.answer or "").lower()
          or reg.box["proposed"])
    check("EOF-001 the proposal tool still ran", reg.box["proposed"])
    check("EOF-001 native tools dropped after the crash",
          box["sent_tools"] and box["sent_tools"][0] and not box["sent_tools"][-1])

    passed = sum(1 for _, ok in checks if ok)
    total = len(checks)
    print("\n%d/%d passed" % (passed, total))
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
