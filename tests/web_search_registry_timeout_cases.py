import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.agent_tools import build_default_registry


def main() -> int:
    reg = build_default_registry()
    tool = reg.get("web_search")
    assert tool is not None, "web_search nav reģistrēts build_default_registry()"
    assert tool.timeout_seconds == 45.0, (
        f"web_search kopējais reģistrētais timeout_seconds ir {tool.timeout_seconds}, gaidīts 45.0"
    )
    print("OK: web_search registered timeout_seconds is 45.0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
