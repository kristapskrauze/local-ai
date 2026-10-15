# -*- coding: utf-8 -*-
"""repo_symbols / repo_refs: where a thing is defined, and who uses it.

These exist because pyright's workspace/symbol returns nothing in this
project without an index, and because "where does this come from" and "what
breaks if I change it" are the two questions that precede almost every edit.

The property worth testing is that they are AST-backed and not textual: a
name inside a string or a comment is not a reference, and that is the whole
difference between this and repo_search.

    py tests\\symbol_tools_cases.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import selfinspect as si   # noqa: E402

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("    %-54s %s" % (name[:54], "ok" if ok else "FAIL  " + str(detail)[:70]))
    return ok


def case_definitions():
    print("\n1. repo_symbols finds definitions")
    out = si.repo_symbols("select_best")
    syms = out["symbols"]
    check("finds the function", any(s["name"] == "select_best" for s in syms), syms)
    hit = next(s for s in syms if s["name"] == "select_best")
    check("names the file", hit["path"] == "app/street_imagery.py", hit)
    check("gives a line number", hit["line"] > 0, hit)
    check("classifies it as a function", hit["kind"] == "function", hit)
    check("carries the def line itself", hit["text"].startswith("def select_best"), hit)

    out = si.repo_symbols("StreetImageryProvider")
    check("finds a class",
          any(s["kind"] == "class" for s in out["symbols"]), out["symbols"][:2])

    out = si.repo_symbols("MAX_RADIUS_M")
    check("finds a module constant",
          any(s["kind"] == "constant" for s in out["symbols"]), out["symbols"][:2])


def case_exact_first():
    print("\n2. exact matches sort above substring matches")
    out = si.repo_symbols("search")
    names = [s["name"] for s in out["symbols"]]
    exact = [i for i, n in enumerate(names) if n == "search"]
    if exact:
        check("an exact match is first", exact[0] == 0, names[:4])
    else:
        check("an exact match is first", True, "no exact 'search' defined")
    check("substring matches are still returned", len(names) > 1, names[:4])


def case_kind_and_path_filters():
    print("\n3. filters")
    out = si.repo_symbols("", path="app/street_imagery.py")
    check("can list one file's symbols", len(out["symbols"]) > 10, len(out["symbols"]))
    check("everything is from that file",
          all(s["path"] == "app/street_imagery.py" for s in out["symbols"]),
          {s["path"] for s in out["symbols"]})
    out = si.repo_symbols("", path="app/street_imagery.py", kind="class")
    check("kind filter narrows to classes",
          all(s["kind"] == "class" for s in out["symbols"]) and out["symbols"],
          [s["name"] for s in out["symbols"]][:4])


def case_references():
    print("\n4. repo_refs finds users")
    out = si.repo_refs("select_best")
    check("finds references", out["total"] >= 4, out["total"])
    flat = [(p, r["line"], r["kind"]) for p, rows in out["files"].items() for r in rows]
    check("includes the definition",
          any(k == "definition" for _p, _l, k in flat), out.get("by_kind"))
    check("includes calls from another module",
          any(p == "app/agent_tools.py" and k == "call" for p, _l, k in flat), flat)
    check("includes calls from the suites",
          any(p.startswith("tests/") for p, _l, _k in flat), flat)
    check("one line is reported once",
          len(flat) == len({(p, l) for p, l, _k in flat}), flat)


def case_agrees_with_the_language_server():
    print("\n5. it agrees with pyright, which is the point")
    sym = si.repo_symbols("select_best")["symbols"][0]
    refs = si.repo_refs("select_best")
    lines = {(p, r["line"]) for p, rows in refs["files"].items() for r in rows}
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def _line_of(rel, needle):
        with open(os.path.join(root, rel), encoding="utf-8") as fh:
            return next(n for n, ln in enumerate(fh, 1) if needle in ln)
    want_def = _line_of("app/street_imagery.py", "def select_best(")
    want_call = _line_of("app/agent_tools.py", "si.select_best(")
    check("definition line matches the file (%d)" % want_def,
          sym["line"] == want_def, sym)
    check("the agent_tools call site matches the file (%d)" % want_call,
          ("app/agent_tools.py", want_call) in lines, sorted(lines))


def case_not_textual():
    print("\n6. AST-backed, not grep")
    # repo_search matches the word wherever it appears, including inside
    # strings and comments. repo_refs must not.
    text_hits = si.repo_search("select_best")["matches"]
    ast_total = si.repo_refs("select_best")["total"]
    check("text search finds at least as many", len(text_hits) >= ast_total,
          "%d text vs %d ast" % (len(text_hits), ast_total))
    docstring_only = si.repo_refs("Nespeju")
    check("a word that only lives in prose has no references",
          docstring_only["total"] == 0, docstring_only["total"])


def case_refusals_and_sandbox():
    print("\n7. sandbox and refusals")
    try:
        si.repo_refs("x")
        check("a one-character name is refused", False, "no Refused raised")
    except si.Refused:
        check("a one-character name is refused", True)
    try:
        si.repo_symbols("")
        check("an empty query is refused", False, "no Refused raised")
    except si.Refused:
        check("an empty query is refused", True)
    out = si.repo_symbols("", path="app")
    roots = {s["path"].split("/")[0] for s in out["symbols"]}
    check("results stay inside the allowed subtrees",
          roots <= set(si.ALLOWED_DIRS), roots)


def case_the_model_is_told_they_exist():
    print("\n8b. registered is not the same as reachable")
    from app import investigate
    described = {n for n, _d in investigate.TOOL_SPEC}
    callable_ = set(si.TOOLS)
    check("every callable tool is described to the model",
          not (callable_ - described), sorted(callable_ - described))
    check("nothing is described that cannot be called",
          not (described - callable_), sorted(described - callable_))
    for name in ("repo_symbols", "repo_refs", "code_search"):
        check("%s is on the menu" % name, name in described)


def case_speed():
    print("\n8. fast enough to actually get used")
    si.repo_symbols("warmup_only_symbol")
    t0 = time.time()
    si.repo_symbols("select_best")
    warm = time.time() - t0
    check("a warm symbol lookup is under 1s", warm < 1.0, "%.2fs" % warm)
    check("the parse cache is populated", len(si._AST_CACHE) > 20, len(si._AST_CACHE))


def main():
    print(__doc__.strip().splitlines()[0])
    started = time.time()
    for fn in (case_definitions, case_exact_first, case_kind_and_path_filters,
               case_references, case_agrees_with_the_language_server,
               case_not_textual, case_refusals_and_sandbox,
               case_the_model_is_told_they_exist, case_speed):
        try:
            fn()
        except Exception as exc:
            check(fn.__name__, False, "%s: %s" % (type(exc).__name__, exc))
    print("\n%d passed, %d failed  (%.0fs)"
          % (len(PASS), len(FAIL), time.time() - started))
    for name in FAIL:
        print("   FAILED: %s" % name)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
