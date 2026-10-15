# -*- coding: utf-8 -*-
"""Fresh audit scenarios for the full-system claim review.

These are intentionally new prompts, not copies of the phase reports. They use
real registry/policy/code paths with temp-backed state and fake transports for
side effects. The model is scripted in the few cases that must force a specific
tool call, so this proves server behavior, not Qwen selection quality.
"""
import io
import os
import shutil
import sys
from datetime import datetime, timedelta, timezone
import tempfile
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import agent_tools as T  # noqa: E402
from app import date_plans as D  # noqa: E402
from app import goals as G  # noqa: E402
from app import local_stt as STT  # noqa: E402
from app import main as API  # noqa: E402
from app import messaging as M  # noqa: E402
from app import qwen_agent as Q  # noqa: E402
from app import rig_agent as R  # noqa: E402


TARGET = "iMessage;-;+37122222222"
OTHER = "iMessage;-;+37133333333"
HANDLE = "+37122222222"
OTHER_HANDLE = "+37133333333"


def response_tool(name, args):
    return {"message": {"role": "assistant", "content": "", "tool_calls": [{
        "id": "call_01", "function": {"name": name, "arguments": args},
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
            "assistant_chats": ["iMessage;-;self"],
            "allowed_senders": [HANDLE, OTHER_HANDLE],
            "contact_names": {HANDLE: "Alex", OTHER_HANDLE: "Alicia"},
            "partner_handles": [HANDLE],
            "trusted_recipient_handles": [OTHER_HANDLE],
            "timezone": "Europe/Riga",
            "provider": "bluebubbles_http",
            "grammar_fix": "off",
            "read_links": False,
            "web_access": True,
        },
    }


class FakeResp:
    headers = {"Content-Type": "application/octet-stream"}

    def __init__(self, chunks):
        self.chunks = list(chunks)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _n=-1):
        return self.chunks.pop(0) if self.chunks else b""


def main():
    checks = []

    def check(label, passed):
        checks.append((label, bool(passed)))

    tmp = Path(tempfile.mkdtemp(prefix="fresh-audit-"))
    real = {
        "outbox": M.OUTBOX_PATH,
        "queue": M.SEND_QUEUE_PATH,
        "deliver": M._deliver,
        "m_config": M.load_config,
        "plans": D.PLANS_PATH,
        "upcoming": R.upcoming_events,
        "ledger": T.ACTION_LEDGER_PATH,
        "goals_dir": G.DB_DIR,
        "goals_path": G.DB_PATH,
        "download_root": T.DOWNLOAD_ROOT,
        "safe_urlopen": M._safe_urlopen,
        "link_is_safe": M._link_is_safe,
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
        D.PLANS_PATH = tmp / "date_plans.json"
        R.upcoming_events = lambda days=7: []
        T.ACTION_LEDGER_PATH = tmp / "agent_actions.jsonl"
        T.DOWNLOAD_ROOT = tmp / "downloads"
        G.DB_DIR = str(tmp / "goals")
        G.DB_PATH = str(tmp / "goals" / "goals.sqlite3")
        G._init()
        reg = T.build_default_registry()

        _now_iso = (datetime.now(timezone(timedelta(hours=3)))
                    .replace(hour=10, minute=0, second=0, microsecond=0)
                    .isoformat(timespec="seconds"))
        public_ctx = T.AgentContext(actor="fresh")
        private_ctx = T.AgentContext(actor="fresh", allow_private=True, now=_now_iso)
        local_ctx = T.AgentContext(
            actor="fresh", allow_private=True, allow_local_write=True,
            interface="dashboard_assistant", turn_text="Save this file from https://example.com/a.bin")
        external_ctx = T.AgentContext(
            actor="fresh", allow_private=True, allow_external_write=True,
            conversation_id=TARGET, sender=HANDLE, now=_now_iso)

        inv_public = T.tool_inventory(public_ctx)
        inv_private = T.tool_inventory(private_ctx)
        check("FRESH-001 private contact tool hidden publicly",
              not next(t for t in inv_public if t["name"] == "contact_lookup")["available"])
        check("FRESH-002 private contact tool visible privately",
              next(t for t in inv_private if t["name"] == "contact_lookup")["available"])
        check("FRESH-003 audio tool hidden without audio",
              not next(t for t in inv_private if t["name"] == "transcribe_audio")["available"])
        check("FRESH-004 audio tool visible with audio",
              next(t for t in T.tool_inventory(T.AgentContext(
                  actor="fresh", attachments=({"attachment_id": "a1", "kind": "audio"},)))
                   if t["name"] == "transcribe_audio")["available"])
        check("FRESH-005 image tool visible with image",
              next(t for t in T.tool_inventory(T.AgentContext(
                  actor="fresh", attachments=({"attachment_id": "i1", "kind": "image"},)))
                   if t["name"] == "inspect_image")["available"])
        check("FRESH-006 video tool visible with video",
              next(t for t in T.tool_inventory(T.AgentContext(
                  actor="fresh", attachments=({"attachment_id": "v1", "kind": "video"},)))
                   if t["name"] == "inspect_video")["available"])
        check("FRESH-007 download hidden without local write",
              not next(t for t in inv_private if t["name"] == "download_file")["available"])
        check("FRESH-008 send hidden without external write",
              not next(t for t in inv_private if t["name"] == "send_message")["available"])
        check("FRESH-009 download permission does not unlock goals",
              "download_file" in set(API._agent_allowed_tools(True, False) or ())
              and "add_goal" not in set(API._agent_allowed_tools(True, False) or ()))
        check("FRESH-010 goal permission does not unlock downloads",
              "add_goal" in set(API._agent_allowed_tools(False, True) or ())
              and "download_file" not in set(API._agent_allowed_tools(False, True) or ()))

        fn = scripted(response_text("plain answer"))
        no_tool = Q.run_agent_turn(
            [{"role": "user", "content": "Give me a one-line definition of context windows."}],
            "offline-qwen", registry=reg, chat_fn=fn,
            tool_policy=Q.ToolPolicy(allowed_tools=("system_status",)),
            interface_context=Q.InterfaceContext(interface="fresh", actor="fresh"))
        check("FRESH-011 scripted no-tool turn stays tool-free",
              no_tool.tool_steps == 0 and no_tool.answer == "plain answer")
        fn = scripted(response_tool("system_status", {}), response_text("saw status"))
        one_tool = Q.run_agent_turn(
            [{"role": "user", "content": "Check this machine status."}],
            "offline-qwen", registry=reg, chat_fn=fn,
            tool_policy=Q.ToolPolicy(allowed_tools=("system_status",), max_tool_steps=2),
            interface_context=Q.InterfaceContext(interface="fresh", actor="fresh"))
        check("FRESH-012 tool result returns to the next model call",
              one_tool.tool_steps == 1 and any(m.get("role") == "tool" for m in fn.box["payloads"][1]["messages"]))
        fn = scripted(response_tool("system_status", {}), response_tool("system_status", {}),
                      response_text("reused"))
        dup = Q.run_agent_turn(
            [{"role": "user", "content": "Check status twice only if needed."}],
            "offline-qwen", registry=reg, chat_fn=fn,
            tool_policy=Q.ToolPolicy(allowed_tools=("system_status",), max_tool_steps=3),
            interface_context=Q.InterfaceContext(interface="fresh", actor="fresh"))
        check("FRESH-013 exact duplicate read reuses result",
              len(dup.tool_results) == 2 and dup.tool_results[1].get("reused"))
        fn = scripted(response_tool("download_file", {"url": "https://example.com/a.bin"}),
                      response_text("blocked"))
        refused = Q.run_agent_turn(
            [{"role": "user", "content": "Download this file."}], "offline-qwen",
            registry=reg, chat_fn=fn,
            tool_policy=Q.ToolPolicy(allowed_tools=("web_search",), allow_local_write=True),
            interface_context=Q.InterfaceContext(interface="fresh", actor="fresh",
                                                turn_text="Download this file."))
        check("FRESH-014 tool outside policy is refused",
              refused.tool_results[0].get("error_type") == "tool_not_available")
        empty = Q.run_agent_turn(
            [{"role": "user", "content": "Say anything."}], "offline-qwen",
            registry=reg, chat_fn=scripted(response_text(""), response_text("")),
            tool_policy=Q.ToolPolicy(max_model_calls=2),
            interface_context=Q.InterfaceContext(interface="fresh", actor="fresh"))
        check("FRESH-015 repeated empty replies stop safely",
              empty.loop_limit_reached and empty.answer)

        lookup = reg.execute("contact_lookup", {"query": "Alex"}, private_ctx)
        check("FRESH-016 exact contact lookup resolves Alex",
              lookup.get("ok") and (lookup.get("result", {}).get("matches") or [{}])[0].get("display_name") == "Alex")
        current = reg.execute("contact_lookup", {}, T.AgentContext(
            actor="fresh", allow_private=True, conversation_id=TARGET))
        check("FRESH-017 current conversation lookup resolves",
              current.get("ok") and (current.get("result", {}).get("matches") or [{}])[0]
              .get("channels", [{}])[0].get("conversation_id") == TARGET)
        unknown = reg.execute("contact_lookup", {"query": "Nobody Here"}, private_ctx)
        check("FRESH-018 unknown contact refuses",
              not unknown.get("ok") and unknown.get("result", {}).get("error_type") == "unknown_contact")
        ambiguous = reg.execute("contact_lookup", {"query": "A"}, private_ctx)
        check("FRESH-019 ambiguous contact refuses",
              not ambiguous.get("ok") and ambiguous.get("result", {}).get("error_type") == "ambiguous_contact")

        read_msg = reg.execute("send_message", {"conversation_id": TARGET, "text": "ok"},
                               T.AgentContext(**{**external_ctx.__dict__,
                                                 "turn_text": "What was Alex's message last night?"}))
        check("FRESH-020 message-read wording does not send",
              not read_msg.get("ok") and len(sends) == 0)
        draft = reg.execute("send_message", {"conversation_id": TARGET, "text": "ok"},
                            T.AgentContext(**{**external_ctx.__dict__,
                                              "turn_text": "Maybe draft a text to Alex saying ok."}))
        check("FRESH-021 draft wording does not send",
              not draft.get("ok") and len(sends) == 0)
        sent = reg.execute("send_message", {"conversation_id": TARGET, "text": "audit hello"},
                           T.AgentContext(**{**external_ctx.__dict__,
                                             "turn_text": "Send Alex: audit hello"}))
        check("FRESH-022 explicit send uses fake transport",
              sent.get("ok") and len(sends) == 1)
        replay = reg.execute("send_message", {"conversation_id": TARGET, "text": "audit hello"},
                             T.AgentContext(**{**external_ctx.__dict__,
                                               "turn_text": "Send Alex: audit hello"}))
        check("FRESH-023 duplicate send replays",
              replay.get("ok") and replay.get("idempotent_replay") and len(sends) == 1)
        bad_recipient = reg.execute("send_message", {"conversation_id": "iMessage;-;+3700000", "text": "x"},
                                    T.AgentContext(**{**external_ctx.__dict__,
                                                      "turn_text": "Send Alex: x"}))
        check("FRESH-024 untrusted recipient denied",
              not bad_recipient.get("ok") and bad_recipient.get("result", {}).get("error_type") == "untrusted_recipient")
        mismatched_recipient = reg.execute(
            "send_message", {"conversation_id": OTHER, "text": "audit hello"},
            T.AgentContext(**{**external_ctx.__dict__,
                              "turn_text": "Send Alex: audit hello"}))
        check("FRESH-024a mismatched recipient denied",
              not mismatched_recipient.get("ok")
              and mismatched_recipient.get("result", {}).get("error_type") == "recipient_mismatch")
        quoted_send = reg.execute(
            "send_message", {"conversation_id": TARGET, "text": "audit hello"},
            T.AgentContext(**{**external_ctx.__dict__,
                              "turn_text": 'Earlier I said "Send Alex: audit hello"; what did I ask?'}))
        check("FRESH-024b quoted prior send command denied",
              not quoted_send.get("ok")
              and quoted_send.get("result", {}).get("error_type") == "authorization_required")

        owner_ctx = T.AgentContext(**{**external_ctx.__dict__, "owner_authored": True})
        contact_cal = reg.execute("calendar_create", {"title": "Errand", "start": "tomorrow 12:00"},
                                  T.AgentContext(**{**external_ctx.__dict__,
                                                    "turn_text": "Add errand tomorrow at 12:00"}))
        check("FRESH-025 a contact's turn cannot add to his calendar",
              not contact_cal.get("ok")
              and contact_cal.get("result", {}).get("error_type") == "authorization_required")
        event = reg.execute("calendar_create", {
            "title": "Audit dentist", "start": "tomorrow 12:00", "duration_minutes": 30},
            T.AgentContext(**{**owner_ctx.__dict__,
                              "turn_text": "Add dentist appointment tomorrow at 12:00"}))
        check("FRESH-026 explicit calendar write creates date_plan",
              event.get("ok") and event.get("result", {}).get("verified"))
        conflict = reg.execute("calendar_create", {
            "title": "Overlap", "start": "tomorrow 12:15", "duration_minutes": 30},
            T.AgentContext(**{**owner_ctx.__dict__,
                              "turn_text": "Add meeting tomorrow at 12:15"}))
        check("FRESH-027 calendar conflict denied",
              not conflict.get("ok") and conflict.get("result", {}).get("error_type") == "calendar_conflict")
        search = reg.execute("calendar_search", {"when": "tomorrow", "query": "dentist"}, private_ctx)
        check("FRESH-028 calendar search sees temp event",
              search.get("ok") and search.get("result", {}).get("count") == 1)
        past = reg.execute("calendar_create", {
            "title": "Past", "start": "yesterday 12:00", "duration_minutes": 15},
            T.AgentContext(**{**owner_ctx.__dict__,
                              "turn_text": "Add meeting yesterday at 12:00"}))
        check("FRESH-029 past calendar write denied",
              not past.get("ok"))
        reminder = reg.execute("calendar_create", {
            "title": "Depo", "start": "tomorrow 13:00", "duration_minutes": 15},
            T.AgentContext(**{**owner_ctx.__dict__,
                              "turn_text": "Atgādini rīt 13:00, ka mums vajag depo"}))
        check("FRESH-029a his own reminder wording is not second-guessed",
              reminder.get("ok") and reminder.get("result", {}).get("verified"))

        def his(text):
            return T.AgentContext(actor="fresh", allow_private=True, allow_local_write=True,
                                  turn_text=text, owner_authored=True)

        def contact(text):
            return T.AgentContext(actor="fresh", allow_private=True, allow_local_write=True,
                                  turn_text=text, sender=HANDLE)

        goal = reg.execute("add_goal", {"title": "Finish the audit write-up"},
                           his("Add a goal to finish the audit write-up."))
        gid = goal.get("result", {}).get("id")
        check("FRESH-030 explicit goal add works", goal.get("ok") and gid)
        goal_replay = reg.execute("add_goal", {"title": "Finish the audit write-up"},
                                  his("Add a goal to finish the audit write-up."))
        check("FRESH-031 duplicate goal add replays",
              goal_replay.get("ok") and goal_replay.get("idempotent_replay"))
        contact_add = reg.execute("add_goal", {"title": "Stretch at 8"},
                                  contact("Add a goal: stretch every morning."))
        check("FRESH-032 a contact's turn cannot write his goals",
              not contact_add.get("ok") and contact_add.get("error_type") == "authorization_required")
        latvian = reg.execute("add_goal", {"title": "Iemācīties Rust"},
                              his("pieglabā šo manos mērķos: iemācīties Rust"))
        check("FRESH-033 his Latvian wording is not second-guessed", latvian.get("ok"))
        contact_done = reg.execute("complete_goal", {"goal_id": gid},
                                   contact("Mark the audit goal done."))
        check("FRESH-034 a contact's turn cannot complete his goal",
              not contact_done.get("ok") and G.get(gid).get("goal", {}).get("status") == "active")
        contact_cancel = reg.execute("cancel_goal", {"goal_id": gid},
                                     contact("Drop the audit goal."))
        check("FRESH-034a a contact's turn cannot cancel his goal",
              not contact_cancel.get("ok") and G.get(gid).get("goal", {}).get("status") == "active")
        progress = reg.execute("update_goal", {"goal_id": gid, "progress": 25,
                                               "progress_note": "Outlined sections."},
                               his("I made progress on the audit goal."))
        check("FRESH-035 explicit goal progress works",
              progress.get("ok") and progress.get("result", {}).get("goal", {}).get("progress") == 25)
        completed = reg.execute("complete_goal", {"goal_id": gid},
                                his("Šo esmu izdarījis"))
        check("FRESH-036 his completion in any words works",
              completed.get("ok") and completed.get("result", {}).get("goal", {}).get("status") == "done")
        focus_goal = G.create("Tune the Qwen tool loop", contexts=["qwen", "tools"], dedupe=False)
        fn = scripted(response_text("focus"))
        Q.run_agent_turn(
            [{"role": "user", "content": "What should I focus on for Qwen tools?"}],
            "offline-qwen", registry=reg, chat_fn=fn,
            tool_policy=Q.ToolPolicy(allowed_tools=("note_goal_surfaced",),
                                     allow_private=True, allow_local_write=True,
                                     max_model_calls=1),
            interface_context=Q.InterfaceContext(interface="dashboard_assistant",
                                                actor="fresh",
                                                turn_text="What should I focus on for Qwen tools?"))
        prompt_text = "\n".join(m.get("content", "") for m in fn.box["payloads"][0]["messages"])
        check("FRESH-037 self context injects relevant goals",
              "Relevant Active Goals" in prompt_text)
        fn = scripted(response_text("private omitted"))
        Q.run_agent_turn(
            [{"role": "user", "content": "What should I focus on for Qwen tools?"}],
            "offline-qwen", registry=reg, chat_fn=fn,
            tool_policy=Q.ToolPolicy(allowed_tools=("note_goal_surfaced",),
                                     allow_private=True, allow_local_write=True,
                                     max_model_calls=1),
            interface_context=Q.InterfaceContext(interface="bluebubbles_assistant",
                                                actor="fresh",
                                                conversation_id=OTHER,
                                                turn_text="What should I focus on for Qwen tools?"))
        third_prompt = "\n".join(m.get("content", "") for m in fn.box["payloads"][0]["messages"])
        check("FRESH-038 third-party context omits private goals",
              "Relevant Active Goals" not in third_prompt)
        reg.execute("note_goal_surfaced", {"goal_id": focus_goal["id"]},
                    T.AgentContext(actor="fresh", allow_private=True,
                                   allow_local_write=True,
                                   interface="dashboard_assistant"))
        resurfaced = G.select_for_turn("Qwen tools", private_context=True)
        check("FRESH-039 surfaced goal enters cooldown",
              all(g.get("id") != focus_goal["id"] for g in resurfaced.get("goals", [])))

        no_intent_download = reg.execute("download_file", {
            "url": "https://example.com/a.bin", "filename": "a.bin"},
            T.AgentContext(actor="fresh", allow_local_write=True,
                           turn_text="Read this link before replying."))
        check("FRESH-040 download requires explicit save intent",
              not no_intent_download.get("ok"))
        quoted_download = reg.execute("download_file", {
            "url": "https://example.com/a.bin", "filename": "a.bin"},
            T.AgentContext(actor="fresh", allow_local_write=True,
                           turn_text='Earlier I said "Download this file"; what did I ask?'))
        check("FRESH-040a quoted prior file download command denied",
              not quoted_download.get("ok"))
        M._link_is_safe = lambda _url: True
        M._safe_urlopen = lambda *_args, **_kwargs: FakeResp([b"abc", b""])
        downloaded = reg.execute("download_file", {
            "url": "https://example.com/a.bin", "filename": "a.bin"},
            local_ctx)
        check("FRESH-041 explicit download writes temp file",
              downloaded.get("ok") and (T.DOWNLOAD_ROOT / "a.bin").exists())
        downloaded_again = reg.execute("download_file", {
            "url": "https://example.com/a.bin", "filename": "a.bin"},
            local_ctx)
        check("FRESH-042 duplicate download replays from existing file",
              downloaded_again.get("ok") and downloaded_again.get("result", {}).get("idempotent_replay"))
        M._safe_urlopen = lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("refusing redirect"))
        blocked_download = reg.execute("download_file", {
            "url": "https://example.com/private.bin", "filename": "private.bin"},
            local_ctx)
        check("FRESH-043 safe opener failure blocks download",
              not blocked_download.get("ok") and not (T.DOWNLOAD_ROOT / "private.bin").exists())
        M._link_is_safe = real["link_is_safe"]
        M._safe_urlopen = real["safe_urlopen"]

        cached_audio = reg.execute("transcribe_audio", {}, T.AgentContext(
            actor="fresh", attachments=({"attachment_id": "aud1", "kind": "audio",
                                         "transcript": "meet at seven"},)))
        check("FRESH-044 cached audio transcript returns text",
              cached_audio.get("ok") and "seven" in cached_audio.get("result", {}).get("transcript", ""))
        missing_audio = reg.execute("transcribe_audio", {}, T.AgentContext(
            actor="fresh", attachments=({"attachment_id": "aud2", "kind": "audio"},)))
        check("FRESH-045 missing audio bytes fail structured",
              not missing_audio.get("ok") and missing_audio.get("error_type") == "refused")
        image_missing = reg.execute("inspect_image", {}, T.AgentContext(
            actor="fresh", attachments=({"attachment_id": "img1", "kind": "image"},)))
        check("FRESH-046 image without pixels refuses",
              not image_missing.get("ok"))
        video = reg.execute("inspect_video", {}, T.AgentContext(
            actor="fresh", attachments=({"attachment_id": "vid1", "kind": "video"},)))
        check("FRESH-047 video reports limitation truthfully",
              video.get("ok") and video.get("result", {}).get("video_inspection_available") is False)
        bad_file = reg.execute("inspect_file", {"path": ".env", "include_preview": True}, private_ctx)
        check("FRESH-048 protected file path denied",
              not bad_file.get("ok"))
        bad_url = reg.execute("web_read", {"url": "http://127.0.0.1:8765/api/health"}, public_ctx)
        check("FRESH-049 private URL read denied",
              not bad_url.get("ok"))
        if STT.STT_ROOT.exists():
            check("FRESH-050 local STT binaries are currently available",
                  STT.available()[0])
        else:
            # whisper.cpp and its model live outside the repo (AI_HOME)
            print("SKIP FRESH-050 local STT is not installed on this machine")

        seen = {}

        def fake_agent(**kwargs):
            seen.update(kwargs)
            return type("Run", (), {
                "answer": "central agent reply",
                "tool_steps": 0,
                "model_calls": 1,
                "trace_id": "fresh-live",
                "loop_limit_reached": False,
                "cancelled": False,
                "tool_results": [],
                "metrics": lambda self: {"trace_id": "fresh-live"},
            })()

        entry = {"text": "Could you check the current model status?",
                 "sender": HANDLE, "is_from_me": True, "attachments": []}
        with __import__("unittest.mock").mock.patch.object(
                M, "_build_reply_messages",
                return_value=[{"role": "system", "content": "assistant"},
                              {"role": "user", "content": entry["text"]}]), \
                __import__("unittest.mock").mock.patch.object(
                    M.qwen_agent, "run_agent_turn", side_effect=fake_agent), \
                __import__("unittest.mock").mock.patch.object(
                    M, "_polish_reply", side_effect=lambda text, _guid=None: text):
            reply, mode = M._generate_reply(fake_config()["auto_reply"], {}, [entry],
                                            "iMessage;-;self")
        check("FRESH-051 self BlueBubbles assistant reaches central agent",
              reply == "central agent reply" and mode == "assistant-agent"
              and seen["interface_context"].interface == "bluebubbles_assistant")
        check("FRESH-052 live assistant policy excludes downloads by default",
              "download_file" not in seen["tool_policy"].allowed_tools)

    finally:
        M.OUTBOX_PATH = real["outbox"]
        M.SEND_QUEUE_PATH = real["queue"]
        M._deliver = real["deliver"]
        M.load_config = real["m_config"]
        D.PLANS_PATH = real["plans"]
        R.upcoming_events = real["upcoming"]
        T.ACTION_LEDGER_PATH = real["ledger"]
        G.DB_DIR = real["goals_dir"]
        G.DB_PATH = real["goals_path"]
        T.DOWNLOAD_ROOT = real["download_root"]
        M._safe_urlopen = real["safe_urlopen"]
        M._link_is_safe = real["link_is_safe"]
        shutil.rmtree(tmp, ignore_errors=True)

    ok = True
    for label, passed in checks:
        ok = ok and passed
        print(("OK   " if passed else "FAIL ") + label)
    print("\n%d fresh scenarios, VERDICT: %s" % (len(checks), "PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
