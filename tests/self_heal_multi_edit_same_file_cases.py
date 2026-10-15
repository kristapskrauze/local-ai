# -*- coding: utf-8 -*-
"""Two edits to the SAME file in one proposal, chained not clobbered.

Two backups of the same path also broke a clean revert on a red test: they
were restored in forward order, so a same-file two-edit apply that failed
its suite ended up reverted to "after edit 1, before edit 2" instead of the
true pristine content edit 1 started from.

Every scratch file this suite creates or backs up is removed in a finally.

    py -3 tests/self_heal_multi_edit_same_file_cases.py
"""
import io
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import self_heal as SH  # noqa: E402

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("    %-64s %s" % (name, "ok" if ok else "FAIL  " + str(detail)[:100]))


_ORIGINAL = (
    "# -*- coding: utf-8 -*-\n"
    "FIRST = 1\n"
    "SECOND = 2\n"
    "def main():\n"
    "    assert isinstance(FIRST, int) and isinstance(SECOND, int)\n"
    '    print("1 passed, 0 failed")\n'
    "    return 0\n\n\n"
    'if __name__ == "__main__":\n'
    "    import sys; sys.exit(main())\n"
)
# The suite's own assert only checks the TYPE, not the value, so changing
# both FIRST and SECOND still passes -- case 2 needs a suite that goes green
# once both edits land, to prove neither was silently dropped.


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def case_verify_edits_chains_two_edits_to_one_file():
    print("1. verify_edits: edit 2's find is checked against edit 1's result")
    path = "tests/_scratch_chain_probe_cases.py"
    real = SH._resolved(path)
    _write(real, _ORIGINAL)
    try:
        checked = SH.verify_edits([
            {"path": path, "find": "FIRST = 1", "replace": "FIRST = 10"},
            {"path": path, "find": "SECOND = 2", "replace": "SECOND = 20"},
        ])
        check("both edits accepted", len(checked) == 2, checked)
        check("edit 2's `before` already contains edit 1's change",
              "FIRST = 10" in checked[1]["before"], checked[1]["before"])
        check("edit 2's `after` contains BOTH changes",
              "FIRST = 10" in checked[1]["after"]
              and "SECOND = 20" in checked[1]["after"], checked[1]["after"])
        check("verify_edits itself never writes to disk",
              real.read_text(encoding="utf-8") == _ORIGINAL)
    finally:
        if real.exists():
            real.unlink()


def case_apply_edits_keeps_both_changes_not_just_the_last():
    print("")
    print("2. apply_edits: both edits land, the second does not erase the first")
    path = "tests/_scratch_chain_apply_probe_cases.py"
    real = SH._resolved(path)
    _write(real, _ORIGINAL)
    try:
        res = SH.apply_edits([
            {"path": path, "find": "FIRST = 1", "replace": "FIRST = 10"},
            {"path": path, "find": "SECOND = 2", "replace": "SECOND = 20"},
        ], suites=[path], reason="test")
        check("applied", res.get("applied") is True, res)
        on_disk = real.read_text(encoding="utf-8")
        check("edit 1 survived on disk", "FIRST = 10" in on_disk, on_disk)
        check("edit 2 survived on disk", "SECOND = 20" in on_disk, on_disk)
        check("only one backup was made for the one path touched",
              len(res.get("backups") or []) == 1, res.get("backups"))
    finally:
        if real.exists():
            real.unlink()


def case_revert_on_red_restores_the_true_pristine_file():
    print("")
    print("3. a red suite reverts BOTH edits back to the true original, "
          "not to an in-between state")
    path = "tests/_scratch_chain_revert_probe_cases.py"
    real = SH._resolved(path)
    _write(real, _ORIGINAL)
    try:
        res = SH.apply_edits([
            {"path": path, "find": "FIRST = 1", "replace": "FIRST = 10"},
            {"path": path,
             "find": "assert isinstance(FIRST, int) and isinstance(SECOND, int)",
             "replace": 'assert False, "deliberately wrong"'},
        ], suites=[path], reason="test")
        check("not applied (second edit deliberately breaks the assert)",
              res.get("applied") is False, res)
        check("reverted", res.get("reverted") is True, res)
        on_disk = real.read_text(encoding="utf-8")
        check("file is back to the EXACT original, not left at edit 1",
              on_disk == _ORIGINAL, on_disk)
    finally:
        if real.exists():
            real.unlink()


def case_second_edit_to_a_just_created_file_also_chains():
    print("")
    print("4. a create (empty find) followed by a normal edit to the SAME "
          "new path also chains")
    path = "tests/_scratch_chain_create_probe_cases.py"
    real = SH._resolved(path)
    check("does not exist beforehand", not real.exists())
    try:
        checked = SH.verify_edits([
            {"path": path, "find": "", "replace": _ORIGINAL},
            {"path": path, "find": "FIRST = 1", "replace": "FIRST = 10"},
        ])
        check("both accepted", len(checked) == 2, checked)
        check("second edit's `after` has the creation AND the tweak",
              "SECOND = 2" in checked[1]["after"]
              and "FIRST = 10" in checked[1]["after"], checked[1]["after"])
    finally:
        if real.exists():
            real.unlink()


def case_second_empty_find_on_an_already_touched_path_is_refused():
    print("")
    print("5. a SECOND empty-find edit for a path this batch already "
          "touched is refused, not silently accepted")
    path = "tests/_scratch_chain_double_create_probe_cases.py"
    real = SH._resolved(path)
    try:
        SH.verify_edits([
            {"path": path, "find": "", "replace": _ORIGINAL},
            {"path": path, "find": "", "replace": "something else"},
        ])
        check("refused", False, "did not raise")
    except SH.HealError as exc:
        check("refused", "already has an earlier edit" in str(exc), exc)
    finally:
        if real.exists():
            real.unlink()


def main():
    print(__doc__.strip().splitlines()[0])
    print("")
    for fn in (case_verify_edits_chains_two_edits_to_one_file,
               case_apply_edits_keeps_both_changes_not_just_the_last,
               case_revert_on_red_restores_the_true_pristine_file,
               case_second_edit_to_a_just_created_file_also_chains,
               case_second_empty_find_on_an_already_touched_path_is_refused):
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
