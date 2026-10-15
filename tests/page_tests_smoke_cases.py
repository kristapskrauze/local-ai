"""Playwright runs here, from this account: one real test on a tiny page.

The build pipeline's Playwright step (app/page_tests.py) runs inside the
SysMonitor service as LocalSystem, whose profile and temp folders are not
dobby's. This is the quick check that the browser launches and a test passes
from whatever account runs it -- a few seconds, so the rig's own test tool
(30 s) can run it as well.

    python tests/page_tests_smoke_cases.py
"""
import json
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import page_tests as PT  # noqa: E402

PAGE = ('<!doctype html><html><head><meta charset="utf-8"><title>t</title></head>'
        '<body><button id="b" onclick="this.textContent=\'ok\'">go</button></body></html>')
TEST = ('test("tapping the button changes it", async ({ page }) => {\n'
        '  await page.locator("#b").tap();\n'
        '  await expect(page.locator("#b")).toHaveText("ok");\n'
        '});')


def test_a_real_playwright_test_runs_and_passes():
    d = PT._new_run_dir("smoke", "check")
    try:
        (d / "page.html").write_text(PAGE, encoding="utf-8")
        (d / "page.spec.mjs").write_text(
            PT._compose("smoke", "smoke check", TEST, json.dumps((d / "page.html").as_uri())),
            encoding="utf-8", newline="\n")
        r = PT._playwright(d, quick=True, timeout=25)
    finally:
        shutil.rmtree(str(d), ignore_errors=True)
    assert not r.get("infra"), r
    assert [(t["title"], t["ok"]) for t in r["tests"]] == [
        ("tapping the button changes it", True)], r


if __name__ == "__main__":
    if not PT.available():
        print("FAIL: Playwright is not installed in _tools/page_tests")
        sys.exit(1)
    test_a_real_playwright_test_runs_and_passes()
    print("OK")
