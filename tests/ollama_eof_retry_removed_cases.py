"""The EOF retry loop in _ollama_chat must be gone.

The qwen tool-call parser that discarded generations (and forced the
retry) has been removed in app/propose_patch.py. Re-rolling up to 4 times
hides the failure from every downstream caller, so the loop is deleted
and the single original call's failure path raises as-is.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SRC = (ROOT / "app" / "messaging.py").read_text(encoding="utf-8")


def test_eof_retry_loop_removed():
    # The whole retry mechanism must be gone.
    assert "_tries = 0" not in SRC, "retry counter still present"
    assert 'while (not res.get("ok") and _tries < 4' not in SRC, "retry while-loop still present"
    assert "ollama EOF (tool-call parser), retry" not in SRC, "retry log line still present"
    assert "Do not respond with a tool call" not in SRC, "retry system message still present"
    assert "a pinned seed defeats the point" not in SRC, "retry seed-pop comment still present"
    # The single original call and the existing failure path must survive.
    assert 'res = _json_request(url, method="POST", payload=payload, timeout=acfg.get("timeout_seconds", 180))' in SRC
    assert 'raise MessagingError("ollama chat failed: HTTP %s: %s"' in SRC


if __name__ == "__main__":
    test_eof_retry_loop_removed()
    print("OK")
