# -*- coding: utf-8 -*-
"""Check a change the way a reviewer would, and only blame it for its own faults.

self_heal already refused invented patches (the `find` text must appear exactly
once in the real file) and already reverted anything that failed its suites.
Between those two there was nothing: a change could be real, apply cleanly,
leave the file importing a name that does not exist, and still pass, because
the suites that run are not the code that broke.

So this is the middle of the ladder:

    syntax/AST -> ruff -> pyright -> allowlisted suites -> diff -> keep or revert

with one rule that decides whether it is usable at all: A CHANGE IS JUDGED
AGAINST WHAT THE FILE LOOKED LIKE BEFORE IT. This repository carries 85 ruff
findings and 497 pyright errors that predate any of this -- 152 in
messaging.py alone. A gate that demanded a clean file would refuse every edit
to the files most worth editing, and the first thing anyone would do is switch
it off. Instead each touched file is compared with its own recorded baseline,
so old debt is inert and a NEW finding in a file being changed is a blocker.

The baselines are counts per file-and-rule rather than line numbers, because
line numbers move for innocent reasons and a rule appearing where it did not
appear before is the signal worth having. Rebaselining is a deliberate act:

    py -3 app/verify.py --rebaseline

Never do it to make a regression go away. That is the one thing this file is
for.
"""

import collections
import io
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from app import paths

ROOT = Path(__file__).resolve().parent.parent
LINT_BASELINE = ROOT / "lint-baseline.json"
TYPE_BASELINE = ROOT / "type-baseline.json"

PYTHON = os.environ.get("SELF_HEAL_PYTHON") or paths.SERVICE_PYTHON
# Pyright is a node program; the service runs as LocalSystem and its PATH does
# not include the npm prefix, so the npm-global copy is the fallback.
PYRIGHT = (os.environ.get("RIG_PYRIGHT_CMD") or shutil.which("pyright")
           or os.path.join(paths.NPM_GLOBAL, "pyright.cmd"))

TOOL_TIMEOUT = 300


def _rel(path):
    try:
        return str(Path(path).resolve().relative_to(ROOT)).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")


def _loadfull(path):
    try:
        return json.load(io.open(path, encoding="utf-8"))
    except Exception:
        return {}


def _load(path):
    try:
        return json.load(io.open(path, encoding="utf-8")).get("counts") or {}
    except Exception:
        return {}


def ast_check(paths):
    import ast
    bad = []
    for rel in paths:
        target = ROOT / rel
        if target.suffix.lower() not in (".py", ".pyi") or not target.is_file():
            continue
        try:
            ast.parse(io.open(target, encoding="utf-8").read())
        except SyntaxError as exc:
            bad.append({"path": rel, "line": exc.lineno,
                        "message": "SyntaxError: %s" % (exc.msg or "")})
    return {"stage": "ast", "ok": not bad, "findings": bad}


def _python_paths(paths):
    out = []
    for rel in paths:
        target = ROOT / rel
        if target.suffix.lower() in (".py", ".pyi") and target.is_file():
            out.append(rel)
    return out


def _is_main_guard(test):
    import ast
    if not (isinstance(test, ast.Compare) and len(test.ops) == 1
            and isinstance(test.ops[0], ast.Eq)):
        return False
    sides = [test.left] + list(test.comparators)
    return (any(isinstance(s, ast.Name) and s.id == "__name__" for s in sides)
            and any(isinstance(s, ast.Constant) and s.value == "__main__" for s in sides))


# a runner that finds its own tests: every test it defines runs
_DISCOVERY_NAMES = frozenset(("globals", "vars", "dir", "getattr", "getmembers"))


def unrun_tests(src):
    import ast
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    funcs = {n.name: n for n in tree.body
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    tests = [name for name in funcs if name.startswith(("test_", "case_"))]
    if not tests:
        return []
    todo: list = [n for n in tree.body if isinstance(n, ast.If) and _is_main_guard(n.test)]
    reached = set()
    while todo:
        for sub in ast.walk(todo.pop()):
            name = (sub.id if isinstance(sub, ast.Name)
                    else sub.attr if isinstance(sub, ast.Attribute) else None)
            if name is None:
                continue
            if name in _DISCOVERY_NAMES:
                return []
            if name not in reached:
                reached.add(name)
                if name in funcs:
                    todo.append(funcs[name])
    return [name for name in tests if name not in reached]


def tests_run_check(paths, before=None):
    """Every test a changed suite defines must be reached from its __main__
    block (unrun_tests). Judged like ruff and pyright: a test that was already
    unreached before the change does not count against it."""
    import ast
    bad = []
    # one path per file: three edits to one test file listed its finding thrice
    for rel in dict.fromkeys(paths):
        if not str(rel).replace("\\", "/").startswith("tests/"):
            continue
        target = ROOT / rel
        if target.suffix.lower() != ".py" or not target.is_file():
            continue
        src = io.open(target, encoding="utf-8").read()
        old = set((before or {}).get(rel) or [])
        lines = {}
        try:
            lines = {n.name: n.lineno for n in ast.parse(src).body
                     if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        except SyntaxError:
            pass
        for name in unrun_tests(src):
            if name not in old:
                bad.append({"path": rel, "line": lines.get(name),
                            "message": "%s is never called from the __main__ block, "
                                       "so it never runs" % name})
    return {"stage": "tests_run", "ok": not bad, "findings": bad}


def _ruff_findings(targets):
    """(counts, detail, error) for the target files as they are right now."""
    try:
        done = subprocess.run(
            [PYTHON, "-m", "ruff", "check", "--no-cache",
             "--output-format", "json", *targets],
            cwd=str(ROOT), capture_output=True, timeout=TOOL_TIMEOUT)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return None, None, "ruff could not run: %s" % str(exc)[:120]
    try:
        rows = json.loads((done.stdout or b"[]").decode("utf-8", "replace"))
    except ValueError:
        return None, None, "ruff output was not json"
    counts = collections.Counter()
    detail = collections.defaultdict(list)
    for row in rows:
        key = "%s::%s" % (_rel(row.get("filename")), row.get("code"))
        counts[key] += 1
        detail[key].append({
            "path": _rel(row.get("filename")),
            "line": (row.get("location") or {}).get("row"),
            "code": row.get("code"),
            "message": (row.get("message") or "")[:160],
        })
    return counts, detail, ""


def ruff_check(paths, before=None):
    """Ruff on the touched files, minus what those files already had."""
    targets = _python_paths(paths)
    if not targets:
        return {"stage": "ruff", "ok": True, "findings": [], "skipped": "no python files"}
    counts, detail, error = _ruff_findings(targets)
    if error:
        # A checker that cannot run is not a passing checker. Say so; the
        # caller decides, and the caller here reverts.
        return {"stage": "ruff", "ok": False, "findings": [], "error": error}
    return _against_baseline("ruff", counts, detail, _load(LINT_BASELINE),
                             _loadfull(LINT_BASELINE), before=before)


def pyright_check(paths, before=None):
    """Pyright on the touched files, minus what those files already had."""
    targets = _python_paths(paths)
    if not targets:
        return {"stage": "pyright", "ok": True, "findings": [],
                "skipped": "no python files"}
    counts, detail, error = _pyright_findings(targets)
    if error:
        return {"stage": "pyright", "ok": False, "findings": [], "error": error}
    return _against_baseline("pyright", counts, detail, _load(TYPE_BASELINE),
                             _loadfull(TYPE_BASELINE), before=before)


def _pyright_findings(targets):
    """(counts, detail, error) for the target files as they are right now."""
    try:
        done = subprocess.run([PYRIGHT, "--outputjson", *targets],
                              cwd=str(ROOT), capture_output=True,
                              timeout=TOOL_TIMEOUT, shell=False)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return None, None, "pyright could not run: %s" % str(exc)[:120]
    raw = (done.stdout or b"").decode("utf-8", "replace")
    start = raw.find("{")
    try:
        data = json.loads(raw[start:]) if start >= 0 else {}
    except ValueError:
        return None, None, "pyright output was not json"
    counts = collections.Counter()
    detail = collections.defaultdict(list)
    for row in (data.get("generalDiagnostics") or []):
        if row.get("severity") not in ("error", None):
            continue
        rule = row.get("rule") or "general"
        key = "%s::%s" % (_rel(row.get("file")), rule)
        counts[key] += 1
        detail[key].append({
            "path": _rel(row.get("file")),
            "line": ((row.get("range") or {}).get("start") or {}).get("line", -1) + 1,
            "code": rule,
            "message": (row.get("message") or "").split("\n")[0][:160],
        })
    return counts, detail, ""


def _fingerprint(message):
    """A finding's identity: the message with the volatile parts removed."""
    text = str(message or "").split("\n")[0]
    text = re.sub(r"\d+", "#", text)          # line/col numbers inside messages
    return " ".join(text.split())[:160]


def _against_baseline(stage, counts, detail, baseline, prints=None, before=None):
    """Only what this change added. Old debt is recorded, not charged.

    Two comparisons, because either alone has a blind spot. The COUNT catches
    a new occurrence of a rule the file already had. The FINGERPRINT catches a
    finding whose text is new even though the count did not move.

    `before` is what the same files carried just before the edit (snapshot()).
    The recorded baseline goes stale whenever a file is edited outside this
    ladder; what was there a moment ago cannot, so either one excuses a
    finding.
    """
    before = before or {}
    baseline = dict(baseline or {})
    for key, found in (before.get("counts") or {}).items():
        baseline[key] = max(int(baseline.get(key) or 0), int(found or 0))
    new = []
    known = set((prints or {}).get("fingerprints") or [])
    known.update(before.get("fingerprints") or [])
    if known:
        for key, items in sorted(detail.items()):
            for item in items:
                fp = "%s::%s" % (key.split("::")[0], _fingerprint(item.get("message")))
                if fp not in known:
                    # Same shape as the count path, so a caller never has to
                    # know which comparison caught it.
                    new.append(dict(item, baseline=int(baseline.get(key) or 0),
                                    now=int(counts.get(key) or 0), added=1,
                                    baseline_note="not in the recorded "
                                                  "findings for this file"))
    for key, found in sorted(counts.items()):
        allowed = int(baseline.get(key) or 0)
        if found <= allowed:
            continue
        # A count, not an identity. The baseline records how many of a rule a
        # file had, not which lines, because line numbers move for innocent
        # reasons -- so this knows the count ROSE but cannot say which
        # occurrence is the new one. Saying "these are new" would be a
        # guess; the listed lines are every occurrence in the file, and the
        # counts say how many of them are the change's fault.
        shown = detail[key][:max(1, found - allowed)] if not allowed else detail[key][:6]
        for item in shown:
            new.append(dict(
                item, baseline=allowed, now=found, added=found - allowed,
                note=("new in this file" if not allowed else
                      "%d more than the recorded %d; the listed lines are all "
                      "occurrences, not only the added ones"
                      % (found - allowed, allowed))))
    seen, unique = set(), []
    for item in new:
        key = (item.get("path"), item.get("line"), item.get("code"))
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return {"stage": stage, "ok": not unique, "findings": unique,
            "checked": sorted({k.split("::")[0] for k in counts})}


def run_suites(suites):
    """The named suites, through self_heal's runner so there is one of them."""
    from app import self_heal
    results = [self_heal.run_suite(s) for s in list(suites)[:6]]
    return {"stage": "tests", "ok": bool(results) and all(r["ok"] for r in results),
            "findings": [r for r in results if not r["ok"]], "results": results}


LADDER = ("ast", "tests_run", "ruff", "pyright")


def snapshot(paths):
    """What the files carry right now, per stage: {"ruff": {...}, "pyright": {...}}.

    self_heal takes this before it writes an edit and hands it back through
    verify_paths(before=...), so a finding that was already there does not
    count against the change. A stage whose tool cannot run is left out, and
    for that stage the recorded baseline alone decides, as it always did.
    """
    targets = _python_paths([_rel(p) for p in (paths or [])])
    out = {}
    if not targets:
        return out
    unrun = {}
    for rel in targets:
        if rel.startswith("tests/"):
            try:
                unrun[rel] = sorted(unrun_tests(io.open(ROOT / rel, encoding="utf-8").read()))
            except Exception:
                pass
    if unrun:
        out["tests_run"] = unrun
    for stage, collect in (("ruff", _ruff_findings), ("pyright", _pyright_findings)):
        counts, detail, error = collect(targets)
        if error or counts is None or detail is None:
            continue
        out[stage] = {
            "counts": dict(counts),
            "fingerprints": sorted({
                "%s::%s" % (key.split("::")[0], _fingerprint(item.get("message")))
                for key, items in detail.items() for item in items}),
        }
    return out


def verify_paths(paths, suites=(), before=None):
    """Walk the ladder over touched files. Stops at the first stage that fails.

    Ordered by cost and by authority: a file that does not parse cannot be
    linted, and a type error is worth more than a lint finding but less than a
    test that actually ran. `before` is a snapshot() of the same files taken
    just before the edit.
    """
    rel = [_rel(p) for p in (paths or [])]
    before = before or {}
    stages, ok = [], True
    for run in (lambda: ast_check(rel),
                lambda: tests_run_check(rel, before=before.get("tests_run")),
                lambda: ruff_check(rel, before=before.get("ruff")),
                lambda: pyright_check(rel, before=before.get("pyright"))):
        result = run()
        stages.append(result)
        if not result.get("ok"):
            ok = False
            break
    if ok and suites:
        result = run_suites(suites)
        stages.append(result)
        ok = result["ok"]
    return {"ok": ok, "paths": rel, "stages": stages,
            "failed_stage": next((s["stage"] for s in stages if not s.get("ok")), "")}


def _collect_all():
    lint, types = collections.Counter(), collections.Counter()
    lint_prints, type_prints = set(), set()
    done = subprocess.run([PYTHON, "-m", "ruff", "check", ".", "--no-cache",
                           "--output-format", "json"],
                          cwd=str(ROOT), capture_output=True, timeout=900)
    for row in json.loads((done.stdout or b"[]").decode("utf-8", "replace")):
        lint["%s::%s" % (_rel(row.get("filename")), row.get("code"))] += 1
        lint_prints.add("%s::%s" % (_rel(row.get("filename")),
                                    _fingerprint(row.get("message"))))
    done = subprocess.run([PYRIGHT, "--outputjson"], cwd=str(ROOT),
                          capture_output=True, timeout=900)
    raw = (done.stdout or b"").decode("utf-8", "replace")
    start = raw.find("{")
    data = json.loads(raw[start:]) if start >= 0 else {}
    for row in (data.get("generalDiagnostics") or []):
        if row.get("severity") not in ("error", None):
            continue
        types["%s::%s" % (_rel(row.get("file")), row.get("rule") or "general")] += 1
        type_prints.add("%s::%s" % (_rel(row.get("file")),
                                    _fingerprint(row.get("message"))))
    return (lint, sorted(lint_prints)), (types, sorted(type_prints))


def rebaseline():
    (lint, lint_prints), (types, type_prints) = _collect_all()
    for path, counts, prints, tool, select in (
            (LINT_BASELINE, lint, lint_prints, "ruff 0.16.5", ["E9", "F", "B"]),
            (TYPE_BASELINE, types, type_prints, "pyright 1.1.413", ["basic"])):
        try:
            existing = json.load(io.open(path, encoding="utf-8"))
        except Exception:
            existing = {}
        existing.update({
            "_comment": ("Findings that already existed. verify.py compares a "
                         "touched file against its entry here, so historical "
                         "debt never blocks a change and a NEW finding in a "
                         "file being edited does. Regenerate deliberately, "
                         "never to silence a regression."),
            "tool": tool, "select": select,
            "total": sum(counts.values()),
            "counts": dict(sorted(counts.items())),
            # Identity, not just quantity: without these a change can swap one
            # broken name for another and the count never moves.
            "fingerprints": prints,
        })
        io.open(path, "w", encoding="utf-8", newline="\n").write(
            json.dumps(existing, indent=2) + "\n")
        print("%s: %d findings across %d file/rule pairs"
              % (path.name, existing["total"], len(counts)))


def main(argv):
    if "--rebaseline" in argv:
        rebaseline()
        return 0
    paths = [a for a in argv[1:] if not a.startswith("-")]
    if not paths:
        print(__doc__.strip().splitlines()[0])
        print("\nusage: py -3 app/verify.py <file.py> [...]"
              "\n       py -3 app/verify.py --rebaseline")
        return 2
    out = verify_paths(paths)
    print(json.dumps(out, indent=2)[:4000])
    return 0 if out["ok"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
