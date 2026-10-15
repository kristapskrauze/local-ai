# -*- coding: utf-8 -*-
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app import self_heal  # noqa: E402


def _rejected(edits):
    try:
        self_heal.verify_edits(edits)
    except self_heal.HealError as exc:
        return str(exc)
    return None


def test_cross_language_new_file_rejected():
    # app/static/pet.html exists; creating app/pet.py is
    # the model routing an edit to an invented Python path
    why = _rejected([{"path": "app/pet.py", "find": "",
                      "replace": "def anim():\n    return []\n"}])
    assert why and "pet.html" in why, why


def test_orphan_new_module_rejected():
    why = _rejected([{"path": "app/totally_unused_zzz.py", "find": "",
                      "replace": "X = 1\n"}])
    assert why and "nothing imports" in why, why


def test_noop_test_file_rejected():
    src = "def test_something():\n    assert 1 == 1\n"
    why = _rejected([{"path": "tests/scratch_noop_probe_cases.py", "find": "",
                      "replace": src}])
    assert why and "never calls them" in why, why


def test_proper_test_file_accepted():
    src = ("def test_something():\n    assert 1 == 1\n\n"
           "if __name__ == \"__main__\":\n    test_something()\n    print(\"OK\")\n")
    # legit new test file -> no HealError
    self_heal.verify_edits([{"path": "tests/scratch_ok_probe_cases.py",
                             "find": "", "replace": src}])


def test_real_edit_to_existing_file_still_works():
    # editing an existing file with a matching find is untouched by the guards
    target = "app/static/pet.html"
    src = (ROOT and __import__("pathlib").Path(ROOT, *target.split("/")).read_text(
        encoding="utf-8"))
    anchor = "function anim() {"
    assert anchor in src
    self_heal.verify_edits([{"path": target, "find": anchor,
                             "replace": anchor + "  /* x */"}])


def test_helper_unit():
    assert self_heal._test_file_runs_its_cases(
        "def test_x():\n    assert 1\nif __name__=='__main__':\n    test_x()") is True
    assert self_heal._test_file_runs_its_cases(
        "def test_x():\n    assert 1\n") is False
    assert self_heal._test_file_runs_its_cases("print('hello')\n") is True


if __name__ == "__main__":
    test_cross_language_new_file_rejected()
    test_orphan_new_module_rejected()
    test_noop_test_file_rejected()
    test_proper_test_file_accepted()
    test_real_edit_to_existing_file_still_works()
    test_helper_unit()
    print("OK")
