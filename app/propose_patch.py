# -*- coding: utf-8 -*-
"""PROPOSE_PATCH — the next permission level, and no further.

Antons may, when the OWNER asks, work out what it would change and write that
down. It may not change anything. The distinction is enforced structurally
rather than by instruction: this module imports `selfinspect`, which has no
write verb in it, and never opens a file for writing — so there is no code
path from a proposal to a modified working tree, whatever the model emits and
whatever a message says.

A proposal is a document with five required parts, because a patch nobody can
review is not a proposal:

    files      which files, and why each one
    test       the FAILING regression to add first
    patch      the minimal change, as a unified-diff sketch
    risks      what this could break, including the inverse case
    plan       which suites to run

`capabilities.code_write` and `capabilities.deploy` stay false while this
exists. Proposing is not patching; the owner reads the proposal and decides.
"""
import json
import logging
import re
import time

LOGGER = logging.getLogger(__name__)

MAX_TOOL_STEPS = 6
MAX_MODEL_CALLS = 6
# how many times it may be told its edit does not match the file and try again
MAX_GROUND_ATTEMPTS = 6


# Ollama runs qwen3.8's declared tool-call parser (PARSER qwen3.5) over every
# response, and a proposal reply keeps coming back shaped enough like a tool
# call that the parser dies and Ollama discards the generation as HTTP 500
# (server.log: qwen35.go:108 'qwen3.5 tool call parsing failed' error=EOF).
# Removing the action/args wrapper from _SYS was not enough. Setting Ollama's
# structured-output `format` makes it decode against this schema instead of
# running the tool-call parser at all, which is how ten other lanes in
# messaging.py already avoid the same failure (see _WATCHER_SCHEMA).
_PROPOSAL_SCHEMA = {
    "type": "object",
    "properties": {
        "files": {"type": "array", "items": {
            "type": "object",
            "properties": {"path": {"type": "string"},
                           "why": {"type": "string"}},
            "required": ["path"]}},
        "test": {"type": "object",
                 "properties": {"suite": {"type": "string"},
                                "case": {"type": "string"},
                                "asserts": {"type": "string"}},
                 "required": ["suite"]},
        "edits": {"type": "array", "items": {
            "type": "object",
            "properties": {"path": {"type": "string"},
                           "find": {"type": "string"},
                           "replace": {"type": "string"}},
            "required": ["path", "find", "replace"]}},
        "risks": {"type": "array", "items": {"type": "string"}},
        "plan": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["files", "test", "edits", "risks", "plan"],
}

REQUIRED_PARTS = ("files", "test", "patch", "risks", "plan")
PROMPT_BUDGET_CHARS = 60000

_SYS = (
    "You are proposing a code change to your OWN source. You are Antons.\n\n"
    "Put the json inside a ```json fence. Start the reply with the fence, "
    "never with a bare '{'.\n\n"
    "You have already gathered observations from your logs and source. Write "
    "the proposal as ONE json object inside that fence and nothing else:\n\n"
    '  {\n'
    '    "files":  [{"path": "app/...", "why": "..."}],\n'
    '    "test":   {"suite": "tests/..._cases.py", "case": "<name>", '
    '"asserts": "<what must fail before the fix>"},\n'
    '    "edits":  [{"path": "app/...", "find": "<the EXACT lines now in '
    'the file>", "replace": "<what they become>"}],\n'
    '    "risks":  ["<what this could break, including the inverse case>"],\n'
    '    "plan":   ["<suite to run>", ...]\n'
    '}\n\n'
    "RULES:\n"
    "- `find` MUST be copied character for character out of what repo_read "
    "showed you, WITHOUT the line numbers, and must appear exactly once in "
    "the file. If you did not read the lines, read them before proposing. An "
    "edit that does not match the file is rejected and you will be told so.\n"
    "- The failing test comes FIRST and must fail for the reason you claim.\n"
    "- `test.suite` must be a test file that EXISTS in tests/, OR one you are "
    "creating right now as one of your OWN `edits` -- for a file that does "
    "not exist yet, put an EMPTY string for that edit's `find` and the WHOLE "
    "new test file, verbatim, in `replace`. Removing a Downloads tile from "
    "the dashboard with no test covering it is exactly this case: write a "
    "short test that asserts the tile's markup is gone from app.js, include "
    "it as an edit, name it in `test.suite`. Do not name a suite you are not "
    "either running as-is or creating in this same proposal, and do not ask "
    "to apply without a test out of not knowing this was allowed.\n"
    "- Suites run as `python tests/<name>.py`, not pytest -- exit code 0 is "
    "green, anything else is red.\n"
    "- Minimal: change the specific behaviour, not the surrounding code.\n"
    "- Name a real file you actually read. Do not invent paths for an edit "
    "to an EXISTING file -- only a brand new file (empty find) may be a path "
    "you have not read, because there is nothing there yet to read.\n"
    "- To ADD a new agent tool: it is registered by a "
    "`reg.register(Tool(name, desc, schema, handler, RISK, owner_only, source))` "
    "call inside `build_default_registry` in app/agent_tools.py. A module-level "
    "`TOOL_X = {...}` dict registers NOTHING. Read the `build_default_registry` "
    "section and copy an existing `reg.register` call before proposing one, "
    "and read the real return shape of any function your handler calls.\n"
    "- A new test file must import only what it actually uses, since an unused "
    "import fails the lint check and reverts the whole proposal. A test which "
    "reads a source file as text usually needs nothing beyond os."
)


def nearest_source(path, wanted, span=24):
    """The real lines around what it TRIED to change.

    Telling it "that text is not in the file" three times just produced three
    more inventions, because nothing in the conversation ever contained the
    actual source. This finds the region its guess was aiming at -- the line
    most like the first line it claimed -- and hands back exactly that, so the
    next attempt can copy instead of remember.
    """
    import difflib
    from app import selfinspect
    try:
        real = selfinspect._resolve(str(path or ""))
        lines = real.read_text(encoding="utf-8").splitlines()
    except Exception as exc:
        return "%s could not be read: %s" % (path, str(exc)[:80])
    head = ""
    for candidate in str(wanted or "").splitlines():
        if candidate.strip():
            head = candidate.strip()
            break
    if not head:
        return ""
    stripped = [ln.strip() for ln in lines]
    hit = difflib.get_close_matches(head, [x for x in stripped if x], n=1, cutoff=0.4)
    if not hit:
        return ""
    index = stripped.index(hit[0])
    start = max(0, index - span // 2)
    end = min(len(lines), index + span)
    return "\n".join(lines[start:end])


def _clip(obj, n=1800):
    try:
        return json.dumps(obj, ensure_ascii=False, default=str)[:n]
    except Exception:
        return str(obj)[:n]


def _observations_prompt(observations):
    """Whole observation lines, in order, until the budget is spent.

    Never truncates an individual observation; it simply stops adding once
    the running character total reaches PROMPT_BUDGET_CHARS.
    """
    lines = []
    total = 0
    for o in observations:
        line = "- %s: %s" % (o.get("source"), str(o.get("fact")))
        total += len(line) + 1
        if total > PROMPT_BUDGET_CHARS:
            break
        lines.append(line)
    return "\n".join(lines) or "- (none)"


def _canonical_proposal(value, observations=()):
    """A proposal, or None. Every required part must be present and non-empty."""
    from app import selfinspect
    if not isinstance(value, dict):
        return {'__error__': 'the reply was not a json object at all'}
    args = value.get("args") if isinstance(value.get("args"), dict) else value
    out = {}
    files = args.get("files")
    if isinstance(files, dict):
        files = [files]
    if not isinstance(files, list) or not files:
        return {"__error__": "the files key is missing or empty"}
    clean_files = []
    for f in files[:8]:
        if isinstance(f, str):
            f = {"path": f, "why": ""}
        if not isinstance(f, dict):
            continue
        path = str(f.get("path") or "").strip().replace("\\", "/")
        if not path:
            continue
        # A PATH THE SANDBOX WOULD REFUSE IS NOT A PROPOSAL. Checking it here
        # means a proposal naming config/ or .ssh/ is rejected while it is
        # still just text, long before anything could act on it.
        try:
            selfinspect._resolve(path)
        except Exception as exc:
            clean_files.append({"path": path, "why": str(f.get("why") or "")[:200],
                                "refused": str(exc)[:120]})
            continue
        clean_files.append({"path": path, "why": str(f.get("why") or "")[:200]})
    if not clean_files:
        return {"__error__": "no files listed"}
    out["files"] = clean_files
    test = args.get("test")
    if isinstance(test, str):
        test = {"suite": test, "case": "", "asserts": ""}
    if not isinstance(test, dict) or not str(test.get("suite") or "").strip():
        return {"__error__": "test.suite is required and must be a non-empty string"}
    out["test"] = {"suite": str(test.get("suite"))[:120],
                   "case": str(test.get("case") or "")[:120],
                   "asserts": str(test.get("asserts") or "")[:400]}
    # THE EDIT MUST MATCH THE FILE. Everything above is prose the model is
    # good at producing whether or not it read anything; this is the part that
    # cannot be written from imagination.
    from app import self_heal
    edits = args.get("edits")
    if isinstance(edits, dict):
        edits = [edits]
    if not isinstance(edits, list) or not edits:
        return {"__error__": "no `edits` given -- a proposal must say exactly "
                             "which text in which file becomes what"}
    try:
        checked = self_heal.verify_edits(edits)
    except Exception as exc:
        return {"__error__": str(exc)[:300]}
    out["edits"] = [{"path": e["path"], "find": e["find"], "replace": e["replace"]}
                    for e in checked]
    out["patch"] = self_heal.diff_for(checked)
    risks = args.get("risks")
    risks = [risks] if isinstance(risks, str) else risks
    out["risks"] = [str(r)[:300] for r in (risks or [])][:8]
    plan = args.get("plan")
    plan = [plan] if isinstance(plan, str) else plan
    out["plan"] = [str(x)[:120] for x in (plan or [])][:10]
    if not out["risks"] or not out["plan"]:
        missing = []
        if not out["risks"]:
            missing.append("risks")
        if not out["plan"]:
            missing.append("plan")
        return {"__error__": "missing required part(s): " + ", ".join(missing)}
    return out


def _rehearse(proposal):
    """Apply the proposal for real, run its checks and its own test, and put
    the files back whatever happened. None when it could not be tried at all;
    the proposal is then shown unrehearsed, as it was before this existed."""
    from app import self_heal
    suite = str(((proposal or {}).get("test") or {}).get("suite") or "").strip()
    try:
        return self_heal.apply_edits(proposal["edits"],
                                     suites=[suite] if suite else [],
                                     reason="rehearsal before approval",
                                     rehearse=True)
    except self_heal.HealError as exc:
        return {"green": False, "note": str(exc)[:600], "suites": []}
    except Exception:
        LOGGER.exception("proposal rehearsal could not run")
        return None


# A name the change made up, as the checkers and Python report it: on a
# module, and on a class.
_MADE_UP_NAME_RES = (
    re.compile(r'"(?P<name>\w+)" is not a known attribute of module "(?P<module>[\w.]+)"'),
    re.compile(r"cannot import name '(?P<name>\w+)' from '(?P<module>[\w.]+)'"),
    re.compile(r"module '(?P<module>[\w.]+)' has no attribute '(?P<name>\w+)'"),
)
_MADE_UP_ATTR_RES = (
    re.compile(r'Cannot access attribute "(?P<name>\w+)" for class "(?P<cls>\w+)"'),
    re.compile(r"'(?P<cls>\w+)' object has no attribute '(?P<name>\w+)'"),
)
_MADE_UP_LOCAL_RES = (
    re.compile(r"(?P<path>[\w./\\-]+\.py):\d+ Undefined name `(?P<name>\w+)`"),
    re.compile(r'(?P<path>[\w./\\-]+\.py):\d+ "(?P<name>\w+)" is not defined'),
)


def _definitions(body, lines):
    """name -> its defining line, for one module or class body."""
    import ast
    defs = {}
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defs[node.name] = lines[node.lineno - 1].strip()
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            defs[node.target.id] = lines[node.lineno - 1].strip()
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    defs.setdefault(target.id, lines[node.lineno - 1].strip())
    return defs


def _closest(where, name, defs):
    import difflib
    close = difflib.get_close_matches(name, list(defs), n=4, cutoff=0.5)
    line = "%s has no `%s`." % (where, name)
    if close:
        line += " The closest names it does define:\n%s" % "\n".join(
            "  %s" % defs[c] for c in close)
    return line


def _real_names_hint(text):
    """What a module or class really defines, for a name the change made up."""
    import ast
    from app import selfinspect
    text = str(text or "")
    out = []
    for rx in _MADE_UP_NAME_RES:
        for m in rx.finditer(text):
            rel = m.group("module").replace(".", "/") + ".py"
            try:
                src = selfinspect._resolve(rel).read_text(encoding="utf-8")
                tree = ast.parse(src)
            except Exception:
                continue
            out.append(_closest(rel, m.group("name"),
                                _definitions(tree.body, src.splitlines())))
    for rx in _MADE_UP_LOCAL_RES:
        for m in rx.finditer(text):
            rel = m.group("path").replace("\\", "/")
            try:
                src = selfinspect._resolve(rel).read_text(encoding="utf-8")
                tree = ast.parse(src)
            except Exception:
                continue
            out.append(_closest(rel, m.group("name"),
                                _definitions(tree.body, src.splitlines())))
    for rx in _MADE_UP_ATTR_RES:
        for m in rx.finditer(text):
            cls = m.group("cls")
            for path in sorted(selfinspect.ROOT.glob("app/*.py")):
                try:
                    src = path.read_text(encoding="utf-8")
                    if "class %s" % cls not in src:
                        continue
                    tree = ast.parse(src)
                except Exception:
                    continue
                for node in tree.body:
                    if isinstance(node, ast.ClassDef) and node.name == cls:
                        out.append(_closest(
                            "class %s (app/%s)" % (cls, path.name), m.group("name"),
                            _definitions(node.body, src.splitlines())))
    return "\n".join(dict.fromkeys(out))


def _rehearsal_failure(res):
    """The failed rehearsal as a correction the model can act on."""
    parts = [str((res or {}).get("note") or "the checks failed")]
    for s in ((res or {}).get("suites") or []):
        if isinstance(s, dict) and not s.get("ok"):
            parts.append("%s output:\n%s" % (s.get("suite"),
                                            str(s.get("output") or "")[-1500:]))
    hint = _real_names_hint("\n".join(parts))
    if hint:
        parts.append(hint)
    return ("the change was TRIED and does not pass its own checks, so nothing "
            "was changed:\n%s\nA test that already exists is the spec for "
            "everything the GOAL does not ask to change: fix the code so it "
            "passes those assertions as they are. Where the GOAL itself changes "
            "what an existing assertion pins -- behaviour it asks to remove or "
            "replace -- update that assertion to the new behaviour in the same "
            "proposal, and name it in `risks`." % "\n".join(parts))


def propose(goal, acfg, allowed=False, chat_guid="", who="", chat_fn=None,
            evidence=None, max_steps=MAX_TOOL_STEPS,
            max_model_calls=MAX_MODEL_CALLS, deadline=None, rehearse=None):
    """Diagnose, then write a proposal. Read-only from start to finish."""
    from app import investigate as inv
    if not allowed:
        return {"ok": False, "state": "DENIED",
                "reason": "patch proposals are owner-only", "proposal": None}
    if rehearse is None:
        rehearse = chat_fn is None
    # Reuse the investigation loop for the diagnosis half rather than running a
    # second, differently-behaved agent over the same evidence.
    _t_diag = time.time()
    diag = evidence if isinstance(evidence, dict) else inv.investigate(
        goal, acfg, allowed=True, chat_guid=chat_guid, who=who, chat_fn=chat_fn,
        max_steps=max_steps, max_model_calls=max_model_calls, deadline=deadline)
    observations = [e for e in (diag.get("evidence") or [])
                    if e.get("kind") == inv.OBSERVATION]
    LOGGER.info("proposal: diagnosis took %.0f s, %d observation(s)",
                time.time() - _t_diag, len(observations))
    chat_kw = {}
    if chat_fn is None:
        from app import messaging as M
        chat_fn = M._ollama_chat
        chat_kw = {"format_schema": _PROPOSAL_SCHEMA}
        cfg = dict(acfg or {})
        cfg["model"] = (cfg.get("assistant_model") or cfg.get("model")
                        or M.BASE_MODEL)
    else:
        cfg = dict(acfg or {})
    cfg["timeout_seconds"] = max(int(cfg.get("timeout_seconds") or 0), 600)
    convo = [{"role": "system", "content": _SYS},
             {"role": "user", "content": "GOAL: %s\n\nOBSERVATIONS:\n%s"
               % (str(goal or "")[:6000],
                  _observations_prompt(observations))}]
    t0 = time.time()
    if deadline is not None and time.time() >= deadline:
        # the diagnosis phase alone already used up the whole budget -- do
        # not spend the caller's remaining patience on a call this function
        # cannot itself finish either
        return {"ok": False, "state": "FAILED", "proposal": None,
                "reason": "stopped before the proposal call: past its deadline",
                "diagnosis": diag}
    try:
        raw = chat_fn(cfg, convo,
                      options={"temperature": 0.1, "num_predict": 6000},
                      **chat_kw)
    except Exception as exc:
        return {"ok": False, "state": "FAILED", "proposal": None,
                "reason": "model call failed: %s" % str(exc)[:120],
                "diagnosis": diag}
    attempts = []
    proposal = None
    for attempt in range(MAX_GROUND_ATTEMPTS):
        parsed = inv._parse_json(raw)
        if parsed is None:
            got = {"__error__": "the model reply did not parse as json: %s" % str(raw)[:200]}
        else:
            got = _canonical_proposal(parsed, observations)
        if isinstance(got, dict) and not got.get("__error__") and rehearse:
            tried = _rehearse(got)
            LOGGER.info("proposal rehearsal %d: %s", attempt + 1,
                        "could not run" if tried is None else
                        "green" if tried.get("green") else
                        "red -- %s" % str(tried.get("note") or "")[:200])
            if tried is not None and not tried.get("green"):
                # a failed rehearsal is a rejection like any other: the model
                # hears exactly which check failed and tries again
                got = {"__error__": _rehearsal_failure(tried)}
            elif tried is not None:
                got["rehearsal"] = "passed"
        if isinstance(got, dict) and not got.get("__error__"):
            proposal = got
            break
        why = (got or {}).get("__error__") if isinstance(got, dict) else (
            "proposal missing one of: %s" % ", ".join(REQUIRED_PARTS))
        attempts.append(why)
        _why = " ".join(str(why or "").split())
        # a failed suite's reason is boilerplate first and the assertion LAST
        # (the traceback's tail): keep both ends, not just the first 400
        LOGGER.info("proposal attempt %d rejected: %s", attempt + 1,
                    _why if len(_why) <= 700 else _why[:250] + " … " + _why[-450:])
        if attempt == MAX_GROUND_ATTEMPTS - 1:
            return {"ok": False, "state": "INCOMPLETE", "proposal": None,
                    "reason": why, "attempts": attempts, "diagnosis": diag,
                    "seconds": round(time.time() - t0, 1)}
        # Tell it exactly what did not match and let it look again. This is the
        # loop that turns a plausible patch into a real one.
        convo.append({"role": "assistant", "content": str(raw)[:24000]})
        # Hand it the source its guess was aiming at. Copying beats recalling.
        shown = ""
        try:
            guess = ((parsed or {}).get("args") or parsed or {}).get("edits") or []
            guess = list([guess] if isinstance(guess, dict) else guess)
            _named = re.search(r"\bedit (\d+)\b", str(why or ""))
            if _named and 0 < int(_named.group(1)) <= len(guess):
                guess.insert(0, guess.pop(int(_named.group(1)) - 1))
            for one in guess[:2]:
                if not isinstance(one, dict):
                    continue
                body = nearest_source(one.get("path"), one.get("find"))
                if body:
                    shown += "\n\n--- %s (as it ACTUALLY reads) ---\n%s" % (
                        one.get("path"), body[:2200])
        except Exception:
            LOGGER.exception("could not show source back")
        convo.append({"role": "user", "content":
                      "That proposal was REJECTED: %s%s\n\nAnswer again with "
                      "ONE json object, inside a ```json fence (never start "
                      "the reply with a bare '{'), carrying ALL of these "
                      "keys:\n"
                      '{"files": [{"path": "...", "why": "..."}], "test": {"suite": '
                      '"tests/..._cases.py", "case": "...", "asserts": "..."}, '
                      '"edits": [{"path": "...", "find": "...", "replace": '
                      '"..."}], "risks": ["..."], "plan": ["tests/..._cases.py"]}'
                      "\n\nCopy `find` verbatim out of the lines above, "
                      "indentation included." % (why, shown)})
        if deadline is not None and time.time() >= deadline:
            return {"ok": False, "state": "FAILED", "proposal": None,
                    "reason": "stopped before a retry: past its deadline",
                    "attempts": attempts, "diagnosis": diag}
        try:
            raw = chat_fn(cfg, convo, options={"temperature": 0.1,
                                               "num_predict": 6000},
                          **chat_kw)
        except Exception as exc:
            return {"ok": False, "state": "FAILED", "proposal": None,
                    "reason": "model call failed: %s" % str(exc)[:120],
                    "attempts": attempts, "diagnosis": diag}
    # The proposal is DATA. Nothing below writes it anywhere except the reply.
    return {"ok": True, "state": "PROPOSED", "proposal": proposal,
            "diagnosis": diag, "applied": False, "attempts": attempts,
            "confirmed_cause": bool(diag.get("confirmed")),
            "seconds": round(time.time() - t0, 1)}


def render(result, lang="lv"):
    """The proposal as something a person can read in a message."""
    p = (result or {}).get("proposal")
    if not p:
        return ""
    lines = []
    lines.append("Faili:")
    for f in p["files"]:
        suffix = (" — ATTEIKTS: %s" % f["refused"]) if f.get("refused") else ""
        lines.append("  %s%s%s" % (f["path"], (" — " + f["why"]) if f.get("why") else "",
                                   suffix))
    t = p["test"]
    lines.append("Tests (vispirms, ar sarkanu): %s %s" % (t["suite"], t["case"]))
    if t["asserts"]:
        lines.append("  %s" % t["asserts"])
    lines.append("Labojums:")
    for ln in str(p["patch"]).splitlines()[:20]:
        lines.append("  %s" % ln)
    lines.append("Riski:")
    for r in p["risks"]:
        lines.append("  - %s" % r)
    lines.append("Jāizdzen: %s" % ", ".join(p["plan"]))
    if p.get("rehearsal") == "passed":
        lines.append("Jau izmēģināts: ar šo izmaiņu visas pārbaudes un %s iet "
                     "cauri, faili atlikti atpakaļ." % t["suite"])
    lines.append("(nekas nav mainīts — tas ir tikai priekšlikums)")
    return "\n".join(lines)
