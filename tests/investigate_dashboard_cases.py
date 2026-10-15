import os, sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
APP_JS = os.path.join(ROOT, "app", "static", "app.js")

def test_downloads_tile_removed_media_intact():
    with open(APP_JS, encoding="utf-8") as f:
        src = f.read()
    assert 'miniCard("Downloads"' not in src, "Downloads tile should be removed"
    assert 'sp_dl' not in src, "Downloads span id should be removed"
    assert 'miniCard("Digest"' in src, "Media/Digest section must remain"
    assert 'sp_dig' in src, "Digest span id must remain"
    print("OK: Downloads tile removed, Media section intact")

if __name__ == "__main__":
    test_downloads_tile_removed_media_intact()
    sys.exit(0)