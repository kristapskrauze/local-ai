# -*- coding: utf-8 -*-
"""The play-through heuristic cannot fail a game the model's own checks vouch for.

Offline: node and Chrome are faked; the scorer runs on a recorded verdict.

    py -3 tests/build_selftest_vouch_cases.py
"""
import io
import json
import os
import sys
import types
import unittest
from unittest.mock import patch


sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import build_selftest as S  # noqa: E402


PROGRESS = "does not actually progress"


def _verdict(passing_checks, total_checks, real_moves, advanced=False):
    checks = [{"name": "check %d" % i, "pass": i < passing_checks, "detail": ""}
              for i in range(total_checks)]
    return {"ok": True, "passed": passing_checks + 1, "total": total_checks + 2,
            "load_errors": [], "smoke": {"pass": True},
            "checks": checks,
            "play": {"moves": real_moves, "real_moves": real_moves, "advanced": advanced,
                     "ended": "stalled", "peak_score": 0, "peak_max": 0}}


def _score(data):
    fake_run = types.SimpleNamespace(stdout=json.dumps(data), stderr="", returncode=0)
    with patch.object(S._br, "_node_bin", return_value="node"), \
            patch.object(S._br, "_chrome_bin", return_value="chrome"), \
            patch.object(S.subprocess, "run", return_value=fake_run):
        return S.run_selftest("http://127.0.0.1:8765/static/test-page.html")


class PlayThroughVouchTests(unittest.TestCase):
    def test_vouched_game_is_not_failed_by_the_progress_heuristic(self):
        out = _score(_verdict(passing_checks=6, total_checks=6, real_moves=0))
        self.assertFalse(any(PROGRESS in f for f in out["failures"]), out["failures"])
        self.assertTrue(out["ok"], out["failures"])

    def test_the_models_own_failing_checks_still_fail_the_build(self):
        out = _score(_verdict(passing_checks=4, total_checks=6, real_moves=0))
        self.assertFalse(any(PROGRESS in f for f in out["failures"]), out["failures"])
        self.assertEqual(sum(1 for f in out["failures"] if f.startswith("check FAILED")), 2)
        self.assertFalse(out["ok"])

    def test_an_unvouched_tile_game_that_does_not_progress_still_fails(self):
        out = _score(_verdict(passing_checks=1, total_checks=6, real_moves=5))
        self.assertTrue(any(PROGRESS in f for f in out["failures"]), out["failures"])
        self.assertFalse(out["ok"])


if __name__ == "__main__":
    unittest.main()
