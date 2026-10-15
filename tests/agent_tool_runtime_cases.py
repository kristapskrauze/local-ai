# -*- coding: utf-8 -*-
"""Deterministic tests for the central Qwen agent tool runtime.

The model is scripted here.  These cases verify that tool use is selected only
from model output, then bounded, validated, executed, logged and returned to
the next model call.

    py -3 tests/agent_tool_runtime_cases.py
"""
import io
import json
import os
import sys
import time

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


def scripted(*responses):
    box = {"i": 0, "payloads": []}

    def chat_fn(payload):
        box["payloads"].append(payload)
        i = box["i"]
        box["i"] += 1
        if i < len(responses):
            item = responses[i]
            return item(payload) if callable(item) else item
        return response_text("script exhausted")

    chat_fn.box = box
    return chat_fn


def fake_registry():
    reg = T.ToolRegistry()

    def ok(label):
        def handler(args, ctx):
            return {"label": label, "args": args, "fact": "%s-result" % label}
        return handler

    common_query = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "minLength": 2, "maxLength": 300},
            "limit": {"type": "integer", "minimum": 1, "maximum": 10},
        },
        "required": ["query"],
    }
    reg.register(T.Tool("web_search", "fake web search", common_query,
                        ok("web_search"), T.READ_ONLY, 1.0))
    reg.register(T.Tool("web_read", "fake web read", {
        "type": "object",
        "properties": {"url": {"type": "string", "minLength": 8, "maxLength": 300}},
        "required": ["url"],
    }, ok("web_read"), T.READ_ONLY, 1.0))
    reg.register(T.Tool("research", "fake research", {
        "type": "object",
        "properties": {"question": {"type": "string", "minLength": 4, "maxLength": 500}},
        "required": ["question"],
    }, ok("research"), T.READ_ONLY, 1.0))
    reg.register(T.Tool("search_personal_memory", "fake memory", common_query,
                        ok("memory"), T.READ_ONLY, 1.0, private=True))
    reg.register(T.Tool("search_chat_history", "fake chat history", common_query,
                        ok("chat_history"), T.READ_ONLY, 1.0, private=True))
    reg.register(T.Tool("search_knowledge_base", "fake kb", common_query,
                        ok("kb"), T.READ_ONLY, 1.0, private=True))
    reg.register(T.Tool("search_files", "fake file search", common_query,
                        ok("files"), T.READ_ONLY, 1.0, private=True))
    reg.register(T.Tool("inspect_file", "fake file inspect", {
        "type": "object",
        "properties": {"path": {"type": "string", "minLength": 1, "maxLength": 300}},
        "required": ["path"],
    }, ok("file_info"), T.READ_ONLY, 1.0, private=True))
    reg.register(T.Tool("system_status", "fake system status",
                        {"type": "object", "properties": {}, "required": []},
                        ok("system"), T.READ_ONLY, 1.0))
    reg.register(T.Tool("download_file", "fake download", {
        "type": "object",
        "properties": {"url": {"type": "string", "minLength": 8, "maxLength": 300}},
        "required": ["url"],
    }, ok("download"), T.LOCAL_WRITE, 1.0))

    def slow(args, ctx):
        time.sleep(0.25)
        return {"late": True}

    def boom(args, ctx):
        raise RuntimeError("boom")

    def huge(args, ctx):
        return {"blob": "x" * 20000}

    def weird(args, ctx):
        return {"value": object()}

    reg.register(T.Tool("slow_tool", "slow", {"type": "object", "properties": {}, "required": []},
                        slow, T.READ_ONLY, 0.05))
    reg.register(T.Tool("boom_tool", "boom", {"type": "object", "properties": {}, "required": []},
                        boom, T.READ_ONLY, 1.0))
    reg.register(T.Tool("huge_tool", "huge", {"type": "object", "properties": {}, "required": []},
                        huge, T.READ_ONLY, 1.0))
    reg.register(T.Tool("invalid_data_tool", "weird", {"type": "object", "properties": {}, "required": []},
                        weird, T.READ_ONLY, 1.0))
    return reg


def run_case(prompt, fn, reg=None, ctx=None, max_steps=6, max_calls=8, think=True):
    return Q.run_agent(
        [{"role": "user", "content": prompt}],
        "offline-qwen",
        registry=reg or fake_registry(),
        ctx=ctx or T.AgentContext(actor="test", allow_private=True),
        chat_fn=fn,
        max_tool_steps=max_steps,
        max_model_calls=max_calls,
        options={"temperature": 0, "num_ctx": 2048},
        think=think,
    )


def main():
    checks = []

    def check(label, passed):
        checks.append((label, bool(passed)))

    inv = T.tool_inventory(T.AgentContext(actor="test", allow_private=True))
    check("REG-001 default registry exposes model-selectable tools",
          any(t["name"] == "web_search" for t in inv))
    check("REG-002 download_file is denied without local-write permission",
          not next(t for t in inv if t["name"] == "download_file")["available"])
    no_download_intent = T.default_registry().execute(
        "download_file",
        {"url": "https://example.com/file.txt", "filename": "file.txt"},
        T.AgentContext(actor="test", allow_local_write=True,
                       turn_text="Summarize this link."))
    check("REG-003 download_file requires explicit download intent",
          no_download_intent.get("ok") is False
          and no_download_intent.get("error_type") == "refused"
          and no_download_intent.get("action_state") == "DENIED")

    fn = scripted(response_text("hi"))
    res = run_case("Explain RAG in one sentence.", fn)
    check("AGENT-001 no-tool conversation uses one model call",
          res.tool_steps == 0 and res.model_calls == 1 and res.answer == "hi")
    check("AGENT-001 no keyword router ran before Qwen",
          "tools" in fn.box["payloads"][0])
    # thinking is ON by design now (the reasoning lane); what matters is that
    # the setting the caller chose is the one the model gets
    check("AGENT-001 the think setting reaches the model (on by default)",
          fn.box["payloads"][0].get("think") is True)
    fn = scripted(response_text("hi"))
    run_case("Explain RAG in one sentence.", fn, think=False)
    check("AGENT-001 ...and think:false when the caller turns it off",
          fn.box["payloads"][0].get("think") is False)

    fn = scripted(response_tool(("system_status", {})), response_text("denied"))
    policy = Q.ToolPolicy(allowed_tools=("web_search",), allow_private=True)
    res = Q.run_agent_turn(
        [{"role": "user", "content": "How is the system doing?"}],
        "offline-qwen",
        tool_policy=policy,
        interface_context=Q.InterfaceContext(
            interface="test_surface", actor="test", conversation_id="chat-1"),
        registry=fake_registry(),
        chat_fn=fn,
        options={"temperature": 0, "num_ctx": 2048},
    )
    check("AGENT-001b policy filters schemas shown to Qwen",
          [t["function"]["name"] for t in fn.box["payloads"][0]["tools"]] == ["web_search"])
    check("AGENT-001b hallucinated registered tool outside policy is refused",
          res.tool_results[0]["error_type"] == "tool_not_available")
    check("AGENT-001b interface metadata reaches metrics",
          res.metrics()["interface"] == "test_surface"
          and res.metrics()["conversation_id"] == "chat-1")

    cancelled = Q.run_agent_turn(
        [{"role": "user", "content": "hello"}],
        "offline-qwen",
        tool_policy=Q.ToolPolicy(allowed_tools=("web_search",)),
        interface_context=Q.InterfaceContext(interface="test_cancel"),
        registry=fake_registry(),
        chat_fn=scripted(response_text("should not run")),
        cancellation_token=lambda: True,
    )
    check("AGENT-001c cancellation stops before a stale model call",
          cancelled.cancelled and cancelled.model_calls == 0 and not cancelled.answer)

    fn = scripted(response_text(""), response_text(""), response_text("should not be called"))
    empty = run_case("empty model loop", fn, max_steps=3, max_calls=8)
    check("AGENT-001d repeated empty model replies stop early",
          empty.loop_limit_reached and empty.model_calls <= 3
          and "usable response" in empty.answer)

    fn = scripted(
        {"message": {"role": "assistant", "content": ""},
         "done_reason": "length", "eval_count": 96, "prompt_eval_count": 600},
        response_tool(("research", {"question": "compare Windows speech to text options"})),
        response_text("researched"))
    res = Q.run_agent(
        [{"role": "user", "content": "Research Windows speech-to-text options."}],
        "offline-qwen",
        registry=fake_registry(),
        ctx=T.AgentContext(actor="test", allow_private=True),
        chat_fn=fn,
        max_tool_steps=3,
        max_model_calls=5,
        options={"temperature": 0, "num_predict": 96, "num_ctx": 2048},
        # the 256 retry is the no-thinking path; with thinking on, a cut
        # reasoning step is widened instead (_THINKING_RETRY_PREDICT, 09-14)
        think=False,
    )
    check("AGENT-001e empty length response retries with larger budget",
          res.answer == "researched"
          and fn.box["payloads"][1]["options"].get("num_predict") == 256
          and [r["tool"] for r in res.tool_results] == ["research"])

    fn = scripted(response_tool(("web_search", {"query": "qwen latest", "limit": 3})),
                  response_text("grounded answer"))
    res = run_case("What is the latest Qwen release?", fn)
    check("AGENT-002 one tool call returns to Qwen",
          [r["tool"] for r in res.tool_results] == ["web_search"]
          and res.model_calls == 2 and res.answer == "grounded answer")
    check("AGENT-002 tool result is in the second prompt",
          any(m.get("role") == "tool" and "web_search-result" in m.get("content", "")
              for m in fn.box["payloads"][1]["messages"]))

    calls = {"n": 0}
    reg = T.ToolRegistry()

    def counted_search(args, ctx):
        calls["n"] += 1
        return {"query": args["query"], "results": [
            {"title": "Current Qwen", "url": "https://example.com/qwen",
             "snippet": "latest release"}
        ]}

    reg.register(T.Tool("web_search", "counted web search", {
        "type": "object",
        "properties": {
            "query": {"type": "string", "minLength": 2, "maxLength": 300},
            "limit": {"type": "integer", "minimum": 1, "maximum": 10},
        },
        "required": ["query"],
    }, counted_search, T.READ_ONLY, 1.0))
    fn = scripted(
        response_tool(("web_search", {"query": "latest qwen release", "limit": 5})),
        response_tool(("web_search", {"query": "Latest   Qwen release", "limit": 5})),
        response_text("reused and answered"),
    )
    res = run_case("duplicate query", fn, reg=reg, ctx=T.AgentContext(actor="test"))
    check("AGENT-003 exact duplicate tool call is reused",
          calls["n"] == 1
          and len(res.tool_results) == 2
          and res.tool_results[1].get("duplicate_call") is True
          and res.tool_results[1].get("duplicate_type") == "exact"
          and res.tool_results[1].get("executed") is False
          and res.tool_results[1].get("previous_result_id") == res.tool_results[0].get("result_id"))
    check("AGENT-003 duplicate feedback reaches Qwen",
          any(m.get("role") == "tool" and "duplicates an earlier web_search" in m.get("content", "")
              for m in fn.box["payloads"][2]["messages"]))

    calls = {"n": 0}

    def counted_file_search(args, ctx):
        calls["n"] += 1
        return {"query": args["query"], "matches": [
            {"path": "C:/repo/app/qwen_agent.py", "name": "qwen_agent.py"}
        ]}

    reg = T.ToolRegistry()
    reg.register(T.Tool("search_files", "counted file search", {
        "type": "object",
        "properties": {
            "query": {"type": "string", "minLength": 2, "maxLength": 300},
        },
        "required": ["query"],
    }, counted_file_search, T.READ_ONLY, 1.0, private=True))
    fn = scripted(
        response_tool(("search_files", {"query": "qwen agent"})),
        response_tool(("search_files", {"query": "agent qwen"})),
        response_text("path found"),
    )
    res = run_case("semantic duplicate file search", fn, reg=reg,
                   ctx=T.AgentContext(actor="test", allow_private=True))
    check("AGENT-004 semantic duplicate retrieval is reused",
          calls["n"] == 1
          and res.tool_results[1].get("duplicate_call") is True
          and res.tool_results[1].get("duplicate_type") == "semantic")

    calls = {"n": 0}

    def counted_empty_search(args, ctx):
        calls["n"] += 1
        return {"query": args["query"], "results": [], "backend_error": "no results"}

    reg = T.ToolRegistry()
    reg.register(T.Tool("web_search", "empty web search", {
        "type": "object",
        "properties": {
            "query": {"type": "string", "minLength": 2, "maxLength": 300},
        },
        "required": ["query"],
    }, counted_empty_search, T.READ_ONLY, 1.0))
    fn = scripted(
        response_tool(("web_search", {"query": "missing qwen fact"})),
        response_tool(("web_search", {"query": "missing qwen fact official source"})),
        response_text("nothing found"),
    )
    res = run_case("legitimate retry after empty", fn, reg=reg,
                   ctx=T.AgentContext(actor="test"))
    check("AGENT-005 retry after empty result is allowed",
          calls["n"] == 2
          and not any(r.get("duplicate_call") for r in res.tool_results))

    calls = {"n": 0}

    def counted_read(args, ctx):
        calls["n"] += 1
        return {"url": args["url"], "title": "Bench", "text": "enough text"}

    reg = T.ToolRegistry()
    reg.register(T.Tool("web_read", "counted read", {
        "type": "object",
        "properties": {
            "url": {"type": "string", "minLength": 8, "maxLength": 300},
            "max_chars": {"type": "integer", "minimum": 300, "maximum": 3500},
        },
        "required": ["url"],
    }, counted_read, T.READ_ONLY, 1.0))
    fn = scripted(
        response_tool(("web_read", {"url": "https://bench.local/result/",
                                    "max_chars": 1200})),
        response_tool(("web_read", {"url": "https://bench.local/result#section",
                                    "max_chars": 2000})),
        response_text("read once"),
    )
    res = run_case("duplicate url read", fn, reg=reg,
                   ctx=T.AgentContext(actor="test"))
    check("AGENT-006 same URL read reuses prior non-truncated result",
          calls["n"] == 1
          and res.tool_results[1].get("duplicate_call") is True
          and res.tool_results[1].get("duplicate_type") == "semantic")

    research_calls = {"n": 0}
    search_calls = {"n": 0}

    def counted_research(args, ctx):
        research_calls["n"] += 1
        return {
            "question": args["question"],
            "coverage": "good",
            "pages": [
                {"title": "A", "text": "Latvian NLP evidence A"},
                {"title": "B", "text": "Latvian NLP evidence B"},
            ],
            "search": [
                {"title": "A", "url": "https://bench.local/latvian-nlp/a"},
            ],
        }

    def counted_followup_search(args, ctx):
        search_calls["n"] += 1
        return {"query": args["query"], "results": [
            {"title": "Duplicate", "url": "https://bench.local/latvian-nlp/a"}
        ]}

    reg = T.ToolRegistry()
    reg.register(T.Tool("research", "counted research", {
        "type": "object",
        "properties": {
            "question": {"type": "string", "minLength": 4, "maxLength": 500},
        },
        "required": ["question"],
    }, counted_research, T.READ_ONLY, 1.0))
    reg.register(T.Tool("web_search", "counted web search", {
        "type": "object",
        "properties": {
            "query": {"type": "string", "minLength": 2, "maxLength": 300},
        },
        "required": ["query"],
    }, counted_followup_search, T.READ_ONLY, 1.0))
    fn = scripted(
        response_tool(("research", {
            "question": "Current Latvian NLP tooling comparison with tokenizers, lemmatizers, POS, NER, and tradeoffs",
        })),
        response_tool(("web_search", {
            "query": "Latvian NLP tools lemmatizer POS tagger NER library",
        })),
        response_text("answered from research"),
    )
    res = run_case("research follow-up duplicate", fn, reg=reg,
                   ctx=T.AgentContext(actor="test"))
    check("AGENT-007 overlapping web search after research is reused",
          research_calls["n"] == 1
          and search_calls["n"] == 0
          and len(res.tool_results) == 2
          and res.tool_results[1].get("duplicate_call") is True
          and res.tool_results[1].get("duplicate_type") == "semantic"
          and res.tool_results[1].get("previous_result_id") == res.tool_results[0].get("result_id"))
    check("AGENT-007 feedback names the research result",
          any(m.get("role") == "tool" and "duplicates an earlier research" in m.get("content", "")
              for m in fn.box["payloads"][2]["messages"]))

    inv_no_media = T.tool_inventory(T.AgentContext(actor="test", allow_private=True))
    check("MEDIA-TOOLS-001 audio tool is hidden without audio attachments",
          not next(t for t in inv_no_media if t["name"] == "transcribe_audio")["available"])
    audio_ctx = T.AgentContext(actor="test", allow_private=True, attachments=({
        "attachment_id": "att_voice",
        "kind": "audio",
        "mime": "audio/mp4",
        "name": "voice.m4a",
        "transcript": "meet me at seven",
    },))
    inv_audio = T.tool_inventory(audio_ctx)
    check("MEDIA-TOOLS-002 audio tool appears with audio attachment",
          next(t for t in inv_audio if t["name"] == "transcribe_audio")["available"])
    out = T.default_registry().execute(
        "transcribe_audio", {"attachment_id": "att_voice"}, audio_ctx)
    check("MEDIA-TOOLS-003 cached audio transcript is returned as text",
          out.get("ok") and out.get("result", {}).get("transcript") == "meet me at seven")
    missing_stt = T.default_registry().execute(
        "transcribe_audio", {"attachment_id": "att_no_text"},
        T.AgentContext(actor="test", attachments=({
            "attachment_id": "att_no_text", "kind": "audio",
            "mime": "audio/mp4", "name": "blank.m4a",
        },)))
    check("MEDIA-TOOLS-004 missing transcript/audio bytes is structured, not blank",
          missing_stt.get("ok") is False
          and missing_stt.get("error_type") == "refused"
          and missing_stt.get("message")
          and not str(missing_stt.get("result") or "").strip())
    image_ctx = T.AgentContext(actor="test", attachments=({
        "attachment_id": "att_img", "kind": "image",
        "mime": "image/png", "name": "screen.png",
    },), media_image_inspector=lambda _att, question="": {
        "description": "a screenshot with a settings panel", "question": question})
    image_out = T.default_registry().execute(
        "inspect_image", {"attachment_id": "att_img", "question": "what is visible?"},
        image_ctx)
    check("MEDIA-TOOLS-005 image inspection can use injected vision backend",
          image_out.get("ok")
          and "settings panel" in image_out.get("result", {}).get("description", ""))
    video_out = T.default_registry().execute(
        "inspect_video", {"attachment_id": "att_vid"},
        T.AgentContext(actor="test", attachments=({
            "attachment_id": "att_vid", "kind": "video",
            "mime": "video/mp4", "name": "clip.mp4",
        },)))
    check("MEDIA-TOOLS-006 video inspection reports current limitation",
          video_out.get("ok")
          and video_out.get("result", {}).get("video_inspection_available") is False)

    action_calls = {"n": 0}

    def fake_write(args, ctx):
        action_calls["n"] += 1
        return {"path": args["path"], "action_id": "act-save-1"}

    reg = T.ToolRegistry()
    reg.register(T.Tool("save_note", "fake local write", {
        "type": "object",
        "properties": {"path": {"type": "string", "minLength": 1, "maxLength": 120}},
        "required": ["path"],
    }, fake_write, T.LOCAL_REVERSIBLE_WRITE, 1.0))
    fn = scripted(
        response_tool(("save_note", {"path": "notes/today.txt"})),
        response_tool(("save_note", {"path": "notes/today.txt"})),
        response_text("saved once"),
    )
    res = Q.run_agent(
        [{"role": "user", "content": "Save this note once."}],
        "offline-qwen",
        registry=reg,
        ctx=T.AgentContext(actor="test", allow_local_write=True),
        chat_fn=fn,
        max_tool_steps=4,
        max_model_calls=5,
    )
    check("ACTION-001 duplicate local write executes once per turn",
          action_calls["n"] == 1
          and len(res.tool_results) == 2
          and res.tool_results[1].get("idempotent_replay") is True
          and res.tool_results[1].get("executed") is False
          and res.tool_results[1].get("previous_action_id") == "act-save-1")

    action_calls = {"n": 0}
    reg = T.ToolRegistry()
    reg.register(T.Tool("save_note", "fake local write", {
        "type": "object",
        "properties": {"path": {"type": "string", "minLength": 1, "maxLength": 120}},
        "required": ["path"],
    }, fake_write, T.LOCAL_REVERSIBLE_WRITE, 1.0))
    token = {"n": 0}

    def stale_after_model():
        token["n"] += 1
        return token["n"] >= 2

    stale = Q.run_agent(
        [{"role": "user", "content": "Save this note."}],
        "offline-qwen",
        registry=reg,
        ctx=T.AgentContext(actor="test", allow_local_write=True),
        chat_fn=scripted(response_tool(("save_note", {"path": "notes/stale.txt"}))),
        max_tool_steps=2,
        max_model_calls=3,
        cancellation_token=stale_after_model,
    )
    check("ACTION-002 stale turn stops before sensitive action executes",
          stale.cancelled and action_calls["n"] == 0 and stale.tool_steps == 0)

    workflows = [
        ("old bug plus upstream",
         [("search_personal_memory", {"query": "qwen bug"}),
          ("web_search", {"query": "qwen bug upstream"}),
          ("web_read", {"url": "https://example.com/fix"})]),
        ("local grammar resource size",
         [("search_files", {"query": "latvian grammar"}),
          ("inspect_file", {"path": "resources/latvian/grammar/lv_grammar_rules.json"})]),
        ("new version and download",
         [("web_search", {"query": "driver latest"}),
          ("web_read", {"url": "https://example.com/driver"}),
          ("download_file", {"url": "https://example.com/driver.exe"})]),
        ("memory plus chat",
         [("search_personal_memory", {"query": "Sunday"}),
          ("search_chat_history", {"query": "Sunday"})]),
        ("kb plus web verify",
         [("search_knowledge_base", {"query": "Latvian grammar"}),
          ("web_search", {"query": "Latvian grammar current"})]),
        ("deep compare",
         [("research", {"question": "compare local models"}),
          ("web_read", {"url": "https://example.com/model"})]),
        ("system and driver",
         [("system_status", {}),
          ("web_search", {"query": "RTX 3090 current driver"})]),
        ("irrelevant first source",
         [("web_search", {"query": "package release"}),
          ("web_read", {"url": "https://example.com/wrong"}),
          ("web_read", {"url": "https://example.com/right"})]),
        ("file then kb",
         [("search_files", {"query": "rag index"}),
          ("inspect_file", {"path": "C:/Data/AI/rag_index/facts.npy"}),
          ("search_knowledge_base", {"query": "facts"})]),
        ("grammar explanation",
         [("search_latvian_grammar", {"text": "Es ir gatavs"}),
          ("search_knowledge_base", {"query": "darbības vārda saskaņošana"})]),
    ]
    good_multi = 0
    for i, (label, calls) in enumerate(workflows, 1):
        ctx = T.AgentContext(actor="test", allow_private=True,
                             allow_local_write=("download_file" in [c[0] for c in calls]))
        fn = scripted(response_tool(*calls), response_text("final %d" % i))
        res = run_case(label, fn, ctx=ctx)
        seq = [r["tool"] for r in res.tool_results]
        ok = seq == [c[0] for c in calls] and res.answer == "final %d" % i
        good_multi += 1 if ok else 0
        check("MULTI-%03d %s" % (i, label), ok)
    check("MULTI-011 at least 10 multi-tool workflows pass", good_multi >= 10)

    fn = scripted(response_tool(("web_search", {"query": "a"}),
                                ("web_read", {"url": "https://example.com"})),
                  response_text("read after one bad arg"))
    res = run_case("malformed first arg", fn)
    check("FAIL-001 malformed args become tool result, not crash",
          res.tool_results[0]["ok"] is False
          and res.tool_results[0]["error_type"] == "invalid_arguments"
          and res.tool_results[1]["ok"] is True)

    fn = scripted(response_tool(("time_travel", {"year": 1900})),
                  response_text("no such tool"))
    res = run_case("unknown tool", fn)
    check("FAIL-002 unknown tool is rejected",
          res.tool_results[0]["error_type"] == "unknown_tool")

    fn = scripted(response_tool(("repo_write", {"path": "x"})),
                  response_text("hallucinated write refused"))
    res = run_case("write hallucination", fn)
    check("FAIL-003 hallucinated write tool never executes",
          res.tool_results[0]["error_type"] == "unknown_tool")

    fn = scripted(response_tool(("download_file", {"url": "https://example.com/a.bin"})),
                  response_text("permission denied"))
    res = run_case("download without permission", fn,
                   ctx=T.AgentContext(actor="test", allow_private=True))
    check("FAIL-004 local write denied by default",
          res.tool_results[0]["error_type"] == "permission_denied")

    fn = scripted(response_tool(("slow_tool", {})), response_text("timeout noted"))
    res = run_case("timeout", fn)
    check("FAIL-005 timeout is structured",
          res.tool_results[0]["error_type"] == "timeout")

    fn = scripted(response_tool(("boom_tool", {})), response_text("exception noted"))
    res = run_case("exception", fn)
    check("FAIL-006 exception is structured",
          res.tool_results[0]["error_type"] == "exception")

    fn = scripted(response_tool(("huge_tool", {})), response_text("huge clipped"))
    res = run_case("huge output", fn)
    check("FAIL-007 huge tool output is clipped",
          isinstance(res.tool_results[0]["result"], dict)
          and res.tool_results[0]["result"].get("truncated") is True)

    fn = scripted(response_tool(("invalid_data_tool", {})), response_text("weird survived"))
    res = run_case("invalid data", fn)
    check("FAIL-008 invalid data is serializable to model",
          any(m.get("role") == "tool" for m in res.messages))

    loopy = scripted(*([response_tool(("system_status", {}))] * 12))
    res = run_case("loop forever", loopy, max_steps=3, max_calls=4)
    check("FAIL-009 loop limit terminates",
          res.loop_limit_reached and res.tool_steps <= 3 and res.model_calls <= 5)

    fn = scripted(response_tool(("web_search", {"query": "first source fails"})),
                  response_tool(("web_search", {"query": "try another"})),
                  response_text("recovered"))
    res = run_case("failure recovery", fn)
    check("FAIL-010 repeated recovery steps are handed back",
          [r["tool"] for r in res.tool_results] == ["web_search", "web_search"]
          and res.answer == "recovered")

    ok = True
    for label, passed in checks:
        ok = ok and passed
        print(("OK   " if passed else "FAIL ") + label)
    print("\n%d checks, VERDICT: %s" % (len(checks), "PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
