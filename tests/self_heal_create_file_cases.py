# -*- coding: utf-8 -*-
"""self_heal can create a new file, not just edit an existing one.

An edit with an empty `find` on a path that does not yet exist now creates
that file; `replace` is the whole new file, verbatim. Every scratch file this
suite creates is removed in a finally, whether the test passes or not.

    py -3 tests/self_heal_create_file_cases.py
"""
import io
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import agent_tools as AT  # noqa: E402
from app import self_heal as SH  # noqa: E402

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("    %-56s %s" % (name, "ok" if ok else "FAIL  " + str(detail)[:100]))


_PASSING = ('# -*- coding: utf-8 -*-\n'
           'def main():\n'
           '    assert 1 + 1 == 2\n'
           '    print("1 passed, 0 failed")\n'
           '    return 0\n\n\n'
           'if __name__ == "__main__":\n'
           '    import sys; sys.exit(main())\n')

_FAILING = ('# -*- coding: utf-8 -*-\n'
           'def main():\n'
           '    assert 1 + 1 == 3, "deliberately wrong"\n'
           '    return 0\n\n\n'
           'if __name__ == "__main__":\n'
           '    import sys; sys.exit(main())\n')


def case_verify_edits_creates_a_new_file():
    print("1. verify_edits: empty find + new path -> creation, not a write")
    path = "tests/_scratch_verify_probe_cases.py"
    real = SH._resolved(path)
    check("does not exist beforehand", not real.exists())
    checked = SH.verify_edits([{"path": path, "find": "", "replace": _PASSING}])
    check("accepted", len(checked) == 1, checked)
    check("before is empty (nothing existed)", checked[0]["before"] == "")
    check("verify_edits itself never writes to disk", not real.exists())


def case_cannot_create_over_an_existing_file():
    print("")
    print("2. empty find on a path that already exists is refused")
    try:
        SH.verify_edits([{"path": "tests/run_offline.py",
                          "find": "", "replace": "x"}])
        check("refused", False, "did not raise")
    except SH.HealError as exc:
        check("refused", "already exists" in str(exc), exc)


def case_creating_empty_content_is_refused():
    print("")
    print("3. an empty find with blank replace is refused, not a no-op create")
    try:
        SH.verify_edits([{"path": "tests/_scratch_blank_probe_cases.py",
                          "find": "", "replace": "   "}])
        check("refused", False, "did not raise")
    except SH.HealError as exc:
        check("refused", "content" in str(exc), exc)


def case_apply_creates_and_keeps_a_passing_new_test():
    print("")
    print("4. apply_edits creates the file when its own new test passes")
    path = "tests/_scratch_apply_pass_probe_cases.py"
    real = SH._resolved(path)
    try:
        res = SH.apply_edits([{"path": path, "find": "", "replace": _PASSING}],
                             suites=[path], reason="test")
        check("applied", res.get("applied") is True, res)
        check("file exists on disk", real.exists())
        check("suite it just created actually ran and passed",
              res.get("suites") and res["suites"][0].get("ok"), res.get("suites"))
    finally:
        if real.exists():
            real.unlink()


def case_apply_deletes_a_created_file_that_fails_its_own_test():
    print("")
    print("5. a new file whose own test fails is deleted, not left behind")
    path = "tests/_scratch_apply_fail_probe_cases.py"
    real = SH._resolved(path)
    try:
        res = SH.apply_edits([{"path": path, "find": "", "replace": _FAILING}],
                             suites=[path], reason="test")
        check("not applied", res.get("applied") is False, res)
        check("reverted", res.get("reverted") is True, res)
        check("file does NOT exist -- revert of a creation means delete",
              not real.exists())
    finally:
        if real.exists():
            real.unlink()   # would be a bug if reached, but never leave cruft


def case_antons_run_tests_is_read_only_and_owner_gated():
    print("")
    print("6. antons_run_tests: read-only, reports real pass/fail, owner-gated")
    reg = AT.default_registry()
    tool = reg.get("antons_run_tests")
    check("registered as READ_ONLY", tool is not None and tool.risk == AT.READ_ONLY,
          tool.risk if tool else None)
    owner = AT.AgentContext(allow_private=True, allow_privileged=True,
                            sender="owner@example.com")
    ok_run = reg.execute("antons_run_tests", {"suite": "tests/test_build_rules_md.py"}, owner)
    check("runs a real suite and reports green",
          ok_run.get("ok") and (ok_run.get("result") or {}).get("result", {}).get("ok"),
          ok_run)
    missing = reg.execute("antons_run_tests", {"suite": "tests/nope_cases.py"}, owner)
    check("a missing suite reports missing, does not crash",
          (missing.get("result") or {}).get("result", {}).get("missing") is True,
          missing)
    stranger = AT.AgentContext(allow_private=True, allow_privileged=True,
                               sender="+37100000000")
    denied = reg.execute("antons_run_tests", {"suite": "tests/code_intel_cases.py"}, stranger)
    check("a stranger is refused", not denied.get("ok"), denied)


def main():
    print(__doc__.strip().splitlines()[0])
    print("")
    for fn in (case_verify_edits_creates_a_new_file,
               case_cannot_create_over_an_existing_file,
               case_creating_empty_content_is_refused,
               case_apply_creates_and_keeps_a_passing_new_test,
               case_apply_deletes_a_created_file_that_fails_its_own_test,
               case_antons_run_tests_is_read_only_and_owner_gated):
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
