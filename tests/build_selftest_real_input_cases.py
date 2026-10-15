# -*- coding: utf-8 -*-
"""A page's own checks can touch it like a person: __tap / __key / __swipe.

Runs the real off-screen Chrome on two fixture pages (skipped without it).

    py -3 tests/build_selftest_real_input_cases.py
"""
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import build_selftest as S  # noqa: E402

HEAD = ('<!doctype html><html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<style>html,body{margin:0;height:100%%;background:#111;color:#fff}'
        '#go{position:absolute;left:100px;top:300px;width:200px;height:80px}'
        '#pad{position:absolute;left:50px;top:450px;width:300px;height:200px;background:#333}'
        '%s</style></head><body>')

COVERED = (HEAD % "#veil{position:fixed;inset:0;opacity:0}") + """
<button id="go">Start</button><div id="veil"></div>
<script>var state = 'menu';
document.getElementById('go').addEventListener('click', function () { state = 'playing'; });</script>
<script type="application/json" id="selftest">[
{"name":"a synthetic click starts it","js":"document.getElementById('go').click();return state==='playing';"},
{"name":"a real tap starts it","js":"await __tap('#go');return state==='playing';"}
]</script></body></html>"""

PLAIN = (HEAD % "") + """
<button id="go">Start</button><div id="pad"></div>
<script>window.got = {tap: false, key: '', swipe: 0, trusted: true};
document.getElementById('go').addEventListener('click', function (e) {
  got.tap = true; got.trusted = got.trusted && e.isTrusted; });
document.addEventListener('keydown', function (e) {
  got.key = e.key; got.trusted = got.trusted && e.isTrusted; });
var sx = null, pad = document.getElementById('pad');
pad.addEventListener('touchstart', function (e) { sx = e.touches[0].clientX; });
pad.addEventListener('touchend', function (e) { got.swipe = e.changedTouches[0].clientX - sx; });
</script>
<script type="application/json" id="selftest">[
{"name":"tap","js":"await __tap('#go');return got.tap&&got.trusted;"},
{"name":"key","js":"await __key('ArrowLeft');return got.key==='ArrowLeft'&&got.trusted;"},
{"name":"swipe","js":"await __swipe('#pad',-120,0);return got.swipe<-60;"},
{"name":"tap at a point","js":"var r=document.getElementById('go').getBoundingClientRect();await __tapAt(r.left+5,r.top+5);return got.tap;"}
]</script></body></html>"""


def _run(page):
    d = Path(tempfile.mkdtemp(prefix="real_input_"))
    p = d / "page.html"
    p.write_text(page, encoding="utf-8")
    out = S.run_selftest(p.as_uri())
    return {c["name"]: c for c in out.get("checks") or []}, out


@unittest.skipUnless(S.available(), "node / Chrome not available")
class RealInputTests(unittest.TestCase):
    def test_a_real_tap_lands_on_what_is_on_top(self):
        checks, out = _run(COVERED)
        self.assertTrue(checks["a synthetic click starts it"]["pass"], out)
        self.assertFalse(checks["a real tap starts it"]["pass"], out)

    def test_taps_keys_and_swipes_arrive_as_a_person_makes_them(self):
        checks, out = _run(PLAIN)
        for name in ("tap", "key", "swipe", "tap at a point"):
            self.assertTrue(checks[name]["pass"], (name, checks.get(name), out.get("failures")))


if __name__ == "__main__":
    unittest.main()
