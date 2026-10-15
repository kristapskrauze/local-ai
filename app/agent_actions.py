# -*- coding: utf-8 -*-
"""The closed set of things the supervisor may decide to DO.

Modelled on `_canonical_watcher_verdict` in messaging.py, deliberately: that
one already proves the shape works — a closed enum, a canonicaliser that
returns None for anything it does not recognise, and callers that treat None
as "do the safe thing". This is the same contract widened from
{reply, silent} to the agent verbs.

Three gates, in this order, all in code and none of them in the prompt:

Anything that fails a gate comes back as a refusal WITH A REASON, because the
refusal is itself an observation the model has to account for — the same rule
the read-only tools follow.
"""
import logging

LOGGER = logging.getLogger(__name__)

ANSWER = "ANSWER"
ASK_CLARIFICATION = "ASK_CLARIFICATION"
USE_TOOL = "USE_TOOL"
SEARCH_THREAD = "SEARCH_THREAD"
SEARCH_MEMORY = "SEARCH_MEMORY"
RESEARCH = "RESEARCH"
INVESTIGATE_SELF = "INVESTIGATE_SELF"
PROPOSE_PATCH = "PROPOSE_PATCH"
REPORT_FAILURE = "REPORT_FAILURE"

# name -> what it needs. `capability` is a path into the runtime registry
# (app/capabilities.py); None means the action needs nothing but the pipeline
# itself. `owner_only` is a permission, never a probe.
ACTION_SPEC = {
    ANSWER:            {"owner_only": False, "capability": None,
                        "args": ("text",)},
    ASK_CLARIFICATION: {"owner_only": False, "capability": None,
                        "args": ("question",)},
    SEARCH_THREAD:     {"owner_only": False, "capability": "tools.thread_search",
                        "args": ("query", "chat_guid", "limit")},
    SEARCH_MEMORY:     {"owner_only": False, "capability": "tools.memory",
                        "args": ("query", "limit")},
    RESEARCH:          {"owner_only": False, "capability": "tools.web",
                        "args": ("question",)},
    # Reading its own logs and source is owner-only: it exposes the rig, the
    # routing decisions and the code that makes them.
    USE_TOOL:          {"owner_only": True, "capability": "tools.repo_read",
                        "args": ("tool", "args")},
    INVESTIGATE_SELF:  {"owner_only": True, "capability": "tools.logs_read",
                        "args": ("goal", "chat_guid")},
    # The next permission level up: it may WRITE A PROPOSAL, never a file.
    # `tools.code_write` stays false in the registry during this phase, and
    # this action does not check it — proposing is not patching.
    PROPOSE_PATCH:     {"owner_only": True, "capability": "tools.repo_read",
                        "args": ("goal", "chat_guid")},
    REPORT_FAILURE:    {"owner_only": False, "capability": None,
                        "args": ("reason",)},
}
ACTIONS = tuple(ACTION_SPEC)

# Actions that mutate anything are NOT in the table at all. Naming them here
# would make them one permission flag away from reachable; the intent is that
# they require a code change and a separate owner instruction.
FORBIDDEN_THIS_PHASE = ("APPLY_PATCH", "DEPLOY", "RESTART", "WRITE_FILE")


def canonical_action(value):
    """The one closed shape every caller can consume, or None."""
    if not isinstance(value, dict):
        return None
    name = str(value.get("action") or "").strip().upper()
    if name not in ACTION_SPEC:
        return None
    args = value.get("args")
    args = dict(args) if isinstance(args, dict) else {}
    allowed = set(ACTION_SPEC[name]["args"])
    # unknown argument names are dropped rather than passed through: a tool is
    # called with what it declares, never with whatever the model invented
    clean = {k: v for k, v in args.items() if k in allowed}
    out = {"action": name, "args": clean}
    if value.get("reason"):
        out["reason"] = str(value.get("reason"))[:300]
    return out


def authorize(action, is_owner=False, acfg=None, capabilities_mod=None):
    """(ok, reason). Permission first, then whether the machine can do it."""
    act = canonical_action(action) if not isinstance(action, str) else {
        "action": str(action).upper(), "args": {}}
    if not act or act["action"] not in ACTION_SPEC:
        return False, "not an action"
    name = act["action"]
    spec = ACTION_SPEC[name]
    if spec["owner_only"] and not is_owner:
        return False, "%s is owner-only" % name
    if name == USE_TOOL:
        # the tool itself must be one of the read-only ones; a generated name
        # is refused here rather than at the executor
        from app import selfinspect
        tool = str(act["args"].get("tool") or "")
        if tool not in selfinspect.READ_ONLY:
            return False, "%r is not a read-only inspection tool" % tool
    cap = spec["capability"]
    if cap:
        try:
            caps = capabilities_mod
            if caps is None:
                from app import capabilities as caps
            if not caps.can(cap, acfg):
                return False, "%s unavailable: %s" % (cap, caps.why(cap, acfg))
        except Exception as exc:
            return False, "capability check failed: %s" % str(exc)[:80]
    return True, ""


def prompt_menu(is_owner=False):
    """The actions this requester may pick, as prompt text.

    A non-owner is never shown the owner-only ones — the model cannot choose
    what it was not offered, and `authorize` refuses them anyway.
    """
    lines = []
    for name, spec in ACTION_SPEC.items():
        if spec["owner_only"] and not is_owner:
            continue
        args = ", ".join(spec["args"]) or "-"
        lines.append("  %s(%s)" % (name, args))
    return "\n".join(lines)
