# -*- coding: utf-8 -*-
"""A code corpus for the RAG that already exists, chunked where the code is.

The existing corpora are messages, facts and two bibles. None of them helps
with "where is the thing that decides whether to answer" -- a question with no
literal answer, because the word "decides" appears nowhere near the code that
does it. repo_search needs the string, repo_symbols needs the name. Neither
finds code you cannot yet name, and that is the gap this fills.

Chunked on AST boundaries, not character windows. A window cuts a function in
half and embeds two fragments that each describe nothing; a function, with its
signature and its docstring, is the unit a person would have pointed at. Each
chunk is capped, because a 400-line function embedded whole is a smear -- the
head of it (name, signature, docstring, opening logic) is what says what it is
for.

IMPORTANT, and enforced by how it is exposed rather than by hoping: this is
the WEAKEST evidence in the stack. Similarity is a hint about where to look,
not a fact about what the code does. Anything retrieved here is a path and a
line number to go and read, and it ranks below the source it points at and
below anything the language server or a test says.

    py -3 app/code_index.py --build
"""

import ast
import io
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:      # so `py -3 app/code_index.py` works too
    sys.path.insert(0, str(ROOT))

# Enough to carry the name, the signature, the docstring and the first real
# logic. Past that a chunk stops describing one thing.
MAX_CHUNK_CHARS = 1200
MIN_CHUNK_CHARS = 40


def _header(path, qualname, kind, lineno):
    return "%s::%s (%s) line %d" % (path, qualname, kind, lineno)


def _chunk_text(path, qualname, kind, lineno, lines, start, end):
    body = "\n".join(lines[start - 1:end])
    if len(body) > MAX_CHUNK_CHARS:
        body = body[:MAX_CHUNK_CHARS] + "\n    # ... truncated"
    return "%s\n%s" % (_header(path, qualname, kind, lineno), body)


def _end_line(node, lines):
    end = getattr(node, "end_lineno", None)
    return end if end else min(len(lines), getattr(node, "lineno", 1) + 40)


def chunks():
    """Every function, class and module in the readable subtrees, once each.

    Uses the same file walk and the same sandbox as the symbol tools, so the
    corpus can never contain a file the read tools would refuse to open.
    """
    from app import selfinspect as si

    for file_path in si._iter_source_files():
        rel = str(file_path.relative_to(ROOT)).replace("\\", "/")

        if file_path.suffix.lower() != ".py":
            # JavaScript has no end_lineno from the extractor, so a chunk runs
            # to the next definition. Same units as the Python side --
            # functions, not character windows -- which is what makes a hit
            # mean something.
            try:
                lines = io.open(file_path, encoding="utf-8",
                                errors="replace").read().splitlines()
            except OSError:
                continue
            syms = sorted(si._js_analysis(file_path)["symbols"],
                          key=lambda x: x.get("line") or 0)
            for i, sym in enumerate(syms):
                start = int(sym.get("line") or 0)
                if start < 1:
                    continue
                nxt = int(syms[i + 1].get("line") or 0) if i + 1 < len(syms) else 0
                end = (nxt - 1) if nxt > start else min(len(lines), start + 40)
                text = _chunk_text(rel, sym.get("name") or "?",
                                   sym.get("kind") or "function", start,
                                   lines, start, max(start, end))
                yield {"text": si.redact(text), "path": rel,
                       "name": sym.get("name") or "?",
                       "kind": sym.get("kind") or "function",
                       "line": start, "end_line": max(start, end)}
            continue

        parsed = si._parsed(file_path)
        if not parsed:
            continue
        tree, lines = parsed

        # The module itself: its docstring is usually the only place the
        # intent of the file is written down.
        doc = ast.get_docstring(tree) or ""
        if doc:
            text = "%s\n%s" % (_header(rel, Path(rel).stem, "module", 1),
                               doc[:MAX_CHUNK_CHARS])
            yield {"text": si.redact(text), "path": rel,
                   "name": Path(rel).stem, "kind": "module", "line": 1}

        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                yield _row(si, rel, node.name, "class", node, lines)
                for sub in node.body:
                    if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        yield _row(si, rel, "%s.%s" % (node.name, sub.name),
                                   "method", sub, lines)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                yield _row(si, rel, node.name, "function", node, lines)


def _row(si, rel, qualname, kind, node, lines):
    start = node.lineno
    end = _end_line(node, lines)
    text = _chunk_text(rel, qualname, kind, start, lines, start, end)
    return {"text": si.redact(text), "path": rel, "name": qualname,
            "kind": kind, "line": start, "end_line": end}


def write_source():
    """Write the corpus source file the RAG's build() reads."""
    from app import rag
    target = rag.CORPORA["code"]
    target.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with io.open(target, "w", encoding="utf-8", newline="\n") as fh:
        for row in chunks():
            if len(row["text"]) < MIN_CHUNK_CHARS:
                continue
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            written += 1
    return written, target


def build(progress=None):
    """Write the source rows, then embed them through the existing pipeline."""
    from app import rag
    written, target = write_source()
    rows = rag.build("code", progress=progress)
    return {"chunks": written, "embedded": rows, "source": str(target)}


def search(query, k=6, min_score=0.35, kind="", path=""):
    """Conceptually similar code. A pointer to go and read, never a citation.

    Returns the same shape the other RAG searches return, plus the location,
    so every hit can be checked against the file it names.
    """
    from app import rag
    # "Not built" and "nothing matched" must never look the same. An empty
    # list reads as "there is no such code", which is a false negative that
    # sends the reader off to write something that already exists.
    if not (rag.INDEX_DIR / "code.npy").exists():
        raise FileNotFoundError(
            "the code corpus has not been built yet: py -3 app/code_index.py --build")
    hits = rag.search(query, corpora=["code"], k=max(k * 3, k), min_score=min_score)
    out = []
    for hit in hits:
        if kind and hit.get("kind") != kind:
            continue
        if path and path not in str(hit.get("path") or ""):
            continue
        out.append({
            "path": hit.get("path"), "name": hit.get("name"),
            "kind": hit.get("kind"), "line": hit.get("line"),
            "score": round(float(hit.get("score") or 0.0), 3),
            "preview": " ".join(str(hit.get("text") or "").split())[:200],
        })
        if len(out) >= k:
            break
    return {"query": query, "results": out,
            "note": ("semantic similarity only -- open the file at the line "
                     "given and confirm before relying on any of this")}


def main(argv):
    if "--build" in argv:
        def show(done, total):
            print("  embedded %d/%d" % (done, total), flush=True)
        out = build(progress=show)
        print(json.dumps(out, indent=2))
        return 0
    if "--write-source" in argv:
        written, target = write_source()
        print("wrote %d chunks to %s" % (written, target))
        return 0
    query = " ".join(a for a in argv[1:] if not a.startswith("-"))
    if not query:
        print((__doc__ or "").strip().splitlines()[0])
        print("\nusage: py -3 app/code_index.py --build"
              "\n       py -3 app/code_index.py <query>")
        return 2
    print(json.dumps(search(query), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
