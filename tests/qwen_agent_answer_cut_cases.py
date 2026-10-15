# -*- coding: utf-8 -*-
"""An answer cut off at the length limit is retried with room and told why.

    py -3 tests/qwen_agent_answer_cut_cases.py
"""
import io
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import agent_tools as T  # noqa: E402
from app import qwen_agent as Q  # noqa: E402

LOOP = "Priekšlikums gatavs, kungs. Maine: `no nacam obola no no no no no no no"
GOOD = "Priekšlikums gatavs, kungs. Pārbaude iet cauri; saki jā, un es to pielikšu."


def scripted(*responses):
    box = {"payloads": []}

    def chat_fn(payload):
        box["payloads"].append(payload)
        i = len(box["payloads"]) - 1
        return responses[i] if i < len(responses) else {
            "message": {"role": "assistant", "content": "script exhausted"},
            "done_reason": "stop"}

    chat_fn.box = box
    return chat_fn


def cut(text):
    return {"message": {"role": "assistant", "content": text}, "done_reason": "length"}


def done(text):
    return {"message": {"role": "assistant", "content": text}, "done_reason": "stop"}


def run(fn, num_predict, think=True):
    return Q.run_agent(
        [{"role": "user", "content": "uzraksti priekšlikumu"}],
        "offline-qwen",
        registry=T.ToolRegistry(),
        ctx=T.AgentContext(actor="bluebubbles_assistant",
                           conversation_id="iMessage;-;self"),
        chat_fn=fn,
        tool_names=(),
        max_tool_steps=4,
        max_model_calls=6,
        options={"temperature": 0, "num_ctx": 2048, "num_predict": num_predict},
        think=think,
    )


def _note_in(payload):
    return any("ran out of room before it finished" in str(m.get("content") or "")
               for m in payload.get("messages") or [])


def test_a_cut_long_answer_is_retried_with_no_less_room_and_told_why():
    fn = scripted(cut(LOOP), done(GOOD))
    res = run(fn, 3500)
    first, second = fn.box["payloads"][:2]
    assert int(second["options"]["num_predict"]) >= int(first["options"]["num_predict"]), (
        first["options"], second["options"])
    assert _note_in(second), second["messages"]
    # the loop itself is not handed back to continue from
    assert not any("no no no" in str(m.get("content") or "")
                   for m in second["messages"]), second["messages"]
    assert res.answer == GOOD, res.answer


def test_a_short_answer_still_gets_double_room():
    # without thinking there is no 3500 floor, so the old doubling still applies
    fn = scripted(cut("Te ir saraksts un saite: http://exa"), done(GOOD))
    run(fn, 500, think=False)
    assert int(fn.box["payloads"][1]["options"]["num_predict"]) == 1000, (
        fn.box["payloads"][1]["options"])


if __name__ == "__main__":
    test_a_cut_long_answer_is_retried_with_no_less_room_and_told_why()
    test_a_short_answer_still_gets_double_room()
    print("OK")
