# -*- coding: utf-8 -*-
"""Offline Phase 5 checks for personal-assistant tools.

These cases exercise the real central registry and Qwen tool loop with fake
messaging/calendar storage. They never touch the live outbox, queue, calendar,
or action ledger.
"""
import io
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.people import PARTNER
from app import agent_tools as T  # noqa: E402
from app import date_plans as D  # noqa: E402
from app import messaging as M  # noqa: E402
from app import qwen_agent as Q  # noqa: E402
from app import rig_agent as R  # noqa: E402

TARGET = "iMessage;-;+37111111111"
HANDLE = "+37111111111"
OTHER_TARGET = "iMessage;-;+37112222222"
OTHER_HANDLE = "+37112222222"
NOW = (datetime.now(timezone(timedelta(hours=3)))
       .replace(hour=10, minute=0, second=0, microsecond=0)
       .isoformat(timespec="seconds"))


def response_tool(name, args):
    return {"message": {"role": "assistant", "content": "", "tool_calls": [{
        "id": "call_01",
        "function": {"name": name, "arguments": args},
    }]}}


def response_text(text):
    return {"message": {"role": "assistant", "content": text}}


def scripted(*responses):
    box = {"i": 0, "payloads": []}

    def chat_fn(payload):
        box["payloads"].append(payload)
        i = box["i"]
        box["i"] += 1
        return responses[i] if i < len(responses) else response_text("done")

    chat_fn.box = box
    return chat_fn


def fake_config():
    return {
        "default_provider": "bluebubbles_http",
        "providers": {"bluebubbles_http": {
            "enabled": True,
            "server_url": "http://127.0.0.1:1",
            "password_env": "TEST_ONLY",
            "send_retries": 1,
            "include_temp_guid": True,
        }},
        "auto_reply": {
            "enabled": True,
            "assistant_enabled": True,
            "assistant_agent_tools": "active",
            "allowed_senders": [HANDLE, OTHER_HANDLE],
            "contact_names": {HANDLE: PARTNER, OTHER_HANDLE: "Alex"},
            "partner_handles": [HANDLE],
            "trusted_recipient_handles": [OTHER_HANDLE],
            "assistant_chats": ["iMessage;-;self"],
            "timezone": "Europe/Riga",
        },
    }


def main():
    checks = []

    def check(label, passed):
        checks.append((label, bool(passed)))

    tmp = Path(tempfile.mkdtemp(prefix="antons-phase5-"))
    real = {
        "outbox": M.OUTBOX_PATH,
        "queue": M.SEND_QUEUE_PATH,
        "deliver": M._deliver,
        "m_config": M.load_config,
        "plans": D.PLANS_PATH,
        "upcoming": R.upcoming_events,
        "ledger": T.ACTION_LEDGER_PATH,
    }
    calls = []

    def fake_deliver(selected, pcfg, target, body, metadata):
        calls.append({
            "selected": selected,
            "target": target,
            "text": body,
            "temp_guid": (metadata or {}).get("_temp_guid"),
            "idem": (metadata or {}).get("idem"),
        })
        return {
            "ok": True,
            "status_code": 200,
            "body": {"status": 200},
            "bluebubbles_temp_guid": (metadata or {}).get("_temp_guid"),
        }

    try:
        os.environ.setdefault("TEST_ONLY", "x")
        M.OUTBOX_PATH = tmp / "outbox.jsonl"
        M.SEND_QUEUE_PATH = tmp / "send_queue.json"
        D.PLANS_PATH = tmp / "date_plans.json"
        T.ACTION_LEDGER_PATH = tmp / "agent_actions.jsonl"
        M.OUTBOX_PATH.write_text("", encoding="utf-8")
        M._deliver = fake_deliver
        M.load_config = fake_config
        R.upcoming_events = lambda days=7: []

        reg = T.build_default_registry()
        private_ctx = T.AgentContext(
            actor="test",
            allow_private=True,
            conversation_id=TARGET,
            turn_text=f"What is {PARTNER}'s contact?",
            now=NOW,
            sender=HANDLE,
        )
        write_ctx = T.AgentContext(
            actor="test",
            allow_private=True,
            allow_external_write=True,
            conversation_id=TARGET,
            turn_text=f"Send {PARTNER}: on my way",
            now=NOW,
            sender=HANDLE,
        )

        inv_no_write = T.tool_inventory(private_ctx)
        check("P5-001 send_message is hidden without external-write policy",
              not next(t for t in inv_no_write if t["name"] == "send_message")["available"])
        inv_write = T.tool_inventory(write_ctx)
        check("P5-002 personal write tools appear only with policy",
              next(t for t in inv_write if t["name"] == "send_message")["available"]
              and next(t for t in inv_write if t["name"] == "calendar_create")["available"])

        lookup = reg.execute("contact_lookup", {"query": PARTNER}, private_ctx)
        match = ((lookup.get("result") or {}).get("matches") or [{}])[0]
        channel = (match.get("channels") or [{}])[0]
        check("P5-003 contact_lookup resolves configured contacts",
              lookup.get("ok") and channel.get("conversation_id") == TARGET)

        denied_policy = reg.execute(
            "send_message", {"conversation_id": TARGET, "text": "blocked"}, private_ctx)
        check("P5-004 registry denies external writes without policy",
              denied_policy.get("ok") is False
              and denied_policy.get("risk") == T.EXTERNAL_WRITE
              and denied_policy.get("action_state") == "DENIED")

        draft_ctx = T.AgentContext(
            actor="test",
            allow_private=True,
            allow_external_write=True,
            conversation_id=TARGET,
            turn_text=f"Maybe draft a text to {PARTNER} saying on my way.",
            now=NOW,
            sender=HANDLE,
        )
        denied_auth = reg.execute(
            "send_message", {"conversation_id": TARGET, "text": "on my way"}, draft_ctx)
        check("P5-005 send_message requires explicit current-turn authorization",
              denied_auth.get("ok") is False
              and denied_auth.get("error_type") == "authorization_required"
              and denied_auth.get("action_state") == "DENIED")
        read_message_ctx = T.AgentContext(
            actor="test",
            allow_private=True,
            allow_external_write=True,
            conversation_id=TARGET,
            turn_text=f"What was {PARTNER}'s message yesterday?",
            now=NOW,
            sender=HANDLE,
        )
        denied_read_message = reg.execute(
            "send_message", {"conversation_id": TARGET, "text": "on my way"},
            read_message_ctx)
        check("P5-005b message read queries do not authorize sends",
              denied_read_message.get("ok") is False
              and denied_read_message.get("error_type") == "authorization_required"
              and denied_read_message.get("action_state") == "DENIED")
        mismatched_recipient = reg.execute(
            "send_message", {"conversation_id": OTHER_TARGET, "text": "on my way"},
            write_ctx)
        check("P5-005c turn recipient must match send_message target",
              mismatched_recipient.get("ok") is False
              and mismatched_recipient.get("error_type") == "recipient_mismatch"
              and mismatched_recipient.get("action_state") == "DENIED")
        quoted_message_ctx = T.AgentContext(
            actor="test",
            allow_private=True,
            allow_external_write=True,
            conversation_id=TARGET,
            turn_text=f'Earlier I said "Send {PARTNER}: on my way"; what did I ask?',
            now=NOW,
            sender=HANDLE,
        )
        quoted_send = reg.execute(
            "send_message", {"conversation_id": TARGET, "text": "on my way"},
            quoted_message_ctx)
        check("P5-005d quoted prior message does not authorize sends",
              quoted_send.get("ok") is False
              and quoted_send.get("error_type") == "authorization_required"
              and quoted_send.get("action_state") == "DENIED")
        check("P5-006 denied sends never hit transport", len(calls) == 0)

        sent = reg.execute(
            "send_message", {"conversation_id": TARGET, "text": "on my way"}, write_ctx)
        check("P5-007 authorized send_message uses the real send path",
              sent.get("ok") and sent.get("action_state") == "EXECUTED"
              and len(calls) == 1 and calls[-1]["target"] == TARGET)
        replay = reg.execute(
            "send_message", {"conversation_id": TARGET, "text": "on my way"}, write_ctx)
        check("P5-008 send_message durable idempotency suppresses repeats",
              replay.get("ok") and replay.get("idempotent_replay")
              and len(calls) == 1)

        create_ctx = T.AgentContext(
            actor="test",
            allow_private=True,
            allow_external_write=True,
            conversation_id="iMessage;-;self",
            turn_text="Add dentist appointment tomorrow at 14:00",
            now=NOW,
            sender=HANDLE,
            owner_authored=True,
        )
        created = reg.execute("calendar_create", {
            "title": "Dentist appointment",
            "start": "tomorrow 14:00",
            "duration_minutes": 30,
        }, create_ctx)
        check("P5-009 calendar_create writes and verifies date_plans",
              created.get("ok") and created.get("action_state") == "EXECUTED"
              and created.get("result", {}).get("verified"))
        conflict = reg.execute("calendar_create", {
            "title": "Second appointment",
            "start": "tomorrow 14:15",
            "duration_minutes": 30,
        }, create_ctx)
        check("P5-010 calendar_create refuses conflicts by default",
              conflict.get("ok") is False
              and conflict.get("error_type") == "calendar_conflict"
              and conflict.get("action_state") == "DENIED")
        contact_calendar_ctx = T.AgentContext(
            actor="test",
            allow_private=True,
            allow_external_write=True,
            conversation_id=TARGET,
            turn_text="Ieliec kalendārā rīt 16:00 zobārstu",
            now=NOW,
            sender=HANDLE,
        )
        denied_contact = reg.execute("calendar_create", {
            "title": "Second appointment",
            "start": "tomorrow 16:00",
            "duration_minutes": 30,
        }, contact_calendar_ctx)
        check("P5-010b a turn someone else wrote cannot add to his calendar",
              denied_contact.get("ok") is False
              and denied_contact.get("error_type") == "authorization_required"
              and denied_contact.get("action_state") == "DENIED")
        own_wording_ctx = T.AgentContext(
            actor="test",
            allow_private=True,
            allow_external_write=True,
            conversation_id=TARGET,
            turn_text="Atgādini ka mums vajag depo",
            now=NOW,
            sender=HANDLE,
            owner_authored=True,
        )
        own_wording = reg.execute("calendar_create", {
            "title": "Depo",
            "start": "tomorrow 16:00",
            "duration_minutes": 30,
        }, own_wording_ctx)
        check("P5-010c his own turn is not second-guessed by its wording",
              own_wording.get("ok") and own_wording.get("action_state") == "EXECUTED")
        search = reg.execute("calendar_search", {"when": "tomorrow", "query": "dentist"}, private_ctx)
        check("P5-011 calendar_search handles day-level ranges",
              search.get("ok") and search.get("result", {}).get("count") == 1)

        calls.clear()
        qfn = scripted(
            response_tool("send_message", {
                "conversation_id": TARGET,
                "text": "central path",
            }),
            response_text("sent"),
        )
        qrun = Q.run_agent_turn(
            [{"role": "user", "content": f"Send {PARTNER}: central path"}],
            "offline-qwen",
            tool_policy=Q.ToolPolicy(
                allowed_tools=("send_message",),
                allow_private=True,
                allow_external_write=True,
                max_tool_steps=2,
                max_model_calls=3,
            ),
            interface_context=Q.InterfaceContext(
                interface="phase5_test",
                actor="phase5_test",
                conversation_id=TARGET,
                turn_text=f"Send {PARTNER}: central path",
                sender=HANDLE,
                audit_id="phase5-test",
            ),
            registry=reg,
            chat_fn=qfn,
            options={"temperature": 0, "num_ctx": 2048},
        )
        check("P5-012 Qwen tool loop executes model-selected send_message",
              qrun.answer == "sent"
              and qrun.tool_results[0].get("ok")
              and qrun.tool_results[0].get("result_id", "").startswith("send_")
              and len(calls) == 1)
        check("P5-013 no keyword pre-router forced the action",
              [t["function"]["name"] for t in qfn.box["payloads"][0]["tools"]]
              == ["send_message"])
    finally:
        M.OUTBOX_PATH = real["outbox"]
        M.SEND_QUEUE_PATH = real["queue"]
        M._deliver = real["deliver"]
        M.load_config = real["m_config"]
        D.PLANS_PATH = real["plans"]
        R.upcoming_events = real["upcoming"]
        T.ACTION_LEDGER_PATH = real["ledger"]
        shutil.rmtree(tmp, ignore_errors=True)

    ok = True
    for label, passed in checks:
        ok = ok and passed
        print(("OK   " if passed else "FAIL ") + label)
    print("\n%d checks, VERDICT: %s" % (len(checks), "PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
