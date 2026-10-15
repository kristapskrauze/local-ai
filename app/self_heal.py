# -*- coding: utf-8 -*-
"""Turn a proposal into a change that is real, checked, and reversible.

So an edit here is not prose. It is (path, find, replace) where `find` must
appear EXACTLY ONCE in the real file — that single rule kills every invented
patch before it is shown to anyone, because invented code does not match.

Applying is separate, owner-gated at the caller, and self-reverting: the file
is backed up, the edit applied, the result parsed, the named suites run, and
anything short of green puts the original back. The service is not restarted
here; picking up a change is still a deliberate act.
"""
import ast
import difflib
import hashlib
import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from app import paths

LOGGER = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
BACKUP_DIR = ROOT / ".messaging_history" / "self_heal"
# where a rehearsal keeps what it must put back (recover_interrupted_rehearsals)
REHEARSAL_DIR = Path(os.environ.get("SELF_HEAL_REHEARSAL_DIR")
                     or (BACKUP_DIR / "rehearsals"))
# the interpreter the service itself runs under, not whatever is on PATH: the
# suites import numpy through `rag`, and the other python on this box has none
PYTHON = os.environ.get("SELF_HEAL_PYTHON") or (
    paths.SERVICE_PYTHON)
SUITE_TIMEOUT = 600
MAX_EDITS = 12


class HealError(Exception):
    pass


def _resolved(path):
    """Repo-relative -> real path, through the read sandbox's own rules."""
    from app import selfinspect
    return selfinspect._resolve(path)


def verify_edits(edits):
    """[(edit, ...)] -> checked edits, or HealError naming what did not match."""
    if not isinstance(edits, (list, tuple)) or not edits:
        raise HealError("no edits given")
    checked = []
    current = {}   # path -> content after every edit to it seen so far

    def _which(n, find):
        first = next((ln.strip() for ln in find.splitlines() if ln.strip()), "")
        return "edit %d (find starts `%s`)" % (n, first[:80])

    for n, raw in enumerate(list(edits)[:MAX_EDITS], 1):
        if not isinstance(raw, dict):
            raise HealError("an edit must be {path, find, replace}")
        path = str(raw.get("path") or "").strip().replace("\\", "/")
        find = str(raw.get("find") or "")
        replace = str(raw.get("replace") or "")
        if not path:
            raise HealError("an edit needs a path")
        real = _resolved(path)
        if path in current:
            source = current[path]
            if find == "":
                raise HealError(
                    "%s: an empty find only creates a file, and this batch "
                    "already has an earlier edit touching that path" % path)
        elif find == "":
            if real.exists():
                raise HealError(
                    "%s already exists -- an empty find only creates a NEW "
                    "file; to edit an existing one, find must be the exact "
                    "text being replaced" % path)
            if not replace.strip():
                raise HealError("%s: creating a file needs actual content" % path)
            source = ""
        else:
            if not real.exists():
                raise HealError("%s does not exist" % path)
            source = real.read_text(encoding="utf-8")
        if find == "":
            after = replace
        else:
            if find == replace:
                raise HealError(
                    "%s: %s changes nothing -- its replace is the same text as its "
                    "find. Put the changed lines in replace, or leave that edit out"
                    % (path, _which(n, find)))
            hits = source.count(find)
            if hits == 0:
                raise HealError(
                    "%s does not contain the text of %s -- read the file and copy "
                    "the lines exactly as they are" % (path, _which(n, find)))
            if hits > 1:
                raise HealError(
                    "%s contains the text of %s %d times, so the edit is ambiguous "
                    "-- include enough surrounding lines to make it unique"
                    % (path, _which(n, find), hits))
            after = source.replace(find, replace)
        if path.endswith(".py"):
            try:
                ast.parse(after)
            except SyntaxError as exc:
                raise HealError("%s would not parse after the edit: %s"
                                % (path, str(exc)[:120]))
        current[path] = after
        checked.append({"path": path, "find": find, "replace": replace,
                        "before": source, "after": after})

    _py, _web = (".py", ".pyi"), (".js", ".mjs", ".html", ".htm")
    _existing_src = None
    for e in checked:
        if e["find"] != "":
            continue
        np = e["path"]
        n_leaf = np.rsplit("/", 1)[-1]
        n_stem = "".join(ch for ch in n_leaf.rsplit(".", 1)[0]
                         if ch not in "-_ ").lower()
        n_ext = ("." + n_leaf.rsplit(".", 1)[-1].lower()) if "." in n_leaf else ""
        _other = _web if n_ext in _py else (_py if n_ext in _web else ())
        if not _other:
            continue
        if _existing_src is None:
            _existing_src = []
            for sub in ("app", "tests", "scripts", "_tools", "_service"):
                base = ROOT / sub
                if not base.is_dir():
                    continue
                _existing_src += [p for p in base.rglob("*")
                                  if p.is_file()
                                  and p.suffix.lower() in (_py + _web)]
        for existing in _existing_src:
            if existing.suffix.lower() not in _other:
                continue
            e_stem = "".join(ch for ch in existing.stem
                             if ch not in "-_ ").lower()
            if e_stem != n_stem:
                continue
            rel = str(existing.relative_to(ROOT)).replace("\\", "/")
            raise HealError(
                "%s does not exist. %s already holds this code -- EDIT %s, "
                "do not create a new file in another language" % (np, rel, rel))

    for e in checked:
        p = e["path"]
        if not (p.startswith("tests/") and p.endswith(".py")):
            continue
        if not _test_file_runs_its_cases(e["after"]):
            raise HealError(
                "%s defines test functions but never calls them -- run as "
                "`python %s` it asserts nothing. Add `if __name__ == "
                "\"__main__\":` that calls each test and prints OK." % (p, p))

    for e in checked:
        if not (e["find"] == "" and e["path"].startswith("app/")
                and e["path"].endswith(".py")):
            continue
        leaf = e["path"].rsplit("/", 1)[-1][:-3]
        needle_a, needle_b = "import %s" % leaf, "%s import" % leaf
        if any(needle_a in o["after"] or needle_b in o["after"]
               for o in checked if o is not e):
            continue
        found = False
        try:
            for pf in (ROOT / "app").rglob("*.py"):
                if not pf.is_file():
                    continue
                txt = pf.read_text(encoding="utf-8", errors="replace")
                if needle_a in txt or needle_b in txt:
                    found = True
                    break
        except Exception:
            found = True   # cannot prove it is an orphan -> do not block
        if not found:
            batch_paths = {o["path"] for o in checked}
            try:
                for pf in (ROOT / "tests").rglob("*.py"):
                    rel = str(pf.relative_to(ROOT)).replace("\\", "/")
                    if rel in batch_paths or not pf.is_file():
                        continue
                    txt = pf.read_text(encoding="utf-8", errors="replace")
                    if needle_a in txt or needle_b in txt:
                        found = True
                        break
            except Exception:
                pass
        if not found:
            raise HealError(
                "%s would be a NEW module that nothing imports -- an orphan "
                "file changes no behaviour. The code to change is in an "
                "existing file; edit that." % e["path"])
    return checked


def _test_file_runs_its_cases(source: str) -> bool:
    """True unless the file defines test-case functions that nothing invokes."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return True   # the ast check above already handles a broken .py
    cases = {n.name for n in ast.walk(tree)
             if isinstance(n, ast.FunctionDef)
             and (n.name.startswith("test") or n.name.endswith("_cases")
                  or "_case" in n.name)}
    if not cases:
        return True   # not a case-style test file; nothing to enforce here
    called, has_main = set(), False
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            called.add(getattr(fn, "id", None) or getattr(fn, "attr", None))
        if isinstance(node, ast.If):
            t = node.test
            if (isinstance(t, ast.Compare) and isinstance(t.left, ast.Name)
                    and t.left.id == "__name__"):
                has_main = True
    if cases & called:
        return True
    return bool(has_main and called & {"main", "globals", "vars", "dir",
                                       "getattr", "getmembers"})


def diff_for(checked):
    """A real unified diff, generated from the files rather than described."""
    out = []
    for edit in checked:
        out.extend(difflib.unified_diff(
            edit["before"].splitlines(True), edit["after"].splitlines(True),
            fromfile="a/" + edit["path"], tofile="b/" + edit["path"], n=3))
    return "".join(out)[:8000]


SUITE_EVIDENCE_LINES = 15
SUITE_EVIDENCE_LINE_CHARS = 240
SUITE_EVIDENCE_CHARS = 1500


def _suite_evidence(text):
    """The end of a suite's output, its last line guaranteed, within a budget."""
    kept, total = [], 0
    for line in reversed(str(text or "").strip().splitlines()[-SUITE_EVIDENCE_LINES:]):
        line = line.rstrip()
        if len(line) > SUITE_EVIDENCE_LINE_CHARS:
            line = line[:SUITE_EVIDENCE_LINE_CHARS] + "…"
        if kept and total + len(line) + 1 > SUITE_EVIDENCE_CHARS:
            break
        kept.append(line)
        total += len(line) + 1
    return "\n".join(reversed(kept))


def run_suite(suite, timeout=SUITE_TIMEOUT):
    """One test file. Its exit code is the verdict; the tail is the evidence.

    Accepts either a path ("tests/debounce_cases.py") or the bare suite name
    that selfinspect.run_allowlisted_test uses ("debounce_cases"). The two
    halves of this codebase named the same thing differently, so a caller that
    had one shape got a sandbox refusal rather than a test run.
    """
    name = str(suite or "").strip().replace("\\", "/")
    if name and "/" not in name:
        name = "tests/%s" % (name if name.endswith(".py") else name + ".py")
    target = _resolved(name or suite)
    if not target.exists():
        return {"suite": suite, "ok": False, "missing": True,
                "output": "no such suite"}
    env = dict(os.environ)
    env["ANTON_LEDGER_DIR"] = tempfile.mkdtemp(prefix="anton_ledger_")
    try:
        done = subprocess.run([PYTHON, "-X", "utf8", str(target)],
                              cwd=str(ROOT), env=env,
                              capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"suite": suite, "ok": False, "output": "timed out"}
    text = ((done.stdout or b"") + b"\n" + (done.stderr or b"")).decode("utf-8", "replace")
    return {"suite": suite, "ok": done.returncode == 0,
            "exit_code": done.returncode, "output": _suite_evidence(text)}


def _revert(saved):
    """Put every touched file back to how it was before this apply.

    `keep` is None for a file this same apply CREATED -- there is nothing to
    restore it FROM, reverting means the file goes back to not existing.
    """
    for real, keep in saved:
        if keep is None:
            real.unlink(missing_ok=True)
        else:
            shutil.copy2(str(keep), str(real))


def _text_sha(text):
    return hashlib.sha256(str(text).replace("\r\n", "\n").encode("utf-8")).hexdigest()


def _backup_name(path, stamp):
    return "%s.%s.bak" % (path.replace("/", "_"), stamp)


def _write_rehearsal_manifest(backup_dir, by_path, stamp):
    """What a rehearsal is about to write, recorded before it writes anything."""
    entries = [{"path": path, "created": group[0]["find"] == "",
                "backup": None if group[0]["find"] == "" else _backup_name(path, stamp),
                "written_sha": _text_sha(group[-1]["after"])}
               for path, group in by_path.items()]
    (backup_dir / "manifest.json").write_text(json.dumps(entries), encoding="utf-8")


def recover_interrupted_rehearsals(rehearsal_dir=None):
    """Put back what a rehearsal cut off mid-run left changed in the repo."""
    base = Path(rehearsal_dir) if rehearsal_dir else REHEARSAL_DIR
    restored, left = [], []
    for folder in (sorted(base.glob("rehearsal_*")) if base.exists() else []):
        try:
            entries = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        except Exception:
            entries = []
        for entry in entries:
            try:
                real = _resolved(entry["path"])
                if not real.exists():
                    continue            # never written, or already gone
                now = _text_sha(real.read_text(encoding="utf-8"))
                keep = folder / entry["backup"] if entry.get("backup") else None
                if now != entry.get("written_sha"):
                    if not (keep and keep.exists()
                            and now == _text_sha(keep.read_text(encoding="utf-8"))):
                        left.append(entry["path"])    # changed since: not ours
                    continue
                if entry.get("created"):
                    real.unlink()
                elif keep and keep.exists():
                    shutil.copy2(str(keep), str(real))
                else:
                    left.append(entry["path"])
                    continue
                restored.append(entry["path"])
            except Exception:
                left.append(str(entry.get("path")))
        shutil.rmtree(str(folder), ignore_errors=True)
    return {"restored": restored, "left": left}


REHEARSAL_SUITE_TIMEOUT = 240


def apply_edits(edits, suites=(), reason="", rehearse=False):
    """Apply, test, and put it all back if the suites are not green.

    Returns what happened either way. The caller decides who is allowed to ask
    for this; nothing in here checks permission, so nothing in here may be
    reachable from a message without that check.
    """
    checked = verify_edits(edits)
    stamp = time.strftime("%Y%m%dT%H%M%S")
    # a rehearsal's copies are thrown away with it, not kept as history
    if rehearse:
        REHEARSAL_DIR.mkdir(parents=True, exist_ok=True)
        backup_dir = Path(tempfile.mkdtemp(prefix="rehearsal_", dir=str(REHEARSAL_DIR)))
    else:
        backup_dir = BACKUP_DIR
    backup_dir.mkdir(parents=True, exist_ok=True)
    try:
        return _apply_edits(checked, suites, reason, rehearse, stamp, backup_dir)
    finally:
        if rehearse:
            shutil.rmtree(str(backup_dir), ignore_errors=True)


def _apply_edits(checked, suites, reason, rehearse, stamp, backup_dir):
    saved = []
    # One backup and one write PER PATH, not per edit. verify_edits already
    # chains same-file edits into a correct cumulative `after`, but writing
    # each edit's own `after` in turn would still overwrite an earlier edit
    # to the same file with a version that does not contain it -- and two
    # backups of the same path also broke a clean revert, since restoring
    # them in order left the file at "after edit 1" instead of pristine.
    # Group by path (first-seen order); back up the first edit's `before`
    # (the true pristine content) and write the last edit's `after` (the
    # final cumulative content) once each.
    by_path = {}
    for edit in checked:
        by_path.setdefault(edit["path"], []).append(edit)
    from app import verify as _verify
    try:
        before = _verify.snapshot(
            [path for path, group in by_path.items() if group[0]["find"] != ""])
    except Exception:
        before = None   # the recorded baselines alone decide, as they did
    try:
        if rehearse:
            _write_rehearsal_manifest(backup_dir, by_path, stamp)
        for path, group in by_path.items():
            real = _resolved(path)
            if group[0]["find"] == "":
                keep = None   # created here; nothing existed to back up
                real.parent.mkdir(parents=True, exist_ok=True)
            else:
                keep = backup_dir / _backup_name(path, stamp)
                shutil.copy2(str(real), str(keep))
            saved.append((real, keep))
            real.write_text(group[-1]["after"], encoding="utf-8")
    except Exception as exc:
        _revert(saved)
        raise HealError("could not apply: %s" % str(exc)[:160])

    # The middle of the ladder, before the suites. An edit can be real, apply
    # cleanly and still leave the file referring to a name that does not
    # exist -- the suites that run are not the code that broke, so nothing
    # would have caught it. Each touched file is judged against its recorded
    # baseline AND what it carried just before this edit (`before`, above), so
    # old debt stays inert and only what THIS change introduced can block it.
    try:
        checks = _verify.verify_paths([e["path"] for e in checked], before=before)
    except Exception as exc:
        _revert(saved)
        raise HealError("the checks could not run, so the change was put "
                        "back: %s" % str(exc)[:160]) from exc
    if not checks["ok"]:
        _revert(saved)
        failed = next((s for s in checks["stages"] if not s.get("ok")), {})
        return {
            "applied": False,
            "reverted": True,
            "rehearsed": bool(rehearse),
            "green": False,
            "reason": str(reason)[:200],
            "files": [e["path"] for e in checked],
            "diff": diff_for(checked),
            "checks": checks,
            "suites": [],
            "backups": [] if rehearse else [str(k) for _r, k in saved],
            "note": ("%s failed on the changed files, so they were put back: %s"
                     % (checks["failed_stage"],
                        "; ".join("%s:%s %s" % (f.get("path"), f.get("line"),
                                                f.get("message", ""))[:120]
                                  for f in (failed.get("findings") or [])[:3])
                        or failed.get("error", ""))),
        }

    try:
        results = [run_suite(s, timeout=(REHEARSAL_SUITE_TIMEOUT if rehearse
                                          else SUITE_TIMEOUT))
                   for s in list(suites)[:6]]
        for _r in results:
            if _r.get("missing"):
                raise HealError("no such suite: %s -- name a test file that exists" % _r.get("suite"))
    except Exception as exc:
        _revert(saved)
        raise HealError("could not run the suites, so the change was put "
                        "back: %s" % str(exc)[:160]) from exc
    green = bool(results) and all(r["ok"] for r in results)
    if not green or rehearse:
        _revert(saved)
    if rehearse:
        note = ("rehearsal: every check and suite passed; the files were put back"
                if green else "rehearsal: the suites did not pass; the files were put back")
    else:
        note = ("the change is on disk; the service still runs the old code "
                "until it is restarted" if green else
                "the suites did not pass, so the files were put back")
    return {
        "applied": green and not rehearse,
        "reverted": (not green) or bool(rehearse),
        "rehearsed": bool(rehearse),
        "green": green,
        "reason": str(reason)[:200],
        "files": [e["path"] for e in checked],
        "diff": diff_for(checked),
        "checks": checks,
        "suites": results,
        "backups": [] if rehearse else [str(k) for _r, k in saved],
        "note": note,
    }
