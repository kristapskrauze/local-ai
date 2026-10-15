import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_git_status_returns_real_data():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    from app.main import _run_local
    r = _run_local(["git", "status"], cwd=root)
    assert r["ok"], "git status failed: %s" % r["output"]
    assert len(r["output"]) > 0, "git status returned empty output"
    assert "branch" in r["output"].lower() or "nothing to commit" in r["output"].lower() or "modified" in r["output"].lower(), "git status output lacks expected content: %s" % r["output"][:200]


if __name__ == "__main__":
    test_git_status_returns_real_data()
    print("OK")
