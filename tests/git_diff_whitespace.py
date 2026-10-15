# -*- coding: utf-8 -*-
"""The git diff reaches the page with its leading spaces intact.

A diff context line starts with a space, and `.strip()` on the command output
ate it: _run_local keeps leading whitespace (`.rstrip()`), and /api/git/diff
returns the diff as text/plain. The first version of this test pinned line
numbers (3320, 3635) and went red with the next unrelated edit to main.py;
it finds the two functions by name now.

    py -3 tests/git_diff_whitespace.py
"""
import ast
import os

MAIN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app", "main.py")


def _functions():
    src = open(MAIN, encoding="utf-8").read()
    out = {}
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out[node.name] = (node, ast.get_source_segment(src, node) or "")
    return out


def test_run_local_keeps_leading_whitespace():
    _, body = _functions()["_run_local"]
    joined = [ln for ln in body.splitlines() if "r.stdout" in ln]
    assert joined, "_run_local no longer joins stdout -- re-read it"
    for ln in joined:
        assert ".rstrip()" in ln and ".strip()" not in ln, ln


def test_diff_endpoint_returns_text_plain():
    hits = [body for node, body in _functions().values()
            if any("/api/git/diff" in ast.unparse(d) for d in node.decorator_list)]
    assert len(hits) == 1, "expected one /api/git/diff handler, found %d" % len(hits)
    assert "Response(" in hits[0] and 'media_type="text/plain"' in hits[0], hits[0][-300:]


if __name__ == "__main__":
    test_run_local_keeps_leading_whitespace()
    test_diff_endpoint_returns_text_plain()
    print("OK")
