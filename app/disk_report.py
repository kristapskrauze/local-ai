# -*- coding: utf-8 -*-
"""Read-only disk + Ollama-blob reporter for the assistant.

The C: drive on this rig is chronically ~90% full, and the invisible
consumer is Ollama: every model is a set of content-addressed blob files
under the model store, and the manifests under ``<store>/manifests`` name
the digests they use.  A blob whose digest matches no manifest is an
orphan -- usually the debris of an ``ollama create`` that ran out of space
and died mid-write -- and it will never show up in ``ollama list``.

This module only READS.  It reports free space per drive and the combined
size of the orphans so a human can decide what to reclaim; it only reads
the store and writes nothing, and it deliberately has no path for
modifying or freeing files itself.
"""
from __future__ import annotations

import json
import os
import shutil
import string
from pathlib import Path

DEFAULT_MODELS = "C:\\ollama\\models"
_GIB = 1024 ** 3


def model_store_path() -> str:
    """Honor OLLAMA_MODELS if set, else the known store root on this rig."""
    return os.environ.get("OLLAMA_MODELS") or DEFAULT_MODELS


def _digest_name(digest: str) -> str:
    """Manifest digest ``sha256:abc`` -> on-disk blob filename ``sha256-abc``.

    Ollama spells the same digest with a colon inside the manifest JSON and
    with a dash in the filename.  Only the first colon is a separator, so it
    is replaced once and the hex body is left untouched.
    """
    if not digest:
        return ""
    if ":" in digest:
        return digest.replace(":", "-", 1)
    return digest


def _digests_from(obj, out: set) -> None:
    """Collect a manifest's referenced digests (in filename form) into ``out``."""
    if not isinstance(obj, dict):
        return
    cfg = obj.get("config")
    if isinstance(cfg, dict) and isinstance(cfg.get("digest"), str):
        name = _digest_name(cfg["digest"])
        if name:
            out.add(name)
    layers = obj.get("layers")
    if isinstance(layers, list):
        for lyr in layers:
            if isinstance(lyr, dict) and isinstance(lyr.get("digest"), str):
                name = _digest_name(lyr["digest"])
                if name:
                    out.add(name)


def find_orphans(store_path) -> dict:
    """Compare the manifest-referenced digest set against files on disk.

    A blob file whose name is not in the referenced set is an orphan: it
    exists on disk but no manifest points at it, so the data no working
    model needs.  Reports each orphan and the combined size in GB.  Read-only:
    nothing under the store is opened for write.
    """
    store = Path(str(store_path))
    blobs_dir = store / "blobs"
    manifests_dir = store / "manifests"
    res = {
        "store_path": str(store),
        "store_found": store.is_dir(),
        "blobs_dir": str(blobs_dir),
        "blobs_total": 0,
        "blobs_bytes": 0,
        "referenced_count": 0,
        "orphan_count": 0,
        "orphan_bytes": 0,
        "orphan_gb": 0.0,
        "orphan_files": [],
        "error": "",
    }
    if not store.is_dir():
        res["error"] = "model store directory not found"
        return res

    referenced: set = set()
    if manifests_dir.is_dir():
        for pf in sorted(manifests_dir.rglob("*")):
            if not pf.is_file():
                continue
            try:
                data = json.loads(pf.read_bytes().decode("utf-8", "replace"))
            except Exception:
                continue  # not parseable / not a manifest: skip, keep going
            _digests_from(data, referenced)
    res["referenced_count"] = len(referenced)

    present: dict = {}
    if blobs_dir.is_dir():
        for f in blobs_dir.iterdir():
            if f.is_file():
                try:
                    present[f.name] = f.stat().st_size
                except OSError:
                    continue
    res["blobs_total"] = len(present)
    res["blobs_bytes"] = sum(present.values())

    orphan_items = sorted(
        (name, size) for name, size in present.items() if name not in referenced
    )
    res["orphan_files"] = [
        {"file": name, "bytes": size, "gb": round(size / _GIB, 2)}
        for name, size in orphan_items
    ]
    res["orphan_count"] = len(orphan_items)
    res["orphan_bytes"] = sum(size for _, size in orphan_items)
    res["orphan_gb"] = round(res["orphan_bytes"] / _GIB, 2)
    return res


def _usage_entry(root: str, u) -> dict:
    total = u.total
    free = u.free
    used = total - free
    return {
        "path": root,
        "total_bytes": total,
        "used_bytes": used,
        "free_bytes": free,
        "percent_used": round(100.0 * used / total, 1) if total else 0.0,
        "free_gb": round(free / _GIB, 2),
        "total_gb": round(total / _GIB, 2),
    }


def _drive_entries() -> list:
    out = []
    if os.name == "nt":
        for letter in string.ascii_uppercase:
            root = letter + ":\\"
            try:
                out.append(_usage_entry(root, shutil.disk_usage(root)))
            except OSError:
                continue  # letter names a drive that is not present
    else:
        for root in ("/",):
            try:
                out.append(_usage_entry(root, shutil.disk_usage(root)))
            except OSError:
                continue
    return out


def build_report() -> dict:
    """Assemble the full read-only report: per-drive space + Ollama orphans."""
    return {
        "drives": _drive_entries(),
        "ollama": find_orphans(model_store_path()),
        "note": ("read-only: reports free space and which Ollama blobs appear "
                 "reclaimable; it does not modify the store"),
    }
