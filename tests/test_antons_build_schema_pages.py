import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import agent_tools as at


def main():
    reg = at.build_default_registry()
    tool = reg.get("antons_build")
    assert tool is not None, "antons_build is not registered"
    schema = tool.ollama_schema()
    desc = schema["function"]["parameters"]["properties"]["files"]["description"]
    assert "Existing pages" in desc, desc
    pages = [p[:-5] for p in os.listdir(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app", "static")) if p.endswith(".html")]
    assert any(name in desc for name in pages), desc
    print("OK")


if __name__ == "__main__":
    main()
