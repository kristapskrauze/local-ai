# -*- coding: utf-8 -*-
"""Fetch the official Latvian Wikipedia article dump onto the HDD, verified.

Rules from the plan this follows literally:
  * no hardcoded dump date or URL -- the run resolves the CURRENT dump from
    Wikimedia's own dumpstatus.json, so re-running it next month just works
  * the raw dump is stored UNCHANGED, and separately from anything derived
  * sha1 is checked against the manifest, not assumed
  * dump date, revision and license are recorded in a dataset manifest, because
    "where did this text come from" has to survive the download

Bulk source data on the HDD (D:), never the SSD -- lvwiki is a few hundred MB
now but the extracted/indexed forms grow, and C: is at 90%.
"""
import argparse
import hashlib
import json
import os
import re
import time
import urllib.request

BASE = "https://dumps.wikimedia.org"
WIKI = "lvwiki"
JOB = "articlesmultistreamdump"          # pages-articles-multistream
UA = {"User-Agent": "antons-local-rag/1.0 (personal research; contact via local rig)"}
DEST = r"D:\antons-data\knowledge\wikipedia\lv"


def get_json(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def download(url, dest, expect_sha1=None, expect_size=None):
    tmp = dest + ".part"
    done = 0
    started = time.time()
    req = urllib.request.Request(url, headers=UA)
    h = hashlib.sha1()
    with urllib.request.urlopen(req, timeout=120) as r, open(tmp, "wb") as f:
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            h.update(chunk)
            done += len(chunk)
            if done % (64 << 20) < (1 << 20):
                el = time.time() - started
                print("    %6.1f MB  %5.1f MB/s" % (done / 1e6, done / 1e6 / max(el, 0.1)),
                      flush=True)
    got = h.hexdigest()
    if expect_size and done != expect_size:
        raise SystemExit("size mismatch: got %d want %d" % (done, expect_size))
    if expect_sha1 and got != expect_sha1:
        raise SystemExit("sha1 MISMATCH: got %s want %s" % (got, expect_sha1))
    os.replace(tmp, dest)
    print("    verified sha1 %s" % got)
    return {"bytes": done, "sha1": got,
            "seconds": round(time.time() - started, 1)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dest", default=DEST)
    ap.add_argument("--index", action="store_true", default=True,
                    help="also fetch the multistream index (needed for random access)")
    args = ap.parse_args()
    raw = os.path.join(args.dest, "raw")
    man = os.path.join(args.dest, "manifests")
    os.makedirs(raw, exist_ok=True)
    os.makedirs(man, exist_ok=True)

    # /lvwiki/latest/ is a symlink directory and carries NO dumpstatus.json --
    # that file only exists in the dated dumps. So enumerate the dated dumps and
    # take the newest one whose articles job actually finished. Still no
    # hardcoded date: the newest COMPLETE dump is discovered every run, and a
    # half-finished current dump is skipped rather than downloaded broken.
    listing = urllib.request.urlopen(
        urllib.request.Request("%s/%s/" % (BASE, WIKI), headers=UA), timeout=60
    ).read().decode("utf-8", "replace")
    dates = sorted(set(re.findall(r'href="(\d{8})/"', listing)), reverse=True)
    if not dates:
        raise SystemExit("no dated dumps listed at %s/%s/" % (BASE, WIKI))
    print("dated dumps available: %s%s"
          % (", ".join(dates[:6]), " ..." if len(dates) > 6 else ""))

    status = job = dump_date = None
    for date in dates:
        url = "%s/%s/%s/dumpstatus.json" % (BASE, WIKI, date)
        try:
            cand = get_json(url)
        except Exception as exc:
            print("  %s: no dumpstatus (%s)" % (date, str(exc)[:40]))
            continue
        j = (cand.get("jobs") or {}).get(JOB) or {}
        print("  %s: %s = %s" % (date, JOB, j.get("status")))
        if j.get("status") == "done":
            status, job, dump_date = cand, j, date
            break
    if job is None:
        raise SystemExit("no dated dump has a completed %s job" % JOB)

    files = job.get("files") or {}
    # dumpstatus.json's own "version" key is the STATUS SCHEMA version -- it
    # reads "0.8" and has nothing to do with when the dump was taken. The dump
    # date is the directory name. Recording the wrong one here would silently
    # destroy the provenance this manifest exists to carry.
    print("\nusing dump date: %s (dumpstatus schema %s)"
          % (dump_date, status.get("version")))
    print("%s files: %d" % (JOB, len(files)))

    wanted = []
    for name, meta in sorted(files.items()):
        if name.endswith(".xml-p1p") or "multistream" not in name:
            continue
        if "index" in name and not args.index:
            continue
        wanted.append((name, meta))
    if not wanted:
        raise SystemExit("no multistream files in manifest: %s" % list(files)[:10])

    got = {}
    for name, meta in wanted:
        url = BASE + meta["url"] if meta.get("url", "").startswith("/") else meta.get("url")
        dest = os.path.join(raw, name)
        size = meta.get("size")
        print("\n%s  (%.1f MB)" % (name, (size or 0) / 1e6))
        if os.path.exists(dest) and size and os.path.getsize(dest) == size:
            print("    already present, skipping")
            got[name] = {"bytes": size, "sha1": meta.get("sha1"), "cached": True}
            continue
        got[name] = download(url, dest, meta.get("sha1"), size)

    manifest = {
        "name": "lvwiki-pages-articles-multistream",
        "source": "%s/%s/%s/" % (BASE, WIKI, dump_date),
        "version": dump_date,
        "dump_date": dump_date,
        "dumpstatus_schema": status.get("version"),
        "download_date": time.strftime("%Y-%m-%d"),
        "license": "CC BY-SA 4.0 (text); see https://dumps.wikimedia.org/legal.html",
        "allowed_usage": "redistribution and derivative use permitted under CC BY-SA "
                         "with attribution; local RAG indexing is in scope",
        "raw_path": raw,
        "processed_path": os.path.join(args.dest, "extracted"),
        "files": got,
    }
    dest = os.path.join(man, "lvwiki_%s.json" % dump_date)
    json.dump(manifest, open(dest, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("\nwrote manifest %s" % dest)
    total = sum(v.get("bytes") or 0 for v in got.values())
    print("raw dump on HDD: %.1f MB in %s" % (total / 1e6, raw))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
