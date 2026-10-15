# -*- coding: utf-8 -*-
"""INVESTIGATE_SELF — answer "why did you do that?" from evidence.

    round 1   model plans up to PLAN_WIDTH lookups
    code      executes them, each through the permission layer
    round 2   model sees every observation; answers, or asks for a few more
    round 3   verdict only — ANSWER or REPORT_FAILURE, no further tools

Evidence is a first-class object with a KIND, and the kinds are not
interchangeable:

    observation      a fact a tool returned, with the tool and the field
    inference        something the model concluded from observations
    confirmed_cause  an inference whose supporting fact CODE FOUND in the
                     observations — not one the model asserted confidently

That last check is the point. A model that says "confirmed: the watcher
silenced it" while nothing in the log says so gets its claim downgraded to an
inference and the answer marked unconfirmed. Plausibility is not evidence.

Budgets are recorded in telemetry (`agent_steps`, `agent_model_calls`,
`agent_budget`) so an investigation's cost sits next to a reply's cost in the
same trace, and the state lives in the request's own investigation task
(app/obligations.py) so a clarification mid-run keeps the original goal.
"""
import json
import logging
import re
import time
from app.people import PARTNER

LOGGER = logging.getLogger(__name__)

# Tool lookups per investigation, and model calls to direct them.
MAX_TOOL_STEPS = 18
MAX_MODEL_CALLS = 20
PLAN_WIDTH = 3                 # lookups one decision may ask for
MAX_OBSERVATION_CHARS = 8000

# A quote has to be long enough to identify a specific line, or
# "confirmed" means nothing — see _cites_supported.
MIN_CITATION_CHARS = 12
MIN_CITATION_WORDS = 3

OBSERVATION = "observation"
INFERENCE = "inference"
CONFIRMED = "confirmed_cause"

# What the model is allowed to ask for. The descriptions say what the tool
# RETURNS, because that is what it needs to plan with.
TOOL_SPEC = [
    ("find_message",
     "text_contains, chat_guid, since_minutes, limit -> the actual messages: "
     "message_guid, sender, from_me, when, text. START HERE when the question "
     "names a person or quotes what was said — the routing log stores no "
     "message text, only guids."),
    ("find_event",
     "chat_guid, contains, since_minutes, limit -> what the pipeline DID with "
     "a message: decision, reason, watcher_action, watcher_reason, "
     "watcher_subject, disagreed. `contains` matches the guid or the reason, "
     "never the message text."),
    ("repo_search", "query, limit -> file:line matches in the source. Use "
     "when you know the exact string; it also matches comments and strings."),
    ("repo_symbols",
     "name, kind, path, limit -> WHERE A THING IS DEFINED: function, class or "
     "constant, with file and line. Use this before repo_search when you know "
     "what the thing is called."),
    ("repo_refs",
     "name, limit -> WHO USES IT: imports, calls and references with file and "
     "line, counted by kind. Ask this before saying a change is safe, and "
     "before saying nothing calls something."),
    ("code_search",
     "query, limit, kind, path -> code that is conceptually similar, for when "
     "you cannot name what you are looking for ('the bit that decides whether "
     "to reply'). Similarity only: every hit is a path and a line to OPEN with "
     "repo_read before believing anything about it."),
    ("repo_read", "path, start, end -> numbered source lines of one file. This "
     "is the source itself and it outranks anything the searches suggested."),
    ("read_recent_logs",
     "name (service.err|service.out|traces|routing|obligations|self_restart), "
     "contains, limit -> the tail of that log."),
    ("read_config_safe", "key -> live behaviour settings (no secrets, no handles)."),
    ("service_status", "-> health, uptime, which models are resident."),
    ("runtime_status", "-> the capability registry."),
    ("model_status",
     "-> which model each lane runs and with what options, plus what Ollama "
     "has loaded right now and what is installed. Read this before saying "
     "anything about which model you are or how you are configured."),
    ("git_status", "-> files changed but not committed."),
    ("git_log", "n -> recent commits, each with its date and subject."),
    ("git_diff",
     "path, stat, added_only -> the working tree. stat=True: which files and "
     "how many lines. added_only=True with a path: the added lines of that "
     "file, nothing else."),
    ("run_allowlisted_test", "name -> runs one offline suite, returns PASS/FAIL."),
]


def _tool_doc():
    return "\n".join("  %s(%s)" % (n, d) for n, d in TOOL_SPEC)


# qwen3.8 declares PARSER qwen3.5 and Ollama runs that tool-call parser over
# every /api/chat response -- a reply shaped like a JSON object (which every
# decision here IS) crashes it mid-parse: HTTP 500 error=EOF, generation
# discarded, and the whole investigate/propose_fix path fails. Handing Ollama a
# `format` schema makes it decode against the schema and skip the tool-call
# parser entirely (same fix propose_patch._PROPOSAL_SCHEMA already uses).
_DECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string"},
        "plan": {"type": "array", "items": {
            "type": "object",
            "properties": {"tool": {"type": "string"},
                           "args": {"type": "object"}}}},
        "args": {"type": "object"},
        "answer": {"type": "string"},
        "reason": {"type": "string"},
        "question": {"type": "string"},
        "cause": {"type": "string"},
        "cites": {"type": "array", "items": {"type": "string"}},
    },
}


_PLAN_SYS = (
    "You are diagnosing your OWN behaviour. You are Antons, running on this "
    "machine, and you can read your own logs, source and runtime state.\n\n"
    "Plan the lookups. Answer with ONE json object and nothing else:\n"
    '  {"action": "USE_TOOL", "plan": [{"tool": "...", "args": {...}}, ...]}\n'
    "at most %d lookups, ordered so the first one identifies the specific "
    "message or behaviour being asked about.\n\n"
    "Tools:\n%s\n\n"
    "You cannot change anything: there is no tool that writes, patches or "
    "deploys, and asking for one wastes a lookup."
)

_VERDICT_SYS = (
    "You are %s, explaining your OWN behaviour to the person who runs you. "
    "Do not deny being %s and do not describe yourself as software — you "
    "are simply answering for what you did. Below are the OBSERVATIONS "
    "your lookups returned. Answer with ONE json object and nothing "
    "else:\n\n"
    '  {"action": "USE_TOOL", "plan": [{"tool": "...", "args": {...}}, ...]} '
    "  only if a specific fact is still missing and you have lookups left -- "
    "EVERY plan item MUST be an object with `tool` and `args` exactly like "
    "that, never a bare string.\n"
    '  {"action": "ANSWER", "args": {"text": "<in the language you were asked '
    'in: 1-2 sentences like a text message, UNLESS the question asks for a '
    'list or a breakdown — then answer with a list, one item per line, and '
    'keep each item to one line>", "cause": "<the '
    'mechanism>", "cites": ["<exact phrase copied from an observation>", '
    '...]}}\n'
    '  {"action": "REPORT_FAILURE", "args": {"reason": "<what is missing, '
    'in the language you were asked in>"}}\n\n'
    "Tools (only these; a name not on this list does not exist and calling "
    "it wastes a lookup):\n%s\n\n"
    "RULES:\n"
    "- Every string in `cites` must appear VERBATIM in the observations AND "
    "be a phrase of at least three words. They are checked against the "
    "recorded data; a citation that is missing, or too short to identify a "
    "line, downgrades your answer to unconfirmed.\n"
    "- LOOK BEFORE YOU GIVE UP. If lookups remain and the cause is not yet "
    "visible, USE_TOOL again with a DIFFERENT tool from the list above, one "
    "actually suited to what is still missing -- a question about your OWN "
    "code is answered by reading the code (repo_read, repo_search, "
    "repo_symbols, repo_refs); a question about a past conversation is "
    "answered by find_message or find_event. ONLY the tools named above "
    "exist here -- not antons_read_code or any other tool you may have used "
    "elsewhere, this is a different, narrower toolset.\n"
    "- If the observations still do not show WHY, use REPORT_FAILURE. That "
    "is a correct answer. A story that fits is not.\n"
    "- Do not describe what you would look at next. You either know or you "
    "report that you do not."
)


def _first_object(text):
    """The json object that starts at the first '{' in `text`, or None.

    Strict single-object decode: handles braces that appear INSIDE string
    values (a proposal's `replace` field is full of Python) and any prose the
    model tacked on after the closing brace.
    """
    start = text.find("{")
    if start < 0:
        return None
    try:
        obj, _end = json.JSONDecoder().raw_decode(text, start)
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


def _parse_json(raw):
    """The model's decision, or None. Tolerates prose, fences and slips."""
    text = str(raw or "").strip()
    if not text:
        return None
    fenced = ""
    if "```" in text:
        fenced = re.sub(r"^.*?```(?:json)?", "", text, flags=re.S).split("```")[0]
    for candidate in ((fenced, text) if text.startswith("```") else (text, fenced)):
        obj = _first_object(candidate) if candidate else None
        if obj is not None:
            return obj
    text = fenced or text
    start = text.find("{")
    if start < 0:
        return None
    end = text.rfind("}")
    if end <= start:
        return None
    body = text[start:end + 1]
    try:
        obj = json.loads(body)
        return obj if isinstance(obj, dict) else None
    except Exception:
        return _salvage(body)


_ACTION_RE = re.compile(r'"action"\s*:\s*"([A-Z_]+)"')
_FIELD_RE = r'"%s"\s*:\s*"((?:[^"\\\\]|\\\\.)*)"'
_CITES_RE = re.compile(r'"cites"\s*:\s*\[(.*?)\]', re.S)
_QUOTED_RE = re.compile(r'"((?:[^"\\\\]|\\\\.)*)"')


def _salvage(body):
    """Recover a VERDICT from json the model got slightly wrong.

    Only ANSWER and REPORT_FAILURE are salvaged: a mangled tool PLAN is
    not repaired, because guessing which tool to run with which arguments
    is a decision, not a repair.
    """
    m = _ACTION_RE.search(body or "")
    action = m.group(1) if m else ""
    if action not in ("ANSWER", "REPORT_FAILURE"):
        return None

    def field(name):
        hit = re.search(_FIELD_RE % name, body)
        return hit.group(1) if hit else ""
    if action == "REPORT_FAILURE":
        reason = field("reason")
        return {"action": action, "args": {"reason": reason}} if reason else None
    text = field("text")
    if not text:
        return None
    cites = []
    chunk = _CITES_RE.search(body)
    if chunk:
        # every quoted run inside the array, whatever punctuation the
        # model put between them; junk fragments are dropped later by the
        # phrase-length rule in _cites_supported
        cites = [c for c in _QUOTED_RE.findall(chunk.group(1)) if c.strip()]
    LOGGER.info("investigate: salvaged a malformed %s decision", action)
    return {"action": action, "args": {"text": text, "cause": field("cause"),
                                       "cites": cites}}


def _clip(obj):
    try:
        s = json.dumps(obj, ensure_ascii=False, default=str)
    except Exception:
        s = str(obj)
    return s[:MAX_OBSERVATION_CHARS]


def _plan_from(decision):
    """The list of {tool, args} a decision asks for, capped at PLAN_WIDTH."""
    if not isinstance(decision, dict):
        return []
    plan = decision.get("plan")
    if isinstance(plan, dict):
        plan = [plan]
    if not isinstance(plan, list):
        # a single USE_TOOL in the flat shape is still a plan of one
        args = decision.get("args") if isinstance(decision.get("args"), dict) else {}
        if args.get("tool"):
            plan = [{"tool": args.get("tool"), "args": args.get("args") or {}}]
        else:
            plan = []
    out = []
    for item in plan[:PLAN_WIDTH]:
        if not isinstance(item, dict):
            continue
        tool = str(item.get("tool") or "").strip()
        args = item.get("args") if isinstance(item.get("args"), dict) else {}
        if tool:
            out.append({"tool": tool, "args": args})
    return out


def _observation(tool, args, result, ok, note=""):
    return {"kind": OBSERVATION, "tool": tool, "args": args, "ok": bool(ok),
            "note": str(note or ""), "data": result}


def _cites_supported(cites, observations):
    """Which of the model's citations actually appear in what the tools returned.

    This is what separates `confirmed_cause` from `inference`. The check is
    deliberately literal: the cited string has to be IN the observation text.
    A model that paraphrases loses the confirmation, which is the right way
    round — the reader can then find the exact string themselves.
    """
    blob = "\n".join(_clip(o.get("data")) for o in observations if o.get("ok"))
    blob_low = blob.lower()
    supported, unsupported = [], []
    for c in (cites or []):
        c = str(c or "").strip()
        if not c:
            continue
        if len(c) < MIN_CITATION_CHARS or len(c.split()) < MIN_CITATION_WORDS:
            unsupported.append(c)
            continue
        (supported if c.lower() in blob_low else unsupported).append(c)
    return supported, unsupported


def build_evidence(observations, cause="", cites=()):
    """Turn the run into typed evidence: observations, then what they support."""
    ev = []
    for o in observations:
        if not o.get("ok"):
            continue
        ev.append({"kind": OBSERVATION, "source": o.get("tool"),
                    "fact": _clip(o.get("data"))})
    supported, unsupported = _cites_supported(cites, observations)
    for c in supported:
        ev.append({"kind": OBSERVATION, "source": "cited", "fact": c[:300]})
    if cause:
        ev.append({"kind": CONFIRMED if supported else INFERENCE,
                   "claim": str(cause)[:400],
                   "supported_by": supported[:6],
                   "unsupported": unsupported[:6]})
    return ev



YES, MAYBE, NO = "yes", "maybe", "no"


def self_inquiry_signal(text, is_owner=False, is_direct=False):
    """First pass: may this be the owner asking about ANTONS ITSELF?

    Returns MAYBE (the supervisor model decides) or NO. Only the facts that are
    not about wording are checked here: the owner and direct-request facts,
    which the caller computes from the transport and the obligation layer, so
    nothing here can promote a non-owner.
    """
    if not is_owner or not is_direct:
        return NO
    if not " ".join(str(text or "").split()):
        return NO
    return MAYBE


_SUPERVISOR_SYS = (
    "One json object, nothing else. The OWNER of an AI assistant sent the "
    "message below. Decide what the assistant should do:\n"
    '  {"action": "INVESTIGATE_SELF"}  the owner wants an EXPLANATION of the '
    "ASSISTANT'S OWN past behaviour, its logs, its code or a decision it "
    "made — read-only, answerable by inspecting itself, nothing changes\n"
    '  {"action": "ANSWER"}            anything else, including: questions '
    "about other people, the world, or the rig's hardware; AND -- this is "
    "the important one -- any request to CHANGE, FIX, BUILD or APPLY "
    "something, even when it is about the assistant's own code. Wanting to "
    "know why something is broken is INVESTIGATE_SELF; wanting it FIXED is "
    "ANSWER, because fixing needs the tools that only the other lane has.\n\n"
    "Examples:\n"
    "kāpēc tu man neatbildēji? -> INVESTIGATE_SELF\n"
    "kas tur notika ar to ziņu? -> INVESTIGATE_SELF\n"
    "salabo šo kļūdu savā kodā -> ANSWER (a fix request, not an explanation)\n"
    "Antons, fix this bug in your own code -> ANSWER (a fix request)\n"
    "kāpēc web_search neatrada rezultātus? -> INVESTIGATE_SELF (asking why, "
    "not asking to fix it)\n"
    f"kāpēc {PARTNER} neatbildēja? -> ANSWER\n"
    "kas notika Ukrainā? -> ANSWER\n"
    "cik grādi GPU? -> ANSWER"
)


def supervisor_says_investigate(acfg, text, chat_fn=None):
    from app import agent_actions
    if chat_fn is None:
        from app import messaging as M
        chat_fn = M._ollama_chat
        cfg = dict(acfg or {})
        cfg["model"] = (cfg.get("pipeline_model") or cfg.get("model")
                        or M.BASE_MODEL)
    else:
        cfg = dict(acfg or {})
    try:
        raw = chat_fn(cfg, [{"role": "system", "content": _SUPERVISOR_SYS},
                            {"role": "user", "content": str(text or "")[:300]}],
                      options={"temperature": 0, "num_predict": 16},
                      format_schema=_DECISION_SCHEMA)
    except Exception:
        return False
    act = agent_actions.canonical_action(_parse_json(raw) or {})
    return bool(act and act["action"] == agent_actions.INVESTIGATE_SELF)

def investigate(question, acfg, allowed=False, chat_guid="", who="",
                chat_fn=None, max_steps=MAX_TOOL_STEPS,
                max_model_calls=MAX_MODEL_CALLS, task=None,
                clarifications=(), persona="Anton", deadline=None):
    """Run the bounded loop. Never raises, never writes, always terminates."""
    from app import selfinspect
    from app import agent_actions
    if not allowed:
        return {"ok": False, "state": "DENIED", "answer": "",
                "reason": "self-inspection is owner-only",
                "evidence": [], "steps": 0, "model_calls": 0, "confirmed": False}
    if chat_fn is None:
        from app import messaging as M
        chat_fn = M._ollama_chat
        cfg = dict(acfg or {})
        cfg["model"] = (cfg.get("assistant_model") or cfg.get("model")
                        or M.BASE_MODEL)
    else:
        cfg = dict(acfg or {})

    goal = str(question or "")[:800]
    ask = goal
    if clarifications:
        # the clarification is ADDED to the goal, never a replacement for it
        ask += "\nPRECIZĒJUMS: " + " / ".join(str(c)[:200] for c in clarifications)
    if chat_guid:
        ask += "\n(chat: %s)" % chat_guid

    voice = str(persona or "Anton")
    verdict_sys = _VERDICT_SYS % (voice, voice, _tool_doc())
    try:
        from app import capabilities as _caps
        _cap = _caps.prompt_lines(acfg) or []
    except Exception:
        _cap = []
    if _cap:
        verdict_sys += "\n\n" + "\n".join(str(l) for l in _cap)
    convo = [{"role": "system", "content": _PLAN_SYS % (PLAN_WIDTH, _tool_doc())},
             {"role": "user", "content": ask}]
    observations, steps, calls = [], 0, 0
    correction_note = ""
    t0 = time.time()

    def _record(state, answer, cause="", cites=(), reason=""):
        ev = build_evidence(observations, cause=cause, cites=cites)
        confirmed = any(e.get("kind") == CONFIRMED for e in ev)
        out = {"ok": state != "FAILED", "state": state, "answer": str(answer or ""),
               "cause": str(cause or ""), "evidence": ev, "confirmed": confirmed,
               "steps": steps, "model_calls": calls,
               "budget": {"max_steps": int(max_steps),
                          "max_model_calls": int(max_model_calls)},
               "seconds": round(time.time() - t0, 1)}
        if reason:
            out["reason"] = reason
        try:
            from app import telemetry
            telemetry.set_meta(agent_steps=steps, agent_model_calls=calls,
                               agent_budget="%d/%d" % (int(max_steps),
                                                       int(max_model_calls)),
                               agent_state=state, agent_confirmed=confirmed)
        except Exception:
            pass
        if task and chat_guid:
            try:
                from app import obligations
                obligations.update_investigation(
                    chat_guid, steps_used=steps, model_calls=calls,
                    next_action=None)
                obligations.finish_investigation(
                    chat_guid,
                    {"ANSWERED": obligations.INV_ANSWERED,
                     "INSUFFICIENT": obligations.INV_INSUFFICIENT,
                     "FAILED": obligations.INV_FAILED}.get(
                         state, obligations.INV_FAILED),
                    answer=out["answer"])
            except Exception:
                LOGGER.exception("investigation task not finalised")
        return out

    while calls < int(max_model_calls):
        if deadline is not None and time.time() >= deadline and calls > 0:
            # not before the FIRST call -- a deadline that already expired
            # before this ever started should not silently produce no
            # diagnosis at all; that shape is for the caller to decide, not
            # this loop.
            return _record("FAILED", "",
                           reason="stopped before another model call: past "
                                  "its deadline")
        # LAST CALL IS A VERDICT, NOT A PLAN. Without this the loop can spend
        # its final model call asking for lookups it will never get to read.
        final = (calls == int(max_model_calls) - 1) or steps >= int(max_steps)
        if calls:
            convo = [{"role": "system", "content": verdict_sys},
                     {"role": "user", "content": ask},
                     {"role": "user", "content": "OBSERVATIONS:\n" + "\n".join(
                         "- %s -> %s" % (o.get("tool"), _clip(o.get("data"))
                                         if o.get("ok") else "REFUSED: " + o.get("note", ""))
                         for o in observations) or "- (none)"}]
            if final:
                convo.append({"role": "user", "content":
                              "No lookups remain. ANSWER or REPORT_FAILURE only."})
            else:
                convo.append({"role": "user", "content":
                              "You have %d lookups left."
                              % max(0, int(max_steps) - steps)})
            if correction_note:
                convo.append({"role": "user", "content": correction_note})
                correction_note = ""   # shown once; stale after that
        try:
            raw = chat_fn(cfg, convo,
                          options={"temperature": 0.1, "num_predict": 400},
                          format_schema=_DECISION_SCHEMA)
            calls += 1
        except Exception as exc:
            LOGGER.exception("investigate: model call failed")
            return _record("FAILED", "", reason="model call failed: %s" % str(exc)[:120])
        decision = _parse_json(raw)
        if decision is None:
            if final:
                return _record("INSUFFICIENT",
                               "Nevaru droši noteikt — nesanāca izlasīt pierādījumus.")
            correction_note = (
                "That was not valid json. Answer with exactly ONE json "
                "object: USE_TOOL with a plan, ANSWER, or REPORT_FAILURE. "
                "Nothing else, no prose around it.")
            continue
        name = str(decision.get("action") or "").strip().upper()

        if name == agent_actions.ANSWER:
            args = decision.get("args") if isinstance(decision.get("args"), dict) else {}
            return _record("ANSWERED", args.get("text") or decision.get("answer") or "",
                           cause=args.get("cause") or "",
                           cites=args.get("cites") or [])
        if name == agent_actions.REPORT_FAILURE:
            args = decision.get("args") if isinstance(decision.get("args"), dict) else {}
            return _record("INSUFFICIENT",
                           args.get("reason")
                           or "Nevaru droši noteikt — pierādījumi to nerāda.")
        if name == agent_actions.ASK_CLARIFICATION:
            args = decision.get("args") if isinstance(decision.get("args"), dict) else {}
            return _record("NEEDS_CLARIFICATION", args.get("question") or "")

        plan = _plan_from(decision) if not final else []
        if not plan:
            if final:
                return _record("INSUFFICIENT",
                               "Nevaru droši noteikt — pierādījumu nepietiek.")
            raw_plan = decision.get("plan")
            if isinstance(raw_plan, list) and raw_plan:
                correction_note = (
                    'Your plan items must each be a json OBJECT: '
                    '{"tool": "<name from the Tools list>", "args": {...}}. '
                    "You sent %r, which is not that shape. Try again with "
                    "real objects." % raw_plan[:2])
            else:
                correction_note = (
                    "Answer with one json object: USE_TOOL with a plan, "
                    "ANSWER, or REPORT_FAILURE.")
            continue
        for item in plan:
            if steps >= int(max_steps):
                break
            ok, why = agent_actions.authorize(
                {"action": agent_actions.USE_TOOL,
                 "args": {"tool": item["tool"], "args": item["args"]}},
                is_owner=True, acfg=acfg)
            if not ok:
                observations.append(_observation(item["tool"], item["args"],
                                                 None, False, why))
                steps += 1
                continue
            res = selfinspect.call(item["tool"], item["args"], allowed=True, who=who)
            observations.append(_observation(
                item["tool"], item["args"], res.get("result"),
                bool(res.get("ok")), res.get("refused") or ""))
            steps += 1
            if task and chat_guid:
                try:
                    from app import obligations
                    obligations.update_investigation(
                        chat_guid, observation={"tool": item["tool"],
                                                "ok": bool(res.get("ok"))},
                        steps_used=steps, model_calls=calls,
                        next_action="VERDICT")
                except Exception:
                    pass
    return _record("INSUFFICIENT",
                   "Nevaru droši noteikt — beidzās soļu budžets.")
