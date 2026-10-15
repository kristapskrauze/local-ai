# -*- coding: utf-8 -*-
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app import agent_tools  # noqa: E402


def test_apply_yes_is_not_a_restart_request():
    assert agent_tools._restart_requested("Jā, pieliec to.") is False
    assert agent_tools._restart_requested("yes, apply it") is False
    assert agent_tools._restart_requested("labo to un uzliec") is False


def test_explicit_restart_is_authorized():
    assert agent_tools._restart_requested("restartē servisu") is True
    assert agent_tools._restart_requested("tagad pārstartē") is True
    assert agent_tools._restart_requested("restart the service now") is True
    assert agent_tools._restart_requested("salabo un pārlādē") is True


def test_setup_turn_counts():
    assert agent_tools._restart_requested(
        "jā", owner_recent_text="salabo genLevel un restartē servisu") is True


def test_restart_word_only_in_a_quote_does_not_count():
    # _current_action_text strips quoted text
    assert agent_tools._restart_requested(
        'viņš rakstīja "restartē to" bet es negribu') is False


if __name__ == "__main__":
    test_apply_yes_is_not_a_restart_request()
    test_explicit_restart_is_authorized()
    test_setup_turn_counts()
    test_restart_word_only_in_a_quote_does_not_count()
    print("OK")
