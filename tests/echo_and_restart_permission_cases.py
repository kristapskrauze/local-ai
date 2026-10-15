# -*- coding: utf-8 -*-
"""python tests/echo_and_restart_permission_cases.py
"""
import io
import os
import sys
import uuid

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import agent_tools as T  # noqa: E402
from app import messaging as M  # noqa: E402
from app import qwen_agent as Q  # noqa: E402

CHAT = "iMessage;-;owner@example.com"
OWNER = "owner@example.com"


def _unique(text):
    return "%s %s" % (text, uuid.uuid4().hex[:8])


def test_a_bot_send_is_an_echo_before_its_outbox_row_exists():
    first = _unique("Pielikts, kungs.")
    reply = first + "\nViena lieta: vajag restartu."
    M._note_inflight_send(CHAT, reply, {"auto_reply": True, "_temp_guid": "temp-x1"})
    assert M._is_our_recent_send(CHAT, reply) is True
    # it comes back whole, or split into its lines
    assert M._text_matches_recent_send(CHAT, first) is True
    assert M._is_our_recent_send(CHAT, "something else", temp_guid="temp-x1") is True
    # another chat's identical text is not this chat's echo
    assert M._is_our_recent_send("iMessage;-;+37100000000", reply) is False


def test_his_own_words_are_never_noted():
    mine = _unique("Uzliec man atgādinājumu")
    M._note_inflight_send(CHAT, mine, {"mode": "manual"})
    assert M._inflight_send_matches(CHAT, mine) is False


def test_a_noted_send_stops_counting_after_its_ttl():
    old = _unique("Vecs atgādinājums")
    M._note_inflight_send(CHAT, old, {"auto_reply": True})
    with M._INFLIGHT_SENDS_LOCK:
        M._INFLIGHT_SENDS[-1]["ts"] -= M._INFLIGHT_SEND_TTL + 1
    assert M._inflight_send_matches(CHAT, old) is False


def test_the_restart_permission_reads_only_his_words():
    anton = 'Pielikts, kungs. Viena lieta: vajag restartu. Sak "restartē", un es to darīšu.'
    assert T._restart_requested("Jā.", "") is False
    assert T._restart_requested("restartē", "") is True
    assert T._restart_requested("Jā.", "salabo to un restartē") is True
    from app import service_restart as SR
    saved, calls = SR.request_self_restart, []
    SR.request_self_restart = lambda reason="": calls.append(reason) or (True, "stub")
    try:
        ctx = T.AgentContext(sender=OWNER, turn_text="Jā.", recent_text=anton,
                             owner_recent_text="Jā.")
        try:
            T._tool_antons_restart_service({}, ctx)
            refused = False
        except T.ToolRefused:
            refused = True
        assert refused and not calls, calls
        ok = T._tool_antons_restart_service({}, T.AgentContext(
            sender=OWNER, turn_text="restartē", owner_recent_text="restartē"))
        assert ok["restarting"] is True and len(calls) == 1, (ok, calls)
    finally:
        SR.request_self_restart = saved


def test_the_owner_text_reaches_the_tool_context():
    seen = {}
    reg = T.ToolRegistry()
    reg.register(T.Tool(
        "probe", "records the context it ran with",
        {"type": "object", "properties": {}, "required": []},
        lambda args, ctx: seen.update(owner=ctx.owner_recent_text,
                                      recent=ctx.recent_text) or {"ok": True},
        T.READ_ONLY, 5.0, False))
    calls = iter([
        {"message": {"role": "assistant", "content": "",
                     "tool_calls": [{"id": "c1", "function": {"name": "probe", "arguments": {}}}]}},
        {"message": {"role": "assistant", "content": "Gatavs."}, "done_reason": "stop"},
    ])
    Q.run_agent_turn(
        [{"role": "user", "content": "Jā."}], "offline-qwen",
        tool_policy=Q.ToolPolicy(allowed_tools=("probe",)),
        interface_context=Q.InterfaceContext(
            conversation_id=CHAT, sender=OWNER, turn_text="Jā.",
            recent_text="Jā. vajag restartu", owner_recent_text="Jā."),
        registry=reg, chat_fn=lambda payload: next(calls), think=False,
        options={"temperature": 0, "num_ctx": 2048})
    assert seen == {"owner": "Jā.", "recent": "Jā. vajag restartu"}, seen


if __name__ == "__main__":
    test_a_bot_send_is_an_echo_before_its_outbox_row_exists()
    test_his_own_words_are_never_noted()
    test_a_noted_send_stops_counting_after_its_ttl()
    test_the_restart_permission_reads_only_his_words()
    test_the_owner_text_reaches_the_tool_context()
    print("OK")
