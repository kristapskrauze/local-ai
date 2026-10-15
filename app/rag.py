"""Local retrieval over several corpora, for the iMessage persona bot.

Embeddings come from Ollama (bge-m3: multilingual, handles Latvian). Vectors are
stored as float16 .npy next to a .jsonl of metadata; both are memory-mapped and
cached per process. No new Python dependencies beyond numpy.
"""
import json
import logging
import os
import re
import threading
import time
import urllib.request
from pathlib import Path

import numpy as np
from app import paths

LOG = logging.getLogger(__name__)

OLLAMA = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
EMBED_MODEL = os.environ.get("RAG_EMBED_MODEL", "embed")
SRC_DIR = Path(paths.ai("rag_src"))
INDEX_DIR = Path(paths.ai("rag_index"))
_lock = threading.Lock()
_cache = {}

# source file -> corpus name
CORPORA = {
    "own_msgs": SRC_DIR / "own_msgs.jsonl",
    "bible_lv": SRC_DIR / "bible_lv.jsonl",
    "bible_kjv": SRC_DIR / "bible_kjv.jsonl",
    "facts": SRC_DIR / "facts.jsonl",
    # Written by app/code_index.py, chunked on AST boundaries. Supplemental
    # to the symbol tools, never a substitute: a hit here is a path and a line
    # to go and read.
    "code": SRC_DIR / "code.jsonl",
}


_INTERACTIVE_TIMEOUT = 20
_BACKOFF = (10, 30, 60)


def embed(texts, timeout=180, tries=4, interactive=False, gpu=False):
    """Embed a list of strings via Ollama. Returns float32 (n, dim).

    Retries with backoff because Ollama opens an internal TCP connection per
    input, and a sustained run exhausts the Windows ephemeral port range
    (~16k ports, 120s TIME_WAIT): indexing at ~124 texts/s died with
    'bind: ... system lacked sufficient buffer space' after ~24k rows. The
    sleep lets sockets drain; build() also throttles the request rate.

    `interactive=True` means somebody is waiting for a message: one attempt,
    a short timeout, no backoff sleep. Ranking memories is a nice-to-have and
    every caller already falls back to token order when this raises.

    `gpu=True` is for INDEXING ONLY. The CPU default exists so that ranking a
    handful of memories mid-reply cannot evict the chat model, and for a
    handful it costs nothing. For a corpus it is fatal: a cold single text
    takes ~14.5s on the CPU, so the 2696-chunk code corpus works out at about
    ten hours and never finishes. A build is a deliberate one-off where paying
    one 13.5s reload of the chat model afterwards is the right trade.
    """
    if isinstance(texts, str):
        texts = [texts]
    texts = [(str(t).strip() or "-")[:2000] for t in texts]
    if interactive:
        tries, timeout = 1, min(int(timeout), _INTERACTIVE_TIMEOUT)
    embed_options = {} if gpu else {"num_gpu": 0}
    body = json.dumps({"model": EMBED_MODEL, "input": texts,
                       # same reason as every chat lane: a call that omits it
                       # makes Ollama reload the embedding model
                       "keep_alive": "2h",
                       # ON THE CPU by default. See the module note: on a card
                       # with ~1.8 GB free this 1.2 GB model evicts the 17.3 GB
                       # chat model, and getting it back costs ~13.5s. Indexing
                       # passes gpu=True because on the CPU a corpus build does
                       # not finish at all.
                       "options": embed_options}).encode("utf-8")
    last = None
    for attempt in range(tries):
        started = time.time()
        ps_before = _ollama_snapshot()
        try:
            req = urllib.request.Request(
                OLLAMA + "/api/embed", data=body,
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                out = json.loads(r.read().decode("utf-8"))
            vecs = out.get("embeddings") or []
            if not vecs:
                raise RuntimeError(
                    "no embeddings returned (is %s pulled?)" % EMBED_MODEL)
            arr = np.asarray(vecs, dtype=np.float32)
            norms = np.linalg.norm(arr, axis=1, keepdims=True)
            _note(EMBED_MODEL, time.time() - started, len(texts),
                  options=embed_options, ps_before=ps_before,
                  ps_after=_ollama_snapshot())
            return arr / np.clip(norms, 1e-9, None)
        except Exception as exc:
            last = exc
            _note(EMBED_MODEL, time.time() - started, len(texts),
                  error=str(exc), options=embed_options,
                  ps_before=ps_before, ps_after=_ollama_snapshot())
            if attempt == tries - 1:
                break
            time.sleep(_BACKOFF[min(attempt, len(_BACKOFF) - 1)])
    raise last


_EMBED_FAILS = {"n": 0, "warned": 0.0}


def warm_embed():
    """Load the embedding model at boot, ON THE CPU, so nothing pays for it later.

    It was never loaded automatically: the first retrieval of the day paid the
    cold load, and because a cold embed lands wherever there is room, it was
    also the call most likely to land on the GPU and evict the 17.3 GB chat
    model -- ~13.5s to get that back, 105 reloads over 120 turns before the
    CPU pin went in.

    This deliberately goes through embed() rather than posting its own request:
    Ollama keys a runner on the OPTIONS it was given, so a warm call that
    differed even slightly would load a SECOND copy instead of the one the
    real lanes reuse -- warming the wrong runner and evicting the chat model
    is precisely the failure this is meant to prevent.

    gpu=False and a one-token input, so it costs no VRAM and cannot push the
    chat model off the card. Failure is logged and ignored: a cold embedder is
    slow, an exception at boot would be worse.
    """
    try:
        started = time.time()
        embed(["warm"], timeout=300, tries=1, gpu=False)
        LOG.info("embedding model %s warmed on the CPU in %.1fs",
                 EMBED_MODEL, time.time() - started)
    except Exception as exc:
        LOG.info("embed warm skipped: %s", str(exc)[:160])


def _ollama_snapshot():
    try:
        from app import telemetry
        return telemetry.snapshot_ollama(OLLAMA)
    except Exception:
        return None


def _note(model, seconds, count, error="", options=None,
          ps_before=None, ps_after=None):
    """Put this call in the turn's trace, and shout if the embedder is dead."""
    try:
        from app import telemetry
        telemetry.record_embed(model, seconds, count=count, error=error,
                               options=options, ps_before=ps_before,
                               ps_after=ps_after)
    except Exception:
        pass
    try:
        if error:
            _EMBED_FAILS["n"] += 1
            # three in a row is not a slow day, it is a stuck loader: every
            # /api/embed hanging blocks Ollama from loading ANY model, which
            # times out the agent and drops the turn to a tool-less answer
            if _EMBED_FAILS["n"] >= 3 and time.time() - _EMBED_FAILS["warned"] > 600:
                _EMBED_FAILS["warned"] = time.time()
                LOG.warning(
                    "EMBEDDING MODEL NOT RESPONDING: %d consecutive failures on "
                    "%s (last: %s). Ollama's loader is probably wedged — chat "
                    "may still work while every model LOAD hangs behind it. "
                    "Restart Ollama.", _EMBED_FAILS["n"], model, str(error)[:120])
        else:
            _EMBED_FAILS["n"] = 0
    except Exception:
        pass


def build(name, batch=64, progress=None, pause=1.5, gpu=True):
    """Embed a corpus and write <name>.npy + <name>.meta.jsonl.

    `pause` throttles between batches on purpose: Windows runs out of ephemeral
    ports if we drive Ollama flat out (see embed()). batch=192 with pause=1.5
    sustains ~60 texts/s, which indexes 30k rows in ~8 min without dying.
    """
    src = CORPORA.get(name)
    if not src or not src.exists():
        raise FileNotFoundError("no source for corpus %r (%s)" % (name, src))
    rows = []
    seen = set()
    with open(src, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            text = " ".join(str(obj.get("text") or "").split())
            # short fragments ("ok", "jā") carry no retrievable signal and would
            # dominate a style search; drop them and exact duplicates
            if len(text) < 12 or text.lower() in seen:
                continue
            seen.add(text.lower())
            obj["text"] = text
            rows.append(obj)
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    vecs = np.zeros((len(rows), 1024), dtype=np.float16)
    dim = None
    for i in range(0, len(rows), batch):
        chunk = [r["text"] for r in rows[i:i + batch]]
        got = embed(chunk, gpu=gpu)
        if dim is None:
            dim = got.shape[1]
            if dim != vecs.shape[1]:
                vecs = np.zeros((len(rows), dim), dtype=np.float16)
        vecs[i:i + len(chunk)] = got.astype(np.float16)
        if progress and (i // batch) % 10 == 0:
            progress(i + len(chunk), len(rows))
        if pause and i + batch < len(rows):
            time.sleep(pause)
    np.save(INDEX_DIR / ("%s.npy" % name), vecs[:, :dim])
    with open(INDEX_DIR / ("%s.meta.jsonl" % name), "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    _cache.pop(name, None)
    return len(rows)


def _load(name):
    with _lock:
        hit = _cache.get(name)
        vp = INDEX_DIR / ("%s.npy" % name)
        mp = INDEX_DIR / ("%s.meta.jsonl" % name)
        if not vp.exists() or not mp.exists():
            return None
        stamp = (vp.stat().st_mtime, mp.stat().st_mtime)
        if hit and hit["stamp"] == stamp:
            return hit
        meta = []
        with open(mp, encoding="utf-8") as f:
            for line in f:
                try:
                    meta.append(json.loads(line))
                except Exception:
                    pass
        entry = {"vecs": np.load(vp, mmap_mode="r"), "meta": meta, "stamp": stamp}
        _cache[name] = entry
        return entry


def available():
    """Corpora that have a usable index, with row counts."""
    out = {}
    for name in CORPORA:
        e = _load(name)
        if e is not None:
            out[name] = len(e["meta"])
    return out


def search(query, corpora=None, k=5, min_score=0.35):
    """Top-k matches across the given corpora. Returns dicts + 'score'/'corpus'."""
    names = [n for n in (corpora or list(CORPORA)) if _load(n) is not None]
    if not names or not str(query or "").strip():
        return []
    try:
        # every caller of search() is a live turn or a tool with a timeout
        q = embed([str(query)], interactive=True)[0].astype(np.float32)
    except Exception:
        return []
    hits = []
    for name in names:
        e = _load(name)
        vecs = np.asarray(e["vecs"], dtype=np.float32)
        if vecs.size == 0:
            continue
        sims = vecs @ q
        take = min(k, sims.shape[0])
        idx = np.argpartition(-sims, take - 1)[:take]
        for i in idx[np.argsort(-sims[idx])]:
            s = float(sims[i])
            if s < min_score:
                continue
            row = dict(e["meta"][int(i)])
            row["score"] = round(s, 4)
            row["corpus"] = name
            hits.append(row)
    hits.sort(key=lambda r: -r["score"])
    return hits[:k]


def style_phrases(query, k=6):
    return [h["text"] for h in search(query, ["own_msgs"], k=k, min_score=0.45)]


def bible_verse(query, k=1, prefer_lv=True):
    """A relevant verse. Latvian NT verbatim when it matches; KJV otherwise."""
    order = ["bible_lv", "bible_kjv"] if prefer_lv else ["bible_kjv", "bible_lv"]
    for name in order:
        hits = search(query, [name], k=k, min_score=0.42)
        if hits:
            return hits[0]
    return None


def relevance(texts, corpus="own_msgs", top_k=3):
    entry = _load(corpus)
    if entry is None or not texts:
        return [0.0] * len(texts)
    try:
        q = embed(list(texts))
    except Exception:
        return [0.0] * len(texts)
    vecs = np.asarray(entry["vecs"], dtype=np.float32)
    if vecs.size == 0:
        return [0.0] * len(texts)
    sims = q @ vecs.T                      # (n_texts, n_msgs)
    k = min(max(1, top_k), sims.shape[1])
    # mean of the top-k matches: one lucky match is noise, a cluster is interest
    part = np.partition(sims, -k, axis=1)[:, -k:]
    return [float(x) for x in part.mean(axis=1)]


def random_fact(seed_text="", k=1):
    hits = search(seed_text or "interesting fact", ["facts"], k=k, min_score=0.30)
    return hits[0] if hits else None
