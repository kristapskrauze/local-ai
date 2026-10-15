"""The grounding pass rewrites the WHOLE draft, never a fragment of it.

No model runs: a stand-in hands back exactly the draft it was given.

    python tests/grounding_whole_draft_cases.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import messaging as M  # noqa: E402


def _grounded_with_echo(draft):
    seen = []

    def echo(cfg, messages, options=None, **_kw):
        seen.append(dict(options or {}))
        return messages[-1]["content"].split("ČERNOVIKS:\n", 1)[1]

    saved = M._ollama_chat
    M._ollama_chat = echo
    try:
        out = M._grounded_reply({}, draft, "ATRADUMS: kaut kas izlasīts", "jautājums")
    finally:
        M._ollama_chat = saved
    return out, seen


def test_a_long_draft_comes_back_whole():
    draft = " ".join("Teikums numur %d ir pilnīgi kārtībā." % i for i in range(80))
    assert len(draft) > 2500
    out, seen = _grounded_with_echo(draft)
    assert out == draft, (len(out), len(draft), out[-60:])
    # room to write it all back, not 400 tokens for 2500+ characters
    assert seen and seen[0]["num_predict"] >= len(draft) // 2, seen


def test_a_draft_too_long_to_hand_over_whole_is_left_alone():
    draft = "Garš teksts. " * 900
    out, seen = _grounded_with_echo(draft)
    assert out == draft and not seen, (len(out), seen)


if __name__ == "__main__":
    test_a_long_draft_comes_back_whole()
    test_a_draft_too_long_to_hand_over_whole_is_left_alone()
    print("OK")
