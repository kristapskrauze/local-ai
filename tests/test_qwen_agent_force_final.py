# -*- coding: utf-8 -*-
"""Pin the force_final branch shape in app/qwen_agent.py.

The force_final branch must:
  * NOT zero out `remaining` (the two `remaining = 0` lines are gone),
  * still compute tools with the `if remaining > 0 else []` guard,
  * read `if force_final:` rather than `elif force_final:`.
"""
import os


def _source() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, "..", "app", "qwen_agent.py")
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def test_force_final_branch_shape() -> None:
    src = _source()
    # The two `remaining = 0` lines under force_final must be gone.
    assert "remaining = 0" not in src, "force_final must not zero out remaining"
    # Tools must still be computed with the remaining > 0 guard.
    assert (
        "tools = reg.ollama_tools(ctx, names=allowed_names) if remaining > 0 else []"
        in src
    ), "tools line with remaining > 0 guard is missing"
    # The branch must read `if force_final:`, not `elif force_final:`.
    assert "if force_final:" in src, "force_final branch must be `if force_final:`"
    assert "elif force_final:" not in src, "force_final branch must not be `elif force_final:`"


if __name__ == "__main__":
    test_force_final_branch_shape()
    print("OK")
