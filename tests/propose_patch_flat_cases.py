# -*- coding: utf-8 -*-
"""The _SYS prompt must not show the model a tool-call-shaped json.

A shape like {"action": "PROPOSE_PATCH", "args": {...}} looks like a native
tool call to a tool-trained model.  Ollama's qwen parser then throws the
generation away with HTTP 500.  The prompt must show a FLAT object with
files, test, edits, risks and plan at the top level -- no action key, no
args wrapper.
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.propose_patch import _SYS


def test_sys_prompt_is_flat_and_has_no_action_or_args():
    """_SYS must not contain the tool-call-shaped keys."""
    assert 'PROPOSE_PATCH' not in _SYS, (
        "_SYS still names the action key 'PROPOSE_PATCH'; a tool-trained "
        "model will emit a native tool call and Ollama's qwen parser will "
        "throw the generation away with HTTP 500."
    )
    assert '"args"' not in _SYS, (
        "_SYS still shows an 'args' wrapper in the example json; the model "
        "will mirror that shape and trigger the same parser failure."
    )
    # The five required top-level keys must still be present.
    for key in ("files", "test", "edits", "risks", "plan"):
        assert '"%s"' % key in _SYS, (
            "_SYS no longer shows the required top-level key '%s' in the "
            "example json block." % key
        )


if __name__ == "__main__":
    test_sys_prompt_is_flat_and_has_no_action_or_args()
    print("OK")
