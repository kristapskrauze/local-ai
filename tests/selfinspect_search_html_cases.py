# -*- coding: utf-8 -*-
"""repo_search must reach .html files and accept a `path` scope."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app import selfinspect as si  # noqa: E402


def _search(**kw):
    out = si.call("repo_search", kw, allowed=True, who="test")
    return out


def _matches(**kw):
    out = _search(**kw)
    assert out.get("ok"), "repo_search refused: %s" % out.get("refused")
    return out["result"]["matches"]


def test_html_reached_unscoped():
    # every .html file starts with this; if none come back, .html is invisible
    m = _matches(query="<!doctype html>")
    assert any(h["path"].lower().endswith(".html") for h in m), \
        "unscoped repo_search reached no .html file: %r" % [h["path"] for h in m]


def test_path_scopes_to_one_file():
    m = _matches(query="<script", path="app/static/pet.html")
    assert m, "path-scoped repo_search returned nothing"
    assert all(h["path"] == "app/static/pet.html" for h in m), \
        "path scope leaked to other files: %r" % [h["path"] for h in m]


def test_path_scopes_to_a_subtree():
    m = _matches(query="def test_", path="tests")
    assert m and all(h["path"].startswith("tests/") for h in m)


def test_named_path_still_refuses_protected_files():
    out = _search(query="anything", path="app/my_secret_token.py")
    assert not out.get("ok"), "a path naming a protected file must be refused"
    assert "protected" in (out.get("refused") or "").lower()


def test_named_path_still_refuses_outside_repo():
    out = _search(query="anything", path="../outside.py")
    assert not out.get("ok"), "a path outside the repo must be refused"


def test_missing_path_is_a_clean_refusal_not_a_crash():
    out = _search(query="anything", path="app/does_not_exist_zzz.py")
    assert not out.get("ok") and out.get("refused")


def test_unscoped_search_unchanged_for_python():
    m = _matches(query="def repo_search(")
    assert any(h["path"] == "app/selfinspect.py" for h in m)


if __name__ == "__main__":
    test_html_reached_unscoped()
    test_path_scopes_to_one_file()
    test_path_scopes_to_a_subtree()
    test_named_path_still_refuses_protected_files()
    test_named_path_still_refuses_outside_repo()
    test_missing_path_is_a_clean_refusal_not_a_crash()
    test_unscoped_search_unchanged_for_python()
    print("OK")
