# -*- coding: utf-8 -*-
"""Does it know what it could delete?

The rig's C: drive runs at ~90% and the invisible consumer is Ollama: an
``ollama create`` that runs out of space dies mid-write and leaves its blobs
behind with no manifest pointing at them.  Those orphans never appear in
``ollama list`` and have been worth more than 30 GB here, so "90% full" is
useless without a concrete "delete THIS".

The one easy mistake the detector must not make is forgetting the manifest's
``config`` entry: a manifest holds its blobs in ``layers`` AND in ``config``,
and a blob referenced only by ``config`` belongs to a working model.  Drop
that reference and the detector would call a live model's blob an orphan, so
"cleaning up" it would break the model.  This suite builds a small fake store
(the real one currently has no orphans, so it can never be tested there) with
known blobs and manifests and checks the arithmetic and that config-only
blobs are protected.

    fake store
      blobs:  sha256-aaa111   100 B   <- a layer
              sha256-ccc222   200 B   <- config only (must NOT be an orphan)
              sha256-ddd333   300 B   <- referenced nowhere -> orphan
      so orphan_bytes == 300, orphan_gb == round(300 / GiB, 2)

    py -3 tests/disk_report_cases.py
"""
import io
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import disk_report as D                                    # noqa: E402

_GIB = 1024 ** 3


def _write_full(path: Path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\0" * size)


def build_store(base: Path) -> None:
    """Lay down a fake model store with one working model + one orphan."""
    blobs = base / "blobs"
    # The real store nests manifests deeply and find_orphans must rglob for
    # them, so put ours in a nested path rather than the top of manifests/.
    manifest = (base / "manifests" / "registry.ollama.ai"
                / "library" / "fake-model" / "2" / "manifest.json")
    _write_full(blobs / "sha256-aaa111", 100)   # a layer
    _write_full(blobs / "sha256-ccc222", 200)   # config only
    _write_full(blobs / "sha256-ddd333", 300)   # orphan
    manifest_data = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.docker.distribution.manifest.v2+json",
        "config": {"mediaType": "application/vnd.docker.container.image.v1+json",
                   "size": 7023, "digest": "sha256:ccc222"},
        "layers": [{"mediaType": "application/vnd.ollama.image.model",
                    "size": 100, "digest": "sha256:aaa111"}],
    }
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps(manifest_data), encoding="utf-8")


def main():
    checks = []

    def check(label, passed):
        checks.append((label, bool(passed)))

    tmp = Path(tempfile.mkdtemp(prefix="disk_report_case_"))
    store = tmp / "store"
    try:
        build_store(store)

        check("DRK-001 a manifest digest maps to its on-disk filename",
              D._digest_name("sha256:abcd1234") == "sha256-abcd1234")
        check("DRK-001 a digest with no colon is left as-is",
              D._digest_name("plainname") == "plainname")

        r = D.find_orphans(store)
        check("DRK-002 the store is found", r["store_found"] is True)
        check("DRK-002 all three blobs are counted", r["blobs_total"] == 3)
        check("DRK-002 the total blob bytes are exact", r["blobs_bytes"] == 600)
        check("DRK-002 both live references are collected (layer + config)",
              r["referenced_count"] == 2)
        check("DRK-002 exactly one blob is orphaned", r["orphan_count"] == 1)
        check("DRK-002 the orphan bytes add up to the 300 B blob",
              r["orphan_bytes"] == 300)
        check("DRK-002 the orphan GB is the correct rounding of those bytes",
              r["orphan_gb"] == round(300 / _GIB, 2))
        orphan_names = [o["file"] for o in r["orphan_files"]]
        check("DRK-002 the lone orphan is the unreferenced blob",
              orphan_names == ["sha256-ddd333"])

        check("DRK-003 a blob referenced only by `config` is NOT an orphan",
              "sha256-ccc222" not in orphan_names)
        check("DRK-003 a blob referenced by a `layers` entry is NOT an orphan",
              "sha256-aaa111" not in orphan_names)

        r2 = D.find_orphans(tmp / "no-such-store")
        check("DRK-004 a missing store is flagged, not an error",
              r2["store_found"] is False and r2["store_found"] is not None
              and "not found" in r2["error"])
        check("DRK-004 a missing store yields no orphans",
              r2["orphan_count"] == 0 and r2["orphan_bytes"] == 0)

        # ---- DRK-005: OLLAMA_MODELS is honoured; fallback is the rig path --
        os.environ["OLLAMA_MODELS"] = str(store)
        check("DRK-005 OLLAMA_MODELS points at the configured store",
              D.model_store_path() == str(store))
        del os.environ["OLLAMA_MODELS"]
        check("DRK-005 with no env var it falls back to the rig path",
              D.model_store_path() == "C:\\ollama\\models")

        # ---- DRK-006: build_report wires drives + orphans together ---------
        os.environ["OLLAMA_MODELS"] = str(store)
        rep = D.build_report()
        check("DRK-006 the report has a drive list", isinstance(rep["drives"], list)
              and len(rep["drives"]) >= 1)
        check("DRK-006 the report carries the orphan audit",
              rep["ollama"]["store_found"] is True
              and rep["ollama"]["orphan_bytes"] == 300)
        # The tool is read-only by contract: the store is untouched.
        check("DRK-006 running the report changed nothing under the store",
              len(list((store / "blobs").iterdir())) == 3)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        os.environ.pop("OLLAMA_MODELS", None)

    print("disk report: free space + orphaned Ollama blobs")
    print("=" * 72)
    for label, ok in checks:
        print("  %s  %s" % ("PASS" if ok else "FAIL", label))
    bad = [l for l, ok in checks if not ok]
    print("=" * 72)
    print("%d/%d passed" % (len(checks) - len(bad), len(checks)))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
