# -*- coding: utf-8 -*-
"""BM25 retrieval over local Latvian Wikipedia, with stable/fresh routing.

Index (built by _tools/lvwiki_bm25_build.py) lives on the SSD; chunk TEXT stays
on the HDD and is fetched by byte offset for the few top-k hits only.
"""
import json
import os
import re
import threading

import numpy as np
from app import paths

INDEX = paths.ai("rag_index", "lvwiki_bm25")

# Tokenisation lives here (the app side) so search and the offline builder share
# ONE definition; the builder imports norm_tokens from this module.
_FOLD = str.maketrans("āčēģīķļņšūžĀČĒĢĪĶĻŅŠŪŽ", "acegiklnsuzACEGIKLNSUZ")
_TOKEN = re.compile(r"[0-9A-Za-zĀČĒĢĪĶĻŅŠŪŽāčēģīķļņšūž]+")
STEM_LEN = 6


def norm_tokens(text):
    """Lowercase, diacritic-fold, 6-char stem. Digits (years) kept whole."""
    out = []
    for m in _TOKEN.findall(text or ""):
        w = m.lower().translate(_FOLD)
        if w.isdigit():
            out.append(w)
        elif len(w) > 2:
            out.append(w[:STEM_LEN])
    return out
_K1, _B = 1.5, 0.75
_lock = threading.Lock()
_state = {"loaded": False, "ok": False}


def _load():
    with _lock:
        if _state["loaded"]:
            return _state["ok"]
        _state["loaded"] = True
        try:
            npz = np.load(INDEX + ".npz", mmap_mode="r")
            meta = json.load(open(INDEX + ".vocab.json", encoding="utf-8"))
            _state.update(
                term_ptr=np.asarray(npz["term_ptr"]),
                doc_ids=npz["doc_ids"], tfs=npz["tfs"],
                doc_len=np.asarray(npz["doc_len"], dtype=np.float32),
                offsets=np.asarray(npz["offsets"]),
                vocab=meta["vocab"], n=int(meta["n_docs"]),
                avg_len=float(meta["avg_len"]),
                chunks_path=meta["chunks_path"], ok=True)
            # precompute idf per term id
            tp = _state["term_ptr"]
            dfreq = np.diff(tp)
            n = _state["n"]
            _state["idf"] = np.log(1.0 + (n - dfreq + 0.5) / (dfreq + 0.5))
        except Exception as exc:
            _state["ok"] = False
            _state["err"] = str(exc)[:200]
        return _state["ok"]


def available():
    return _load()


def _fetch(doc_id):
    """Read one chunk's JSON from the HDD by byte offset."""
    try:
        with open(_state["chunks_path"], "rb") as f:
            f.seek(int(_state["offsets"][doc_id]))
            return json.loads(f.readline().decode("utf-8"))
    except Exception:
        return None


def search(query, k=5, min_score=1.0):
    """Top-k lvwiki chunks for a query, BM25-scored. [] if unavailable."""
    if not _load() or not str(query or "").strip():
        return []
    q_terms = norm_tokens(query)
    vocab, idf = _state["vocab"], _state["idf"]
    tp, doc_ids, tfs = _state["term_ptr"], _state["doc_ids"], _state["tfs"]
    doc_len, avg = _state["doc_len"], _state["avg_len"]
    scores = {}
    for w in set(q_terms):
        tid = vocab.get(w)
        if tid is None:
            continue
        s, e = int(tp[tid]), int(tp[tid + 1])
        if e <= s:
            continue
        w_idf = float(idf[tid])
        d = doc_ids[s:e]
        f = tfs[s:e].astype(np.float32)
        dl = doc_len[d]
        denom = f + _K1 * (1.0 - _B + _B * dl / avg)
        contrib = w_idf * (f * (_K1 + 1.0)) / denom
        for doc, sc in zip(d.tolist(), contrib.tolist()):
            scores[doc] = scores.get(doc, 0.0) + sc
    if not scores:
        return []
    top = sorted(scores.items(), key=lambda x: -x[1])[:k]
    out = []
    for doc_id, sc in top:
        if sc < min_score:
            continue
        row = _fetch(doc_id)
        if row:
            row["score"] = round(float(sc), 3)
            out.append(row)
    return out


_FRESH_HINTS = (
    "šodien", "sodien", "šobrīd", "sobrid", "pašlaik", "paslaik", "tagad",
    "vakar", "rīt", "rit", "šonedēļ", "sonedel", "šomēnes", "pēdējā", "pedeja",
    "jaunāk", "jaunak", "patlaban", "nesen", "gada laikā", "šogad", "sogad",
    "cik pulkstenis", "laika prognoze", "kāds laiks", "kads laiks",
)
# who currently holds an office etc. is public but CHANGES -> not stable local
_VOLATILE_HINTS = ("premjer", "ministr", "prezident", "valdīb", "valdib",
                   "kurss", "cena", "maksā", "temperatūr", "laiks ārā")


def freshness(question):
    """STABLE | FRESH | AMBIGUOUS -- which retrieval a question wants."""
    q = " %s " % str(question or "").lower()
    if any(h in q for h in _FRESH_HINTS):
        return "FRESH"
    if any(h in q for h in _VOLATILE_HINTS):
        return "FRESH"
    return "STABLE"


def stable_lookup(question, k=4, min_score=3.0):
    """Local lvwiki hits for a STABLE question, else []. The gate the fact path
    calls before spending a web round trip."""
    if freshness(question) != "STABLE":
        return []
    return search(question, k=k, min_score=min_score)
