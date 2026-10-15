"""Anton's code reader redacts keys, not long code names.

    python tests/selfinspect_redact_cases.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import selfinspect as S  # noqa: E402


def test_long_code_names_are_shown():
    line = "def test_location_trigger_failure_still_returns_reminder():"
    assert S.redact(line) == line
    assert S.redact("x = some-long-kebab-case-identifier-for-a-css-rule") == (
        "x = some-long-kebab-case-identifier-for-a-css-rule")


def test_keys_are_still_redacted():
    assert "[REDACTED]" in S.redact("password: hunter2")
    hexkey = "9f2c4e81a7b3d6c05e8f1a2b3c4d5e6f7a8b9c0d"
    assert S.redact("k = '%s'" % hexkey) == "k = '[REDACTED]'"
    b64 = "AbCdEfGhIjKlMnOpQrStUvWxYzAbCdEfGhIj"          # letters only, no separator
    assert S.redact(b64) == "[REDACTED]"
    with_digits = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
    assert S.redact(with_digits) == "[REDACTED]"


if __name__ == "__main__":
    test_long_code_names_are_shown()
    test_keys_are_still_redacted()
    print("OK")
