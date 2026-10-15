# -*- coding: utf-8 -*-
"""A real tap that lands on something nobody can see fails the build.

TAPS_JS in _build_selftest.mjs asks the browser what a tap on a grid of points
would hit, after the smoke taps and after each of the page's own checks. An
invisible element over a visible one is a failure line nothing vouches away.

Offline cases fake node and Chrome; the last case runs the real off-screen
Chrome on two tiny fixture pages (skipped when node or Chrome is missing).

    py -3 tests/build_selftest_taps_cases.py
"""
import io
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import build_selftest as S  # noqa: E402

BUTTON = "<button id=ovBtn class=btn> «Sākt spēli»"
BLOCKED = {"what": BUTTON, "over": "<canvas id=c>",
           "via": "<div id=overlay class=hidden>", "points": 3}


def _taps(*blocked):
    return {"href": "http://127.0.0.1:8765/static/test-page.html", "sampled": 55,
            "blocked": list(blocked)}


def _verdict(smoke_taps=None, check_taps=None, real_moves=0):
    """Seven passing checks (they vouch), a play-through that saw nothing."""
    checks = [{"name": "check %d" % i, "pass": True, "detail": "",
               "taps": check_taps if check_taps is not None else _taps()}
              for i in range(7)]
    return {"ok": False, "passed": 8, "total": 9, "load_errors": [],
            "smoke": {"pass": True, "taps": smoke_taps if smoke_taps is not None else _taps()},
            "checks": checks,
            "play": {"moves": 3, "real_moves": real_moves, "advanced": False,
                     "ended": "no effect from tap/key/swipe after 0 real move(s)"}}


def _score(data):
    fake_run = types.SimpleNamespace(stdout=json.dumps(data), stderr="", returncode=0)
    with patch.object(S._br, "_node_bin", return_value="node"), \
            patch.object(S._br, "_chrome_bin", return_value="chrome"), \
            patch.object(S.subprocess, "run", return_value=fake_run):
        return S.run_selftest("http://127.0.0.1:8765/static/test-page.html")


class InvisibleTapCatcherTests(unittest.TestCase):
    def test_a_hidden_button_over_the_game_fails_even_when_the_checks_vouch(self):
        out = _score(_verdict(check_taps=_taps(BLOCKED)))
        lines = [f for f in out["failures"] if "invisible" in f]
        self.assertEqual(len(lines), 1, out["failures"])
        self.assertIn("Sākt spēli", lines[0])
        self.assertIn("<canvas id=c>", lines[0])
        self.assertIn("<div id=overlay class=hidden>", lines[0])
        self.assertFalse(out["ok"])

    def test_the_same_catcher_seen_in_every_state_is_one_line(self):
        out = _score(_verdict(smoke_taps=_taps(BLOCKED), check_taps=_taps(BLOCKED)))
        self.assertEqual(sum(1 for f in out["failures"] if "invisible" in f), 1,
                         out["failures"])

    def test_nothing_blocked_adds_nothing(self):
        out = _score(_verdict())
        self.assertEqual(out["failures"], [])

    def test_the_report_names_the_red_item_instead_of_a_question_mark(self):
        out = _score(_verdict())
        line = S.summary_line(out)
        self.assertNotIn("?", line)
        self.assertIn("automātiskā izspēle", line)


def _fixture(broken):
    button_css = "pointer-events:auto" if broken else ""
    return """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
html,body{margin:0;height:100%%;background:#111;color:#fff;font-family:sans-serif}
canvas{position:fixed;inset:0;display:block;width:100vw;height:100vh}
#pick{position:fixed;left:30px;bottom:30px;width:160px;height:50px;background:#333}
#pick select{position:absolute;inset:0;width:100%%;height:100%%;opacity:0}
#overlay{position:fixed;inset:0;display:flex;align-items:center;justify-content:center;
  background:rgba(0,0,0,.92);transition:opacity .3s;z-index:5}
#overlay.hidden{opacity:0;pointer-events:none}
#overlay button{padding:18px 60px;font-size:20px;%s}
</style></head><body>
<canvas id="c"></canvas>
<div id="pick">Level 1<select><option>1</option><option>2</option></select></div>
<div id="overlay"><button id="go">Start</button></div>
<script>
var c = document.getElementById('c'), g = c.getContext('2d');
c.width = innerWidth; c.height = innerHeight;
g.fillStyle = '#4af'; g.fillRect(20, 20, c.width - 40, 60);
var state = 'menu';
document.getElementById('go').onclick = function () {
  state = 'playing'; document.getElementById('overlay').classList.add('hidden'); };
</script>
<script type="application/json" id="selftest">[
{"name":"start begins the game","js":"document.getElementById('go').click();return state==='playing';"}
]</script>
</body></html>""" % button_css


@unittest.skipUnless(S.available(), "node / Chrome not available")
class RealBrowserTests(unittest.TestCase):
    def _run(self, broken):
        d = Path(tempfile.mkdtemp(prefix="taps_case_"))
        page = d / ("broken.html" if broken else "fixed.html")
        page.write_text(_fixture(broken), encoding="utf-8")
        return S.run_selftest(page.as_uri())

    def test_a_faded_overlay_whose_button_still_takes_taps_is_caught(self):
        out = self._run(broken=True)
        lines = [f for f in out["failures"] if "invisible" in f]
        self.assertTrue(lines, out)
        self.assertIn("<button id=go>", lines[0])
        self.assertIn("<canvas id=c>", lines[0])

    def test_the_fixed_page_and_a_transparent_select_are_left_alone(self):
        out = self._run(broken=False)
        self.assertFalse([f for f in out["failures"] if "invisible" in f], out["failures"])


if __name__ == "__main__":
    unittest.main()
