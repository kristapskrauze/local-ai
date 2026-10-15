"""Playwright tests for built pages: written by the model, proven able to fail.

    python tests/page_tests_cases.py
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import build_selftest as BS  # noqa: E402
from app import page_tests as PT  # noqa: E402

COUNTER = """<!doctype html><html><head><meta charset="utf-8"><title>Skaitītājs</title></head>
<body><h1>Skaitītājs</h1><div id="count">0</div><button id="plus">+</button>
<script>
document.getElementById("plus").addEventListener("click", function () {
  var c = document.getElementById("count");
  c.textContent = String(Number(c.textContent) + 1);
});
</script></body></html>"""

# the same page with the bug the tests must catch: + does nothing
BROKEN = COUNTER.replace("Number(c.textContent) + 1", "Number(c.textContent)")

REAL_TEST = """```js
test("tapping plus raises the count", async ({ page }) => {
  await page.locator("#plus").tap();
  await expect(page.locator("#count")).toHaveText("1");
});
```"""

VACUOUS_TEST = """```js
test("tapping plus raises the count", async ({ page }) => {
  await page.locator("#plus").tap();
  await expect(page.locator("#count")).toHaveText("1");
});

test("always fine", async ({ page }) => {
  expect(1).toBe(1);
});
```"""

WRONG_SELECTOR = REAL_TEST.replace("#plus", "#plusButton")


class Model:
    """Stands in for the build model: hands out answers in order, keeps the
    prompts it was given."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, messages, num_predict):
        self.calls.append(messages)
        return self.answers.pop(0) if self.answers else ""


def _page(tmp, html, name="page.html"):
    p = Path(tmp) / name
    p.write_text(html, encoding="utf-8")
    return p


def _with_tmp(fn):
    def wrapped():
        tmp = tempfile.mkdtemp(prefix="page_tests_cases_")
        saved = PT.SPECS_DIR
        PT.SPECS_DIR = Path(tmp) / "specs"
        try:
            fn(tmp)
        finally:
            PT.SPECS_DIR = saved
            shutil.rmtree(tmp, ignore_errors=True)
    wrapped.__name__ = fn.__name__
    return wrapped


@_with_tmp
def test_a_test_that_passes_on_a_blank_page_is_not_counted(tmp):
    page = _page(tmp, COUNTER)
    model = Model(VACUOUS_TEST, VACUOUS_TEST)
    spec = PT.prepare("counter", "A counter: tapping + adds one.", COUNTER, model,
                      url=page.as_uri())
    assert spec, "no spec"
    assert spec["vacuous"] == ["always fine"], spec
    assert spec["behaviour"] == ["tapping plus raises the count"], spec
    assert len(model.calls) == 2
    assert "«always fine»" in model.calls[1][-1]["content"]
    res = PT.run(spec, page.as_uri())
    assert (res["passed"], res["total"]) == (1, 1), res
    assert (Path(tmp) / "specs" / "counter.spec.mjs").exists()


@_with_tmp
def test_a_broken_page_fails_and_the_repair_sees_the_test(tmp):
    page = _page(tmp, BROKEN)
    model = Model(REAL_TEST,
                  '```json\n{"verdicts": [{"test": "tapping plus raises the count", '
                  '"fault": "page", "why": "+ does not add"}]}\n```')
    spec = PT.prepare("counter", "A counter: tapping + adds one.", BROKEN, model,
                      url=page.as_uri())
    assert spec and spec["behaviour"] == ["tapping plus raises the count"], spec
    res = PT.run(spec, page.as_uri(), first=True, chat=model, html_path=page)
    assert (res["passed"], res["total"]) == (0, 1), res
    assert res["failures"][0].startswith("Playwright «tapping plus raises the count»"), res
    assert spec["triage"][0]["fault"] == "page"
    ctx = PT.repair_context(spec)
    assert '#plus' in ctx and "toHaveText" in ctx, ctx
    merged = PT.merge({"passed": 3, "total": 3, "failures": []}, res)
    assert merged["failures"] == res["failures"] and merged["ok"] is False
    assert BS.summary_line(merged).endswith("· Playwright 0/1"), BS.summary_line(merged)


@_with_tmp
def test_a_test_judged_wrong_is_rewritten_once_and_proven_again(tmp):
    page = _page(tmp, COUNTER)
    model = Model(WRONG_SELECTOR,
                  '```json\n{"verdicts": [{"test": "tapping plus raises the count", '
                  '"fault": "test", "why": "the button is #plus"}]}\n```',
                  REAL_TEST)
    spec = PT.prepare("counter", "A counter: tapping + adds one.", COUNTER, model,
                      url=page.as_uri())
    assert spec and "#plusButton" in spec["body"]
    res = PT.run(spec, page.as_uri(), first=True, chat=model, html_path=page)
    assert res["rewritten"] is True and (res["passed"], res["total"]) == (1, 1), res
    assert "#plusButton" not in spec["body"]


@_with_tmp
def test_a_rewrite_that_proves_less_is_refused(tmp):
    page = _page(tmp, BROKEN)
    model = Model(REAL_TEST,
                  '```json\n{"verdicts": [{"test": "tapping plus raises the count", '
                  '"fault": "test", "why": "too strict"}]}\n```',
                  '```js\ntest("fine", async ({ page }) => {\n  expect(1).toBe(1);\n});\n```')
    spec = PT.prepare("counter", "A counter: tapping + adds one.", BROKEN, model,
                      url=page.as_uri())
    res = PT.run(spec, page.as_uri(), first=True, chat=model, html_path=page)
    # the test that caught the bug is kept, and the page still fails it
    assert res["rewritten"] is False and (res["passed"], res["total"]) == (0, 1), res
    assert "#plus" in spec["body"]


@_with_tmp
def test_a_file_that_does_not_load_goes_back_to_the_model(tmp):
    page = _page(tmp, COUNTER)
    broken_js = '```js\ntest("x", async ({ page }) => {\n  await page.locator("#plus").tap(;\n});\n```'
    model = Model(broken_js, REAL_TEST)
    spec = PT.prepare("counter", "A counter: tapping + adds one.", COUNTER, model,
                      url=page.as_uri())
    assert spec and spec["attempts"] == 2, spec
    assert "did not load" in model.calls[1][-1]["content"]


def test_answer_cleanup_keeps_the_proof_honest():
    body = PT._tests_from(
        "Here:\n```javascript\nimport { test, expect } from '@playwright/test';\n"
        "test(\"t\", async ({ page }) => {\n"
        "  await page.goto('http://127.0.0.1:8765/static/x.html');\n"
        "  await expect(page.locator('#a')).toBeVisible();\n});\n```")
    assert "import" not in body, body
    # a hardcoded address would test the real page in the blank run too
    assert "page.goto(PAGE_URL)" in body, body
    assert PT._inert("<p>a</p><script>x()</script><SCRIPT src=y.js></SCRIPT>") == "<p>a</p>"


if __name__ == "__main__":
    if not PT.available():
        print("SKIP: Playwright is not installed in _tools/page_tests")
        sys.exit(0)
    for fn in (test_answer_cleanup_keeps_the_proof_honest,
               test_a_test_that_passes_on_a_blank_page_is_not_counted,
               test_a_broken_page_fails_and_the_repair_sees_the_test,
               test_a_test_judged_wrong_is_rewritten_once_and_proven_again,
               test_a_rewrite_that_proves_less_is_refused,
               test_a_file_that_does_not_load_goes_back_to_the_model):
        fn()
        print("ok", fn.__name__)
    print("OK")
