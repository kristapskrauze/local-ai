# -*- coding: utf-8 -*-
"""Bounded Qwen tool loop over the central agent tool registry."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import datetime
import hashlib
import json
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from app.people import OWNER
from app import agent_tools

MAX_TOOL_STEPS = 56
MAX_MODEL_CALLS = 40
DEFAULT_MODEL_TIMEOUT_SECONDS = 210.0
DEFAULT_TURN_TIMEOUT_SECONDS = 300.0

_CODE_READ_TOOLS = frozenset({
    "antons_read_code", "antons_investigate", "antons_run_tests",
})
_CODE_ACT_TOOLS = frozenset({"antons_propose_fix", "antons_apply_fix",
                             "antons_build"})

_STALL_CHECK_SYS = (
    "A coding assistant read some of its own code during this turn and is about "
    "to send the reply below to its owner, which ends the turn. Decide whether "
    "the reply is a STATUS UPDATE that only promises to keep looking or to do "
    "the work later -- 'I'll check app.js next', 'once it's clearer I'll add "
    "it', 'now I need to understand X first' -- instead of delivering. A concrete "
    "plan that waits for his go-ahead, a finished answer, a proposal, or a "
    "clearly stated blocker is NOT a status update. The reply may be in Latvian. "
    "Answer only with JSON.")
_STALL_CHECK_SCHEMA = {"type": "object", "properties": {
    "status_update": {"type": "boolean"}}, "required": ["status_update"]}


class AgentCancelled(Exception):
    """Raised internally when an interface supersedes an in-flight turn."""


@dataclass(frozen=True)
class ToolPolicy:
    allowed_tools: tuple[str, ...] | None = None
    allow_private: bool = False
    allow_local_write: bool = False
    allow_external_write: bool = False
    allow_privileged: bool = False
    max_tool_steps: int = MAX_TOOL_STEPS
    max_model_calls: int = MAX_MODEL_CALLS
    model_timeout_seconds: float = DEFAULT_MODEL_TIMEOUT_SECONDS
    max_turn_seconds: float = DEFAULT_TURN_TIMEOUT_SECONDS


@dataclass(frozen=True)
class InterfaceContext:
    interface: str = "assistant"
    actor: str = "assistant"
    conversation_id: str = ""
    audit_id: str = ""
    turn_text: str = ""
    recent_text: str = ""
    now: str = ""
    sender: str = ""
    attachments: tuple = ()
    media_provider_config: object = None
    vision_model: str = ""
    media_transcriber: object = None
    media_image_inspector: object = None
    owner_authored: bool = False
    owner_recent_text: str = ""


@dataclass
class AgentRun:
    answer: str
    messages: list
    tool_results: list
    trace_id: str
    loop_limit_reached: bool
    model_calls: int
    tool_steps: int
    seconds: float
    used_native_tools: bool = True
    fallback_used: bool = False
    final_raw: dict | None = None
    cancelled: bool = False
    interface: str = ""
    conversation_id: str = ""

    def metrics(self) -> dict:
        duplicate_calls = sum(1 for r in self.tool_results
                              if r.get("duplicate_call"))
        semantic_duplicates = sum(1 for r in self.tool_results
                                  if r.get("duplicate_type") == "semantic")
        exact_duplicates = sum(1 for r in self.tool_results
                               if r.get("duplicate_type") == "exact")
        return {
            "trace_id": self.trace_id,
            "model_calls": self.model_calls,
            "tool_steps": self.tool_steps,
            "executed_tool_calls": sum(1 for r in self.tool_results
                                       if r.get("executed") is not False),
            "duplicate_calls": duplicate_calls,
            "duplicate_exact": exact_duplicates,
            "duplicate_semantic": semantic_duplicates,
            "reused_results": sum(1 for r in self.tool_results
                                  if r.get("reused")),
            "loop_limit_reached": self.loop_limit_reached,
            "seconds": round(self.seconds, 3),
            "fallback_used": self.fallback_used,
            "cancelled": self.cancelled,
            "interface": self.interface,
            "conversation_id": self.conversation_id,
        }


def _latest_user_text(messages: list[dict] | None) -> str:
    for msg in reversed(messages or []):
        if not isinstance(msg, dict) or str(msg.get("role") or "") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict):
                    value = item.get("text") or item.get("content")
                    if isinstance(value, str):
                        parts.append(value)
            return " ".join(parts).strip()
        return str(content or "").strip()
    return ""


def _private_goal_context_allowed(ctx: agent_tools.AgentContext) -> bool:
    if not ctx.allow_private:
        return False
    if ctx.interface in ("dashboard_assistant", "openai_proxy"):
        return True
    if ctx.interface == "bluebubbles_assistant":
        try:
            from app import messaging
            acfg = (messaging.load_config().get("auto_reply") or {})
            return str(ctx.conversation_id or "") in set(acfg.get("assistant_chats") or [])
        except Exception:
            return False
    return False


def _with_extra_system_note(messages: list[dict], note: str) -> list[dict]:
    if not str(note or "").strip():
        return list(messages or [])
    out = [dict(m) for m in messages if isinstance(m, dict)]
    if out and out[0].get("role") == "system":
        out[0]["content"] = str(out[0].get("content") or "") + "\n\n" + note
        return out
    return [{"role": "system", "content": note}] + out


def _goal_context_note(messages: list[dict], ctx: agent_tools.AgentContext,
                       allowed_names: tuple[str, ...] | None) -> str:
    if allowed_names is not None and "note_goal_surfaced" not in set(allowed_names):
        return ""
    if not _private_goal_context_allowed(ctx):
        return ""
    text = ctx.turn_text or _latest_user_text(messages)
    try:
        from app import goals
        selected = goals.select_for_turn(
            text, private_context=True, now=ctx.now or None, limit=3)
        return goals.render_context(selected)
    except Exception:
        return ""


def _runtime_context_note(ctx: agent_tools.AgentContext) -> str:
    now = str(ctx.now or "").strip()
    if not now:
        try:
            now = datetime.now().astimezone().isoformat(timespec="seconds")
        except Exception:
            now = ""
    if not now:
        return ""
    return (
        "## Runtime Context\n"
        "Current local date/time: %s. For calendar tools, preserve the user's "
        "natural relative date when possible, or use an exact date only when it "
        "follows from this current date/time." % now
    )


def run_agent_turn(conversation: list[dict], model: str,
                   tool_policy: ToolPolicy | None = None,
                   interface_context: InterfaceContext | None = None,
                   cancellation_token=None,
                   ollama_url: str = "http://127.0.0.1:11434",
                   options: dict | None = None,
                   registry: agent_tools.ToolRegistry | None = None,
                   chat_fn=None, think: bool = True) -> AgentRun:
    """Reusable agent turn for all interfaces.

    The interface layer supplies conversation state and an allowlist policy.
    Qwen still decides which visible tool, if any, to call.
    """
    policy = tool_policy or ToolPolicy()
    icontext = interface_context or InterfaceContext()
    ctx = agent_tools.AgentContext(
        actor=icontext.actor or icontext.interface or "assistant",
        allow_private=policy.allow_private,
        allow_local_write=policy.allow_local_write,
        allow_external_write=policy.allow_external_write,
        allow_privileged=policy.allow_privileged,
        audit_id=icontext.audit_id,
        interface=icontext.interface,
        conversation_id=icontext.conversation_id,
        turn_text=icontext.turn_text or _latest_user_text(conversation),
        recent_text=icontext.recent_text,
        now=icontext.now,
        sender=icontext.sender,
        attachments=icontext.attachments,
        media_provider_config=icontext.media_provider_config,
        vision_model=icontext.vision_model,
        media_transcriber=icontext.media_transcriber,
        media_image_inspector=icontext.media_image_inspector,
        owner_authored=bool(icontext.owner_authored),
        owner_recent_text=icontext.owner_recent_text,
    )
    return run_agent(
        conversation,
        model,
        ollama_url=ollama_url,
        options=options,
        ctx=ctx,
        registry=registry,
        max_tool_steps=policy.max_tool_steps,
        max_model_calls=policy.max_model_calls,
        model_timeout_seconds=policy.model_timeout_seconds,
        max_turn_seconds=policy.max_turn_seconds,
        chat_fn=chat_fn,
        tool_names=policy.allowed_tools,
        cancellation_token=cancellation_token,
        interface_context=icontext,
        think=think,
    )


# Same runner, same lifetime as the messaging lane (app/messaging.py
# _ollama_chat). A shorter value here does not only affect this agent: it
# reschedules the eviction of the model every other lane is using.
KEEP_ALIVE = "2h"


def _snapshot_ollama(ollama_url):
    try:
        from app import telemetry
        return telemetry.snapshot_ollama(ollama_url)
    except Exception:
        return None


def _record_llm(purpose, payload, seconds, body=None, error="",
                ps_before=None, ps_after=None):
    """Put this agent call in the turn's telemetry trace, best effort.

    Same shape as the messaging lane's records, so one report covers both and
    "where did the 93 seconds go" has an answer that includes the agent.
    """
    try:
        from app import telemetry
        telemetry.record_llm(purpose, str((payload or {}).get("model") or ""),
                             seconds,
                             body=body if isinstance(body, dict) else None,
                             options=(payload or {}).get("options"),
                             messages=(payload or {}).get("messages"),
                             error=error,
                             ps_before=ps_before,
                             ps_after=ps_after)
    except Exception:
        pass


def run_agent(messages: list[dict], model: str, ollama_url: str = "http://127.0.0.1:11434",
              options: dict | None = None, ctx: agent_tools.AgentContext | None = None,
              registry: agent_tools.ToolRegistry | None = None,
              max_tool_steps: int = MAX_TOOL_STEPS,
              max_model_calls: int = MAX_MODEL_CALLS,
              model_timeout_seconds: float = DEFAULT_MODEL_TIMEOUT_SECONDS,
              max_turn_seconds: float = DEFAULT_TURN_TIMEOUT_SECONDS,
              chat_fn=None,
              tool_names: tuple[str, ...] | list[str] | None = None,
              cancellation_token=None,
              interface_context: InterfaceContext | None = None,
              think: bool = True) -> AgentRun:
    """Let Qwen decide whether to call tools, execute them, and continue.

    `chat_fn` is injectable for deterministic tests.  It receives a full Ollama
    payload and returns the decoded response dict.
    """
    started = time.time()
    icontext = interface_context or InterfaceContext(
        interface=(ctx.interface if ctx and ctx.interface else "assistant"),
        actor=(ctx.actor if ctx else "assistant"),
        conversation_id=(ctx.conversation_id if ctx else ""),
        audit_id=(ctx.audit_id if ctx else ""),
        turn_text=(ctx.turn_text if ctx else ""),
        now=(ctx.now if ctx else ""),
        sender=(ctx.sender if ctx else ""),
        attachments=(ctx.attachments if ctx else ()),
        media_provider_config=(ctx.media_provider_config if ctx else None),
        vision_model=(ctx.vision_model if ctx else ""),
        media_transcriber=(ctx.media_transcriber if ctx else None),
        media_image_inspector=(ctx.media_image_inspector if ctx else None),
        owner_authored=bool(ctx.owner_authored if ctx else False),
    )
    trace_id = (ctx.audit_id if ctx and ctx.audit_id else
                icontext.audit_id or "agent-" + uuid.uuid4().hex[:16])
    ctx = ctx or agent_tools.AgentContext(audit_id=trace_id)
    try:
        agent_tools.pulse_set("listening", icontext.interface or "")
    except Exception:
        pass
    ctx = replace(
        ctx,
        audit_id=ctx.audit_id or trace_id,
        interface=ctx.interface or icontext.interface,
        conversation_id=ctx.conversation_id or icontext.conversation_id,
        turn_text=ctx.turn_text or icontext.turn_text or _latest_user_text(messages),
        now=ctx.now or icontext.now,
        sender=ctx.sender or icontext.sender,
        owner_authored=bool(ctx.owner_authored or icontext.owner_authored),
    )
    reg = registry or agent_tools.default_registry()
    opts = dict(options or {})
    opts.setdefault("temperature", 0.2)
    if think:
        # reasoning is spent BEFORE the answer starts; give it headroom once, up
        # front, so the options key set stays stable for the whole turn (a key
        # that appears mid-loop is a model reload -- see the note further down).
        opts["num_predict"] = max(int(opts.get("num_predict") or 0), 3500)
    widened_once = False        # one cut-answer retry per turn, see below
    stall_nudge_used = False    # one "you read code, now act" nudge per turn
    stall_nudge_answer = ""     # the reply the nudge set aside, kept in case
    native_tools_broke = False
    model_timeout = _bounded_float(
        model_timeout_seconds, DEFAULT_MODEL_TIMEOUT_SECONDS, 5.0, 900.0)
    turn_timeout = _bounded_float(
        max_turn_seconds, DEFAULT_TURN_TIMEOUT_SECONDS, 0.0, 3600.0)
    deadline = started + turn_timeout if turn_timeout > 0 else None
    allowed_names = None if tool_names is None else tuple(str(n) for n in tool_names)
    base_msgs = list(messages or [])
    runtime_note = _runtime_context_note(ctx)
    if runtime_note:
        base_msgs = _with_extra_system_note(base_msgs, runtime_note)
    goal_note = _goal_context_note(base_msgs, ctx, allowed_names)
    if goal_note:
        base_msgs = _with_extra_system_note(base_msgs, goal_note)
    msgs = _with_agent_guidance(base_msgs)
    tool_results = []
    model_calls = 0
    tool_steps = 0
    loop_limit = False
    fallback_used = False
    final_raw = None
    ledger = TurnToolLedger()
    empty_model_replies = 0
    research_recovery_pending = False
    research_recovery_used = False
    _trace(trace_id, "start",
           interface=icontext.interface or ctx.interface,
            actor=ctx.actor,
            conversation_id=icontext.conversation_id or ctx.conversation_id,
           allowed_tools=list(allowed_names) if allowed_names is not None else "all",
           model_timeout_seconds=model_timeout,
           max_turn_seconds=turn_timeout)

    window = {"prompt_tokens": 0, "prompt_chars": 0}

    def call_model(payload, purpose="model"):
        payload, fitted = _fit_to_window(payload, window)
        if fitted:
            _trace(trace_id, "loop_control", reason="context_budget_trim", **fitted)
        # Up to two trimmed retries when Ollama still refuses the request as too
        # long. Retrying natively keeps the tools; the structured fallback the
        # caller would otherwise take is for the parser crash, not for length.
        for attempt in range(3):
            timeout = _remaining_timeout(deadline, model_timeout)
            if timeout is not None and timeout <= 0:
                raise TimeoutError("agent turn exceeded %.1fs" % turn_timeout)
            _trace(trace_id, "model_request",
                   request_index=model_calls + 1,
                   purpose=purpose,
                   timeout_seconds=round(timeout, 3) if timeout is not None else None,
                   message_count=len(payload.get("messages") or []),
                   prompt_chars=_payload_message_chars(payload),
                   tool_names=_payload_tool_names(payload),
                   num_predict=(payload.get("options") or {}).get("num_predict"),
                   format=bool(payload.get("format")))
            _started = time.time()
            ps_before = None if chat_fn is not None else _snapshot_ollama(ollama_url)
            try:
                if chat_fn is not None:
                    raw = chat_fn(payload)
                else:
                    raw = _ollama_chat(ollama_url, payload, timeout_seconds=timeout)
            except Exception as exc:
                ps_after = None if chat_fn is not None else _snapshot_ollama(ollama_url)
                _record_llm("agent:%s" % purpose, payload, time.time() - _started,
                            error=str(exc)[:120],
                            ps_before=ps_before,
                            ps_after=ps_after)
                smaller = (_shrink_for_retry(payload)
                           if attempt < 2 and _looks_like_context_overflow(exc) else None)
                if smaller is None:
                    raise
                _trace(trace_id, "loop_control", reason="context_overflow_retry",
                       attempt=attempt + 1,
                       from_chars=_payload_message_chars(payload),
                       to_chars=_payload_message_chars(smaller))
                payload = smaller
                continue
            ps_after = None if chat_fn is not None else _snapshot_ollama(ollama_url)
            _record_llm("agent:%s" % purpose, payload, time.time() - _started,
                        body=raw, ps_before=ps_before, ps_after=ps_after)
            prompt_tokens = raw.get("prompt_eval_count") if isinstance(raw, dict) else None
            if (payload.get("tools") and isinstance(prompt_tokens, int)
                    and prompt_tokens > 0):
                window["prompt_tokens"] = prompt_tokens
                window["prompt_chars"] = _payload_message_chars(payload)
            return raw
        # every attempt above either returns or raises
        raise RuntimeError("model call retries exhausted")

    def _is_status_update(content):
        """Ask whether the reply only promises more work later. A check that
        fails means no nudge: this is a quality nudge, not a safety gate."""
        payload = {"model": model, "stream": False, "think": False,
                   "format": _STALL_CHECK_SCHEMA,
                   "options": dict(opts, temperature=0, num_predict=64),
                   "messages": [
                       {"role": "system", "content": _STALL_CHECK_SYS},
                       {"role": "user", "content": "HIS MESSAGE:\n%s\n\nTHE REPLY:\n%s" % (
                           str(ctx.turn_text or "")[:1500], str(content or "")[:4000])}]}
        try:
            raw = call_model(payload, purpose="stall_check")
            said = json.loads(str(((raw or {}).get("message") or {}).get("content") or "{}"))
            return isinstance(said, dict) and said.get("status_update") is True
        except Exception as exc:
            _trace(trace_id, "loop_control", reason="stall_check_failed",
                   error=str(exc)[:120])
            return False

    while model_calls < int(max_model_calls):
        if _deadline_expired(deadline):
            text = _model_error_text(
                "turn_timeout", "agent turn exceeded %.1fs" % turn_timeout, tool_results)
            _trace(trace_id, "final", model_calls=model_calls,
                   tool_steps=tool_steps, loop_limit_reached=True,
                   seconds=time.time() - started, reason="turn_timeout",
                   content_preview=_safe_text_preview(text))
            return AgentRun(text, msgs, tool_results, trace_id, True,
                            model_calls, tool_steps, time.time() - started,
                            fallback_used=fallback_used, final_raw=final_raw,
                            interface=icontext.interface or ctx.interface,
                            conversation_id=icontext.conversation_id or ctx.conversation_id)
        if _is_cancelled(cancellation_token):
            _trace(trace_id, "cancelled", model_calls=model_calls,
                   tool_steps=tool_steps, seconds=time.time() - started)
            return AgentRun("", msgs, tool_results, trace_id, False, model_calls,
                            tool_steps, time.time() - started,
                            fallback_used=fallback_used, final_raw=final_raw,
                            cancelled=True,
                            interface=icontext.interface or ctx.interface,
                            conversation_id=icontext.conversation_id or ctx.conversation_id)
        remaining = max(0, int(max_tool_steps) - tool_steps)
        force_final = ledger.should_force_final()
        tools = reg.ollama_tools(ctx, names=allowed_names) if remaining > 0 else []
        msgs = _compact_history(msgs)
        # Trim the HISTORY to the window, not only the request built from it.
        # The force_final / budget-exhausted variants below flatten every tool
        # result into one user message, which compaction never touches: on the
        # first run after the window fix a turn stuck re-reading one range
        # climbed to 32,698 of 32,768 tokens with nothing left it could trim.
        _fitted, _fit_info = _fit_to_window({"messages": msgs, "options": opts}, window)
        if _fit_info:
            msgs = _fitted["messages"]
            _trace(trace_id, "loop_control", reason="context_budget_trim",
                   where="history", **_fit_info)
        payload = {
            "model": model,
            "messages": msgs,
            "stream": False,
            "think": bool(think),
            "keep_alive": KEEP_ALIVE,
            "options": opts,
        }
        # once the native tool-call parser has crashed this turn, sending
        # `tools` again crashes it identically -- `_structured_fallback` becomes
        # the tool path for the rest of the turn (it returns the same
        # {message:{content,tool_calls}} shape, so everything downstream is
        # unchanged).
        _use_structured = bool(native_tools_broke and tools)
        if tools and not _use_structured:
            payload["tools"] = tools
        if force_final:
            payload["messages"] = _flatten_tool_turns(msgs) + [{
                "role": "system",
                "content": (
                    "You are repeating a tool call that already returned a "
                    "result, so repeating it will not help. Either try a "
                    "different approach, answer from the evidence you already "
                    f"have, or ask {OWNER} the one question that would unblock "
                    "you. Asking a direct question is a perfectly good way to "
                    "end the turn."
                ),
            }]
            _trace(trace_id, "loop_control",
                   reason="duplicate_tool_loop", duplicate_calls=ledger.duplicates,
                   reused_results=ledger.reused)
        elif remaining <= 0:
            payload["messages"] = _flatten_tool_turns(msgs) + [{
                "role": "system",
                "content": "Tool budget is exhausted. Answer from the evidence already observed; do not ask for more tools.",
            }]
        try:
            call_started = time.time()
            try:
                agent_tools.pulse_set("thinking" if tool_steps == 0 else "reasoning",
                                      icontext.interface or "")
            except Exception:
                pass
            if _use_structured:
                fallback_used = True
                raw = _structured_fallback(call_model, model, msgs, opts, reg,
                                           ctx, remaining, allowed_names)
            else:
                raw = call_model(payload)
        except Exception as exc:
            _trace(trace_id, "model_error", request_index=model_calls + 1,
                   error_type=type(exc).__name__,
                   message=str(exc)[:500],
                   seconds=time.time() - call_started)
            if _looks_like_toolcall_parser_crash(exc):
                native_tools_broke = True
                _trace(trace_id, "loop_control",
                       reason="native_toolcall_parser_dead")
            if tools:
                fallback_used = True
                try:
                    raw = _structured_fallback(call_model, model, msgs, opts, reg,
                                               ctx, remaining, allowed_names)
                except Exception as fallback_exc:
                    _trace(trace_id, "model_error",
                           request_index=model_calls + 1,
                           purpose="structured_fallback",
                           error_type=type(fallback_exc).__name__,
                           message=str(fallback_exc)[:500])
                    text = _model_error_text(type(fallback_exc).__name__, fallback_exc,
                                             tool_results)
                    _trace(trace_id, "final", model_calls=model_calls,
                           tool_steps=tool_steps, loop_limit_reached=True,
                           seconds=time.time() - started, reason="model_error",
                           content_preview=_safe_text_preview(text))
                    return AgentRun(text, msgs, tool_results, trace_id, True,
                                    model_calls, tool_steps, time.time() - started,
                                    fallback_used=True, final_raw=final_raw,
                                    interface=icontext.interface or ctx.interface,
                                    conversation_id=icontext.conversation_id or ctx.conversation_id)
            else:
                text = _model_error_text(type(exc).__name__, exc, tool_results)
                _trace(trace_id, "final", model_calls=model_calls,
                       tool_steps=tool_steps, loop_limit_reached=True,
                       seconds=time.time() - started, reason="model_error",
                       content_preview=_safe_text_preview(text))
                return AgentRun(text, msgs, tool_results, trace_id, True,
                                model_calls, tool_steps, time.time() - started,
                                fallback_used=fallback_used, final_raw=final_raw,
                                interface=icontext.interface or ctx.interface,
                                conversation_id=icontext.conversation_id or ctx.conversation_id)
        model_latency = time.time() - call_started
        model_calls += 1
        final_raw = raw
        msg = raw.get("message") if isinstance(raw, dict) else {}
        if not isinstance(msg, dict):
            msg = {}
        calls = _tool_calls_from_message(msg)
        content = _clean_content(msg.get("content") or "")
        _trace(trace_id, "model_call", model_calls=model_calls,
               tool_calls=len(calls), final=not bool(calls),
               content_chars=len(content), seconds=model_latency,
               content_preview=_safe_text_preview(content),
               **_raw_diagnostics(raw))

        if not calls:
            if content:
                empty_model_replies = 0
                if (_done_reason(raw) == "length" and content
                        and not widened_once
                        and not re.search(r"[.!?…]\s*$", content.strip())):
                    # NB the flag lives outside `opts`: an unknown key in the
                    # options dict changes the runner Ollama keys on, which is
                    # a model reload for nothing.
                    widened_once = True
                    opts = dict(opts)
                    _had = int(opts.get("num_predict") or 500)
                    opts["num_predict"] = max(_had, min(2 * _had, 1600))
                    msgs.append({"role": "system", "content": (
                        "Your previous attempt at this answer ran out of room "
                        "before it finished, so it was not sent. Write the "
                        "answer again from the start, short and complete: plain "
                        "sentences, and quote no code or long text.")})
                    _trace(trace_id, "loop_control", reason="answer_cut_retry",
                           retry_num_predict=opts["num_predict"])
                    continue
                if (not stall_nudge_used
                        and remaining > 0
                        and allowed_names is not None
                        and (_CODE_ACT_TOOLS & set(allowed_names))
                        and any(e.tool in _CODE_READ_TOOLS for e in ledger.entries)
                        and not any(e.tool in _CODE_ACT_TOOLS for e in ledger.entries)
                        and _is_status_update(content)):
                    stall_nudge_used = True
                    stall_nudge_answer = content
                    msgs.append({"role": "assistant", "content": content})
                    msgs.append({"role": "system", "content": (
                        "This turn is your only execution window and it is not "
                        "over. You have read code but not acted, and the message "
                        "above is a status update, not a concrete plan. Do NOT "
                        "send 'I'll check X next', 'once it's clearer', or 'I'll "
                        "add it when it makes sense'. Choose one:\n"
                        "- if he asked you to MAKE or CREATE something NEW (a "
                        "new page, screen, dashboard, tool): reply with the "
                        "concrete plan -- what you will build, what data it "
                        "shows, the file -- and ask him to confirm; when he "
                        "confirms you call antons_build. Do not call it yet, "
                        "but do give him a real plan to say yes to, not 'shall "
                        "I look into it'.\n"
                        "- if this is a fix to existing code and he already "
                        "asked for it, call antons_propose_fix with one concrete "
                        "sentence (it re-reads the file itself), or\n"
                        "- make the ONE more antons_read_code / grep call you "
                        "still actually need, then do one of the above, or\n"
                        "- if you genuinely cannot proceed, state the exact "
                        "missing fact that blocks you.\n"
                        "Only if the message above already fully answers a "
                        "question he asked and there is no code change to make: "
                        "send it again unchanged."
                    )})
                    _trace(trace_id, "loop_control", reason="code_stall_nudge",
                           tool_steps=tool_steps, model_calls=model_calls,
                           content_preview=_safe_text_preview(content))
                    continue
                _trace(trace_id, "final", model_calls=model_calls,
                       tool_steps=tool_steps, seconds=time.time() - started,
                       done_reason=_done_reason(raw),
                       content_preview=_safe_text_preview(content))
                try:
                    agent_tools.pulse_set("speaking", icontext.interface or "")
                except Exception:
                    pass
                return AgentRun(content, msgs, tool_results, trace_id, loop_limit,
                                model_calls, tool_steps, time.time() - started,
                                fallback_used=fallback_used, final_raw=raw,
                                interface=icontext.interface or ctx.interface,
                                conversation_id=icontext.conversation_id or ctx.conversation_id)
            empty_model_replies += 1
            empty_due_to_length = _raw_stopped_for_length(raw, opts)
            wider = _thinking_retry_predict(raw, opts) if think else 0
            if wider:
                previous_predict = int(opts.get("num_predict") or 0)
                opts = dict(opts)
                opts["num_predict"] = wider
                _trace(trace_id, "loop_control", reason="thinking_cut_retry",
                       previous_num_predict=previous_predict,
                       retry_num_predict=wider,
                       thinking_chars=len(_message_thinking(raw)),
                       empty_model_replies=empty_model_replies,
                       model_calls=model_calls)
                continue
            if empty_due_to_length and tools:
                old_predict = int(opts.get("num_predict") or 0)
                if _can_research_recover(ctx, allowed_names, reg, tool_results, remaining):
                    research_recovery_pending = True
                if old_predict < 256:
                    opts["num_predict"] = 256
                    msgs.append({
                        "role": "system",
                        "content": (
                            "The previous assistant turn reached the generation "
                            "length limit before emitting visible text or a tool "
                            "call. On the next turn, emit exactly one appropriate "
                            "tool call if evidence is needed; otherwise answer "
                            "concisely. Do not spend the whole response on hidden "
                            "reasoning. If this is a broad research/comparison "
                            "request, call the research tool immediately."
                        ),
                    })
                    _trace(trace_id, "loop_control",
                           reason="empty_length_retry",
                           previous_num_predict=old_predict,
                           retry_num_predict=opts["num_predict"],
                           empty_model_replies=empty_model_replies,
                           model_calls=model_calls)
                    continue
            if (research_recovery_pending and not research_recovery_used
                    and _can_research_recover(
                        ctx, allowed_names, reg, tool_results, remaining)):
                call = _research_recovery_call(ctx)
                msgs.append(_assistant_tool_call_message([call], ""))
                _trace(trace_id, "recovery",
                       reason="research_empty_length",
                       tool="research",
                       safe_args=_safe_args(call.get("arguments") or {}))
                results = _execute_calls(reg, [call], ctx, allowed_names, ledger,
                                         cancellation_token)
                result = results[0]
                research_recovery_used = True
                empty_model_replies = 0
                tool_steps += 1
                tool_results.append(result)
                msgs.append({
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "name": call["name"],
                    "content": json.dumps(result, ensure_ascii=False, default=str),
                })
                _trace(trace_id, "tool", step=tool_steps, tool=call["name"],
                       ok=bool(result.get("ok")), error_type=result.get("error_type"),
                       seconds=result.get("seconds"),
                       safe_args=_safe_args(call.get("arguments") or {}),
                       result_preview=_tool_result_preview(result),
                       result_id=result.get("result_id"),
                       executed=result.get("executed") is not False,
                       reused=bool(result.get("reused")),
                       duplicate_call=bool(result.get("duplicate_call")),
                       duplicate_type=result.get("duplicate_type"),
                       previous_result_id=result.get("previous_result_id"),
                       result_count=result.get("result_count"),
                       source_count=result.get("source_count"),
                       coverage=result.get("coverage"),
                       truncated=bool(result.get("truncated")))
                msgs.append({
                    "role": "system",
                    "content": (
                        "A research tool result is now available because the "
                        "model returned no visible content before calling a "
                        "tool. Answer from that observed result now. Do not ask "
                        "the user to repeat the research request."
                    ),
                })
                _trace(trace_id, "continuation",
                       reason="research_recovery_result_appended",
                       message_count=len(msgs),
                       tool_steps=tool_steps,
                       remaining_tool_steps=max(0, int(max_tool_steps) - tool_steps))
                continue
            if empty_model_replies >= 2 and stall_nudge_answer:
                _trace(trace_id, "final", model_calls=model_calls,
                       tool_steps=tool_steps, loop_limit_reached=False,
                       seconds=time.time() - started,
                       reason="empty_after_nudge_kept_answer",
                       content_preview=_safe_text_preview(stall_nudge_answer))
                return AgentRun(stall_nudge_answer, msgs, tool_results, trace_id, False,
                                model_calls, tool_steps, time.time() - started,
                                fallback_used=True, final_raw=final_raw,
                                interface=icontext.interface or ctx.interface,
                                conversation_id=icontext.conversation_id or ctx.conversation_id)
            if empty_model_replies >= 2:
                recovered_text = _research_recovery_answer(ctx, tool_results)
                if recovered_text:
                    fallback_used = True
                    _trace(trace_id, "final", model_calls=model_calls,
                           tool_steps=tool_steps, loop_limit_reached=False,
                           seconds=time.time() - started,
                           reason="research_recovery_summary",
                           content_preview=_safe_text_preview(recovered_text))
                    return AgentRun(recovered_text, msgs, tool_results, trace_id, False,
                                    model_calls, tool_steps, time.time() - started,
                                    fallback_used=True, final_raw=final_raw,
                                    interface=icontext.interface or ctx.interface,
                                    conversation_id=icontext.conversation_id or ctx.conversation_id)
                loop_limit = True
                _trace(trace_id, "loop_control",
                       reason="empty_model_responses",
                       empty_model_replies=empty_model_replies,
                       model_calls=model_calls)
                text = (
                    "I could not get a usable response from the model. "
                    "No reliable answer was produced."
                )
                _trace(trace_id, "final", model_calls=model_calls,
                       tool_steps=tool_steps, loop_limit_reached=True,
                       seconds=time.time() - started,
                       reason="empty_model_responses",
                       content_preview=_safe_text_preview(text))
                return AgentRun(text, msgs, tool_results, trace_id, True,
                                model_calls, tool_steps, time.time() - started,
                                fallback_used=fallback_used, final_raw=final_raw,
                                interface=icontext.interface or ctx.interface,
                                conversation_id=icontext.conversation_id or ctx.conversation_id)
            if remaining <= 0:
                break
            msgs.append({
                "role": "system",
                "content": (
                    "The previous assistant turn was empty. On the next turn, "
                    "either answer directly from the conversation or call one "
                    "appropriate tool. Use research for broad multi-source "
                    "public comparisons or investigations; use web_search for "
                    "one current fact or source discovery."
                ),
            })
            _trace(trace_id, "loop_control",
                   reason="empty_model_response_retry",
                   empty_model_replies=empty_model_replies,
                   model_calls=model_calls)
            continue

        empty_model_replies = 0
        if remaining <= 0:
            loop_limit = True
            break

        calls = calls[:remaining]
        msgs.append(_assistant_tool_call_message(calls, content))
        if _is_cancelled(cancellation_token):
            _trace(trace_id, "cancelled", model_calls=model_calls,
                   tool_steps=tool_steps, seconds=time.time() - started)
            return AgentRun("", msgs, tool_results, trace_id, False, model_calls,
                            tool_steps, time.time() - started,
                            fallback_used=fallback_used, final_raw=final_raw,
                            cancelled=True,
                            interface=icontext.interface or ctx.interface,
                            conversation_id=icontext.conversation_id or ctx.conversation_id)
        _trace(trace_id, "tool_batch_start",
               count=len(calls),
               tools=[c.get("name") for c in calls],
               remaining_tool_steps=remaining)
        batch_started = time.time()
        try:
            agent_tools.pulse_set("tool", (calls[0] or {}).get("name") or "")
        except Exception:
            pass
        results = _execute_calls(reg, calls, ctx, allowed_names, ledger,
                                 cancellation_token)
        _trace(trace_id, "tool_batch_done",
               count=len(results),
               seconds=time.time() - batch_started)
        for call, result in zip(calls, results):
            # every executed call costs a step, reused or not: the budget is
            # what bounds a stuck turn, so a repeat must still drain it
            tool_steps += 1
            tool_results.append(result)
            msgs.append({
                "role": "tool",
                "tool_call_id": call["id"],
                "name": call["name"],
                "content": json.dumps(result, ensure_ascii=False, default=str),
            })
            _trace(trace_id, "tool", step=tool_steps, tool=call["name"],
                   ok=bool(result.get("ok")), error_type=result.get("error_type"),
                   seconds=result.get("seconds"),
                   safe_args=_safe_args(call.get("arguments") or {}),
                   result_preview=_tool_result_preview(result),
                   result_id=result.get("result_id"),
                   executed=result.get("executed") is not False,
                   reused=bool(result.get("reused")),
                   duplicate_call=bool(result.get("duplicate_call")),
                   duplicate_type=result.get("duplicate_type"),
                   previous_result_id=result.get("previous_result_id"),
                   result_count=result.get("result_count"),
                   source_count=result.get("source_count"),
                   coverage=result.get("coverage"),
                   truncated=bool(result.get("truncated")))
        _trace(trace_id, "continuation",
               reason="tool_results_appended",
               message_count=len(msgs),
               tool_steps=tool_steps,
               remaining_tool_steps=max(0, int(max_tool_steps) - tool_steps))

    loop_limit = True
    final_payload = {
        "model": model,
        "messages": _flatten_tool_turns(msgs) + [{
            "role": "system",
            "content": "No more tool calls are allowed. Give the best concise answer from the observations. If the evidence is insufficient, say exactly what failed.",
        }],
        "stream": False,
        "think": bool(think),
        "keep_alive": KEEP_ALIVE,
        "options": opts,
    }
    try:
        raw = call_model(final_payload, purpose="final")
        model_calls += 1
        final_raw = raw
        text = _clean_content(((raw.get("message") or {}).get("content") or ""))
    except Exception as exc:
        _trace(trace_id, "model_error", request_index=model_calls + 1,
               purpose="final", error_type=type(exc).__name__,
               message=str(exc)[:500])
        text = ""
    if not text:
        text = _evidence_answer(msgs, tool_results, model, opts, call_model,
                                trace_id, think=think)
        if text:
            model_calls += 1
    if not text:
        _spent = (model_calls >= int(max_model_calls)
                  or tool_steps >= int(max_tool_steps))
        text = (_research_recovery_answer(ctx, tool_results)
                or ("I reached the tool budget and do not have enough evidence "
                    "to answer safely."
                    if _spent else
                    "The model returned nothing usable on the last step "
                    "(%d/%d model calls, %d/%d tool steps used) -- this is not "
                    "a budget limit." % (model_calls, int(max_model_calls),
                                         tool_steps, int(max_tool_steps))))
    _trace(trace_id, "final", model_calls=model_calls, tool_steps=tool_steps,
           loop_limit_reached=True, seconds=time.time() - started,
           done_reason=_done_reason(final_raw),
           content_preview=_safe_text_preview(text))
    return AgentRun(text, msgs, tool_results, trace_id, True, model_calls,
                    tool_steps, time.time() - started,
                    fallback_used=fallback_used, final_raw=final_raw,
                    interface=icontext.interface or ctx.interface,
                    conversation_id=icontext.conversation_id or ctx.conversation_id)


def _execute_calls(reg, calls, ctx, allowed_names=None, ledger=None,
                   cancellation_token=None):
    # Multiple calls in one assistant message are independent by construction.
    # Read-only batches can run together; any side-effect-capable batch stays
    # ordered and sequential.
    allowed = None if allowed_names is None else set(allowed_names)
    if ledger is not None:
        return [_execute_one_call(reg, c, ctx, allowed, ledger, cancellation_token)
                for c in calls]
    tools = [reg.get(c["name"]) if (allowed is None or c["name"] in allowed)
             else None for c in calls]
    parallel = len(calls) > 1 and all(t and t.risk == agent_tools.READ_ONLY for t in tools)
    def execute_one(call):
        if allowed is not None and call["name"] not in allowed:
            return _tool_not_available(call["name"], call.get("arguments") or {})
        return reg.execute(call["name"], call.get("arguments") or {}, ctx)
    if not parallel:
        return [execute_one(c) for c in calls]
    with ThreadPoolExecutor(max_workers=min(4, len(calls))) as ex:
        futs = [ex.submit(execute_one, c) for c in calls]
        return [f.result() for f in futs]


def _execute_one_call(reg, call, ctx, allowed, ledger, cancellation_token=None):
    name = call["name"]
    args = call.get("arguments") or {}
    if allowed is not None and name not in allowed:
        return ledger.record_result(name, args, _tool_not_available(name, args),
                                    executed=False)
    tool = reg.get(name)
    if tool and tool.risk == agent_tools.READ_ONLY:
        duplicate = ledger.find_duplicate(name, args)
        if duplicate is not None:
            return ledger.reuse_result(name, args, duplicate)
    if tool and tool.risk != agent_tools.READ_ONLY:
        duplicate = ledger.find_action_duplicate(name, args)
        if duplicate is not None:
            return ledger.reuse_result(name, args, duplicate)
        if _is_cancelled(cancellation_token):
            return ledger.record_result(
                name, args,
                _action_not_executed(name, args, "stale_turn",
                                     "sensitive action denied because the turn was superseded",
                                     risk=tool.risk),
                executed=False)
    result = reg.execute(name, args, ctx)
    executed = True
    if tool and tool.risk != agent_tools.READ_ONLY and not bool(result.get("ok")):
        executed = False
    return ledger.record_result(name, args, result, executed=executed)


_CONTEXT_OVERFLOW_MARKERS = (
    "no user query found in messages",
    "exceeds the context",
    "context length",
)


def _looks_like_context_overflow(exc) -> bool:
    text = str(exc or "").lower()
    return any(marker in text for marker in _CONTEXT_OVERFLOW_MARKERS)


def _looks_like_toolcall_parser_crash(exc) -> bool:
    """Ollama's tool-call parser (qwen3.5) dies on a JSON-shaped response and
    returns HTTP 500 with `error=EOF`, discarding the generation. It is shape-
    triggered, so it recurs every call this turn -- once we see it, stop sending
    `tools` and drive tool selection through the structured path instead."""
    if _looks_like_context_overflow(exc):
        return False    # a 500 with a stated reason that is not the parser
    try:
        code = getattr(exc, "code", None)
        if code == 500:
            return True
    except Exception:
        pass
    s = str(exc or "").lower()
    return ("http 500" in s or "http error 500" in s or "status 500" in s
            or "tool call parsing failed" in s or "error=eof" in s
            or ("eof" in s and "pars" in s))


def _ollama_chat(ollama_url, payload, timeout_seconds: float | None = None):
    url = str(ollama_url or "http://127.0.0.1:11434").rstrip("/") + "/api/chat"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    timeout = 900 if timeout_seconds is None else max(0.1, float(timeout_seconds))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        # urllib's own message is only "HTTP Error 500: Internal Server Error";
        # the body is where Ollama says why ("no user query found in messages").
        # Re-raise the same type carrying that reason, so the trace, the log and
        # the overflow/parser checks above see it instead of a bare status.
        try:
            raw = exc.read().decode("utf-8", "replace")
        except Exception:
            raw = ""
        try:
            reason = str(json.loads(raw).get("error") or raw)
        except Exception:
            reason = raw
        if not reason.strip():
            raise
        raise urllib.error.HTTPError(exc.url, exc.code, reason[:500], exc.hdrs,
                                     None) from exc


def _bounded_float(value, default: float, minimum: float, maximum: float) -> float:
    try:
        out = float(value)
    except Exception:
        out = float(default)
    if maximum is not None:
        out = min(out, float(maximum))
    if minimum is not None:
        out = max(out, float(minimum))
    return out


def _remaining_timeout(deadline, per_call_timeout: float) -> float | None:
    if deadline is None:
        return float(per_call_timeout)
    return min(float(per_call_timeout), max(0.0, deadline - time.time()))


def _deadline_expired(deadline) -> bool:
    return deadline is not None and time.time() >= deadline


def _payload_message_chars(payload: dict) -> int:
    total = 0
    for msg in payload.get("messages") or []:
        if isinstance(msg, dict):
            total += len(str(msg.get("content") or ""))
    return total


_HISTORY_BUDGET_CHARS = 76000


def _content_chars(msgs) -> int:
    return sum(len(str(m.get("content") or "")) for m in (msgs or [])
               if isinstance(m, dict))


def _elide(msgs, keep_full, assistant_cap, assistant_keep, assistant_prefix):
    out = []
    for i, m in enumerate(msgs):
        if not isinstance(m, dict):
            out.append(m)
            continue
        role = m.get("role")
        content = str(m.get("content") or "")
        if role == "tool" and i not in keep_full and len(content) > 400:
            prefix = content[:200]
            out.append(dict(m, content=prefix + "\n[earlier %s result elided: %d chars total]"
                            % (m.get("name") or "tool", len(content))))
        elif (role == "assistant" and len(content) > assistant_cap
                and i < len(msgs) - assistant_keep):
            out.append(dict(m, content=content[:assistant_prefix] + " ...[elided]"))
        else:
            out.append(m)
    return out


def _compact_history(msgs, budget=_HISTORY_BUDGET_CHARS, aggressive=False):
    """Elide the middle when the running history outgrows the context window.

    `aggressive` is for a budget worked out from the model's own prompt token
    count (see run_agent). That budget means the next request will not fit the
    window at all, and an overflowing request loses the user's turn, so
    fitting beats keeping one result per tool whole: only the newest result
    stays, cut down if it has to be. User and system messages are never touched.
    """
    try:
        total = _content_chars(msgs)
    except Exception:
        return msgs
    if total <= budget:
        return msgs
    tool_idx = [i for i, m in enumerate(msgs)
                if isinstance(m, dict) and m.get("role") == "tool"]
    keep_full = set(tool_idx[-3:])
    seen = {}
    for i in reversed(tool_idx):
        name = msgs[i].get("name") or "tool"
        if name not in seen:
            seen[name] = i
    keep_full.update(seen.values())
    out = _elide(msgs, keep_full, 1200, 4, 400)
    if not aggressive or _content_chars(out) <= budget:
        return out
    out = _elide(out, set(tool_idx[-1:]), 600, 2, 300)
    over = _content_chars(out) - budget
    if over > 0 and tool_idx:
        last = tool_idx[-1]
        content = str(out[last].get("content") or "")
        note = "\n[result cut to fit the context window: %d chars total]" % len(content)
        keep = max(1500, len(content) - over - len(note))
        if keep < len(content):
            out[last] = dict(out[last], content=content[:keep] + note)
    return out


_CONTEXT_SAFETY_TOKENS = 512
_CHARS_PER_TOKEN_ADDED = 2.0    # measured 2.1-2.75; lower is safer for growth
_CHARS_PER_TOKEN_FREED = 3.0    # higher is safer for what a trim gives back
_MIN_HISTORY_CHARS = 8000


def _payload_option(payload, key) -> int:
    try:
        return int(((payload or {}).get("options") or {}).get(key) or 0)
    except (TypeError, ValueError):
        return 0


def _has_user_turn(payload) -> bool:
    return any(isinstance(m, dict) and m.get("role") == "user"
               for m in ((payload or {}).get("messages") or []))


def _fit_to_window(payload: dict, state: dict) -> tuple[dict, dict | None]:
    """(payload, info): this request trimmed to fit the model's window.

    `state` holds prompt_tokens/prompt_chars from the last successful
    tool-bearing call in this turn. With nothing to judge by, or when the
    request already fits, the payload comes back unchanged with info None.
    """
    num_ctx = _payload_option(payload, "num_ctx")
    last_tokens = int((state or {}).get("prompt_tokens") or 0)
    if not num_ctx or not last_tokens:
        return payload, None
    messages = list(payload.get("messages") or [])
    chars = _content_chars(messages)
    reserve = (_payload_option(payload, "num_predict") or 900) + _CONTEXT_SAFETY_TOKENS
    headroom = num_ctx - reserve - last_tokens
    per_token = _CHARS_PER_TOKEN_ADDED if headroom >= 0 else _CHARS_PER_TOKEN_FREED
    budget = int(int(state.get("prompt_chars") or 0) + headroom * per_token)
    if chars <= budget:
        return payload, None
    budget = max(_MIN_HISTORY_CHARS, budget)
    fitted = dict(payload)
    fitted["messages"] = _compact_history(messages, budget=budget, aggressive=True)
    return fitted, {
        "budget_chars": budget, "from_chars": chars,
        "to_chars": _content_chars(fitted["messages"]),
        "last_prompt_tokens": last_tokens, "num_ctx": num_ctx}


def _shrink_for_retry(payload: dict) -> dict | None:
    """A smaller copy of a request Ollama refused as too long, or None."""
    if not _has_user_turn(payload):
        return None     # no user turn was ever in it; trimming cannot add one
    messages = list(payload.get("messages") or [])
    chars = _content_chars(messages)
    smaller = _compact_history(
        messages, budget=max(_MIN_HISTORY_CHARS, int(chars * 0.6)), aggressive=True)
    if _content_chars(smaller) >= chars:
        return None
    out = dict(payload)
    out["messages"] = smaller
    return out


def _payload_tool_names(payload: dict) -> list[str]:
    names = []
    for item in payload.get("tools") or []:
        fn = item.get("function") if isinstance(item, dict) else {}
        if isinstance(fn, dict) and fn.get("name"):
            names.append(str(fn.get("name")))
    return names[:32]


def _done_reason(raw) -> str:
    if isinstance(raw, dict):
        return str(raw.get("done_reason") or "")
    return ""


def _safe_text_preview(value, limit: int = 240) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit]


def _tool_result_preview(result, limit: int = 360) -> str:
    try:
        text = json.dumps(result, ensure_ascii=False, default=str)
    except Exception:
        text = str(result)
    return _safe_text_preview(text, limit)


def _actions_done(tool_results) -> list[str]:
    """The tools this turn ran that changed something and succeeded."""
    done = []
    for row in tool_results or ():
        if (isinstance(row, dict) and row.get("ok") is True
                and row.get("executed") is not False and not row.get("reused")
                and str(row.get("risk") or "") not in ("", "READ_ONLY")):
            name = str(row.get("tool") or "")
            if name and name not in done:
                done.append(name)
    return done


def _model_error_text(error_type: str, exc, tool_results=None) -> str:
    done = _actions_done(tool_results)
    tail = ("Done in this turn before that: %s." % ", ".join(done)) if done \
        else "No action was taken."
    kind = str(error_type or "model_error")
    message = str(exc or "").strip()
    if kind in ("TimeoutError", "timeout", "socket.timeout", "turn_timeout") \
            or isinstance(exc, socket.timeout) or "timed out" in message.lower() \
            or "exceeded" in message.lower():
        return "The local model ran out of time before writing an answer. " + tail
    return "The local model failed before writing an answer. " + tail


def _can_research_recover(ctx, allowed_names, reg, tool_results, remaining: int) -> bool:
    if remaining <= 0:
        return False
    if any(r.get("tool") == "research" for r in tool_results):
        return False
    if allowed_names is not None and "research" not in set(allowed_names):
        return False
    tool = reg.get("research") if reg else None
    if tool is None:
        return False
    ok, _why = agent_tools.authorize(tool, ctx)
    if not ok:
        return False
    return _looks_like_research_request(getattr(ctx, "turn_text", "") or "")


def _looks_like_research_request(text: str) -> bool:
    low = str(text or "").lower()
    return bool(re.search(
        r"\b(research|investigate|compare|comparison|synthesize|tradeoffs?|"
        r"pros and cons|multi-source|deep dive|look into)\b", low))


def _research_recovery_call(ctx) -> dict:
    question = str(getattr(ctx, "turn_text", "") or "").strip()
    if not question:
        question = "Research the user's broad public question."
    return {
        "id": "recovery_research_0",
        "name": "research",
        "arguments": {"question": question[:500], "limit": 8},
    }


def _lookup_evidence(tool_results: list[dict], limit: int = 4) -> str:
    """Findings from ANY read-only lookup that came back, as short lines."""
    lines = []
    for row in tool_results or []:
        if not row.get("ok") or row.get("tool") not in (
                "web_search", "web_read", "classifieds_search",
                "media_library_search", "search_chat_history"):
            continue
        body = row.get("result") if isinstance(row.get("result"), dict) else {}
        items = (body.get("results") or body.get("matches")
                 or body.get("items") or [])
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    continue
                title = str(item.get("title") or item.get("name") or "").strip()
                snip = str(item.get("snippet") or item.get("text")
                           or item.get("description") or "").strip()
                if title or snip:
                    lines.append(("%s - %s" % (title, snip)).strip(" -")[:260])
                if len(lines) >= limit:
                    break
        text = str(body.get("text") or "").strip()
        if text and len(lines) < limit:
            lines.append(text[:260])
        if len(lines) >= limit:
            break
    return "\n".join("- " + ln for ln in lines[:limit])


_EVIDENCE_SYS = (
    "The findings below are what you looked up. Answer the question "
    "directly, in your own voice, keeping the persona and the style rules "
    "from the system prompt above. Write in the language the question is "
    "written in.\n"
    "Do not recite the findings and never mention searching, sources or "
    "tools - just answer as someone who already knows this. Do not invent "
    "specifics that are not in the findings; if something essential is "
    "missing, say so in one short clause and move on."
)


def _evidence_answer(msgs, tool_results, model, opts, call_model,
                     trace_id=None, think: bool = True) -> str:
    """One clean call: the question plus what the lookups found."""
    evidence = _lookup_evidence(tool_results, limit=6)
    question = ""
    for m in reversed(msgs or []):
        if m.get("role") == "user" and str(m.get("content") or "").strip():
            question = str(m["content"]).strip()
            break
    if not evidence or not question:
        return ""
    head = [m for m in (msgs or [])[:1] if m.get("role") == "system"]
    e_opts = dict(opts or {})
    if think:
        e_opts["num_predict"] = max(int(e_opts.get("num_predict") or 0), 3500)
    payload = {
        "model": model,
        "messages": head + [
            {"role": "system", "content": _EVIDENCE_SYS + "\n\n" + evidence},
            {"role": "user", "content": question[:1200]},
        ],
        "stream": False,
        "think": bool(think),
        "keep_alive": KEEP_ALIVE,
        "options": e_opts,
    }
    try:
        raw = call_model(payload, purpose="evidence_answer")
        text = _clean_content(((raw.get("message") or {}).get("content") or ""))
    except Exception as exc:
        _trace(trace_id, "model_error", purpose="evidence_answer",
               error_type=type(exc).__name__, message=str(exc)[:500])
        return ""
    _trace(trace_id, "evidence_answer", used=bool(text),
           sources=len(evidence.splitlines()))
    return text


def _research_recovery_answer(ctx, tool_results: list[dict]) -> str:
    research = next((r for r in tool_results
                     if r.get("tool") == "research" and r.get("ok")), None)
    if not research:
        chunks = []
        for r in reversed(tool_results):
            if not r.get("ok") or r.get("tool") not in ("web_read", "web_search"):
                continue
            body = r.get("result") if isinstance(r.get("result"), dict) else {}
            txt = str(body.get("text") or "").strip()
            if r.get("tool") == "web_search" and not txt:
                for it in (body.get("results") or [])[:3]:
                    txt += "%s: %s\n" % (it.get("title") or "",
                                         it.get("snippet") or it.get("text") or "")
            if len(txt) > 40:
                chunks.append("- %s" % _safe_text_preview(txt, 500))
            if len(chunks) >= 3:
                break
        if not chunks:
            return ""
        return ("I ran out of room to finish cleanly, but here is what the "
                "lookups turned up:\n" + "\n".join(reversed(chunks)))
    result = research.get("result") if isinstance(research.get("result"), dict) else {}
    if not result:
        return ""
    sources = []
    for item in result.get("search") or []:
        title = str(item.get("title") or item.get("url") or "").strip()
        snippet = str(item.get("snippet") or item.get("text") or "").strip()
        if title or snippet:
            sources.append((title, snippet))
        if len(sources) >= 3:
            break
    page_notes = []
    for page in result.get("pages") or []:
        if not isinstance(page, dict):
            continue
        text = str(page.get("text") or page.get("content") or "").strip()
        if text:
            page_notes.append(text[:260])
        if len(page_notes) >= 2:
            break
    if not sources and not page_notes:
        return ""
    lines = ["I found enough research evidence to answer without asking you to repeat it."]
    if sources:
        lines.append("Top signals:")
        for title, snippet in sources:
            bit = snippet or title
            if title and snippet:
                bit = "%s: %s" % (title, snippet)
            lines.append("- " + _safe_text_preview(bit, 220))
    if page_notes:
        lines.append("Read-through notes:")
        for note in page_notes:
            lines.append("- " + _safe_text_preview(note, 220))
    return "\n".join(lines)


def _with_agent_guidance(messages):
    guide = (
        "You are Qwen operating as the local assistant. You may call tools only "
        "when they materially help. Do not call tools for ordinary conversation, "
        "jokes, simple rewriting, or explanations you already know. Prefer "
        "small composable steps: search first, read only promising pages, then "
        "answer from observed results. Use the recent conversation first: resolve "
        "pronouns, ellipses, follow-ups, and pending context from messages already "
        "in the prompt before asking the user to repeat themselves. When the "
        "answer or action target depends on older prior chat, call "
        "search_chat_history before asking for clarification. Before any extra tool call, ask whether "
        "the existing tool results already answer the request. Do not repeat the "
        "same query or read the same source again unless the prior result was "
        "empty, stale, failed, or clearly irrelevant. Use web_search for one "
        "current public fact or candidate source, web_read for a specific public "
        "URL, research for broad multi-source comparisons or investigations, "
        "especially when the user asks to research, investigate, compare, or "
        "synthesize tradeoffs across sources, "
        "search_personal_memory for saved user facts/preferences, "
        "search_chat_history for actual prior messages, search_knowledge_base "
        "for indexed local documents, search_files only to discover filenames, "
        "inspect_file only after a concrete path is known, contact_lookup to "
        "resolve trusted recipients, calendar_search to read calendar state, "
        "calendar_create when he asks for a calendar entry or a reminder at a time, and "
        "send_message only for an explicitly requested real message. Use "
        "search_goals/list_goals when the user asks about saved goals or "
        "priorities. Use add_goal only for an explicit request to create a "
        "persistent goal, never for reminders, alarms, calendar items, outgoing "
        "messages, or vague maybes. Use update_goal/complete_goal/defer_goal/"
        "cancel_goal only when the current turn clearly authorizes that goal "
        "change. The server "
        "will verify write authorization and trusted recipients. Tool results have "
        "turn-local result_id values; reuse those results instead of calling the "
        "same tool again. Tool errors are evidence; never pretend a failed tool "
        "succeeded. Never claim that an action was completed, started, queued, "
        "fixed, saved, sent, or scheduled unless a successful tool result in "
        "this turn proves it. Never promise to do work later or 'soon': this "
        "turn is the only execution window. If the requested capability is not "
        "among the visible tools, say plainly that you cannot perform it from "
        "this chat, while still offering any useful analysis you can do now."
    )
    out = [m for m in messages if isinstance(m, dict)]
    if out and out[0].get("role") == "system":
        first = dict(out[0])
        first["content"] = str(first.get("content") or "") + "\n\n## Tool Use\n" + guide
        return [first] + out[1:]
    return [{"role": "system", "content": guide}] + out


def _tool_calls_from_message(msg):
    out = []
    for i, call in enumerate(msg.get("tool_calls") or []):
        fn = call.get("function") if isinstance(call, dict) else {}
        if not isinstance(fn, dict):
            continue
        args = fn.get("arguments") or {}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except Exception:
                args = {}
        out.append({
            "id": str(call.get("id") or "call_%d" % i),
            "name": str(fn.get("name") or "").strip(),
            "arguments": args if isinstance(args, dict) else {},
        })
    return [c for c in out if c["name"]]


def _assistant_tool_call_message(calls, content=""):
    return {
        "role": "assistant",
        "content": content or "",
        "tool_calls": [{
            "id": c["id"],
            "function": {"name": c["name"], "arguments": c.get("arguments") or {}},
        } for c in calls],
    }


def _clean_content(text):
    out = str(text or "").strip()
    if "</think>" in out:
        out = out.split("</think>")[-1].strip()
    return out


def _structured_fallback(call_model, model, messages, options, reg, ctx, remaining,
                         allowed_names=None):
    allowed = None if allowed_names is None else set(allowed_names)
    _tools = [t for t in reg.available(ctx)
              if allowed is None or t.name in allowed]
    tool_names = [t.name for t in _tools]

    def _param_hint(t):
        props = (getattr(t, "schema", None) or {}).get("properties") or {}
        req = set((getattr(t, "schema", None) or {}).get("required") or [])
        if not props:
            return "no arguments"
        return ", ".join(
            "%s%s" % (k, "" if k in req else "?")
            + ((":" + str((v or {}).get("type"))) if isinstance(v, dict) and v.get("type") else "")
            for k, v in list(props.items())[:8])

    _catalog = "\n".join(
        "- %s(%s) -- %s" % (t.name, _param_hint(t),
                            " ".join(str(t.description or "").split())[:180])
        for t in _tools[:40])
    prompt = {
        "role": "system",
        "content": (
            "Native tool-calling is unavailable this turn (the model's parser "
            "keeps crashing on JSON-shaped output). Return ONE json object with "
            "either `answer` (a string) OR `tool_calls` (an array of "
            "{\"name\": ..., \"arguments\": {...}}). Prefer a tool call when the "
            "user asked you to DO something and you have not done it yet -- do "
            "not answer with a plan or 'I will now...'; make the call. Tools:\n"
            + _catalog
        ),
    }
    payload = {
        "model": model,
        "messages": [prompt] + _compact_history(list(messages)),
        "stream": False,
        "think": False,
        "format": {
            "type": "object",
            "properties": {
                "answer": {"type": "string"},
                "tool_calls": {"type": "array", "items": {"type": "object"}},
            },
        },
        "options": dict(options or {}, temperature=0),
    }
    raw = call_model(payload)
    content = ((raw.get("message") or {}).get("content") or "").strip()
    try:
        obj = json.loads(content)
    except Exception:
        obj = {"answer": content}
    calls = []
    for i, item in enumerate((obj.get("tool_calls") or [])[:remaining]):
        if isinstance(item, dict) and item.get("name"):
            calls.append({"id": "fallback_%d" % i, "function": {
                "name": item.get("name"),
                "arguments": item.get("arguments") or {},
            }})
    return {"message": {"role": "assistant", "content": obj.get("answer") or "",
                        "tool_calls": calls}}


def _flatten_tool_turns(msgs):
    """Re-express tool_calls/tool turns as ordinary text for a final answer.

        user only                          eval=563  content ok
        user + assistant(tool_calls)+tool  eval=69   content ""
        the same results as a user message eval=335  content ok

    So the model was never refusing and the budget was never the constraint --
    the tool-result turn shape silences it. Every "returned nothing usable on
    the last step" reply traces back to exactly this. The loop still uses real
    tool turns while it is CALLING tools; only the final answer, where no
    further tool call is wanted anyway, is flattened.
    """
    out = []
    pending = []

    def _flush():
        if pending:
            out.append({"role": "user",
                        "content": "Rezultati no rikiem:" + chr(10)
                                   + chr(10).join(pending)})
            del pending[:]

    for m in msgs or []:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        if role == "assistant" and m.get("tool_calls"):
            for call in (m.get("tool_calls") or []):
                fn = call.get("function") if isinstance(call.get("function"), dict) else call
                try:
                    args = json.dumps(fn.get("arguments") or {}, ensure_ascii=False,
                                      default=str)[:400]
                except Exception:
                    args = ""
                pending.append("- izsauca %s(%s)" % (fn.get("name") or "?", args))
            if str(m.get("content") or "").strip():
                pending.append("- piezime: " + str(m["content"])[:400])
            continue
        if role == "tool":
            pending.append("- %s atgrieza: %s"
                           % (m.get("name") or "riks", str(m.get("content") or "")[:2500]))
            continue
        _flush()
        out.append(m)
    _flush()
    return out


def _is_cancelled(token) -> bool:
    if token is None:
        return False
    try:
        if callable(token):
            return bool(token())
        if hasattr(token, "is_cancelled"):
            value = token.is_cancelled
            return bool(value() if callable(value) else value)
        if hasattr(token, "cancelled"):
            value = token.cancelled
            return bool(value() if callable(value) else value)
    except Exception:
        return False
    return False


def _raw_diagnostics(raw) -> dict:
    if not isinstance(raw, dict):
        return {}
    msg = raw.get("message") if isinstance(raw.get("message"), dict) else {}
    out = {}
    for key in ("done_reason", "eval_count", "prompt_eval_count"):
        if key in raw:
            out[key] = raw.get(key)
    if isinstance(msg, dict):
        out["message_keys"] = sorted(str(k) for k in msg.keys())[:12]
        out["raw_tool_calls_present"] = "tool_calls" in msg
        _tk = msg.get("thinking") or msg.get("reasoning") or ""
        if _tk:
            out["thinking_chars"] = len(str(_tk))
            out["thinking_preview"] = _safe_text_preview(str(_tk), 240)
    return out


# A step whose REASONING hit the output cap gets one wider retry, up to this
# many tokens -- never more than half the window, so the prompt keeps the rest.
_THINKING_RETRY_PREDICT = 8192


def _message_thinking(raw) -> str:
    msg = raw.get("message") if isinstance(raw, dict) else None
    if not isinstance(msg, dict):
        return ""
    return str(msg.get("thinking") or msg.get("reasoning") or "")


def _thinking_retry_predict(raw, opts) -> int:
    """The wider num_predict for a step cut off mid-reasoning, else 0.

    0 when the reply was not an empty length stop with thinking in it, or when
    the budget is already at the ceiling -- so a turn widens at most once.
    """
    if not _raw_stopped_for_length(raw, opts) or not _message_thinking(raw).strip():
        return 0
    try:
        old = int((opts or {}).get("num_predict") or 0)
        num_ctx = int((opts or {}).get("num_ctx") or 0)
    except (TypeError, ValueError):
        return 0
    ceiling = (min(_THINKING_RETRY_PREDICT, num_ctx // 2) if num_ctx
               else _THINKING_RETRY_PREDICT)
    return ceiling if ceiling > old else 0


def _raw_stopped_for_length(raw, opts=None) -> bool:
    if not isinstance(raw, dict):
        return False
    if str(raw.get("done_reason") or "").lower() != "length":
        return False
    msg = raw.get("message") if isinstance(raw.get("message"), dict) else {}
    if str(msg.get("content") or "").strip() or (msg.get("tool_calls") or []):
        return False
    try:
        predicted = int((opts or {}).get("num_predict") or 0)
        evaluated = int(raw.get("eval_count") or 0)
    except Exception:
        return True
    return not predicted or evaluated >= max(1, int(predicted * 0.80))


def _action_not_executed(name: str, args: dict | None, error_type: str,
                         message: str, risk: str = "") -> dict:
    return {
        "ok": False,
        "tool": str(name or ""),
        "risk": risk or agent_tools.LOCAL_REVERSIBLE_WRITE,
        "error_type": str(error_type or "action_not_executed"),
        "message": str(message or "")[:500],
        "args": _safe_args(args or {}),
        "action_state": "DENIED",
        "action_id": _turn_action_id(name, args or {}),
    }


@dataclass
class LedgerEntry:
    result_id: str
    tool: str
    args: dict
    normalized_args: dict
    signature: str
    tokens: tuple[str, ...]
    result: dict
    ok: bool
    error_type: str
    coverage: str


class TurnToolLedger:
    def __init__(self):
        self.entries: list[LedgerEntry] = []
        self.by_signature: dict[str, LedgerEntry] = {}
        self.duplicates = 0
        self.reused = 0
        self.semantic_duplicates = 0
        self.exact_duplicates = 0
        self._consecutive_duplicates = 0

    def find_duplicate(self, tool: str, args: dict) -> tuple[LedgerEntry, str, str] | None:
        norm = _normalize_tool_args(tool, args)
        signature = _signature(tool, norm)
        exact = self.by_signature.get(signature)
        if exact and (_entry_reusable(exact) or exact.ok):
            return exact, "exact", "same tool and normalized arguments"
        # a web_read that forces a browser render / a click is a deliberate
        # re-fetch -- the exact signature above already distinguishes it
        if tool == "web_read" and ((args or {}).get("render") or (args or {}).get("click")):
            return None
        research_followup = self._research_followup_duplicate(tool, args, norm)
        if research_followup is not None:
            return research_followup
        read_range_dup = self._read_range_duplicate(tool, norm)
        if read_range_dup is not None:
            return read_range_dup
        for entry in reversed(self.entries):
            if entry.tool != tool or not _entry_reusable(entry):
                continue
            if tool == "web_read" and _same_read_target(entry, args):
                if _material_expansion(tool, entry.args, args, entry):
                    continue
                return entry, "semantic", "same canonical URL was already read"
            if not _semantic_duplicate_tool(tool):
                continue
            if _material_expansion(tool, entry.args, args, entry):
                continue
            if _query_similarity(entry.tokens, _query_tokens_from_args(tool, norm)) >= 0.60:
                return entry, "semantic", "query closely overlaps an earlier useful result"
        return None

    def _read_range_duplicate(self, tool: str, norm: dict) -> tuple[LedgerEntry, str, str] | None:
        """antons_read_code action=read: a new [start,end] that a PRIOR read on
        the same path already covers most of is not new information.
        """
        if tool != "antons_read_code" or norm.get("action") != "read":
            return None
        path = norm.get("path")
        try:
            start, end = int(norm.get("start") or 1), int(norm.get("end") or 0)
        except Exception:
            return None
        if not path or end < start:
            return None
        span = end - start + 1
        best = None
        best_ratio = 0.0
        for entry in reversed(self.entries):
            if (entry.tool != "antons_read_code" or not _entry_reusable(entry)
                    or entry.normalized_args.get("action") != "read"
                    or entry.normalized_args.get("path") != path):
                continue
            try:
                e_start = int(entry.normalized_args.get("start") or 1)
                e_end = int(entry.normalized_args.get("end") or 0)
            except Exception:
                continue
            overlap = min(end, e_end) - max(start, e_start) + 1
            if overlap <= 0:
                continue
            ratio = overlap / float(span)
            if ratio > best_ratio:
                best_ratio, best = ratio, entry
        if best is not None and best_ratio >= 0.5:
            return (best, "semantic",
                    "this line range mostly overlaps a read already done in "
                    "this turn -- read a genuinely different region instead")
        return None

    def find_action_duplicate(self, tool: str, args: dict) -> tuple[LedgerEntry, str, str] | None:
        norm = _normalize_tool_args(tool, args)
        exact = self.by_signature.get(_signature(tool, norm))
        if exact is None:
            return None
        if tool == "antons_apply_fix" and self._proposal_since_last(tool):
            return None
        return exact, "exact", "same side-effect-capable action was already attempted"

    def _proposal_since_last(self, tool: str) -> bool:
        """A successful antons_propose_fix recorded after the latest `tool`."""
        last = max((i for i, e in enumerate(self.entries) if e.tool == tool), default=-1)
        for entry in self.entries[last + 1:]:
            if entry.tool != "antons_propose_fix":
                continue
            body = entry.result.get("result")
            if isinstance(body, dict) and "ok" in body:
                if body.get("ok") is True:
                    return True
            elif entry.ok:
                return True     # clipped result: the envelope is all there is
        return False

    def _research_followup_duplicate(self, tool: str, args: dict,
                                     norm: dict) -> tuple[LedgerEntry, str, str] | None:
        if tool not in ("web_search", "web_read"):
            return None
        current_tokens = _expanded_tokens(_query_tokens_from_args(tool, norm))
        current_url = ""
        if tool == "web_read":
            current_url = _normalize_url(str((args or {}).get("url") or ""))
        for entry in reversed(self.entries):
            if entry.tool != "research" or not _entry_reusable(entry):
                continue
            if entry.coverage not in ("good", "unknown"):
                continue
            if current_url and current_url in _result_urls(entry.result.get("result")):
                return entry, "semantic", "research already included this source URL"
            research_tokens = _expanded_tokens(entry.tokens)
            similarity = _query_similarity(research_tokens, current_tokens)
            if similarity >= 0.35 and not _material_research_followup(
                    research_tokens, current_tokens, similarity):
                return entry, "semantic", (
                    "good research result already covers this overlapping public follow-up")
        return None

    def record_result(self, tool: str, args: dict, result: dict, executed: bool = True) -> dict:
        result = dict(result or {})
        norm = _normalize_tool_args(tool, args)
        if _is_action_result(tool, result):
            action_id = _extract_action_id(tool, norm, result)
            result.setdefault("action_id", action_id)
            result.setdefault("action_state", _action_state(result, executed))
        meta = _result_metadata(result)
        result_id = self._next_id(tool)
        result.update({
            "result_id": result_id,
            "executed": bool(executed),
            "normalized_args": norm,
            "result_count": meta.get("result_count"),
            "source_count": meta.get("source_count"),
            "coverage": meta.get("coverage"),
            "truncated": bool(meta.get("truncated")),
        })
        entry = LedgerEntry(
            result_id=result_id,
            tool=tool,
            args=dict(args or {}),
            normalized_args=norm,
            signature=_signature(tool, norm),
            tokens=_query_tokens_from_args(tool, norm),
            result=result,
            ok=bool(result.get("ok")),
            error_type=str(result.get("error_type") or ""),
            coverage=str(result.get("coverage") or "unknown"),
        )
        self.entries.append(entry)
        self.by_signature.setdefault(entry.signature, entry)
        self._consecutive_duplicates = 0
        return result

    def reuse_result(self, tool: str, args: dict,
                     duplicate: tuple[LedgerEntry, str, str]) -> dict:
        entry, duplicate_type, reason = duplicate
        self.duplicates += 1
        self.reused += 1
        self._consecutive_duplicates += 1
        if duplicate_type == "exact":
            self.exact_duplicates += 1
        elif duplicate_type == "semantic":
            self.semantic_duplicates += 1
        norm = _normalize_tool_args(tool, args)
        is_action = _is_action_result(entry.tool, entry.result)
        action_id = entry.result.get("action_id") or _turn_action_id(entry.tool, entry.normalized_args)
        message = (
            "This tool call duplicates an earlier %s result in the current "
            "turn. Previous result_id: %s. Reuse that result or materially "
            "refine the query."
        ) % (entry.tool, entry.result_id)
        if is_action:
            message = (
                "This side-effect-capable action duplicates an earlier %s "
                "attempt in the current turn. It was not executed again. "
                "Previous result_id: %s."
            ) % (entry.tool, entry.result_id)
        out = {
            "ok": bool(entry.ok) if is_action else True,
            "tool": tool,
            "seconds": 0.0,
            "args": _safe_args(args),
            "normalized_args": norm,
            "result_id": entry.result_id,
            "executed": False,
            "reused": True,
            "duplicate_call": True,
            "duplicate_type": duplicate_type,
            "previous_result_id": entry.result_id,
            "message": message,
            "duplicate_reason": reason,
            "result_count": entry.result.get("result_count"),
            "source_count": entry.result.get("source_count"),
            "coverage": entry.result.get("coverage"),
            "truncated": bool(entry.result.get("truncated")),
            "result": {
                "reused_result_id": entry.result_id,
                "tool": entry.tool,
                "coverage": entry.result.get("coverage"),
                "result_count": entry.result.get("result_count"),
                "source_count": entry.result.get("source_count"),
            },
        }
        if is_action:
            out.update({
                "risk": entry.result.get("risk") or agent_tools.LOCAL_REVERSIBLE_WRITE,
                "action_id": action_id,
                "previous_action_id": action_id,
                "action_state": "REPLAYED" if entry.ok else "DENIED",
                "idempotent_replay": True,
            })
            out["result"].update({
                "previous_action_id": action_id,
                "action_state": out["action_state"],
                "idempotent_replay": True,
            })
        return out

    def should_force_final(self) -> bool:
        return self._consecutive_duplicates >= 2 or self.duplicates >= 4

    def _next_id(self, tool: str) -> str:
        prefix = {
            "web_search": "web",
            "web_read": "web",
            "research": "research",
            "search_personal_memory": "memory",
            "search_chat_history": "chat",
            "search_knowledge_base": "kb",
            "search_latvian_grammar": "lv",
            "search_files": "file",
            "inspect_file": "file",
            "system_status": "system",
            "download_file": "download",
            "transcribe_audio": "audio",
            "inspect_image": "image",
            "inspect_video": "video",
            "contact_lookup": "contact",
            "calendar_search": "calendar",
            "calendar_create": "calendar",
            "send_message": "send",
            "search_goals": "goal",
            "list_goals": "goal",
            "add_goal": "goal",
            "update_goal": "goal",
            "complete_goal": "goal",
            "defer_goal": "goal",
            "cancel_goal": "goal",
            "note_goal_surfaced": "goal",
        }.get(tool, "tool")
        return "%s_%03d" % (prefix, len(self.entries) + 1)


_QUERY_ARG_TOOLS = {
    "web_search", "research", "search_personal_memory",
    "search_chat_history", "search_knowledge_base", "search_files",
    "search_latvian_grammar", "search_goals",
}

_SEMANTIC_DUPLICATE_TOOLS = {
    "web_search", "search_personal_memory", "search_chat_history",
    "search_knowledge_base", "search_files", "search_goals",
}

_QUERY_SYNONYMS = {
    "newest": "latest",
    "current": "latest",
    "recent": "latest",
    "recently": "latest",
    "today": "latest",
    "now": "latest",
    "look": "search",
    "lookup": "search",
    "find": "search",
    "check": "search",
}

_QUERY_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "can", "do", "does",
    "for", "from", "has", "have", "how", "i", "in", "is", "it", "me",
    "my", "of", "on", "or", "please", "public", "search", "the", "there",
    "this", "to", "was", "what", "when", "where", "whether", "with", "you",
}


def _normalize_tool_args(tool: str, args: dict) -> dict:
    out = {}
    for key in sorted((args or {}).keys()):
        value = (args or {}).get(key)
        low_key = str(key).lower()
        if low_key == "url" and isinstance(value, str):
            out[low_key] = _normalize_url(value)
        elif low_key == "path" and isinstance(value, str):
            out[low_key] = _normalize_path(value)
        elif low_key in ("query", "question", "text", "context", "person") and isinstance(value, str):
            out[low_key] = _normalize_query(value)
        elif isinstance(value, str):
            out[low_key] = " ".join(value.lower().split())
        elif isinstance(value, list):
            out[low_key] = sorted(str(v).strip().lower() for v in value)
        else:
            out[low_key] = value
    return out


def _signature(tool: str, norm: dict) -> str:
    return "%s|%s" % (tool, json.dumps(norm, ensure_ascii=False,
                                      sort_keys=True, default=str))


def _turn_action_id(tool: str, norm_or_args: dict) -> str:
    try:
        norm = _normalize_tool_args(tool, norm_or_args or {})
    except Exception:
        norm = norm_or_args or {}
    payload = _signature(tool, norm)
    prefix = re.sub(r"[^A-Za-z0-9_]+", "_", str(tool or "action")).strip("_") or "action"
    return "%s_%s" % (
        prefix,
        hashlib.sha256(payload.encode("utf-8", "replace")).hexdigest()[:16],
    )


def _is_action_result(tool: str, result: dict) -> bool:
    risk = str((result or {}).get("risk") or "")
    return (risk in (
        agent_tools.LOCAL_WRITE,
        agent_tools.LOCAL_REVERSIBLE_WRITE,
        agent_tools.EXTERNAL_WRITE,
        agent_tools.PRIVILEGED,
        agent_tools.DANGEROUS,
    ) or str(tool or "") in ("download_file",))


def _extract_action_id(tool: str, norm: dict, result: dict) -> str:
    body = (result or {}).get("result")
    if isinstance(body, dict) and body.get("action_id"):
        return str(body.get("action_id"))
    if (result or {}).get("action_id"):
        return str(result.get("action_id"))
    return _turn_action_id(tool, norm)


def _action_state(result: dict, executed: bool) -> str:
    if not bool(result.get("ok")):
        return "DENIED"
    if not executed:
        return "REPLAYED"
    return "EXECUTED"


def _normalize_query(text: str) -> str:
    lowered = str(text or "").lower()
    lowered = re.sub(r"https?://\S+", " ", lowered)
    lowered = re.sub(r"[^a-z0-9āčēģīķļņšūž._+-]+", " ", lowered, flags=re.I)
    tokens = []
    for token in lowered.split():
        token = _canonical_query_token(token)
        tokens.append(token)
    return " ".join(tokens)


def _normalize_url(url: str) -> str:
    value = str(url or "").strip()
    try:
        parts = urllib.parse.urlsplit(value)
        scheme = (parts.scheme or "https").lower()
        netloc = parts.netloc.lower()
        if netloc.endswith(":80") and scheme == "http":
            netloc = netloc[:-3]
        if netloc.endswith(":443") and scheme == "https":
            netloc = netloc[:-4]
        path = re.sub(r"/{2,}", "/", parts.path or "/")
        query = urllib.parse.urlencode(sorted(urllib.parse.parse_qsl(
            parts.query, keep_blank_values=True)))
        return urllib.parse.urlunsplit((scheme, netloc, path.rstrip("/") or "/",
                                        query, ""))
    except Exception:
        return " ".join(value.lower().split())


def _normalize_path(path: str) -> str:
    value = str(path or "").strip().strip("\"'").replace("\\", "/")
    value = re.sub(r"/+", "/", value)
    return value.rstrip("/").lower()


def _query_tokens_from_args(tool: str, norm: dict) -> tuple[str, ...]:
    if tool == "web_read":
        text = str(norm.get("url") or "")
    elif tool == "inspect_file":
        text = str(norm.get("path") or "")
    elif tool == "research":
        text = str(norm.get("question") or norm.get("query") or "")
    else:
        text = str(norm.get("query") or norm.get("question") or norm.get("text") or "")
    tokens = []
    for token in re.findall(r"[a-z0-9āčēģīķļņšūž._+-]{2,}", text.lower(), flags=re.I):
        token = _canonical_query_token(token)
        if token in _QUERY_STOPWORDS:
            continue
        tokens.append(token)
    return tuple(dict.fromkeys(tokens))


def _canonical_query_token(token: str) -> str:
    token = _QUERY_SYNONYMS.get(str(token or "").lower(), str(token or "").lower())
    if len(token) > 5 and token.endswith("ies"):
        return token[:-3] + "y"
    if len(token) > 4 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def _expanded_tokens(tokens: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    out = []
    for token in tokens or ():
        token = str(token or "").lower()
        pieces = re.findall(r"[a-z0-9]{2,}", token)
        if not pieces:
            pieces = [token]
        for piece in pieces:
            piece = _canonical_query_token(piece)
            if piece and piece not in _QUERY_STOPWORDS:
                out.append(piece)
    return tuple(dict.fromkeys(out))


def _material_research_followup(research_tokens: tuple[str, ...],
                                current_tokens: tuple[str, ...],
                                similarity: float) -> bool:
    old = set(research_tokens)
    new = set(current_tokens)
    added = new - old
    if len(added) >= 4 and similarity < 0.28:
        return True
    if any(re.search(r"\d|error|bug|fix|release|release-notes|security|cve", t)
           for t in added):
        return len(added) >= 3 and similarity < 0.45
    return False


def _semantic_duplicate_tool(tool: str) -> bool:
    return tool in _SEMANTIC_DUPLICATE_TOOLS


def _same_read_target(entry: LedgerEntry, args: dict) -> bool:
    current = _normalize_tool_args("web_read", args)
    return bool(entry.normalized_args.get("url")
                and entry.normalized_args.get("url") == current.get("url"))


def _query_similarity(left: tuple[str, ...], right: tuple[str, ...]) -> float:
    if not left or not right:
        return 0.0
    a = set(left)
    b = set(right)
    overlap = len(a & b)
    if not overlap:
        return 0.0
    short = overlap / float(max(1, min(len(a), len(b))))
    jaccard = overlap / float(max(1, len(a | b)))
    return max(short * 0.75 + jaccard * 0.25, jaccard)


def _result_urls(obj) -> set[str]:
    found = set()
    if isinstance(obj, dict):
        for key, value in obj.items():
            if str(key).lower() == "url" and isinstance(value, str):
                found.add(_normalize_url(value))
            else:
                found.update(_result_urls(value))
    elif isinstance(obj, list):
        for item in obj:
            found.update(_result_urls(item))
    return found


def _entry_reusable(entry: LedgerEntry) -> bool:
    if not entry.ok:
        return False
    if entry.coverage in ("empty", "failed"):
        return False
    return True


def _material_expansion(tool: str, previous_args: dict, current_args: dict,
                        previous: LedgerEntry) -> bool:
    if tool == "web_read":
        try:
            current = int(current_args.get("max_chars") or 0)
            previous_limit = int(previous_args.get("max_chars") or 0)
            return current > previous_limit and bool(previous.result.get("truncated"))
        except Exception:
            return False
    if tool in _QUERY_ARG_TOOLS:
        old = set(previous.tokens)
        new = set(_query_tokens_from_args(tool, _normalize_tool_args(tool, current_args)))
        added = new - old
        if len(added) >= 3 and _query_similarity(tuple(old), tuple(new)) < 0.72:
            return True
        if any(re.search(r"\d|v\d|[a-z]+-[0-9]+|error|bug|fix|release-notes", t)
               for t in added):
            return len(added) >= 2
        try:
            old_limit = int(previous_args.get("limit") or 0)
            new_limit = int(current_args.get("limit") or 0)
            if new_limit > old_limit and (previous.result.get("result_count") or 0) < new_limit:
                return True
        except Exception:
            pass
    return False


def _result_metadata(result: dict) -> dict:
    ok = bool((result or {}).get("ok"))
    body = (result or {}).get("result")
    count = _result_count(body)
    source_count = _source_count(body)
    truncated = _contains_key(body, "truncated") or bool((result or {}).get("truncated"))
    if not ok:
        coverage = "failed"
    elif count == 0:
        coverage = "empty"
    elif count is None:
        coverage = "unknown"
    else:
        coverage = "good"
    return {
        "result_count": count,
        "source_count": source_count,
        "truncated": truncated,
        "coverage": coverage,
    }


def _result_count(obj):
    if isinstance(obj, dict):
        for key in ("count", "result_count"):
            if isinstance(obj.get(key), int):
                return int(obj[key])
        for key in ("results", "items", "matches", "hits", "pages", "search"):
            value = obj.get(key)
            if isinstance(value, list):
                return len(value)
        if obj.get("backend_error"):
            return 0
    return None


def _source_count(obj):
    if isinstance(obj, dict):
        total = 0
        seen = False
        for key in ("results", "pages", "search", "matches", "hits"):
            value = obj.get(key)
            if isinstance(value, list):
                total += len(value)
                seen = True
        return total if seen else None
    return None


def _contains_key(obj, target: str) -> bool:
    if isinstance(obj, dict):
        if target in obj and bool(obj.get(target)):
            return True
        return any(_contains_key(v, target) for v in obj.values())
    if isinstance(obj, list):
        return any(_contains_key(v, target) for v in obj)
    return False


def _safe_args(args: dict) -> dict:
    out = {}
    for key, value in (args or {}).items():
        if isinstance(value, (int, float, bool)) or value is None:
            out[str(key)] = value
        elif isinstance(value, str):
            text = " ".join(value.split())
            out[str(key)] = text[:120] + ("..." if len(text) > 120 else "")
        elif isinstance(value, list):
            out[str(key)] = {"items": len(value)}
        elif isinstance(value, dict):
            out[str(key)] = {"keys": sorted(str(k) for k in value.keys())[:12]}
        else:
            out[str(key)] = type(value).__name__
    return out


def _tool_not_available(name: str, args: dict | None = None) -> dict:
    return {
        "ok": False,
        "tool": str(name or ""),
        "error_type": "tool_not_available",
        "message": "tool is not available in this interface policy",
        "args": _safe_args(args or {}),
    }


def _trace(trace_id, event, **fields):
    row = {"trace_id": trace_id, "event": event}
    row.update(fields)
    agent_tools.append_trace(row)
