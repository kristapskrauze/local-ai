# -*- coding: utf-8 -*-
"""A page already being built is not started a second time.

Spec (app/agent_tools.py, _tool_antons_build):
  while a build of slug S is in flight, another dispatch of S starts nothing
  and answers ok=True, started=False, already_building=True with a note that
  the link is on its way; once the build ends (success or crash), S can be
  built again. A dispatch that fails before its thread starts does not block
  the next one.

No model, no network, no live files: the build thread is a stand-in and the
page folder, ledger and spec store are patched.

    python tests/build_dispatch_once_cases.py
"""
import os
import sys
import tempfile
import threading
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import agent_tools as T  # noqa: E402


class Ctx:
    conversation_id = "iMessage;-;+37120000000"
    sender = "owner"
    turn_text = "uztaisi redzes simulatoru"


def _patched(run_build):
    saved = {}
    tmp = tempfile.mkdtemp()
    names = {"_owner_turn": lambda ctx, acfg: True,
             "_build_spec_save": lambda *a, **k: None,
             "_append_action_ledger": lambda *a, **k: None,
             "_BUILD_STATIC": Path(tmp),
             "_run_build": run_build}
    for k, v in names.items():
        saved[k] = getattr(T, k)
        setattr(T, k, v)
    return saved


def _restore(saved):
    for k, v in saved.items():
        setattr(T, k, v)
    T._BUILDS_IN_FLIGHT.clear()


GOAL = "Redzes simulators: a vision simulator game with a diopter slider"


def test_a_second_dispatch_while_building_starts_nothing():
    release = threading.Event()
    started = []
    real_run_build = T._run_build

    def fake_build(cid, goal, slug, ts, *rest):
        started.append(slug)
        release.wait(5)
        # the real wrapper's clean-up, without the real build
        with T._BUILD_LOCKS_GUARD:
            if (T._BUILDS_IN_FLIGHT.get(slug) or (None, None))[1] == ts:
                T._BUILDS_IN_FLIGHT.pop(slug, None)

    saved = _patched(fake_build)
    try:
        first = T._tool_antons_build({"goal": GOAL, "files": "redzes-simulators"}, Ctx())
        second = T._tool_antons_build({"goal": GOAL + " v2", "files": "redzes-simulators"}, Ctx())
        assert first.get("started") is True, first
        assert second.get("started") is False and second.get("already_building"), second
        assert "do not call antons_build" in second.get("note", ""), second
        release.set()
        for _ in range(50):
            if "redzes-simulators" not in T._BUILDS_IN_FLIGHT:
                break
            threading.Event().wait(0.05)
        third = T._tool_antons_build({"goal": GOAL, "files": "redzes-simulators"}, Ctx())
        assert third.get("started") is True, "after the build ends it can run again: %s" % third
        release.set()
        assert started.count("redzes-simulators") == 2, started
    finally:
        release.set()
        _restore(saved)
    assert T._run_build is real_run_build


def test_the_real_wrapper_clears_the_page_even_when_the_build_crashes():
    saved = {}
    try:
        saved["_run_build_inner"] = T._run_build_inner

        def boom(*a, **k):
            raise RuntimeError("model died")
        T._run_build_inner = boom
        T._BUILDS_IN_FLIGHT["crashy"] = ("c", 123.0)
        try:
            T._run_build("c", GOAL, "crashy", 123.0)
        except RuntimeError:
            pass
        assert "crashy" not in T._BUILDS_IN_FLIGHT, T._BUILDS_IN_FLIGHT
    finally:
        for k, v in saved.items():
            setattr(T, k, v)
        T._BUILDS_IN_FLIGHT.clear()


def test_a_dispatch_that_fails_to_start_does_not_block_the_next():
    saved = _patched(lambda *a, **k: None)
    try:
        def ledger_down(*a, **k):
            raise OSError("disk full")
        T._append_action_ledger = ledger_down
        try:
            T._tool_antons_build({"goal": GOAL, "files": "pagex"}, Ctx())
        except OSError:
            pass
        assert "pagex" not in T._BUILDS_IN_FLIGHT, T._BUILDS_IN_FLIGHT
    finally:
        _restore(saved)


if __name__ == "__main__":
    test_a_second_dispatch_while_building_starts_nothing()
    test_the_real_wrapper_clears_the_page_even_when_the_build_crashes()
    test_a_dispatch_that_fails_to_start_does_not_block_the_next()
    print("OK")
