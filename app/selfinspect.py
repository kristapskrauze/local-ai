# -*- coding: utf-8 -*-
"""Read-only tools that let Antons look at Antons.

The goal is that "Anton, kāpēc tu tikko ignorēji Annu?" can be answered from
evidence — the event, the route it took, the guard that silenced it, the line
of code that did it — instead of from a plausible story. Everything here is
therefore an OBSERVATION: a match, a line range, a log row, a git status. No
tool in this file draws a conclusion; the model does that from what it gets
back, which is the only way the answer can be checked.

Boundaries, all deterministic and all fail-closed:

  * paths must resolve inside the repo root, and inside the allowlisted
    subtrees within it. `..`, symlinks, absolute paths elsewhere and UNC
    paths are rejected before anything is opened.
  * secrets are never returned: files that hold credentials are refused by
    name, and any line that looks like a token/password/key is redacted even
    if it appears in an allowed file.
  * every entry point takes an explicit `allowed` decision made by the caller
    (messaging gates it on the owner). Nothing here checks the persona, the
    prompt or the model's opinion about who is asking.
  * writes and deploys are NOT here at all. This module cannot modify
    anything; that is the point of splitting it out.

Budgets exist because an agent loop will call these: results are capped in
count and characters, and a test run has a hard timeout.
"""
import ast
import collections
import io
import json
import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path

LOGGER = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent

# Subtrees Antons may read. Everything else under the repo — and everything
# outside it — is invisible.
ALLOWED_DIRS = ("app", "tests", "_service", "scripts", "_tools")
# Individual files worth reading that live at the repo root.
ALLOWED_ROOT_FILES = (
    "AGENTS.md", "README.md", "NO_HARD_GATES.md",
    "requirements.txt", "run.bat",
)
# Logs and runtime state (read-only, no message TEXT beyond what the caller
# already has: routing/telemetry rows only).
ALLOWED_LOGS = {
    "service.err": ROOT / "_service" / "service.err.log",
    "service.out": ROOT / "_service" / "service.out.log",
    "self_restart": ROOT / "_service" / "self_restart.log",
    "traces": ROOT / ".messaging_history" / "traces.jsonl",
    "routing": ROOT / ".messaging_history" / "routing_watcher.jsonl",
    "obligations": ROOT / ".messaging_history" / "obligations.jsonl",
}
# Never, under any flag. Names are matched case-insensitively anywhere in the
# path, so config/messaging.json (which holds the BlueBubbles password env
# wiring and handles) stays out along with keys and dotfiles.
DENY_PATTERNS = (
    ".env", "id_rsa", "id_ed25519", ".ssh", "credential", "secret", "token",
    "password", "passwd", ".pem", ".key", ".pfx", "known_hosts",
)
# Suites an investigation may run. Anything not named here is refused; these
# are all offline and none of them sends a message.
TEST_ALLOWLIST = (
    "postprocess_loss_cases", "debounce_cases", "silence_cases",
    "request_obligation_cases", "query_language_cases", "search_rank_cases",
    "send_queue_cases", "research_guard_cases", "media_constraint_cases",
    "date_time_cases",
    "voice_cases", "capability_matrix", "test_agentic_routing_watcher",
    "test_speaker_attribution", "routing_cases", "trigger_cases",
)

MAX_MATCHES = 40
PER_FILE_MATCHES = 4
MAX_CHARS = 6000
MAX_LINES = 400
TEST_TIMEOUT_SECONDS = 420

SEARCH_SUFFIXES = (".py", ".pyi", ".js", ".mjs", ".md", ".json", ".txt",
                   ".cmd", ".bat", ".html", ".htm", ".css")

READY = True


def readiness():
    missing = [d for d in ALLOWED_DIRS if not (ROOT / d).is_dir()]
    return "repo root %s, %d subtrees%s" % (
        ROOT.name, len(ALLOWED_DIRS) - len(missing),
        (", missing: " + ",".join(missing)) if missing else "")


class Refused(Exception):
    pass



_REDACT_RE = re.compile(
    r"(?i)\b(password|passwd|token|api[_-]?key|secret|authorization|bearer)"
    r"\s*[:=]\s*\S+")
# a long opaque run is a key even when nothing labels it as one
_OPAQUE_RE = re.compile(r"\b[A-Za-z0-9_\-]{32,}\b")


def _opaque(m):
    run = m.group(0)
    if not any(c.isdigit() for c in run) and ("_" in run or "-" in run):
        return run
    return "[REDACTED]"


def redact(text):
    t = _REDACT_RE.sub(lambda m: "%s: [REDACTED]" % m.group(1), str(text or ""))
    return _OPAQUE_RE.sub(_opaque, t)



def _resolve(rel):
    """Repo-relative path -> real path, or Refused. No exceptions to this."""
    raw = str(rel or "").strip().replace("\\", "/")
    if not raw:
        raise Refused("no path given")
    low = raw.lower()
    for bad in DENY_PATTERNS:
        if bad in low:
            raise Refused("path names a protected file (%s)" % bad)
    if raw.startswith("//") or re.match(r"^[a-zA-Z]:", raw):
        raise Refused("absolute paths are not readable")
    p = (ROOT / raw).resolve()
    try:
        p.relative_to(ROOT.resolve())
    except ValueError:
        raise Refused("path is outside the project")
    rel_parts = p.relative_to(ROOT.resolve()).parts
    if len(rel_parts) == 1:
        if rel_parts[0] not in ALLOWED_ROOT_FILES:
            raise Refused("%s is not in the readable set" % rel_parts[0])
    elif rel_parts[0] not in ALLOWED_DIRS:
        raise Refused("%s/ is not a readable subtree" % rel_parts[0])
    return p



def repo_search(query, limit=MAX_MATCHES, regex=False, path=""):
    """Where does this string appear? Returns matches, never an opinion."""
    q = str(query or "").strip()
    if len(q) < 3:
        raise Refused("search needs at least 3 characters")
    try:
        rx = re.compile(q if regex else re.escape(q), re.I)
    except re.error as exc:
        raise Refused("bad pattern: %s" % exc)
    total = max(1, int(limit or MAX_MATCHES))
    per_file = max(2, min(PER_FILE_MATCHES, total))

    scoped = str(path or "").strip().replace("\\", "/").strip("/")
    if scoped in ALLOWED_DIRS and (ROOT / scoped).is_dir():
        # a bare allowed subtree ("tests", "app") -- _resolve treats a
        # single-component path as a root FILE and would refuse it
        candidates = [p for p in sorted((ROOT / scoped).rglob("*"))
                      if p.is_file() and p.suffix.lower() in SEARCH_SUFFIXES]
    elif scoped:
        target = _resolve(scoped)          # sandbox + DENY_PATTERNS, or Refused
        if target.is_file():
            candidates = [target]          # a named file is grepped as-is
        elif target.is_dir():
            candidates = [p for p in sorted(target.rglob("*"))
                          if p.is_file() and p.suffix.lower() in SEARCH_SUFFIXES]
        else:
            raise Refused("no such file or directory in the readable set")
    else:
        candidates = []
        for sub in ALLOWED_DIRS:
            base = ROOT / sub
            if not base.is_dir():
                continue
            candidates.extend(
                p for p in sorted(base.rglob("*"))
                if p.is_file() and p.suffix.lower() in SEARCH_SUFFIXES)

    by_file = []
    found = 0
    for fpath in candidates:
        low = str(fpath).lower()
        if any(b in low for b in DENY_PATTERNS):
            continue
        if "node_modules" in low or "__pycache__" in low or ".git" in low:
            continue
        hits = []
        try:
            with io.open(fpath, encoding="utf-8", errors="replace") as fh:
                for n, line in enumerate(fh, 1):
                    if rx.search(line):
                        found += 1
                        if len(hits) < per_file:
                            hits.append({
                                "path": str(fpath.relative_to(ROOT)).replace("\\", "/"),
                                "line": n,
                                "text": redact(line.rstrip())[:200],
                            })
        except Exception:
            continue
        if hits:
            by_file.append(hits)
    out = []
    depth = 0
    while len(out) < total:
        added = False
        for hits in by_file:
            if depth < len(hits):
                out.append(hits[depth])
                added = True
                if len(out) >= total:
                    break
        if not added:
            break
        depth += 1
    return {"query": q, "matches": out, "truncated": found > len(out),
            "files_matched": len(by_file), "total_matches": found}


# Parsed files, keyed by path and mtime. messaging.py alone is 23k lines and a
# symbol lookup touches every allowed file, so re-parsing the repo on each
# question is the difference between a tool that gets used and one that does
# not.
_AST_CACHE = {}


SOURCE_SUFFIXES = (".py", ".js", ".mjs")

NODE = os.environ.get("RIG_NODE_CMD") or r"C:\Program Files\nodejs\node.exe"
JS_EXTRACTOR = ROOT / "_tools" / "js_symbols.js"


def _iter_source_files():
    """Every source file the sandbox permits, in a stable order."""
    for sub in ALLOWED_DIRS:
        base = ROOT / sub
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if path.suffix.lower() not in SOURCE_SUFFIXES or not path.is_file():
                continue
            if any(bad in str(path).lower() for bad in DENY_PATTERNS):
                continue
            if "__pycache__" in str(path) or "node_modules" in str(path):
                continue
            yield path


def _parsed(path):
    """(tree, lines) for one file, cached on mtime. None if it will not parse."""
    try:
        stamp = path.stat().st_mtime
    except OSError:
        return None
    hit = _AST_CACHE.get(path)
    if hit and hit[0] == stamp:
        return hit[1], hit[2]
    try:
        text = io.open(path, encoding="utf-8", errors="replace").read()
        tree = ast.parse(text)
    except (SyntaxError, ValueError, OSError):
        # A file that does not parse is not an error here -- it is simply a
        # file this tool cannot answer about.
        return None
    lines = text.splitlines()
    _AST_CACHE[path] = (stamp, tree, lines)
    return tree, lines


_JS_CACHE = {}


def _js_analysis(path):
    """{"symbols": [...], "refs": [...]} for one .js file, cached on mtime.

    Python has stdlib ast; JavaScript does not, so this shells out to acorn
    through _tools/js_symbols.js. A real parser rather than regexes, for the
    same reason repo_refs is AST-backed on the Python side: a name inside a
    string or a comment is not a reference.
    """
    try:
        stamp = path.stat().st_mtime
    except OSError:
        return {"symbols": [], "refs": []}
    hit = _JS_CACHE.get(path)
    if hit and hit[0] == stamp:
        return hit[1]
    empty = {"symbols": [], "refs": []}
    if not JS_EXTRACTOR.is_file():
        return empty
    try:
        done = subprocess.run([NODE, str(JS_EXTRACTOR), str(path)],
                              capture_output=True, timeout=60)
        data = json.loads((done.stdout or b"{}").decode("utf-8", "replace"))
    except Exception:
        # A file this tool cannot answer about, same as a .py that will not
        # parse. Never an exception into the caller's loop.
        return empty
    got = data.get(str(path).replace("\\", "/")) or {}
    out = {"symbols": got.get("symbols") or [], "refs": got.get("refs") or []}
    _JS_CACHE[path] = (stamp, out)
    return out


def _py_symbols(tree, lines, want, kind):
    """Definitions from a parsed Python file, in the shared record shape."""
    import ast as _ast
    found = []
    for node in _ast.walk(tree):
        name, node_kind = None, ""
        if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
            name, node_kind = node.name, "function"
        elif isinstance(node, _ast.ClassDef):
            name, node_kind = node.name, "class"
        elif isinstance(node, _ast.Assign):
            for target in node.targets:
                if isinstance(target, _ast.Name) and target.id.isupper():
                    name, node_kind = target.id, "constant"
                    break
        if not name:
            continue
        lineno = getattr(node, "lineno", 0)
        found.append({"name": name, "kind": node_kind, "line": lineno,
                      "text": _line_text(lines, lineno)})
    return found


def _line_text(lines, lineno):
    if 1 <= lineno <= len(lines):
        return redact(lines[lineno - 1].strip())[:200]
    return ""


def _symbols_in(file_path):
    """Definitions in one file, whatever language it is written in."""
    if file_path.suffix.lower() == ".py":
        parsed = _parsed(file_path)
        if not parsed:
            return []
        tree, lines = parsed
        return _py_symbols(tree, lines, "", "")
    return _js_analysis(file_path)["symbols"]


def _refs_in(file_path):
    """Uses in one file: imports, calls, references."""
    if file_path.suffix.lower() == ".py":
        parsed = _parsed(file_path)
        if not parsed:
            return []
        tree, lines = parsed
        return _py_refs(tree, lines)
    return _js_analysis(file_path)["refs"]


def _py_refs(tree, lines):
    """Uses from a parsed Python file, in the shared record shape."""
    import ast as _ast
    out = []
    for node in _ast.walk(tree):
        kind, lineno = "", getattr(node, "lineno", 0)
        name = ""
        if isinstance(node, (_ast.Import, _ast.ImportFrom)):
            for alias in node.names:
                out.append({"name": alias.asname or alias.name.split(".")[-1],
                            "kind": "import", "line": lineno,
                            "text": _line_text(lines, lineno)})
            continue
        if isinstance(node, _ast.Call):
            func = node.func
            if isinstance(func, _ast.Name):
                name, kind = func.id, "call"
            elif isinstance(func, _ast.Attribute):
                name, kind = func.attr, "call"
        elif isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef,
                               _ast.ClassDef)):
            name, kind = node.name, "definition"
        elif isinstance(node, _ast.Name):
            name, kind = node.id, "reference"
        elif isinstance(node, _ast.Attribute):
            name, kind = node.attr, "reference"
        if name and kind and lineno:
            out.append({"name": name, "kind": kind, "line": lineno,
                        "text": _line_text(lines, lineno)})
    return out


def repo_symbols(name="", kind="", path="", limit=MAX_MATCHES):
    """Where is this DEFINED? Functions, classes and module-level constants.

    Exists because pyright's workspace/symbol returns nothing here without an
    index, and because "where does this come from" is the question that
    precedes almost every real edit. Exact match on the name when one is
    given, otherwise a substring, so both `repo_symbols("select_best")` and
    `repo_symbols("street")` are useful.

    Python and JavaScript both, because half the things worth asking about in
    this project live in app/static.
    """
    want = str(name or "").strip()
    kind = str(kind or "").strip().lower()
    only = str(path or "").strip().replace("\\", "/")
    if not want and not only:
        raise Refused("give a symbol name, or a path to list")
    out = []
    truncated = False
    cap = max(int(limit or MAX_MATCHES) * 10, 200)
    for file_path in _iter_source_files():
        rel = str(file_path.relative_to(ROOT)).replace("\\", "/")
        if only and only not in rel:
            continue
        for sym in _symbols_in(file_path):
            if kind and sym.get("kind") != kind:
                continue
            found = sym.get("name") or ""
            if want and want != found and want.lower() not in found.lower():
                continue
            out.append({"name": found, "kind": sym.get("kind"), "path": rel,
                        "line": sym.get("line"),
                        "text": redact(str(sym.get("text") or ""))[:200]})
            if len(out) >= cap:
                truncated = True
                break
        if truncated:
            break
    # Exact matches first: asking for "search" should not bury `search` under
    # `search_nearby`, `search_files` and `repo_search`.
    out.sort(key=lambda r: (r["name"] != want, r["path"], r["line"]))
    keep = int(limit or MAX_MATCHES)
    return {"query": want or only, "symbols": out[:keep],
            "truncated": truncated or len(out) > keep}


def repo_refs(name, limit=MAX_MATCHES):
    """Who USES this? Imports, calls and plain references, with file and line.

    The counterpart to repo_symbols and the thing to run before changing a
    signature. Parser-backed in both languages, so a name inside a string or a
    comment is not a reference.
    """
    want = str(name or "").strip()
    if len(want) < 2:
        raise Refused("give a symbol name of at least 2 characters")
    out = collections.defaultdict(list)
    total = 0
    for file_path in _iter_source_files():
        rel = str(file_path.relative_to(ROOT)).replace("\\", "/")
        for use in _refs_in(file_path):
            if use.get("name") != want:
                continue
            lineno = use.get("line") or 0
            if not lineno:
                continue
            kind = use.get("kind") or "reference"
            # One line is one use, and the more specific kind wins: a walk
            # visits a Call and the Name inside it.
            same = [e for e in out[rel] if e["line"] == lineno]
            if same:
                if kind in ("definition", "call", "import") and \
                        same[0]["kind"] == "reference":
                    same[0]["kind"] = kind
                continue
            out[rel].append({"path": rel, "line": lineno, "kind": kind,
                             "text": redact(str(use.get("text") or ""))[:200]})
            total += 1
            if total >= int(limit or MAX_MATCHES):
                return {"query": want, "files": dict(out), "total": total,
                        "truncated": True}
    counts = collections.Counter(
        e["kind"] for rows in out.values() for e in rows)
    return {"query": want, "files": dict(out), "total": total,
            "by_kind": dict(counts), "truncated": False}


def code_search(query, limit=6, kind="", path=""):
    """Conceptually similar code, when you cannot yet name what you want.

    The weakest of the three code lookups and deliberately the last resort.
    repo_symbols answers "where is X defined" when X has a name; repo_search
    answers "where does this exact string appear"; this one answers "where is
    the code that decides whether to reply", a question with no literal
    answer because the word "decides" appears nowhere near the code that does
    it.

    Every result is a path and a line to go and READ. It is similarity, not
    knowledge -- it ranks below the source it points at, and below anything
    pyright or a test says.
    """
    q = str(query or "").strip()
    if len(q) < 3:
        raise Refused("a semantic search needs at least 3 characters")
    try:
        from app import code_index
    except Exception as exc:
        raise Refused("code index unavailable: %s" % str(exc)[:120]) from exc
    try:
        out = code_index.search(q, k=max(1, min(int(limit or 6), 12)),
                                kind=str(kind or ""), path=str(path or ""))
    except FileNotFoundError as exc:
        raise Refused("the code corpus has not been built yet "
                      "(py -3 app/code_index.py --build)") from exc
    except Exception as exc:
        raise Refused("semantic search failed: %s" % str(exc)[:120]) from exc
    return out


def repo_read(path, start=1, end=None):
    """A bounded slice of one file, with line numbers."""
    p = _resolve(path)
    if not p.is_file():
        raise Refused("no such file in the readable set")
    start = max(1, int(start or 1))
    end = int(end) if end else (start + 120)
    if end - start > MAX_LINES:
        end = start + MAX_LINES
    rows = []
    with io.open(p, encoding="utf-8", errors="replace") as fh:
        for n, line in enumerate(fh, 1):
            if n < start:
                continue
            if n > end:
                break
            rows.append({"line": n, "text": redact(line.rstrip("\n"))[:300]})
    return {"path": str(p.relative_to(ROOT)).replace("\\", "/"),
            "start": start, "end": end, "lines": rows}


def read_recent_logs(name="service.err", contains="", limit=40):
    """The tail of one named log, optionally filtered. Never a whole file."""
    key = str(name or "").strip()
    if key not in ALLOWED_LOGS:
        raise Refused("unknown log %r (have: %s)" % (key, ", ".join(sorted(ALLOWED_LOGS))))
    p = ALLOWED_LOGS[key]
    if not p.exists():
        return {"log": key, "rows": [], "note": "log file does not exist"}
    needle = str(contains or "").lower()
    limit = max(1, min(int(limit or 40), 200))
    rows = []
    try:
        with io.open(p, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if needle and needle not in line.lower():
                    continue
                rows.append(redact(line.rstrip())[:300])
                if len(rows) > limit * 4:
                    del rows[:len(rows) - limit * 2]
    except Exception as exc:
        raise Refused("log unreadable: %s" % str(exc)[:80])
    return {"log": key, "rows": rows[-limit:], "filter": contains or ""}


def find_message(text_contains="", chat_guid="", since_minutes=1440, limit=8):
    needle = str(text_contains or "").strip().lower()
    since = time.time() - max(1, int(since_minutes or 1440)) * 60
    inbox = ROOT / ".messaging_history" / "inbox.jsonl"
    if not inbox.exists():
        return {"messages": [], "note": "no inbox"}
    out = []
    try:
        with io.open(inbox, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if float(row.get("ts") or 0) < since:
                    continue
                if chat_guid and str(chat_guid) != str(row.get("chat_guid") or ""):
                    continue
                text = str(row.get("text") or "")
                if needle and needle not in text.lower():
                    continue
                out.append({
                    "message_guid": row.get("guid") or row.get("message_guid"),
                    "chat_guid": row.get("chat_guid"),
                    "from_me": bool(row.get("is_from_me")),
                    "sender": row.get("sender"),
                    "when": time.strftime("%Y-%m-%d %H:%M:%S",
                                          time.localtime(float(row.get("ts") or 0))),
                    "text": redact(text)[:200],
                })
    except Exception as exc:
        raise Refused("inbox unreadable: %s" % str(exc)[:80])
    out.sort(key=lambda r: r.get("when") or "")
    return {"messages": out[-max(1, min(int(limit or 8), 30)):],
            "searched_minutes": since_minutes}


def find_event(message_id="", chat_guid="", contains="", since_minutes=180, limit=12):
    """What the pipeline DID with a message: the routing/telemetry rows."""
    since = time.time() - max(1, int(since_minutes or 180)) * 60
    hits = []
    routing = ALLOWED_LOGS["routing"]
    if routing.exists():
        try:
            with io.open(routing, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except Exception:
                        continue
                    ts = float(row.get("ts") or 0)
                    if not ts and row.get("timestamp"):
                        # the watcher log writes ISO timestamps, not epochs
                        try:
                            from datetime import datetime
                            ts = datetime.fromisoformat(
                                str(row["timestamp"]).replace("Z", "+00:00")).timestamp()
                        except Exception:
                            ts = 0.0
                    if ts and ts < since:
                        continue
                    if message_id and str(message_id) not in json.dumps(row):
                        continue
                    if chat_guid and str(chat_guid) != str(row.get("chat_guid") or ""):
                        continue
                    if contains and str(contains).lower() not in json.dumps(
                            row, ensure_ascii=False).lower():
                        continue
                    w = row.get("watcher") or {}
                    hits.append({
                        "source": "routing",
                        "when": row.get("timestamp"),
                        "chat_guid": row.get("chat_guid"),
                        "message_guid": row.get("message_guid"),
                        "origin": row.get("origin"),
                        "mode": row.get("mode"),
                        # the two decisions and why each one went that way: the
                        # rule-based gate and the watcher model, plus whether
                        # they disagreed — which is the whole diagnosis for a
                        # message that was silently skipped
                        "decision": row.get("legacy"),
                        "reason": row.get("legacy_reason"),
                        "watcher_action": w.get("action"),
                        "watcher_reason": w.get("reason"),
                        "watcher_subject": w.get("subject"),
                        "watcher_source": w.get("source"),
                        "watcher_confidence": w.get("confidence"),
                        "disagreed": row.get("disagrees"),
                    })
        except Exception as exc:
            raise Refused("routing log unreadable: %s" % str(exc)[:80])
    outbox = ROOT / ".messaging_history" / "outbox.jsonl"
    if outbox.exists():
        try:
            with io.open(outbox, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except Exception:
                        continue
                    if chat_guid and str(chat_guid) != str(row.get("target") or ""):
                        continue
                    if contains and str(contains).lower() not in json.dumps(
                            row, ensure_ascii=False).lower():
                        continue
                    hits.append({"when": row.get("timestamp"),
                                 "chat_guid": row.get("target"),
                                 "decision": row.get("status"),
                                 "reason": row.get("error") or "",
                                 "source": "outbox",
                                 "mode": (row.get("metadata") or {}).get("mode")})
        except Exception:
            pass
    # BOTH SOURCES, IN TIME ORDER. Appending one after the other and slicing
    # the tail meant the outbox (far more rows) crowded the routing evidence
    # out of every answer — and the routing rows are the ones that say WHY a
    # message was skipped, which is the whole question being investigated.
    def _epoch(row):
        raw = str(row.get("when") or "")
        try:
            from datetime import datetime
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
        except Exception:
            return 0.0
    hits.sort(key=_epoch)
    hits = hits[-max(1, min(int(limit or 12), 50)):]
    return {"events": hits, "since_minutes": since_minutes}


def git_status():
    return _git(["status", "--porcelain=v1", "--branch"])


def git_log(n=8):
    return _git(["log", "-%d" % max(1, min(int(n or 8), 40)),
                 "--date=format:%Y-%m-%d %H:%M",
                 "--pretty=format:%h %ad %s"])


def git_diff(path="", stat=True, added_only=False):
    """The working-tree diff. `added_only` returns just the added lines."""
    if added_only:
        args = ["diff", "-U0"]
        if path:
            args.append(str(_resolve(path).relative_to(ROOT)).replace("\\", "/"))
        out = _git(args)
        kept = [ln for ln in str(out.get("output") or "").splitlines()
                if ln.startswith("+") and not ln.startswith("+++")]
        out["output"] = "\n".join(kept)[:MAX_CHARS * 3]
        out["command"] += "   (added lines only)"
        return out
    args = ["diff"] + (["--stat"] if stat else [])
    if path:
        args.append(str(_resolve(path).relative_to(ROOT)).replace("\\", "/"))
    return _git(args)


def _git(args):
    safe = str(ROOT)
    args = ["-c", "safe.directory=" + safe,
            "-c", "safe.directory=" + safe.replace("\\", "/")] + list(args)
    try:
        res = subprocess.run(["git"] + list(args), cwd=str(ROOT), capture_output=True,
                             text=True, timeout=30, encoding="utf-8", errors="replace")
    except Exception as exc:
        raise Refused("git unavailable: %s" % str(exc)[:80])
    body = (res.stdout or "") + (res.stderr or "")
    body = "\n".join(ln for ln in body.splitlines()
                      if not (ln.startswith("warning:") and "CRLF" in ln))
    return {"command": "git " + " ".join(args),
            "output": redact(body)[:MAX_CHARS],
            "code": res.returncode}


def service_status():
    """Health, uptime and residency — the things a diagnosis needs first."""
    out = {"health": None, "ollama": None}
    try:
        import urllib.request
        with urllib.request.urlopen("http://127.0.0.1:8765/api/health", timeout=3) as r:
            out["health"] = json.loads(r.read().decode("utf-8", "replace"))
    except Exception as exc:
        out["health"] = {"ok": False, "error": str(exc)[:120]}
    try:
        import urllib.request
        with urllib.request.urlopen("http://127.0.0.1:11434/api/ps", timeout=3) as r:
            models = json.loads(r.read().decode("utf-8", "replace")).get("models") or []
        out["ollama"] = [{"name": m.get("name"), "vram": m.get("size_vram"),
                          "expires_at": m.get("expires_at")} for m in models]
    except Exception as exc:
        out["ollama"] = {"error": str(exc)[:120]}
    return out


def runtime_status():
    from app import capabilities
    return capabilities.snapshot()


def model_status():
    """Which model each lane runs, and what Ollama is holding right now.

    Read live on every call. The lane table is built from the live config and
    the modules that do the calling, so it cannot drift out of date the way a
    description written into a prompt would.
    """
    import urllib.request

    out = {"loaded": [], "installed": [], "lanes": {}, "notes": []}
    url = "http://127.0.0.1:11434"
    for path, key in (("/api/ps", "loaded"), ("/api/tags", "installed")):
        try:
            with urllib.request.urlopen(url + path, timeout=4) as r:
                models = json.loads(r.read().decode("utf-8", "replace")).get("models") or []
        except Exception as exc:
            out["notes"].append("%s unreadable: %s" % (path, str(exc)[:90]))
            continue
        for m in models:
            row = {"name": m.get("name"),
                   "size_gb": round(float(m.get("size") or 0) / 1e9, 1)}
            if key == "loaded":
                row["vram_gb"] = round(float(m.get("size_vram") or 0) / 1e9, 1)
                row["context_length"] = m.get("context_length")
                row["expires_at"] = m.get("expires_at")
            out[key].append(row)

    try:
        from app import messaging as M
        from app import qwen_agent, agent_tools
        acfg = dict(M.load_config().get("auto_reply") or {})
        chat_model = str(acfg.get("model") or "")
        out["lanes"] = {
            "reply_and_agent": {
                "model": chat_model,
                "options": M._agent_turn_options(acfg, assistant_mode=True),
                "keep_alive": getattr(qwen_agent, "KEEP_ALIVE", ""),
                "model_timeout_seconds": getattr(
                    qwen_agent, "DEFAULT_MODEL_TIMEOUT_SECONDS", None),
                "max_tool_steps": acfg.get("assistant_agent_max_tool_steps"),
                "max_model_calls": acfg.get("assistant_agent_max_model_calls"),
            },
            "lookup_prepass": {
                "model": chat_model,
                "options": {"temperature": 0, "num_predict": 220},
                "note": "read-only tools; its own text is not forwarded",
            },
            "vision": {"model": str(acfg.get("vision_model") or chat_model)},
            "grammar": {"model": str(acfg.get("grammar_model") or chat_model),
                        "note": "falls back to the chat model when unset"},
            "digest": {"model": M._default_chat_model()},
            "tools": {"keep_alive": getattr(agent_tools, "KEEP_ALIVE", "")},
        }
    except Exception as exc:
        out["notes"].append("lane settings unreadable: %s" % str(exc)[:120])

    try:
        from app import rag
        out["lanes"]["embeddings"] = {
            "model": getattr(rag, "EMBED_MODEL", "") or getattr(rag, "MODEL", ""),
        }
    except Exception:
        pass
    out["notes"].append(
        "one Ollama runner serves all lanes; differing num_ctx or keep_alive "
        "between callers is what forces a reload, not a change of model")
    return out


def read_config_safe(key=""):
    """Config values with every secret-shaped entry removed.

    The live config carries the BlueBubbles wiring and the contact handles, so
    this returns the BEHAVIOUR keys only — the ones an investigation needs to
    explain a decision — and never the whole file.
    """
    try:
        from app import messaging as M
        acfg = dict(M.load_config().get("auto_reply") or {})
    except Exception as exc:
        raise Refused("config unreadable: %s" % str(exc)[:80])
    safe = {}
    for k, v in acfg.items():
        low = str(k).lower()
        if any(b in low for b in DENY_PATTERNS) or low in (
                "allowed_senders", "contact_names", "assistant_chats",
                "persona_test_chats", "partner_handles", "owners"):
            continue
        if isinstance(v, (str, int, float, bool)) or v is None:
            safe[k] = v
        elif isinstance(v, (list, tuple)):
            safe[k] = "[%d items]" % len(v)
        elif isinstance(v, dict):
            safe[k] = "{%d keys}" % len(v)
    if key:
        return {"key": key, "value": safe.get(key, None),
                "known": key in safe}
    return {"config": safe}


def allowlisted_tests():
    return [n for n in TEST_ALLOWLIST if (ROOT / "tests" / (n + ".py")).is_file()]


def run_allowlisted_test(name):
    """Run ONE named offline suite and report its verdict."""
    key = str(name or "").strip().replace(".py", "")
    if key not in TEST_ALLOWLIST:
        raise Refused("%r is not an allowlisted suite (have: %s)"
                      % (key, ", ".join(allowlisted_tests())))
    path = ROOT / "tests" / (key + ".py")
    if not path.is_file():
        raise Refused("%s is allowlisted but not present" % key)
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    t0 = time.time()
    try:
        res = subprocess.run([sys.executable, str(path)], cwd=str(ROOT),
                             capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=TEST_TIMEOUT_SECONDS, env=env)
    except subprocess.TimeoutExpired:
        raise Refused("%s did not finish in %ds" % (key, TEST_TIMEOUT_SECONDS))
    except Exception as exc:
        raise Refused("could not run %s: %s" % (key, str(exc)[:80]))
    tail = redact((res.stdout or "") + (res.stderr or ""))[-1500:]
    verdict = "PASS" if res.returncode == 0 else "FAIL"
    return {"test": key, "verdict": verdict, "code": res.returncode,
            "seconds": round(time.time() - t0, 1), "output_tail": tail}



TOOLS = {
    "find_message": find_message,
    "repo_search": repo_search,
    "repo_symbols": repo_symbols,
    "repo_refs": repo_refs,
    "code_search": code_search,
    "repo_read": repo_read,
    "read_recent_logs": read_recent_logs,
    "find_event": find_event,
    "git_status": git_status,
    "git_log": git_log,
    "git_diff": git_diff,
    "service_status": service_status,
    "model_status": model_status,
    "runtime_status": runtime_status,
    "read_config_safe": read_config_safe,
    "run_allowlisted_test": run_allowlisted_test,
}
# Every one of these only reads. The write/deploy verbs are deliberately absent
# from this module — see capabilities.code_write / capabilities.deploy.
READ_ONLY = frozenset(TOOLS)


def call(tool, args=None, allowed=False, who=""):
    """Execute one tool request. `allowed` is the caller's OWNER decision.

    Returns {"ok": ..., "tool": ..., "result"|"refused": ...} — never raises,
    because the loop that calls this hands the result back to the model and a
    refusal is itself an observation ("I am not allowed to read that").
    """
    name = str(tool or "").strip()
    if not allowed:
        LOGGER.info("selfinspect: DENIED %s for %r", name, who)
        return {"ok": False, "tool": name,
                "refused": "self-inspection is owner-only"}
    fn = TOOLS.get(name)
    if fn is None:
        return {"ok": False, "tool": name,
                "refused": "unknown tool (have: %s)" % ", ".join(sorted(TOOLS))}
    kwargs = {k: v for k, v in (args or {}).items() if isinstance(k, str)}
    try:
        result = fn(**kwargs)
    except Refused as exc:
        return {"ok": False, "tool": name, "refused": str(exc)}
    except TypeError as exc:
        return {"ok": False, "tool": name, "refused": "bad arguments: %s" % str(exc)[:120]}
    except Exception as exc:
        LOGGER.exception("selfinspect tool %s failed", name)
        return {"ok": False, "tool": name, "refused": "tool failed: %s" % str(exc)[:120]}
    return {"ok": True, "tool": name, "result": result}
