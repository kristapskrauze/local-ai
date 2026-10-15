import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def test_browser_check_registered():
    from app import agent_tools
    reg = agent_tools.build_default_registry()
    t = reg.get("browser_check")
    assert t is not None, "browser_check not registered"
    assert "url" in t.schema.get("properties", {}), "needs a url param"
    assert callable(t.handler)
    res = t.handler({"url": ""}, agent_tools.AgentContext())
    assert isinstance(res, dict) and res.get("ok") is False, res


if __name__ == "__main__":
    test_browser_check_registered()
    print("OK")
