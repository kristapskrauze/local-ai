# -*- coding: utf-8 -*-
"""Phase 7 context-first torture checks for the central Qwen runtime.

This file is intentionally deterministic: the model is scripted so the checks
exercise the server-side loop, scoped context tools, and action policy without
sending real messages.

    py -3 tests/qwen_context_torture_bench.py
"""
from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

from unittest.mock import patch

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.people import PARTNER
from app import agent_tools as T  # noqa: E402
from app import messaging as M  # noqa: E402
from app import qwen_agent as Q  # noqa: E402


TARGET = "iMessage;-;+37122222222"
OTHER = "iMessage;-;+37133333333"
HANDLE = "+37122222222"


def response_text(text):
    return {"message": {"role": "assistant", "content": text}}


def response_tool(name, args):
    return {"message": {"role": "assistant", "content": "", "tool_calls": [{
        "id": "call_01",
        "function": {"name": name, "arguments": args},
    }]}}


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
            "assistant_agent_tools": "active",
            "assistant_chats": ["iMessage;-;self"],
            "allowed_senders": [HANDLE],
            "contact_names": {HANDLE: PARTNER},
            "partner_handles": [HANDLE],
            "timezone": "Europe/Riga",
            "provider": "bluebubbles_http",
        },
    }


def main() -> int:
    checks = []

    def check(label, passed):
        checks.append((label, bool(passed)))

    tmp = Path(tempfile.mkdtemp(prefix="qwen-context-"))
    real = {
        "outbox": M.OUTBOX_PATH,
        "queue": M.SEND_QUEUE_PATH,
        "deliver": M._deliver,
        "config": M.load_config,
        "ledger": T.ACTION_LEDGER_PATH,
    }
    sends = []

    def fake_deliver(_selected, _pcfg, target, body, metadata):
        sends.append({"target": target, "body": body, "idem": (metadata or {}).get("idem")})
        return {"ok": True, "status_code": 200, "body": {"status": 200}}

    try:
        os.environ.setdefault("TEST_ONLY", "x")
        M.OUTBOX_PATH = tmp / "outbox.jsonl"
        M.SEND_QUEUE_PATH = tmp / "send_queue.json"
        M.OUTBOX_PATH.write_text("", encoding="utf-8")
        M._deliver = fake_deliver
        M.load_config = fake_config
        T.ACTION_LEDGER_PATH = tmp / "agent_actions.jsonl"
        reg = T.build_default_registry()

        fn = scripted(response_text("ORCHID-742, pickup at 18:30."))
        run = Q.run_agent_turn(
            [
                {"role": "user", "content": "The package code is ORCHID-742 and pickup is 18:30."},
                {"role": "user", "content": "Use that code and tell me the pickup window."},
            ],
            "offline-qwen",
            registry=reg,
            chat_fn=fn,
            tool_policy=Q.ToolPolicy(allowed_tools=("search_chat_history",),
                                     allow_private=True),
            interface_context=Q.InterfaceContext(
                interface="dashboard_assistant",
                actor="fresh",
                turn_text="Use that code and tell me the pickup window."),
        )
        check("CTX-001 recent prompt context can answer without ask-back",
              run.answer.startswith("ORCHID-742") and run.tool_steps == 0)

        now = time.time()
        history_rows = [
            {"text": "ORCHID-742 belongs to this chat", "chat_guid": TARGET,
             "sender": HANDLE, "ts": now},
            {"text": "ORCHID-742 belongs to another chat", "chat_guid": OTHER,
             "sender": "+37133333333", "ts": now + 1},
        ]
        hist_dir = Path(tempfile.mkdtemp(prefix="ctx_history_"))
        (hist_dir / "inbox.jsonl").write_text("".join(
            json.dumps(r, ensure_ascii=False) + "\n" for r in history_rows), encoding="utf-8")
        (hist_dir / "outbox.jsonl").write_text("", encoding="utf-8")
        with patch.object(M, "inbox", return_value=history_rows), \
                patch.object(M, "outbox", return_value=[]), \
                patch.object(M, "INBOX_PATH", hist_dir / "inbox.jsonl"), \
                patch.object(M, "OUTBOX_PATH", hist_dir / "outbox.jsonl"):
            scoped = reg.execute(
                "search_chat_history",
                {"query": "ORCHID-742"},
                T.AgentContext(actor="fresh", allow_private=True,
                               interface="bluebubbles_trusted_contact_action",
                               conversation_id=TARGET))
            broad = reg.execute(
                "search_chat_history",
                {"query": "ORCHID-742"},
                T.AgentContext(actor="fresh", allow_private=True,
                               interface="dashboard_assistant",
                               conversation_id=TARGET))
        scoped_matches = scoped.get("result", {}).get("matches") or []
        broad_matches = broad.get("result", {}).get("matches") or []
        check("CTX-002 trusted contact history defaults to current chat",
              len(scoped_matches) == 1 and scoped_matches[0].get("chat_guid") == TARGET)
        check("CTX-003 dashboard history remains broad",
              len(broad_matches) == 2)

        fn = scripted(response_tool("search_chat_history", {"query": "upload glitch"}),
                      response_text("Restart the bridge."))
        searched = Q.run_agent_turn(
            [{"role": "user", "content": "What workaround did I tell you for the upload glitch?"}],
            "offline-qwen",
            registry=reg,
            chat_fn=fn,
            tool_policy=Q.ToolPolicy(allowed_tools=("search_chat_history",),
                                     allow_private=True),
            interface_context=Q.InterfaceContext(
                interface="dashboard_assistant",
                actor="fresh",
                turn_text="What workaround did I tell you for the upload glitch?"),
        )
        check("CTX-004 chat-history evidence can stop ask-back",
              [r.get("tool") for r in searched.tool_results] == ["search_chat_history"]
              and searched.answer == "Restart the bridge.")

        quoted = reg.execute(
            "send_message",
            {"conversation_id": TARGET, "text": "home at seven"},
            T.AgentContext(actor="fresh", allow_private=True, allow_external_write=True,
                           interface="bluebubbles_assistant",
                           conversation_id="iMessage;-;self",
                           sender=HANDLE,
                           turn_text=f'Earlier I said "Send {PARTNER}: home at seven"; what did I ask?'))
        check("CTX-005 quoted prior send instruction is denied",
              not quoted.get("ok") and len(sends) == 0)

        mismatch = reg.execute(
            "send_message",
            {"conversation_id": TARGET, "text": "home at seven"},
            T.AgentContext(actor="fresh", allow_private=True, allow_external_write=True,
                           interface="bluebubbles_assistant",
                           conversation_id="iMessage;-;self",
                           sender=HANDLE,
                           turn_text="Send Alex: home at seven"))
        check("CTX-006 mismatched recipient is denied",
              not mismatch.get("ok") and len(sends) == 0)

    finally:
        M.OUTBOX_PATH = real["outbox"]
        M.SEND_QUEUE_PATH = real["queue"]
        M._deliver = real["deliver"]
        M.load_config = real["config"]
        T.ACTION_LEDGER_PATH = real["ledger"]
        shutil.rmtree(tmp, ignore_errors=True)

    ok = True
    for label, passed in checks:
        ok = ok and passed
        print(("OK   " if passed else "FAIL ") + label)
    print("\n%d context torture checks, VERDICT: %s" %
          (len(checks), "PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
