# -*- coding: utf-8 -*-
"""self_heal.apply_edits, the owner check and the auto-accept switch are faked, so
nothing is written and no model runs.

    py -3 tests/proposal_approval_cases.py
"""
import inspect
import io
import os
import sys
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import agent_tools as T  # noqa: E402
from app import messaging as M  # noqa: E402
from app import rig_agent, self_heal  # noqa: E402

PASS, FAIL = [], []
CHAT = "iMessage;-;proposal-approval-test"
STATE = {"applied": True, "auto_accept": False}
CALLS = []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("    %-62s %s" % (name, "ok" if ok else "FAIL  " + str(detail)[:110]))


def fake_apply(edits, suites=(), reason=""):
    CALLS.append(edits)
    return {"applied": STATE["applied"], "reverted": not STATE["applied"],
            "checks": {}, "note": "" if STATE["applied"] else "ruff failed"}


def propose(stamp):
    M._LAST_PROPOSAL[CHAT] = (stamp, {"proposal": {
        "edits": [{"path": "app/x.py", "find": "a", "replace": "b"}],
        "test": {"suite": "tests/x_cases.py"}}})


def apply_now():
    ctx = T.AgentContext(actor="test", conversation_id=CHAT, sender="owner",
                         allow_privileged=True)
    return T._tool_antons_apply_fix({}, ctx)


def fresh():
    CALLS.clear()
    STATE.update(applied=True, auto_accept=False)
    M._LAST_PROPOSAL.pop(CHAT, None)
    M.clear_proposal_approval(CHAT)


class _Captured(Exception):
    pass


def agent_convo_for(text):
    seen = []

    def fake_run(conversation=None, **_kw):
        seen.extend(conversation or [])
        raise _Captured()

    saved = M.qwen_agent.run_agent_turn
    M.qwen_agent.run_agent_turn = fake_run
    try:
        M._live_assistant_agent_reply({}, [{"role": "user", "content": text}],
                                      CHAT, text, text, sender="owner",
                                      owner_authored=True)
    except _Captured:
        pass
    finally:
        M.qwen_agent.run_agent_turn = saved
    return seen


def main():
    saved = (self_heal.apply_edits, rig_agent.owner_allowed, M._auto_accept_state)
    self_heal.apply_edits = fake_apply
    rig_agent.owner_allowed = lambda *args, **kwargs: True
    M._auto_accept_state = lambda acfg=None: (STATE["auto_accept"], 0.0)
    try:
        print("1. a proposal nobody said yes to is not applied")
        fresh()
        propose(time.time())
        res = apply_now()
        check("refused", res.get("ok") is False and "yes" in str(res.get("reason")), res)
        check("apply_edits never ran", not CALLS, CALLS)

        print("")
        print("2. his yes applies the proposal he saw, once")
        fresh()
        propose(time.time())
        M.approve_pending_proposal(CHAT)
        res = apply_now()
        check("applied", len(CALLS) == 1, (res, CALLS))
        check("the yes is spent", not M.pending_proposal_approved(CHAT))

        print("")
        print("3. a proposal written after his yes needs its own")
        fresh()
        now = time.time()
        propose(now)
        M.approve_pending_proposal(CHAT)
        propose(now + 1.0)
        res = apply_now()
        check("the rewrite is refused", res.get("ok") is False and not CALLS, (res, CALLS))

        print("")
        print("4. a rolled-back attempt spends the yes too")
        fresh()
        propose(time.time())
        M.approve_pending_proposal(CHAT)
        STATE["applied"] = False
        apply_now()
        first = len(CALLS)
        res = apply_now()
        check("the first attempt ran", first == 1, first)
        check("a second attempt on the same yes is refused",
              len(CALLS) == 1 and res.get("ok") is False, (res, CALLS))

        print("")
        print("5. with auto-accept on no separate yes is needed")
        fresh()
        STATE["auto_accept"] = True
        propose(time.time())
        apply_now()
        check("applied", len(CALLS) == 1, CALLS)

        print("")
        print("6. the self-chat records his yes where it routes to the apply")
        src = inspect.getsource(M._live_assistant_agent_reply)
        check("approve_pending_proposal is called on _apply_pending",
              "approve_pending_proposal(chat_guid)" in src)

        print("")
        print("7. his yes shows the model the proposal it approves")
        fresh()
        M._LAST_PROPOSAL[CHAT] = (time.time() - 120, {
            "ok": True, "state": "PROPOSED", "proposal": {
                "files": [{"path": "app/zz_probe.py", "why": "the probe"}],
                "test": {"suite": "tests/zz_probe_cases.py", "case": "c",
                         "asserts": "a"},
                "edits": [{"path": "app/zz_probe.py", "find": "", "replace": "X = 1\n"}],
                "patch": "+X = 1", "risks": ["none"],
                "plan": ["tests/zz_probe_cases.py"], "rehearsal": "passed"}})
        saved_owner = rig_agent.owner_allowed
        try:
            seen = agent_convo_for("Jā.")
        finally:
            rig_agent.owner_allowed = saved_owner
        notes = "\n".join(str(m.get("content") or "") for m in seen
                          if m.get("role") == "system")
        check("the model was reached", bool(seen), seen)
        check("the waiting proposal is in front of it",
              "app/zz_probe.py" in notes and "tests/zz_probe_cases.py" in notes,
              notes[-400:])
        check("his yes is recorded against it", M.pending_proposal_approved(CHAT))
    finally:
        self_heal.apply_edits, rig_agent.owner_allowed, M._auto_accept_state = saved
        fresh()

    print("")
    print("%d passed, %d failed" % (len(PASS), len(FAIL)))
    for name in FAIL:
        print("   FAILED: %s" % name)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
