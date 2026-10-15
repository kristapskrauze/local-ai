# -*- coding: utf-8 -*-
"""Auto-accept: the switch, its expiry, and what the model is told about it.

Deterministic checks only -- no model judgment -- so these assert outright.
The live config is never mutated: CONFIG_PATH is pointed at a temp copy and
restored in a finally, because a test that leaves auto-accept ON would arm an
apply-and-restart autopilot on the real rig.
"""
import json
import pathlib
import sys
import tempfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from app import messaging as M
from app import agent_tools as AT

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("    %-56s %s" % (name, "ok" if ok else "FAIL  " + str(detail)[:90]))


def case_state_helper():
    print("1. the state helper reads the flag honestly")
    check("absent means off", M._auto_accept_active({}) is False)
    check("false means off", M._auto_accept_active({"auto_accept": False}) is False)
    check("a future expiry means on",
          M._auto_accept_active({"auto_accept": True,
                                 "auto_accept_until": time.time() + 600}) is True)
    check("an EXPIRED flag reads off, not ambiguously on",
          M._auto_accept_active({"auto_accept": True,
                                 "auto_accept_until": time.time() - 1}) is False)
    check("a garbage expiry reads off",
          M._auto_accept_active({"auto_accept": True,
                                 "auto_accept_until": "soon"}) is False)


def case_writes_and_clears():
    print("")
    print("2. the toggle persists to config and clears again")
    original = M.CONFIG_PATH
    tmp = pathlib.Path(tempfile.mkdtemp()) / "messaging.json"
    tmp.write_text(json.dumps({"auto_reply": {"enabled": True}}), encoding="utf-8")
    M.CONFIG_PATH = tmp
    try:
        out = M.set_auto_accept(True, 10)
        raw = json.loads(tmp.read_text(encoding="utf-8"))["auto_reply"]
        check("writes auto_accept", raw.get("auto_accept") is True, raw)
        check("writes an expiry in the future",
              float(raw.get("auto_accept_until") or 0) > time.time(), raw)
        check("reports the wall-clock time back", bool(out.get("until_local")), out)
        check("honours the minutes it was given",
              9 <= (float(raw["auto_accept_until"]) - time.time()) / 60 <= 10.1, out)
        check("the helper now sees it on", M._auto_accept_active(raw) is True)

        M.set_auto_accept(False)
        raw = json.loads(tmp.read_text(encoding="utf-8"))["auto_reply"]
        check("clears both keys",
              "auto_accept" not in raw and "auto_accept_until" not in raw, raw)
        check("other config keys survive the write", raw.get("enabled") is True, raw)

        M.set_auto_accept(True, 99999)
        raw = json.loads(tmp.read_text(encoding="utf-8"))["auto_reply"]
        check("an absurd duration is capped, not honoured",
              (float(raw["auto_accept_until"]) - time.time()) / 60 <= 1441, raw)
    finally:
        M.CONFIG_PATH = original


def case_tool_is_owner_only():
    print("")
    print("3. only he can flip it")
    reg = AT.default_registry()
    check("the tool is registered", reg.get("antons_set_auto_accept") is not None)
    tool = reg.get("antons_set_auto_accept")
    check("it is PRIVILEGED", tool.risk == AT.PRIVILEGED, tool.risk)
    stranger = AT.AgentContext(allow_private=True, allow_privileged=True,
                               sender="+37100000000")
    before = dict((M.load_config().get("auto_reply") or {}))
    got = reg.execute("antons_set_auto_accept", {"enabled": True}, stranger)
    check("a stranger is refused", not got.get("ok"), got.get("error_type"))
    after = dict((M.load_config().get("auto_reply") or {}))
    check("and nothing was written",
          before.get("auto_accept") == after.get("auto_accept")
          and before.get("auto_accept_until") == after.get("auto_accept_until"),
          (before, after))


def case_it_knows_what_it_switched_on():
    print("")
    print("4. it can say what the switch does, not just that it flipped")
    original = M.CONFIG_PATH
    tmp = pathlib.Path(tempfile.mkdtemp()) / "messaging.json"
    tmp.write_text(json.dumps({"auto_reply": {"enabled": True}}), encoding="utf-8")
    M.CONFIG_PATH = tmp
    try:
        out = M.set_auto_accept(True, 30)
        msg = ("Auto-akcepts ir IESLĒGTS līdz %s (%d min)."
               % (out["until_local"], out["minutes"]))
        check("the enabled message names an expiry time", ":" in out["until_local"], msg)
        check("minutes come back for the model to relay", out["minutes"] == 30, out)
    finally:
        M.CONFIG_PATH = original


def main():
    print(__doc__.strip().splitlines()[0])
    print("")
    for fn in (case_state_helper, case_writes_and_clears,
               case_tool_is_owner_only, case_it_knows_what_it_switched_on):
        try:
            fn()
        except Exception as exc:
            check(fn.__name__, False, "%s: %s" % (type(exc).__name__, exc))
    print("")
    print("%d passed, %d failed" % (len(PASS), len(FAIL)))
    for n in FAIL:
        print("   FAILED: %s" % n)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
