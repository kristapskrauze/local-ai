"""A new module whose spec test was written FIRST is not an orphan.

Runs against a temporary ROOT: nothing is written into the repo.

    python tests/self_heal_spec_first_cases.py
"""
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import self_heal  # noqa: E402

MODULE = {"path": "app/zz_new_feature.py", "find": "", "replace": "X = 1\n"}
SPEC = ("from app import zz_new_feature as F\n\n"
        "def test_x():\n    assert F.X == 1\n\n"
        "if __name__ == \"__main__\":\n    test_x()\n    print(\"OK\")\n")


def _root(with_spec_on_disk, spec_imports=True):
    tmp = Path(tempfile.mkdtemp())
    (tmp / "app").mkdir()
    (tmp / "tests").mkdir()
    (tmp / "app" / "__init__.py").write_text("", encoding="utf-8")
    if with_spec_on_disk:
        body = SPEC if spec_imports else SPEC.replace("zz_new_feature", "something_else")
        (tmp / "tests" / "zz_new_feature_cases.py").write_text(body, encoding="utf-8")
    return tmp


def _verdict(root, edits):
    with patch.object(self_heal, "ROOT", root):
        try:
            self_heal.verify_edits(edits)
        except self_heal.HealError as exc:
            return str(exc)
    return None


def test_a_spec_written_first_makes_the_module_wanted():
    assert _verdict(_root(True), [dict(MODULE)]) is None


def test_no_importer_is_still_an_orphan():
    why = _verdict(_root(False), [dict(MODULE)])
    assert why and "nothing imports" in why, why


def test_an_existing_test_that_does_not_import_it_does_not_count():
    why = _verdict(_root(True, spec_imports=False), [dict(MODULE)])
    assert why and "nothing imports" in why, why


if __name__ == "__main__":
    test_a_spec_written_first_makes_the_module_wanted()
    test_no_importer_is_still_an_orphan()
    test_an_existing_test_that_does_not_import_it_does_not_count()
    print("OK")
