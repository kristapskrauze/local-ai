# -*- coding: utf-8 -*-
"""The verification ladder, and the thing it must never do.

Two behaviours are load-bearing and pull in opposite directions:

  * a change that introduces a new lint or type error is reverted, and
  * a change to a file that is ALREADY full of lint and type errors is not,
    because this repository has 85 of the former and 498 of the latter and a
    gate that refused to touch messaging.py would simply be turned off.

Everything here runs against a scratch file inside the sandbox, so a failure
leaves nothing behind. The suites are stubbed where a case is about the ladder
rather than about the tests.

    py tests\\verify_ladder_cases.py
"""
import io
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import self_heal, verify   # noqa: E402

PASS, FAIL = [], []
SCRATCH = "tests/_ladder_scratch.py"
ABS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "tests", "_ladder_scratch.py")

CLEAN = '''# -*- coding: utf-8 -*-
"""Scratch file for the ladder suite. Deleted when the suite finishes."""


def add(a, b):
    return a + b
'''


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("    %-54s %s" % (name[:54], "ok" if ok else "FAIL  " + str(detail)[:70]))
    return ok


def write_clean():
    io.open(ABS, "w", encoding="utf-8", newline="\n").write(CLEAN)


def cleanup():
    for path in (ABS,):
        try:
            os.remove(path)
        except OSError:
            pass


def case_stage_order():
    print("\n1. the ladder runs cheapest and most fundamental first")
    check("declared order is ast -> tests_run -> ruff -> pyright",
          verify.LADDER == ("ast", "tests_run", "ruff", "pyright"), verify.LADDER)

    write_clean()
    io.open(ABS, "a", encoding="utf-8", newline="\n").write("def broken(:\n")
    out = verify.verify_paths([SCRATCH])
    check("a file that does not parse fails at ast",
          out["failed_stage"] == "ast", out["failed_stage"])
    check("no later stage is run once ast fails",
          [s["stage"] for s in out["stages"]] == ["ast"],
          [s["stage"] for s in out["stages"]])


def case_clean_change_passes():
    print("\n2. a clean change passes every stage")
    write_clean()
    out = verify.verify_paths([SCRATCH])
    check("ok", out["ok"] is True, out.get("failed_stage"))
    check("all four stages ran",
          [s["stage"] for s in out["stages"]] == ["ast", "tests_run", "ruff", "pyright"],
          [s["stage"] for s in out["stages"]])


def case_new_violation_blocks():
    print("\n3. a NEW violation blocks")
    write_clean()
    io.open(ABS, "a", encoding="utf-8", newline="\n").write(
        "\n\ndef uses_missing():\n    return not_a_real_name\n")
    out = verify.verify_paths([SCRATCH])
    check("blocked", out["ok"] is False)
    check("blamed on ruff", out["failed_stage"] == "ruff", out["failed_stage"])
    finding = next((f for s in out["stages"] for f in s["findings"]), {})
    check("names the undefined symbol",
          "not_a_real_name" in str(finding.get("message")), finding)
    check("reports baseline 0 -> now 1",
          finding.get("baseline") == 0 and finding.get("now") == 1, finding)


def case_substitution_is_caught():
    print("\n3b. swapping one broken name for another does not slip through")
    write_clean()
    io.open(ABS, "a", encoding="utf-8", newline="\n").write(
        "\n\ndef first():\n    return name_one\n")
    before = verify.verify_paths([SCRATCH])
    check("the first undefined name is caught", before["ok"] is False)
    first_msg = str(next(s for s in before["stages"] if s["stage"] == "ruff")
                    ["findings"][0]["message"])
    check("and it is named", "name_one" in first_msg, first_msg)

    # same count, different symbol
    text = io.open(ABS, encoding="utf-8").read().replace("name_one", "name_two")
    io.open(ABS, "w", encoding="utf-8", newline="\n").write(text)
    after = verify.verify_paths([SCRATCH])
    check("the substituted name is caught too", after["ok"] is False)
    msg = str(next(s for s in after["stages"] if s["stage"] == "ruff")
              ["findings"][0]["message"])
    check("and it names the NEW symbol", "name_two" in msg, msg)
    check("fingerprints are what make that possible",
          verify._fingerprint("Undefined name `x` at line 12")
          == verify._fingerprint("Undefined name `x` at line 99"),
          verify._fingerprint("Undefined name `x` at line 12"))
    check("...while different symbols stay different",
          verify._fingerprint("Undefined name `a`")
          != verify._fingerprint("Undefined name `b`"))


def case_exception_still_reverts():
    print("\n3c. an exception during the checks still puts the files back")
    write_clean()
    before = io.open(ABS, encoding="utf-8").read()
    edits = [{"path": SCRATCH, "find": "    return a + b",
              "replace": "    return b + a"}]
    try:
        self_heal.apply_edits(edits, suites=("not_a_path_just_a_name",),
                              reason="ladder test")
        check("a bad suite name raises", False, "no HealError")
    except self_heal.HealError as exc:
        check("a bad suite name raises", True)
        check("the message says it was put back", "put back" in str(exc), str(exc))
    after = io.open(ABS, encoding="utf-8").read()
    check("the file is byte-identical again", after == before)


def case_existing_debt_is_inert():
    print("\n4. a file already carrying findings is not blamed for them")
    # messaging.py has both lint and type findings today; the ladder must
    # pass it untouched or nobody could ever edit it again.
    out = verify.verify_paths(["app/messaging.py"])
    lint = next(s for s in out["stages"] if s["stage"] == "ruff")
    check("messaging.py passes ruff on its baseline", lint["ok"], lint["findings"][:2])
    if out["ok"]:
        types = next((s for s in out["stages"] if s["stage"] == "pyright"), {})
        check("messaging.py passes pyright on its baseline",
              types.get("ok"), (types.get("findings") or [])[:2])
    baseline = verify._load(verify.TYPE_BASELINE)
    check("its type debt is genuinely recorded, not ignored",
          any(k.startswith("app/messaging.py::") for k in baseline),
          len(baseline))
    check("the debt is substantial (so this case means something)",
          sum(v for k, v in baseline.items()
              if k.startswith("app/messaging.py::")) > 50,
          sum(v for k, v in baseline.items()
              if k.startswith("app/messaging.py::")))


def case_apply_edits_reverts_on_new_violation():
    print("\n5. apply_edits reverts a change the ladder rejects")
    write_clean()
    before = io.open(ABS, encoding="utf-8").read()
    edits = [{"path": SCRATCH,
              "find": "    return a + b",
              "replace": "    return a + b + undefined_thing"}]
    out = self_heal.apply_edits(edits, suites=(), reason="ladder test")
    after = io.open(ABS, encoding="utf-8").read()
    check("not applied", out["applied"] is False, out.get("note"))
    check("reverted", out["reverted"] is True)
    check("file is byte-identical to before", after == before)
    check("says which stage refused it",
          out.get("checks", {}).get("failed_stage") == "ruff", out.get("checks"))
    check("the note names the symbol",
          "undefined_thing" in str(out.get("note")), out.get("note"))
    check("no suite was run once the ladder failed", out["suites"] == [],
          out["suites"])


def case_apply_edits_keeps_a_good_change():
    print("\n6. apply_edits keeps a change that passes")
    write_clean()
    edits = [{"path": SCRATCH,
              "find": "    return a + b",
              "replace": "    return b + a"}]
    out = self_heal.apply_edits(edits, suites=(), reason="ladder test")
    after = io.open(ABS, encoding="utf-8").read()
    # suites=() means green is False by design in self_heal (nothing proved
    # it), so the edit is reverted -- but it must have passed the LADDER.
    check("ladder passed the change", out.get("checks", {}).get("ok") is True,
          out.get("checks", {}).get("failed_stage"))
    check("ast/tests_run/ruff/pyright all ran",
          [s["stage"] for s in out["checks"]["stages"]] ==
          ["ast", "tests_run", "ruff", "pyright"], out["checks"]["stages"])
    check("with no suites to prove it, it is still reverted",
          out["applied"] is False and "return a + b" in after, out.get("note"))


def case_exact_match_contract_intact():
    print("\n7. the exact-match-once contract still comes first")
    write_clean()
    try:
        self_heal.verify_edits([{"path": SCRATCH, "find": "text that is not there",
                                 "replace": "x"}])
        check("invented text is refused", False, "no HealError raised")
    except self_heal.HealError as exc:
        check("invented text is refused", True)
        check("the refusal says what did not match",
              "not" in str(exc).lower() or "match" in str(exc).lower(), str(exc))
    try:
        self_heal.verify_edits([{"path": "../outside.py", "find": "a", "replace": "b"}])
        check("a path outside the sandbox is refused", False, "no error")
    except Exception:
        check("a path outside the sandbox is refused", True)


def case_missing_tool_is_not_a_pass():
    print("\n8. a checker that cannot run is not a passing checker")
    real = verify.PYRIGHT
    verify.PYRIGHT = r"C:\does\not\exist\pyright.cmd"
    try:
        write_clean()
        out = verify.pyright_check([SCRATCH])
        check("missing pyright reports not-ok", out["ok"] is False, out)
        check("and says why", "could not run" in str(out.get("error")), out)
    finally:
        verify.PYRIGHT = real


# An undefined name, not an unused variable: ruff's config here does not
# report F841 under tests/, and this has to be a finding the ladder can see.
LEFTOVER = "\n\ndef leftover():\n    return old_missing_name\n"


def case_pre_edit_debt_is_not_blamed():
    print("\n4b. debt the file already had before the edit is not charged to it")
    write_clean()
    io.open(ABS, "a", encoding="utf-8", newline="\n").write(LEFTOVER)
    blind = verify.verify_paths([SCRATCH])
    check("with no snapshot the old finding blocks",
          blind["ok"] is False and blind["failed_stage"] == "ruff",
          blind.get("failed_stage"))
    before = verify.snapshot([SCRATCH])
    check("the snapshot records the finding that is already there",
          any(k.endswith("::F821") for k in (before.get("ruff") or {}).get("counts", {})),
          before)
    text = io.open(ABS, encoding="utf-8").read().replace("return a + b", "return b + a")
    io.open(ABS, "w", encoding="utf-8", newline="\n").write(text)
    out = verify.verify_paths([SCRATCH], before=before)
    check("an unrelated change passes against the snapshot", out["ok"] is True,
          out.get("failed_stage"))
    io.open(ABS, "a", encoding="utf-8", newline="\n").write(
        "\n\ndef fresh():\n    return brand_new_missing\n")
    out = verify.verify_paths([SCRATCH], before=before)
    check("a finding the change introduced still blocks",
          out["ok"] is False and out["failed_stage"] == "ruff", out.get("failed_stage"))
    msg = str(next((f for s in out["stages"] for f in s["findings"]), {}).get("message"))
    check("and it names the new symbol", "brand_new_missing" in msg, msg)


def case_apply_edits_uses_the_snapshot():
    print("\n5b. apply_edits judges a change against the file as it just was")
    write_clean()
    io.open(ABS, "a", encoding="utf-8", newline="\n").write(LEFTOVER)
    edits = [{"path": SCRATCH, "find": "    return a + b",
              "replace": "    return b + a"}]
    out = self_heal.apply_edits(edits, suites=(), reason="ladder test")
    check("the ladder passes a change made next to old debt",
          out.get("checks", {}).get("ok") is True, out.get("note"))


def main():
    print(__doc__.strip().splitlines()[0])
    started = time.time()
    cases = [case_stage_order, case_clean_change_passes,
             case_new_violation_blocks, case_substitution_is_caught,
             case_exception_still_reverts, case_existing_debt_is_inert,
             case_pre_edit_debt_is_not_blamed,
             case_apply_edits_reverts_on_new_violation,
             case_apply_edits_uses_the_snapshot,
             case_apply_edits_keeps_a_good_change,
             case_exact_match_contract_intact,
             case_missing_tool_is_not_a_pass]
    try:
        for fn in cases:
            try:
                fn()
            except Exception as exc:
                check(fn.__name__, False, "%s: %s" % (type(exc).__name__, exc))
    finally:
        cleanup()
    print("\n%d passed, %d failed  (%.0fs)"
          % (len(PASS), len(FAIL), time.time() - started))
    for name in FAIL:
        print("   FAILED: %s" % name)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
