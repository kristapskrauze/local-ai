# -*- coding: utf-8 -*-
"""Personal-assistant tool handlers used by the central Qwen registry."""
from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import timedelta

from app.people import OWNER
from app import temporal


def contact_lookup(args: dict, ctx) -> dict:
    from app import messaging

    query = str(args.get("query") or "").strip()
    cfg = messaging.load_config()
    acfg = cfg.get("auto_reply") or {}
    contacts = _contacts(acfg, ctx)
    if not query and getattr(ctx, "conversation_id", ""):
        query = "current"
    matches = _contact_matches(query, contacts, ctx)
    if not matches:
        return {
            "ok": False,
            "error_type": "unknown_contact",
            "query": query,
            "message": "No configured contact/conversation matched.",
        }
    if len(matches) > 1:
        return {
            "ok": False,
            "error_type": "ambiguous_contact",
            "query": query,
            "candidates": [_public_contact(c) for c in matches[:5]],
        }
    return {"ok": True, "query": query, "matches": [_public_contact(matches[0])]}


def calendar_search(args: dict, ctx) -> dict:
    from app import date_plans, rig_agent

    tz_name = temporal.timezone_name(args.get("timezone") or _configured_timezone())
    now = _ctx_now(ctx, tz_name)
    start_res, end_res = _calendar_search_range(args, now, tz_name)
    if not start_res.ok:
        return _read_failure("calendar_search", start_res)
    if not end_res.ok:
        return _read_failure("calendar_search", end_res)
    start = start_res.dt
    end = end_res.dt
    query = _fold(args.get("query") or "")
    events = []

    for plan in date_plans.list_plans(include_past=True):
        try:
            p_start = temporal.normalize_datetime(
                str(plan.get("start") or ""), now=now, tz_name=tz_name,
                allow_past=True).dt
        except Exception:
            p_start = None
        if p_start is None:
            continue
        p_end = p_start + timedelta(minutes=int(plan.get("duration_minutes") or 60))
        if p_start < end and start < p_end and _event_query_match(query, plan):
            events.append({
                "event_id": str(plan.get("id") or ""),
                "title": str(plan.get("title") or ""),
                "start": p_start.isoformat(timespec="minutes"),
                "end": p_end.isoformat(timespec="minutes"),
                "timezone": tz_name,
                "status": str(plan.get("status") or ""),
                "source": "date_plans",
                "location": str(plan.get("place") or ""),
            })

    external_error = ""
    try:
        days = max(1, min(90, (end.date() - now.date()).days + 2))
        for item in rig_agent.upcoming_events(days=days) or []:
            when, all_day, title, ev_end = rig_agent.event_fields(item)
            if not when:
                continue
            if when.tzinfo is None:
                when = when.replace(tzinfo=start.tzinfo)
            if not ev_end or ev_end <= when:
                ev_end = when + timedelta(days=1 if all_day else 1 / 24)
            if ev_end.tzinfo is None:
                ev_end = ev_end.replace(tzinfo=start.tzinfo)
            if when < end and start < ev_end and _event_query_match(query, {"title": title}):
                events.append({
                    "event_id": "",
                    "title": str(title or ""),
                    "start": when.isoformat(timespec="minutes"),
                    "end": ev_end.isoformat(timespec="minutes"),
                    "timezone": tz_name,
                    "all_day": bool(all_day),
                    "source": "ics",
                })
    except Exception as exc:
        external_error = str(exc)[:160]

    events.sort(key=lambda e: e.get("start") or "")
    return {
        "ok": True,
        "start": start.isoformat(timespec="minutes"),
        "end": end.isoformat(timespec="minutes"),
        "timezone": tz_name,
        "events": events[:20],
        "count": min(len(events), 20),
        "calendar_connected": bool(date_plans.calendar_connected()),
        "external_calendar_error": external_error,
    }


def _calendar_search_range(args: dict, now, tz_name: str):
    start = str(args.get("start") or "").strip()
    end = str(args.get("end") or "").strip()
    when = str(args.get("when") or "").strip()
    if not start and not end and not when:
        return (
            temporal.TemporalResult(True, now, tz_name, source="default_now"),
            temporal.TemporalResult(True, now + timedelta(days=7), tz_name, source="default_7d"),
        )
    first, second = temporal.normalize_range(start, end, when, now=now, tz_name=tz_name)
    if first.ok or first.error_type != "missing_time":
        return first, second
    day_text = start or when
    first = temporal.normalize_datetime(
        day_text + " 00:00", now=now, tz_name=tz_name, allow_past=True)
    if not first.ok:
        return first, second
    if end:
        second = temporal.normalize_datetime(
            end + " 23:59", now=now, tz_name=tz_name, allow_past=True)
        if not second.ok:
            return first, second
    else:
        second = temporal.TemporalResult(
            True, first.dt + timedelta(days=1), tz_name, source="day_default_1d")
    return first, second


def calendar_create(args: dict, ctx) -> dict:
    from app import agent_tools, date_plans

    title = str(args.get("title") or "").strip()
    if not title:
        return _action_failure("calendar_create", "invalid_arguments",
                               "title is required", args, ctx)
    if not agent_tools._owner_turn(ctx, _config().get("auto_reply") or {}):
        return _action_failure(
            "calendar_create", "authorization_required",
            f"only {OWNER}' own messages can add to his calendar; this one was "
            "written by someone else", args, ctx)

    tz_name = temporal.timezone_name(args.get("timezone") or _configured_timezone())
    now = _ctx_now(ctx, tz_name)
    start_res = temporal.normalize_datetime(
        args.get("start") or "", now=now, tz_name=tz_name, allow_past=False)
    if not start_res.ok:
        return _temporal_action_failure("calendar_create", start_res, args, ctx)
    end_res = None
    duration = int(args.get("duration_minutes") or 60)
    if args.get("end"):
        end_res = temporal.normalize_datetime(
            args.get("end"), now=now, tz_name=tz_name, allow_past=False)
        if not end_res.ok:
            return _temporal_action_failure("calendar_create", end_res, args, ctx)
        duration = max(1, int((end_res.dt - start_res.dt).total_seconds() / 60))

    canonical = {
        "title": title,
        "start": start_res.dt.isoformat(timespec="minutes"),
        "duration_minutes": duration,
        "timezone": tz_name,
        "location": str(args.get("location") or "").strip(),
        "notes": str(args.get("notes") or "").strip(),
    }
    action_id = agent_tools._action_id("calendar_create", canonical)
    replay = _ledger_replay(action_id)
    if replay:
        return replay

    conflicts = date_plans.conflicts(start_res.dt.replace(tzinfo=None), duration)
    if conflicts and not bool(args.get("allow_conflicts")):
        out = _action_failure("calendar_create", "calendar_conflict",
                              "requested event overlaps existing calendar items",
                              canonical, ctx, action_id=action_id)
        out["conflicts"] = conflicts[:8]
        return out

    plan = date_plans.add_plan(
        start_res.dt.replace(tzinfo=None),
        title,
        place=canonical["location"],
        notes=canonical["notes"],
        duration_minutes=duration,
        requested_by="agent:%s" % (getattr(ctx, "audit_id", "") or "qwen"),
        status="confirmed",
    )
    event_id = str(plan.get("id") or "")
    verified = next((p for p in date_plans.list_plans(include_past=True)
                     if str(p.get("id") or "") == event_id), None)
    result = {
        "ok": True,
        "action_id": action_id,
        "risk": "EXTERNAL_WRITE",
        "action_state": "EXECUTED",
        "event_id": event_id,
        "title": title,
        "start": canonical["start"],
        "end": (start_res.dt + timedelta(minutes=duration)).isoformat(timespec="minutes"),
        "timezone": tz_name,
        "location": canonical["location"],
        "verified": bool(verified),
        "calendar_backend": "date_plans_ics",
    }
    agent_tools._append_action_ledger(_ledger_row(ctx, action_id, "calendar_create",
                                                  "executed", result, canonical))
    return result


def send_message(args: dict, ctx) -> dict:
    from app import agent_tools, messaging

    auth = _explicit_action_authorized("send_message", getattr(ctx, "turn_text", ""))
    if not auth[0]:
        return _action_failure("send_message", "authorization_required",
                               auth[1], args, ctx)
    text = str(args.get("text") or "").strip()
    if not text:
        return _action_failure("send_message", "invalid_arguments",
                               "message text is required", args, ctx)
    if len(text) > 2000:
        return _action_failure("send_message", "invalid_arguments",
                               "message text is too long", args, ctx)
    conv = _resolve_conversation(args, ctx)
    if not conv.get("ok"):
        return _action_failure("send_message", conv.get("error_type") or "bad_recipient",
                               conv.get("message") or "recipient is not trusted",
                               args, ctx)
    conversation_id = conv["conversation_id"]
    canonical = {"conversation_id": conversation_id, "text": text}
    action_id = agent_tools._action_id("send_message", canonical)
    replay = _ledger_replay(action_id)
    if replay:
        return replay

    metadata = {
        "mode": "assistant-agent-send",
        "agent_action_id": action_id,
        "agent_audit_id": getattr(ctx, "audit_id", "") or "",
        "idem": action_id,
        "_temp_guid": "agent-" + hashlib.sha256(action_id.encode("utf-8")).hexdigest()[:24],
        "authorization": auth[1],
    }
    try:
        sent = messaging.send_message(
            provider=_send_provider(),
            target=conversation_id,
            text=text,
            metadata=metadata,
            real_send=True,
        )
    except Exception as exc:
        out = _action_failure("send_message", "send_failed", str(exc)[:240],
                              canonical, ctx, action_id=action_id)
        agent_tools._append_action_ledger(_ledger_row(ctx, action_id, "send_message",
                                                      "failed", out, canonical))
        return out
    entry = sent.get("entry") if isinstance(sent, dict) else {}
    status = str((sent or {}).get("status") or (entry or {}).get("status") or "")
    result = {
        "ok": True,
        "action_id": action_id,
        "risk": "EXTERNAL_WRITE",
        "action_state": "EXECUTED" if status == "sent" else "QUEUED",
        "conversation_id": conversation_id,
        "message_id": str((entry or {}).get("id") or ""),
        "status": status,
        "sent_real_message": bool((sent or {}).get("sent_real_message")),
        "queued": bool((sent or {}).get("queued")),
        "provider": str((sent or {}).get("provider") or ""),
    }
    agent_tools._append_action_ledger(_ledger_row(ctx, action_id, "send_message",
                                                  status or "executed", result, canonical))
    return result


def _contacts(acfg: dict, ctx) -> list[dict]:
    from app import messaging

    handles = []
    for value in list((acfg or {}).get("allowed_senders") or []):
        if value != "*":
            handles.append(str(value or ""))
    for value in ((acfg or {}).get("contact_names") or {}).keys():
        handles.append(str(value or ""))
    for value in list((acfg or {}).get("partner_handles") or []):
        handles.append(str(value or ""))
    for value in list((acfg or {}).get("assistant_chats") or []):
        handles.append(_handle_from_conversation(value))
    current = _handle_from_conversation(getattr(ctx, "conversation_id", ""))
    if current:
        handles.append(current)

    out, seen = [], set()
    for handle in handles:
        norm = messaging._normalize_handle(handle)
        if not norm or norm in seen:
            continue
        seen.add(norm)
        name = messaging._contact_name(acfg, handle) or _display_from_handle(handle)
        conv = handle if str(handle).startswith("iMessage;-;") else "iMessage;-;" + handle
        out.append({
            "contact_id": _contact_id(handle),
            "display_name": name,
            "conversation_id": conv,
            "is_current_conversation": bool(current and messaging._normalize_handle(current) == norm),
        })
    return out


def _contact_matches(query: str, contacts: list[dict], ctx) -> list[dict]:
    q = _fold(query)
    if q in ("current", "this chat", "this conversation", "her", "him", "them"):
        return [c for c in contacts if c.get("is_current_conversation")]
    if not q:
        return []
    exact = [c for c in contacts if _fold(c.get("display_name")) == q
             or _fold(c.get("contact_id")) == q]
    if exact:
        return exact
    return [c for c in contacts if q in _fold(c.get("display_name"))
            or q in _fold(c.get("conversation_id"))]


def _public_contact(contact: dict) -> dict:
    return {
        "contact_id": contact.get("contact_id"),
        "display_name": contact.get("display_name"),
        "channels": [{
            "type": "imessage",
            "conversation_id": contact.get("conversation_id"),
            "trusted_current_conversation": bool(contact.get("is_current_conversation")),
        }],
    }


def _send_provider() -> str | None:
    cfg = _config()
    acfg = cfg.get("auto_reply") or {}
    return str(acfg.get("provider") or cfg.get("default_provider") or "").strip() or None


def _trusted_action_conversations(acfg: dict) -> set[str]:
    out = set()
    for key in ("assistant_chats", "trusted_conversation_ids",
                "trusted_action_conversations"):
        for value in (acfg or {}).get(key) or []:
            norm = _normalize_conversation_id(value)
            if norm:
                out.add(norm)
    for key in ("partner_handles", "trusted_recipient_handles"):
        for value in (acfg or {}).get(key) or []:
            norm = _normalize_conversation_id(
                value if str(value or "").startswith("iMessage;-;")
                else "iMessage;-;" + str(value or ""))
            if norm:
                out.add(norm)
    return out


def _normalize_conversation_id(value: str) -> str:
    from app import messaging

    raw = str(value or "").strip()
    if not raw:
        return ""
    handle = _handle_from_conversation(raw)
    norm = messaging._normalize_handle(handle)
    if not norm:
        return ""
    return "iMessage;-;" + norm


def _recipient_bound_to_turn(conversation_id: str, ctx, contacts: list[dict],
                             requested_current: bool = False) -> bool:
    text = _current_action_text(getattr(ctx, "turn_text", ""))
    low = _fold(text)
    if not low:
        return False
    norm = _normalize_conversation_id(conversation_id)
    current = _normalize_conversation_id(getattr(ctx, "conversation_id", ""))
    if requested_current and norm and norm == current:
        return True
    contact = next((c for c in contacts
                    if _normalize_conversation_id(c.get("conversation_id")) == norm),
                   {})
    needles = [
        str(contact.get("display_name") or ""),
        str(contact.get("contact_id") or ""),
        _handle_from_conversation(conversation_id),
        conversation_id,
    ]
    for item in needles:
        folded = _fold(item)
        if folded and folded in low:
            return True
    pronoun = re.search(r"\b(her|him|them|vi[nņ]ai|vi[nņ]am|vinu|vi[nņ]u)\b", low)
    if pronoun:
        trusted = _trusted_action_conversations((_config().get("auto_reply") or {}))
        trusted_contacts = [
            c for c in contacts
            if _normalize_conversation_id(c.get("conversation_id")) in trusted
            and _normalize_conversation_id(c.get("conversation_id")) != current
        ]
        if (len(trusted_contacts) == 1
                and _normalize_conversation_id(
                    trusted_contacts[0].get("conversation_id")) == norm):
            return True
    return False


def _current_action_text(turn_text: str) -> str:
    text = str(turn_text or "")
    text = re.sub(r"(?s)```.*?```", " ", text)
    text = re.sub(r"(?m)^\s*>.*$", " ", text)
    for _quote_pat in (r'"[^"]{0,500}"',
                       r"\u201c[^\u201d]{0,500}\u201d",
                       r"\u2018[^\u2019]{0,500}\u2019",
                       r"(?<!\w)'[^']{0,500}'(?!\w)"):
        text = re.sub(_quote_pat, " ", text)
    text = re.sub(
        r"(?i)\b(earlier|previously|before|last time)\s+i\s+said\s+[^?!.]{0,180}",
        " ", text)
    return " ".join(text.split())


def _resolve_conversation(args: dict, ctx) -> dict:
    acfg = (_config().get("auto_reply") or {})
    raw = str(args.get("conversation_id") or "").strip()
    requested_current = not raw or raw in ("current", "current_conversation")
    if not raw or raw in ("current", "current_conversation"):
        raw = str(getattr(ctx, "conversation_id", "") or "").strip()
    if not raw:
        return {"ok": False, "error_type": "missing_recipient",
                "message": "conversation_id is required unless there is a trusted current conversation"}
    contacts = _contacts(acfg, ctx)
    trusted = _trusted_action_conversations(acfg)
    norm_raw = _normalize_conversation_id(raw)
    if norm_raw not in trusted:
        return {"ok": False, "error_type": "untrusted_recipient",
                "message": "conversation is not in the exact trusted action list"}
    known = {str(c.get("conversation_id") or "") for c in contacts}
    by_norm = {_normalize_conversation_id(c.get("conversation_id")): c
               for c in contacts}
    contact = by_norm.get(norm_raw)
    conversation_id = (contact or {}).get("conversation_id") or raw
    current_norm = _normalize_conversation_id(getattr(ctx, "conversation_id", ""))
    if (raw not in known and norm_raw not in by_norm
            and norm_raw != current_norm):
        return {"ok": False, "error_type": "untrusted_recipient",
                "message": "conversation is not in configured contacts"}
    if not _recipient_bound_to_turn(
            conversation_id, ctx, contacts, requested_current=requested_current):
        return {"ok": False, "error_type": "recipient_mismatch",
                "message": "current turn does not authorize this recipient"}
    if norm_raw in trusted:
        return {"ok": True, "conversation_id": conversation_id}
    return {"ok": False, "error_type": "untrusted_recipient",
            "message": "conversation is not in configured contacts or current trusted context"}


def _explicit_action_authorized(kind: str, turn_text: str) -> tuple[bool, str]:
    if kind != "send_message":
        return False, "no word-based authorization exists for %s" % kind
    text = _current_action_text(turn_text)
    low = _fold(text)
    if not low:
        return False, "no current user turn is available for action authorization"
    deny = (
        r"\b(might|maybe|perhaps|thinking about|what should i|should i|could i|"
        r"draft|write a draft|not sure|probably)\b",
        r"\b(varbut|varbūt|domaju|domāju|ko man|vai man|melnrakst|varetu|varētu|laikam)\b",
        r"\b(don't send|dont send|nesuti|nesūti|neko nesuti|neko nesūti)\b",
    )
    if any(re.search(p, low) for p in deny):
        return False, "current turn discusses or drafts an action but does not clearly authorize execution"
    if re.search(
            r"^\s*(what|what's|whats|which|show|find|search|look up|"
            r"tell me|did|do you know)\b.{0,120}\b(message|text|reply)\b",
            low):
        return False, "current turn asks to read message state, not send a message"
    allow = (
        r"\b(send|text|message|reply|ask)\s+(?!me\b)"
        r"(?:[a-z0-9_+@.;:'-]{2,}|him|her|them|this|current)\b",
        r"\btell\s+(?!me\b)"
        r"(?:[a-z0-9_+@.;:'-]{2,}|him|her|them|this|current)\b",
        r"\b(send|text|message|reply)\b.{0,120}\b(saying|that|with|:)\b",
        r"\b(aizsuti|aizsūti|nosuti|nosūti|pasaki|atbildi|uzraksti.*un\s+aizsuti)\b",
    )
    if any(re.search(p, low) for p in allow):
        return True, "explicit_user_request"
    return False, "current turn does not clearly authorize this external action"


def _read_failure(tool: str, result: temporal.TemporalResult) -> dict:
    return {
        "ok": False,
        "tool": tool,
        "error_type": result.error_type,
        "message": result.message,
        "timezone": result.timezone,
    }


def _temporal_action_failure(tool: str, result: temporal.TemporalResult,
                             args: dict, ctx) -> dict:
    return _action_failure(tool, result.error_type, result.message, args, ctx)


def _action_failure(tool: str, error_type: str, message: str, args: dict, ctx,
                    action_id: str = "") -> dict:
    from app import agent_tools

    aid = action_id or agent_tools._action_id(tool, args or {})
    return {
        "ok": False,
        "tool": tool,
        "risk": "EXTERNAL_WRITE",
        "action_id": aid,
        "action_state": "DENIED",
        "error_type": error_type,
        "message": str(message or "")[:500],
    }


def _ledger_replay(action_id: str) -> dict:
    from app import agent_tools

    try:
        lines = agent_tools.ACTION_LEDGER_PATH.read_text(
            encoding="utf-8", errors="ignore").splitlines()
    except Exception:
        return {}
    for line in reversed(lines[-5000:]):
        try:
            row = json.loads(line)
        except Exception:
            continue
        if str(row.get("action_id") or "") != action_id:
            continue
        result = row.get("result") if isinstance(row.get("result"), dict) else {}
        if result and str(row.get("status") or "") not in ("failed", "denied"):
            out = dict(result)
            out["idempotent_replay"] = True
            out["action_state"] = "REPLAYED"
            return out
    return {}


def _ledger_row(ctx, action_id: str, tool: str, status: str,
                result: dict, args: dict) -> dict:
    return {
        "audit_id": getattr(ctx, "audit_id", "") or "",
        "interface": getattr(ctx, "interface", "") or "",
        "conversation_id": getattr(ctx, "conversation_id", "") or "",
        "action_id": action_id,
        "tool": tool,
        "risk": "EXTERNAL_WRITE",
        "status": status,
        "args": args,
        "result": result,
        "ts": time.time(),
    }


def _configured_timezone() -> str:
    try:
        cfg = _config()
        return temporal.timezone_name((cfg.get("auto_reply") or {}).get("timezone") or "")
    except Exception:
        return temporal.DEFAULT_TIMEZONE


def _ctx_now(ctx, tz_name: str):
    raw = str(getattr(ctx, "now", "") or "").strip()
    if raw:
        parsed = temporal.normalize_datetime(raw, tz_name=tz_name, allow_past=True)
        if parsed.ok:
            return parsed.dt
    return temporal.now_in(tz_name)


def _config() -> dict:
    from app import messaging
    return messaging.load_config()


def _event_query_match(query: str, event: dict) -> bool:
    if not query:
        return True
    hay = _fold(" ".join(str(event.get(k) or "") for k in ("title", "with", "place", "notes")))
    return query in hay


def _fold(text) -> str:
    out = str(text or "").lower()
    table = str.maketrans({"ā": "a", "č": "c", "ē": "e", "ģ": "g", "ī": "i",
                           "ķ": "k", "ļ": "l", "ņ": "n", "š": "s", "ū": "u",
                           "ž": "z"})
    return " ".join(out.translate(table).split())


def _contact_id(handle: str) -> str:
    return "contact_" + hashlib.sha1(
        _fold(handle).encode("utf-8", "replace")).hexdigest()[:12]


def _handle_from_conversation(value: str) -> str:
    text = str(value or "")
    if text.startswith("iMessage;-;"):
        return text.split(";-;", 1)[1]
    return text


def _display_from_handle(handle: str) -> str:
    text = _handle_from_conversation(handle)
    if "@" in text:
        return text.split("@", 1)[0]
    return text
