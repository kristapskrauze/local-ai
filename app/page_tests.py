# -*- coding: utf-8 -*-
"""Playwright Test for the pages antons_build makes."""
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from app import browser_render as _br

try:
    from app.logging_setup import get_logger  # type: ignore
    LOGGER = get_logger(__name__)
except Exception:  # pragma: no cover
    import logging
    LOGGER = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
PT_DIR = ROOT / "_tools" / "page_tests"
CLI = PT_DIR / "node_modules" / "@playwright" / "test" / "cli.js"
CONFIG = PT_DIR / "playwright.config.mjs"
SPECS_DIR = PT_DIR / "specs"
# Run copies must sit under PT_DIR: a spec's `import ... "@playwright/test"`
# resolves through the node_modules above it.
RUNS_DIR = PT_DIR / "runs"
RUN_TIMEOUT = 300
PAGE_CHARS = 48000      # page source shown to the model; num_ctx is 32768
WRITE_PREDICT = 12000   # its reasoning plus the tests
JUDGE_PREDICT = 6000

BLANK_HTML = ("<!doctype html><html><head><meta charset=\"utf-8\">"
              "<title>blank</title></head><body></body></html>")

_HEADER = """\
// Playwright tests for {slug}.html, written by the build model from the
// request below and run by app/page_tests.py. By hand:
//   cd _tools/page_tests
//   npx playwright test specs/{slug}.spec.mjs
// Request: {goal}
import {{ test, expect }} from "@playwright/test";

const PAGE_URL = {url};
let pageErrors = [];
test.beforeEach(async ({{ page }}) => {{
  pageErrors = [];
  page.on("pageerror", (e) => pageErrors.push(String((e && e.message) || e)));
  await page.goto(PAGE_URL);
}});
test.afterEach(async () => {{
  expect(pageErrors, "uncaught errors on the page").toEqual([]);
}});

"""

_WRITE_SYS = """\
You write Playwright Test tests for one web page or browser game that was just built. You get what the person asked for and the page's complete source.

Write 3 to 6 tests that use the page the way the person will, on a phone-sized touch screen (420 x 912): tap with locator.tap() or click(), press keys with page.keyboard.press(), swipe with page.mouse or page.touchscreen. After each action, check with Playwright's web-first assertions that what they asked for really happens: await expect(locator).toBeVisible(), .toHaveText(), .toContainText(), .toHaveCount(), .toHaveClass(), .toHaveAttribute(), their .not forms, or await expect.poll(async () => ...).toBe() / .toBeGreaterThan() for a value that changes over time (a score, a timer, a position).

Every test must be able to fail. Before a test counts, it is run on a blank page and on this page with its scripts removed. A test that passes on the blank page checks nothing and is thrown out, and at least one test must fail once the scripts are gone, because only those check that the page really works.

- Each test starts with the page freshly loaded: the file's header already calls page.goto. To start over inside a test, call await page.reload().
- Use selectors that exist in THIS source (ids, classes, roles, visible text), copied exactly. The page's text may be Latvian; match it exactly as the source writes it.
- Check what a person sees on the screen. Use page.evaluate only to read what a canvas draws and the DOM does not show, never to call the page's own functions in place of a real tap or key press.
- Name each test after the behaviour it checks, in English, every title different.
- No waitForTimeout over 1500 ms: let expect and expect.poll do the waiting, with { timeout: 8000 } when something takes a while.
- No try/catch around assertions, no .catch(() => {}), no `|| true`, no test.skip, no expect that only runs under an if.
- Uncaught errors on the page already fail a test: the header listens for them.

Answer with ONLY the test(...) blocks inside one ```js fence -- no imports, no page.goto, nothing else:
```js
test("what it checks", async ({ page }) => {
  ...
});
```"""

_REWRITE_TAIL = ("\n\nWrite the tests again so each one fails on a broken page "
                 "and passes on a working one. Same format: only the test(...) "
                 "blocks, in one ```js fence.")

_JUDGE_SYS = """\
You judge failed Playwright tests. Tests were written for a web page from what the person asked for, then run on the page, and some failed. For each failed test, decide which one is wrong:
- "page": the page does not do what the person asked for, or does it wrongly. The test is right to fail.
- "test": the test is wrong about the page: it looks for something the page has under a different selector or text, expects something the person never asked for, or checks before the page could have reacted. The page does what was asked.
Decide by what the person asked for and by the page source, not by which one is easier to change.
Answer with one JSON object in a ```json fence:
{"verdicts": [{"test": "<the test's exact title>", "fault": "page" or "test", "why": "<one short line>"}]}"""

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_FENCE_RE = re.compile(r"```[ \t]*(?:js|javascript|mjs|ts|typescript)?[ \t]*\n(.*?)```",
                       re.S | re.I)
_PW_IMPORT_RE = re.compile(
    r"^[ \t]*(?:import\s[^;\n]*?from\s*['\"]@playwright/test['\"][ \t]*;?"
    r"|(?:const|let|var)\s*\{[^}]*\}\s*=\s*require\(\s*['\"]@playwright/test['\"]\s*\)[ \t]*;?)"
    r"[ \t]*$", re.M)
_GOTO_RE = re.compile(r"page\.goto\(\s*(['\"`])(?:(?!\1).)*\1", re.S)
_TEST_START_RE = re.compile(r"^[ \t]*test(?:\.only)?\(\s*(['\"`])((?:(?!\1).)*)\1", re.M)


def available() -> bool:
    return bool(_br._node_bin() and CLI.exists() and CONFIG.exists())


def _safe(slug) -> str:
    return re.sub(r"[^\w-]+", "_", str(slug or "page"))[:60] or "page"


def _clean(s) -> str:
    return _ANSI_RE.sub("", str(s or "")).replace("\r", "")


def _compose(slug, goal, body, url_js) -> str:
    return _HEADER.format(slug=_safe(slug), goal=" ".join(str(goal or "").split())[:300],
                          url=url_js) + str(body or "").strip() + "\n"


def _tests_from(text) -> str:
    """The test blocks out of a model answer: the fenced code, without its own
    @playwright/test import (the header has it) and with any page.goto aimed
    at PAGE_URL -- a hardcoded address would test the real page in the blank
    and scripts-removed runs too, and prove nothing."""
    s = str(text or "")
    blocks = _FENCE_RE.findall(s)
    if blocks:
        body = max(blocks, key=len)
    elif "```" in s:
        # cut off before the closing fence
        body = s.split("```", 1)[1]
        body = body.split("\n", 1)[1] if "\n" in body else ""
    else:
        body = s
    body = _PW_IMPORT_RE.sub("", body)
    body = _GOTO_RE.sub("page.goto(PAGE_URL", body)
    return body.strip()


def _json_from(text):
    s = str(text or "")
    m = re.search(r"```(?:json)?\s*\n(.*?)```", s, re.S | re.I)
    cand = m.group(1) if m else s[s.find("{"):s.rfind("}") + 1] if "{" in s else ""
    try:
        return json.loads(cand)
    except Exception:
        return None


def _page_for_prompt(html) -> str:
    s = str(html or "")
    if len(s) <= PAGE_CHARS:
        return s
    return s[:PAGE_CHARS] + "\n<!-- (the rest of the file is cut here) -->"


def _ask(chat, messages, num_predict) -> str:
    try:
        return str(chat(messages, num_predict) or "")
    except Exception as exc:
        LOGGER.warning("page_tests: model call failed: %s", str(exc)[:200])
        return ""



def _kill_tree(pid):
    try:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, timeout=20,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:
        pass


def _new_run_dir(slug, kind) -> Path:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    # a run that crashed can leave its copy behind; nothing is kept past an hour
    for old in RUNS_DIR.iterdir():
        try:
            if old.is_dir() and time.time() - old.stat().st_mtime > 3600:
                shutil.rmtree(str(old), ignore_errors=True)
        except Exception:
            pass
    return Path(tempfile.mkdtemp(prefix="%s-%s-" % (_safe(slug), kind), dir=str(RUNS_DIR)))


def _walk(suite, out, file="", path=()):
    file = file or str(suite.get("file") or suite.get("title") or "")
    for sp in suite.get("specs") or []:
        title = " › ".join(list(path) + [str(sp.get("title") or "?")])
        for t in sp.get("tests") or []:
            results = t.get("results") or []
            last = results[-1] if results else {}
            status = str(last.get("status") or t.get("status") or "")
            errs = last.get("errors") or ([last["error"]] if last.get("error") else [])
            out.append({
                "file": Path(str(sp.get("file") or file)).name,
                "title": title,
                "ok": status == "passed",
                "status": status,
                "error": "\n".join(_clean((e or {}).get("message") or "")
                                   for e in errs).strip(),
            })
    for sub in suite.get("suites") or []:
        _walk(sub, out, file, tuple(path) + (str(sub.get("title") or ""),))


def _launch_failed(err) -> bool:
    e = str(err or "")
    return "browserType.launch" in e or "Executable doesn't exist" in e


def _playwright(run_dir: Path, quick: bool, timeout: int = RUN_TIMEOUT) -> dict:
    """Run every *.spec.mjs in `run_dir` through @playwright/test.
    {tests: [{file, title, ok, status, error}], load_errors, infra, error}."""
    node = _br._node_bin()
    report = run_dir / "report.json"
    env = dict(os.environ)
    env.update({"PAGE_TESTS_DIR": str(run_dir), "PAGE_TESTS_REPORT": str(report),
                "PAGE_TESTS_OUT": str(run_dir / "out"), "FORCE_COLOR": "0", "CI": "1"})
    for k in ("PAGE_TESTS_EXPECT_MS", "PAGE_TESTS_ACTION_MS", "PAGE_TESTS_TEST_MS"):
        env.pop(k, None)
    if quick:
        # the blank / scripts-removed runs only need to see a test fail
        env.update({"PAGE_TESTS_EXPECT_MS": "1500", "PAGE_TESTS_ACTION_MS": "1500",
                    "PAGE_TESTS_TEST_MS": "20000"})
    if not node:
        return {"tests": [], "load_errors": [], "infra": True, "error": "no node"}
    # one browser at a time on this rig: the build self-test and web_read share
    # this lock for the same reason
    if not _br._LOCK.acquire(timeout=timeout + 5):
        return {"tests": [], "load_errors": [], "infra": True, "error": "browser busy"}
    t0 = time.time()
    try:
        try:
            proc = subprocess.Popen(
                [node, str(CLI), "test", "--config", str(CONFIG)],
                cwd=str(PT_DIR), env=env, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, encoding="utf-8",
                errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception as exc:
            return {"tests": [], "load_errors": [], "infra": True,
                    "error": "playwright did not start: %s" % str(exc)[:200]}
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_tree(proc.pid)
            try:
                proc.communicate(timeout=20)
            except Exception:
                pass
            return {"tests": [], "load_errors": [], "infra": True,
                    "error": "playwright timed out after %ds" % timeout}
    finally:
        _br._LOCK.release()
    try:
        data = json.loads(report.read_text(encoding="utf-8"))
    except Exception:
        return {"tests": [], "load_errors": [], "infra": True,
                "error": "no report (exit %s): %s" % (
                    proc.returncode, " ".join(_clean(err or out or "").split())[-300:])}
    tests: list = []
    for s in data.get("suites") or []:
        _walk(s, tests)
    load_errors = [" ".join(_clean((e or {}).get("message") or e).split())[:400]
                   for e in (data.get("errors") or [])]
    infra = bool(tests) and not any(t["ok"] for t in tests) and all(
        _launch_failed(t["error"]) for t in tests)
    if not tests and any(_launch_failed(e) for e in load_errors):
        infra = True
    return {"tests": tests, "load_errors": load_errors, "infra": infra,
            "error": "browser did not launch" if infra else "",
            "seconds": round(time.time() - t0, 1)}


def _inert(html) -> str:
    """The page with every script taken out: the markup without behaviour."""
    return re.sub(r"<script\b[^>]*>.*?</script\s*>", "", str(html or ""),
                  flags=re.S | re.I)


def _prove(slug, goal, body, html) -> dict:
    """Run the tests on a blank page and on the page with no scripts.
    vacuous   = passed on the blank page (or skipped): checks nothing
    static    = failed blank, passed without scripts: checks markup only
    behaviour = failed both: checks that the page works"""
    d = _new_run_dir(slug, "proof")
    try:
        (d / "blank.html").write_text(BLANK_HTML, encoding="utf-8")
        (d / "inert.html").write_text(_inert(html), encoding="utf-8")
        for name in ("blank", "inert"):
            (d / ("%s.spec.mjs" % name)).write_text(
                _compose(slug, goal, body, json.dumps((d / ("%s.html" % name)).as_uri())),
                encoding="utf-8", newline="\n")
        r = _playwright(d, quick=True)
    finally:
        shutil.rmtree(str(d), ignore_errors=True)
    if r.get("infra"):
        return {"infra": True, "error": r.get("error"), "titles": []}
    blank = {t["title"]: t for t in r["tests"] if t["file"] == "blank.spec.mjs"}
    inert = {t["title"]: t for t in r["tests"] if t["file"] == "inert.spec.mjs"}
    if not blank:
        return {"titles": [], "load_error": " | ".join(r.get("load_errors") or [])
                or "no test(...) ran"}
    titles = list(blank)
    vacuous = [x for x in titles
               if blank[x]["ok"] or "skipped" in (blank[x]["status"],
                                                   (inert.get(x) or {}).get("status"))]
    static = [x for x in titles if x not in vacuous and (inert.get(x) or {}).get("ok")]
    behaviour = [x for x in titles if x not in vacuous and x not in static]
    return {"titles": titles, "vacuous": vacuous, "static": static,
            "behaviour": behaviour, "load_error": ""}


def _proof_problems(p) -> str:
    if p.get("load_error"):
        return "The test file did not load, so nothing ran: %s" % str(p["load_error"])[:600]
    out = []
    if p.get("vacuous"):
        out.append("These tests PASSED on a blank page with nothing on it (or were "
                   "skipped), so they cannot catch anything: %s."
                   % ", ".join("«%s»" % t for t in p["vacuous"]))
    if not p.get("behaviour"):
        out.append("Every test also PASSED with the page's scripts removed, so none "
                   "of them checks that the page actually works: drive the page "
                   "with taps or keys and check what changes.")
    return "\n".join(out)


def _rank(p):
    """Higher is a better proven spec."""
    if not p or p.get("load_error") or not p.get("titles"):
        return (-1, 0, 0)
    return (len(p.get("behaviour") or []),
            -len(p.get("vacuous") or []),
            len(p.get("static") or []))


def _goal_text(goal, change) -> str:
    g = str(goal or "").strip()
    if str(change or "").strip():
        g += "\n\nTHE CHANGE JUST MADE (test it too):\n" + str(change).strip()
    return g


def _write_user(goal, slug, html) -> str:
    return "WHAT THE PERSON ASKED FOR:\n%s\n\nTHE PAGE (%s.html):\n%s" % (
        goal, _safe(slug), _page_for_prompt(html))


def _spec_path(slug) -> Path:
    return SPECS_DIR / ("%s.spec.mjs" % _safe(slug))


def _save(spec):
    try:
        SPECS_DIR.mkdir(parents=True, exist_ok=True)
        p = _spec_path(spec["slug"])
        p.write_text(_compose(spec["slug"], spec["goal"], spec["body"],
                              "process.env.PAGE_URL || %s" % json.dumps(spec["url"])),
                     encoding="utf-8", newline="\n")
        meta = {k: spec.get(k) for k in ("slug", "url", "written", "attempts", "titles",
                                         "vacuous", "static", "behaviour", "weak",
                                         "rewritten", "triage", "last_run")}
        (SPECS_DIR / ("%s.spec.json" % _safe(spec["slug"]))).write_text(
            json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8", newline="\n")
    except Exception:
        LOGGER.exception("page_tests: could not save the spec for %s", spec.get("slug"))


def prepare(slug, goal, html, chat, url=None, change="") -> dict | None:
    if not available():
        return None
    url = url or "http://127.0.0.1:8765/static/%s.html" % _safe(slug)
    goal_text = _goal_text(goal, change)
    convo = [{"role": "system", "content": _WRITE_SYS},
             {"role": "user", "content": _write_user(goal_text, slug, html)}]
    best: "dict | None" = None
    for attempt in (1, 2):
        raw = _ask(chat, convo, WRITE_PREDICT)
        body = _tests_from(raw)
        proof = (_prove(slug, goal_text, body, html) if body
                 else {"titles": [], "load_error": "the answer held no test(...) blocks"})
        if proof.get("infra"):
            LOGGER.warning("page_tests: %s -- Playwright could not run: %s",
                           slug, proof.get("error"))
            return None
        if best is None or _rank(proof) > _rank(best):
            best = dict(proof, body=body, attempts=attempt)
        problems = _proof_problems(proof)
        LOGGER.info("page_tests: %s spec attempt %d: %d test(s), %d vacuous, %d "
                    "static, %d behaviour%s", slug, attempt,
                    len(proof.get("titles") or []), len(proof.get("vacuous") or []),
                    len(proof.get("static") or []), len(proof.get("behaviour") or []),
                    (" -- " + problems[:200]) if problems else "")
        if not problems:
            break
        convo += [{"role": "assistant", "content": raw[-20000:]},
                  {"role": "user", "content": problems + _REWRITE_TAIL}]
    if not best or not best.get("titles") or best.get("load_error"):
        return None
    counted = [t for t in best["titles"] if t not in (best.get("vacuous") or [])]
    if not counted:
        LOGGER.info("page_tests: %s -- every test passed on a blank page; no "
                    "Playwright tests for this build", slug)
        return None
    spec = {"slug": slug, "goal": goal_text, "url": url, "body": best["body"],
            "titles": best["titles"], "vacuous": best.get("vacuous") or [],
            "static": best.get("static") or [], "behaviour": best.get("behaviour") or [],
            "weak": not best.get("behaviour"), "attempts": best["attempts"],
            "rewritten": False, "triage": [], "last_run": None,
            "written": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "path": str(_spec_path(slug))}
    _save(spec)
    return spec


def _short_error(t) -> str:
    e = str(t.get("error") or t.get("status") or "failed")
    head, _, log = e.partition("Call log:")
    lines = [ln.strip() for ln in head.splitlines() if ln.strip()]
    # Playwright appends the source lines around the failure; the title says which
    lines = [ln for ln in lines if not re.match(r"^(>?\s*\d+\s*\||\|\s*\^|at\s)", ln)]
    tail = [ln.strip() for ln in log.splitlines() if ln.strip()][:3]
    return " | ".join(lines + tail)[:420]


def _failure_line(t) -> str:
    return "Playwright «%s»: %s" % (t["title"], _short_error(t)[:300])


def _run_real(spec, url) -> dict:
    d = _new_run_dir(spec["slug"], "run")
    try:
        (d / "page.spec.mjs").write_text(
            _compose(spec["slug"], spec["goal"], spec["body"], json.dumps(url)),
            encoding="utf-8", newline="\n")
        r = _playwright(d, quick=False)
    finally:
        shutil.rmtree(str(d), ignore_errors=True)
    if r.get("infra") or (not r.get("tests") and r.get("load_errors")):
        return {"infra": True, "passed": 0, "total": 0, "failures": [], "failed": [],
                "error": r.get("error") or " | ".join(r.get("load_errors") or [])[:300]}
    skip = set(spec.get("vacuous") or [])
    counted = [t for t in r["tests"] if t["title"] not in skip]
    failed = [t for t in counted if not t["ok"]]
    spec["_failed"] = failed
    res = {"infra": False, "error": "", "passed": len(counted) - len(failed),
           "total": len(counted), "weak": bool(spec.get("weak")),
           "failures": list(dict.fromkeys(_failure_line(t) for t in failed))[:4],
           "failed": [t["title"] for t in failed], "rewritten": bool(spec.get("rewritten"))}
    spec["last_run"] = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "passed": res["passed"],
                        "total": res["total"], "failed": res["failed"]}
    return res


def _blocks(body) -> list:
    """(title, source) of each top-level test(...) in the spec body."""
    s = str(body or "")
    starts = [(m.start(), m.group(2)) for m in _TEST_START_RE.finditer(s)]
    out = []
    for i, (pos, title) in enumerate(starts):
        end = starts[i + 1][0] if i + 1 < len(starts) else len(s)
        out.append((title, s[pos:end].strip()))
    return out


def _judge(spec, failed, html, chat) -> list:
    user = ("WHAT THE PERSON ASKED FOR:\n%s\n\nTHE TESTS:\n```js\n%s\n```\n\n"
            "WHAT FAILED:\n%s\n\nTHE PAGE:\n%s" % (
                spec["goal"], spec["body"],
                "\n".join("- «%s»: %s" % (t["title"], _short_error(t)) for t in failed),
                _page_for_prompt(html)))
    raw = _ask(chat, [{"role": "system", "content": _JUDGE_SYS},
                      {"role": "user", "content": user}], JUDGE_PREDICT)
    data = _json_from(raw)
    if not isinstance(data, dict) or not data.get("verdicts"):
        LOGGER.info("page_tests: %s -- no verdict parsed from: %s", spec.get("slug"),
                    " ".join(str(raw or "(empty)").split())[:300])
    names = {t["title"] for t in failed}
    out = []
    for v in ((data or {}).get("verdicts") or []) if isinstance(data, dict) else []:
        if not isinstance(v, dict):
            continue
        title = str(v.get("test") or "")
        fault = str(v.get("fault") or "").strip().lower()
        if title in names and fault in ("page", "test"):
            out.append({"test": title, "fault": fault, "why": str(v.get("why") or "")[:240]})
    return out


def run(spec, url=None, first=False, chat=None, html_path=None) -> dict:
    """Run the spec on the served page. On the FIRST run of a fresh page, the
    model judges each failure (page or test?) and a test it finds wrong is
    rewritten once, proven again, and the page re-run with it.
    {passed, total, failures: [line], failed: [title], weak, rewritten, infra, error}"""
    url = url or spec["url"]
    res = _run_real(spec, url)
    if res.get("infra") or not first or chat is None or not res.get("failed"):
        if not res.get("infra"):
            _save(spec)
        return res
    try:
        html = Path(html_path).read_text(encoding="utf-8") if html_path else ""
    except Exception:
        html = ""
    verdicts = _judge(spec, spec.get("_failed") or [], html, chat)
    spec["triage"] = verdicts
    wrong = [v for v in verdicts if v["fault"] == "test"]
    LOGGER.info("page_tests: %s -- %d test(s) failed on the fresh page; judged: %s",
                spec["slug"], len(res["failed"]),
                "; ".join("%s=%s" % (v["test"][:50], v["fault"]) for v in verdicts) or "nothing")
    if not wrong:
        _save(spec)
        return res
    convo = [{"role": "system", "content": _WRITE_SYS},
             {"role": "user", "content": _write_user(spec["goal"], spec["slug"], html)},
             {"role": "assistant", "content": "```js\n%s\n```" % spec["body"]},
             {"role": "user", "content":
              "These tests failed on the page and were judged wrong about the page, "
              "not the page wrong:\n%s\n\nRewrite just those so they check the same "
              "thing the person asked for, using what the page really has. Keep every "
              "other test exactly as it is. Same format: only the test(...) blocks, "
              "in one ```js fence." % "\n".join(
                  "- «%s»: %s" % (v["test"], v["why"]) for v in wrong)}]
    body = _tests_from(_ask(chat, convo, WRITE_PREDICT))
    proof = _prove(spec["slug"], spec["goal"], body, html) if body else {}
    # a rewrite that proves LESS than the tests it replaces is a test made to
    # pass, not a test fixed: keep the old one and let the page be repaired
    if (proof.get("infra") or proof.get("load_error") or not proof.get("titles")
            or len(proof.get("behaviour") or []) < len(spec.get("behaviour") or [])):
        LOGGER.info("page_tests: %s -- rewritten tests proved less (%s); keeping "
                    "the first ones", spec["slug"], _rank(proof))
        _save(spec)
        return res
    spec.update({"body": body, "titles": proof["titles"],
                 "vacuous": proof.get("vacuous") or [], "static": proof.get("static") or [],
                 "behaviour": proof.get("behaviour") or [],
                 "weak": not proof.get("behaviour"), "rewritten": True})
    res = _run_real(spec, url)
    _save(spec)
    return res


def repair_context(spec) -> str:
    """The failing tests' own code for the repair pass: what a person did and
    what they should then have seen."""
    failed = {t["title"] for t in (spec or {}).get("_failed") or []}
    if not failed:
        return ""
    picked = [src for title, src in _blocks(spec.get("body"))
              if title in failed or any(f.endswith("› " + title) for f in failed)]
    if not picked:
        return ""
    return ("THE PLAYWRIGHT TESTS THAT FAILED (what a person does, and what they "
            "must then see):\n```js\n%s\n```" % "\n\n".join(picked)[:3500])


def merge(res, pw) -> dict:
    """The self-test verdict with the Playwright run folded in: its failures
    join the list the repair loop works from; its counts ride along under
    `playwright` (the summary line shows them apart)."""
    out = dict(res or {})
    if not pw:
        return out
    if pw.get("infra"):
        out["playwright"] = {"error": pw.get("error") or "could not run"}
        return out
    out["playwright"] = {"passed": pw.get("passed", 0), "total": pw.get("total", 0),
                         "weak": bool(pw.get("weak")), "failures": list(pw.get("failures") or [])}
    if pw.get("failures"):
        out["failures"] = list(out.get("failures") or []) + list(pw["failures"])
        out["ok"] = False
    return out
