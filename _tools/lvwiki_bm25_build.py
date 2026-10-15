# -*- coding: utf-8 -*-
"""Build a BM25 lexical index over the lvwiki chunks. No embeddings, no wait.

Index goes on the SSD (rag_index dir) because it is latency-critical; the bulky
chunk TEXT stays on the HDD and is fetched by byte offset at query time, so the
index itself is small (postings + lengths + offsets, no text duplication).

  python lvwiki_bm25_build.py [--chunks <path>] [--min-df 2]
"""
import argparse
import json
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.lvwiki_rag import norm_tokens, STEM_LEN  # ONE tokeniser, shared with search

CHUNKS = r"D:\antons-data\knowledge\wikipedia\lv\extracted\lvwiki_20260801_chunks.jsonl"
OUT = os.path.join(os.environ.get("AI_HOME") or os.path.join(os.path.expanduser("~"), "AI"), "rag_index", "lvwiki_bm25")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--chunks", default=CHUNKS)
    ap.add_argument("--out", default=OUT)
    ap.add_argument("--min-df", type=int, default=2,
                    help="drop terms rarer than this (typos, hapax)")
    args = ap.parse_args()
    os.makedirs(os.path.dirname(args.out), exist_ok=True)

    t0 = time.time()
    # pass 1: byte offsets + document lengths + document frequency per term
    offsets, doc_len, titles = [], [], []
    df = {}
    postings_tmp = {}      # term -> list of (doc_id, tf)
    n = 0
    with open(args.chunks, "rb") as fb:
        while True:
            off = fb.tell()
            raw = fb.readline()
            if not raw:
                break
            try:
                obj = json.loads(raw.decode("utf-8"))
            except Exception:
                continue
            # index title + section + text; title terms matter for fact recall
            toks = norm_tokens("%s %s %s" % (obj.get("title", ""),
                                             obj.get("section", ""),
                                             obj.get("text", "")))
            if not toks:
                continue
            doc_id = n
            n += 1
            offsets.append(off)
            doc_len.append(len(toks))
            titles.append(obj.get("title", "")[:80])
            tf = {}
            for w in toks:
                tf[w] = tf.get(w, 0) + 1
            for w, c in tf.items():
                df[w] = df.get(w, 0) + 1
                postings_tmp.setdefault(w, []).append((doc_id, c))
            if n % 40000 == 0:
                print("  %d docs, %d terms, %.0fs" % (n, len(df), time.time() - t0),
                      flush=True)

    # drop rare terms
    vocab = {w: i for i, w in enumerate(
        sorted(t for t, d in df.items() if d >= args.min_df))}
    print("docs=%d  raw terms=%d  kept terms (df>=%d)=%d  %.0fs"
          % (n, len(df), args.min_df, len(vocab), time.time() - t0), flush=True)

    # CSR-style postings: term_ptr[t]..term_ptr[t+1] index into doc_ids / tfs
    term_ptr = np.zeros(len(vocab) + 1, dtype=np.int64)
    for w, i in vocab.items():
        term_ptr[i + 1] = len(postings_tmp[w])
    np.cumsum(term_ptr, out=term_ptr)
    total = int(term_ptr[-1])
    doc_ids = np.zeros(total, dtype=np.int32)
    tfs = np.zeros(total, dtype=np.int32)
    for w, i in vocab.items():
        s = int(term_ptr[i])
        for j, (d, c) in enumerate(postings_tmp[w]):
            doc_ids[s + j] = d
            tfs[s + j] = c

    np.savez(args.out + ".npz",
             term_ptr=term_ptr, doc_ids=doc_ids, tfs=tfs,
             doc_len=np.asarray(doc_len, dtype=np.int32),
             offsets=np.asarray(offsets, dtype=np.int64))
    json.dump({"vocab": vocab, "chunks_path": args.chunks, "n_docs": n,
               "avg_len": float(np.mean(doc_len)), "stem_len": STEM_LEN,
               "built": time.strftime("%Y-%m-%d %H:%M")},
              open(args.out + ".vocab.json", "w", encoding="utf-8"),
              ensure_ascii=False)
    # titles kept separately for cheap display without seeking the HDD
    json.dump(titles, open(args.out + ".titles.json", "w", encoding="utf-8"),
              ensure_ascii=False)
    sz = os.path.getsize(args.out + ".npz") / 1e6
    print("wrote %s.npz (%.0f MB) + vocab + titles  in %.0fs"
          % (args.out, sz, time.time() - t0))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
