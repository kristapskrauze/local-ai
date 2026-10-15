import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app.messaging import _agent_turn_options

def test_think_level_high_when_thinking_on():
    opts = _agent_turn_options({"agent_thinking": True})
    assert opts["think_level"] == "high", f"expected 'high', got {opts.get('think_level')!r}"
    assert opts["num_predict"] == 3500

def test_think_level_not_high_when_thinking_off():
    opts = _agent_turn_options({"agent_thinking": False})
    assert opts.get("think_level") != "high", f"expected not 'high', got {opts.get('think_level')!r}"
    assert opts["num_predict"] == 800

def test_think_level_defaults_to_high():
    opts = _agent_turn_options(None)
    assert opts["think_level"] == "high", f"expected 'high' by default, got {opts.get('think_level')!r}"

if __name__ == "__main__":
    test_think_level_high_when_thinking_on()
    test_think_level_not_high_when_thinking_off()
    test_think_level_defaults_to_high()
    print("OK")
