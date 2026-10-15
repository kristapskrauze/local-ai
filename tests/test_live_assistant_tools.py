"""The live assistant chat must be able to use browser_check to verify a page it built."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import messaging


def test_browser_check_in_live_assistant_tools():
    assert "browser_check" in messaging._LIVE_ASSISTANT_AGENT_TOOLS, (
        "browser_check is registered in app/agent_tools.py but missing from "
        "_LIVE_ASSISTANT_AGENT_TOOLS, so the assistant never sees it"
    )


if __name__ == "__main__":
    test_browser_check_in_live_assistant_tools()
    print("OK")
