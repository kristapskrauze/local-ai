import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import propose_patch as pp


def case_propose_reports_parse_failure_not_missing_files():
    def fake_chat(cfg, convo, options=None):
        return "this is not json"

    result = pp.propose(
        "goal",
        {},
        allowed=True,
        evidence={"evidence": []},
        chat_fn=fake_chat,
    )
    assert result["ok"] is False, result
    reason = result.get("reason") or ""
    assert "parse" in reason.lower(), reason
    assert "this is not json" in reason, reason
    assert "the files key is missing or empty" not in reason, reason


if __name__ == "__main__":
    case_propose_reports_parse_failure_not_missing_files()
    print("OK")
