"""A suite runs only what its __main__ block reaches.

    python tests/verify_tests_run_cases.py
"""
import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import verify as V  # noqa: E402

SCRATCH = "tests/_scratch_tests_run_cases.py"
ABS = os.path.join(ROOT, "tests", "_scratch_tests_run_cases.py")

WIRED = '''def test_a():
    assert True


def case_b():
    assert True


def main():
    case_b()


if __name__ == "__main__":
    test_a()
    main()
    print("OK")
'''


def test_a_test_the_block_never_calls_is_named():
    assert V.unrun_tests(WIRED + "\n\ndef test_new():\n    assert False\n") == ["test_new"]


def test_tests_reached_through_main_or_a_loop_count_as_run():
    assert V.unrun_tests(WIRED) == []
    loop = ('def test_a():\n    pass\n\n\ndef test_b():\n    pass\n\n\n'
            'if __name__ == "__main__":\n    for fn in (test_a, test_b):\n        fn()\n')
    assert V.unrun_tests(loop) == []


def test_a_file_with_no_block_runs_nothing():
    assert V.unrun_tests("def test_a():\n    assert False\n") == ["test_a"]


def test_a_runner_that_finds_its_own_tests_is_trusted():
    src = ('def test_a():\n    pass\n\n\nif __name__ == "__main__":\n'
           '    for name, fn in list(globals().items()):\n'
           '        if name.startswith("test_"):\n            fn()\n')
    assert V.unrun_tests(src) == []


def test_self_heal_lets_a_self_discovering_runner_through():
    from app import self_heal as SH
    src = ('def test_a():\n    pass\n\n\nif __name__ == "__main__":\n'
           '    for name, fn in list(globals().items()):\n'
           '        if name.startswith("test_") and callable(fn):\n            fn()\n'
           '    print("OK")\n')
    assert SH._test_file_runs_its_cases(src) is True
    # a file that never runs its tests is still refused
    assert SH._test_file_runs_its_cases("def test_a():\n    assert False\n") is False


def test_the_ladder_blames_only_a_new_unrun_test():
    old = WIRED + "\n\ndef test_old():\n    pass\n"
    try:
        io.open(ABS, "w", encoding="utf-8", newline="\n").write(old)
        before = V.snapshot([SCRATCH]).get("tests_run") or {}
        assert before.get(SCRATCH) == ["test_old"], before
        # what was already unreached before the change does not count against it
        assert V.tests_run_check([SCRATCH], before=before)["ok"] is True
        io.open(ABS, "w", encoding="utf-8", newline="\n").write(
            old + "\n\ndef test_added():\n    pass\n")
        out = V.tests_run_check([SCRATCH], before=before)
        assert out["ok"] is False, out
        assert [f["message"].split()[0] for f in out["findings"]] == ["test_added"], out
        assert out["findings"][0]["line"] and out["findings"][0]["path"] == SCRATCH, out
        # a file the change creates has no before: everything unreached counts
        assert len(V.tests_run_check([SCRATCH])["findings"]) == 2
    finally:
        try:
            os.remove(ABS)
        except OSError:
            pass


if __name__ == "__main__":
    test_a_test_the_block_never_calls_is_named()
    test_tests_reached_through_main_or_a_loop_count_as_run()
    test_a_file_with_no_block_runs_nothing()
    test_a_runner_that_finds_its_own_tests_is_trusted()
    test_self_heal_lets_a_self_discovering_runner_through()
    test_the_ladder_blames_only_a_new_unrun_test()
    print("OK")
