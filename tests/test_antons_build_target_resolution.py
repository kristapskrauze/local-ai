import sys
import os
import tempfile
import json

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _make_env(tmpdir):
    """Create a fake _BUILD_STATIC dir and specs file, return (static_dir, specs_path)."""
    static_dir = os.path.join(tmpdir, "static")
    os.makedirs(static_dir, exist_ok=True)
    # Create two existing pages
    for slug in ("tile-mahjong", "old-game"):
        with open(os.path.join(static_dir, slug + ".html"), "w") as f:
            f.write("<html><body>ok</body></html>")
    specs_path = os.path.join(tmpdir, "specs.json")
    specs = {
        "chat-1": {"slug": "tile-mahjong", "goal": "mahjong game", "ios9": True, "ts": 2000.0},
        "chat-2": {"slug": "old-game", "goal": "old game", "ios9": False, "ts": 1000.0},
    }
    with open(specs_path, "w") as f:
        json.dump(specs, f)
    return static_dir, specs_path


def test_resolution():
    import app.agent_tools as at

    with tempfile.TemporaryDirectory() as tmpdir:
        static_dir, specs_path = _make_env(tmpdir)
        # Patch the module-level constants
        orig_static = at._BUILD_STATIC
        orig_specs_path = at._BUILD_SPECS_PATH
        at._BUILD_STATIC = type(orig_static)(static_dir)
        at._BUILD_SPECS_PATH = type(orig_specs_path)(specs_path)
        try:
            # --- Case 1: named page that exists -> resolves to itself ---
            _all_specs = at._build_specs_load()
            _existing_pages = {
                p.stem for p in at._BUILD_STATIC.glob("*.html") if p.is_file()
            }
            _recent = sorted(
                _all_specs.values(),
                key=lambda s: s.get("ts") or 0,
                reverse=True,
            )
            _named = "tile-mahjong"
            _pslug = _named if _named in _existing_pages else None
            assert _pslug == "tile-mahjong", f"expected tile-mahjong, got {_pslug}"

            # --- Case 2: named page that does NOT exist -> None ---
            _named = "nonexistent-page"
            _pslug = _named if _named in _existing_pages else None
            assert _pslug is None, f"expected None, got {_pslug}"

            # --- Case 3: no name -> most recent slug ---
            _named = ""
            _pslug = _recent[0]["slug"] if _recent else None
            assert _pslug == "tile-mahjong", f"expected tile-mahjong (most recent), got {_pslug}"

            # --- Case 4: chosen spec lookup by slug ---
            _chosen_spec = next(
                (s for s in _all_specs.values() if s.get("slug") == "tile-mahjong"),
                {},
            )
            assert _chosen_spec.get("goal") == "mahjong game"
            assert _chosen_spec.get("ios9") is True

            # --- Case 5: chosen spec lookup for a slug with no spec -> {} ---
            _chosen_spec = next(
                (s for s in _all_specs.values() if s.get("slug") == "unknown-slug"),
                {},
            )
            assert _chosen_spec == {}, f"expected empty dict, got {_chosen_spec}"

            print("OK: all resolution assertions passed")
        finally:
            at._BUILD_STATIC = orig_static
            at._BUILD_SPECS_PATH = orig_specs_path


if __name__ == "__main__":
    test_resolution()
