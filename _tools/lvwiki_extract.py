# -*- coding: utf-8 -*-
"""Turn the raw lvwiki dump into retrieval chunks, with provenance intact.

Stops BEFORE embedding on purpose: the plan says do not commit to an embedding
model until it has been tested on Latvian queries, and this JSONL is the input
to whichever one wins. Chunking is the part that does not change.

Chunking is by STRUCTURE, not by character count: article -> section ->
paragraph groups under a token-ish budget. Every chunk carries its title and
section, because a paragraph that says "viņš dzimis 1877. gadā" is useless in a
retrieval hit without knowing who "viņš" is.

Wikitext is reduced with a conservative regex pass rather than a full parser:
templates, refs, files, tables and comments are dropped, links are unwrapped to
their display text. Anything still containing markup after that is skipped
rather than indexed dirty.

  python lvwiki_extract.py [--limit N] [--min-chars 200]
"""
import argparse
import bz2
import hashlib
import json
import os
import re
import time
import xml.etree.ElementTree as ET

RAW = r"D:\antons-data\knowledge\wikipedia\lv\raw"
OUT = r"D:\antons-data\knowledge\wikipedia\lv\extracted"
MANIFESTS = r"D:\antons-data\knowledge\wikipedia\lv\manifests"

# Namespace 0 only: no Talk:, Category:, Template:, Vikipēdija: ...
NS_MAIN = "0"
REDIRECT = re.compile(r"^\s*#(REDIRECT|PĀRADRESĀCIJA)", re.I)

RE_COMMENT = re.compile(r"<!--.*?-->", re.S)
RE_REF = re.compile(r"<ref[^>]*?/>|<ref.*?</ref>", re.S | re.I)
RE_TABLE = re.compile(r"\{\|.*?\|\}", re.S)
RE_HTML = re.compile(r"<[^>]+>")
RE_FILE = re.compile(r"\[\[(?:File|Image|Attēls|Fails)\s*:[^\]]*\]\]", re.I)
RE_LINK_PIPE = re.compile(r"\[\[[^\]|]*\|([^\]]*)\]\]")
RE_LINK = re.compile(r"\[\[([^\]|]*)\]\]")
RE_EXTLINK = re.compile(r"\[https?://[^\s\]]+\s*([^\]]*)\]")
RE_BOLDITAL = re.compile(r"'{2,5}")
RE_HEADING = re.compile(r"^\s*(={2,6})\s*(.+?)\s*\1\s*$", re.M)
RE_WS = re.compile(r"[ \t]+")
RE_BLANK = re.compile(r"\n{3,}")
# Anything still carrying markup after cleaning is dropped rather than indexed
# dirty. The first version only looked for braces and brackets, which let
# 1511 chunks (0.42%) through still holding <ref> tags and table pipes.
RE_LEFTOVER = re.compile(r"\{\{|\}\}|\[\[|\]\]|<ref|</ref>|\|\||^\s*\|", re.M)

# Sections that carry no prose worth retrieving.
SKIP_SECTIONS = {
    "atsauces", "ārējās saites", "skatīt arī", "literatūra", "avoti",
    "bibliogrāfija", "piezīmes", "saites", "references", "external links",
}


def strip_templates(text):
    """Drop {{...}} with nesting, which a regex alone cannot do."""
    out, depth, i = [], 0, 0
    while i < len(text):
        if text.startswith("{{", i):
            depth += 1
            i += 2
        elif text.startswith("}}", i):
            depth = max(0, depth - 1)
            i += 2
        else:
            if depth == 0:
                out.append(text[i])
            i += 1
    return "".join(out)


def clean_wikitext(text):
    text = RE_COMMENT.sub("", text)
    text = RE_REF.sub("", text)
    text = strip_templates(text)
    text = RE_TABLE.sub("", text)
    text = RE_FILE.sub("", text)
    text = RE_LINK_PIPE.sub(r"\1", text)
    text = RE_LINK.sub(r"\1", text)
    text = RE_EXTLINK.sub(r"\1", text)
    text = RE_HTML.sub("", text)
    text = RE_BOLDITAL.sub("", text)
    text = RE_WS.sub(" ", text)
    return RE_BLANK.sub("\n\n", text).strip()


def split_sections(text):
    """[(section_name, body)] with the lead paragraph as section ''."""
    parts, last, name = [], 0, ""
    for m in RE_HEADING.finditer(text):
        body = text[last:m.start()].strip()
        if body:
            parts.append((name, body))
        name, last = m.group(2).strip(), m.end()
    tail = text[last:].strip()
    if tail:
        parts.append((name, tail))
    return parts


RE_SENT = re.compile(r"(?<=[.!?])\s+")


def split_long(para, target):
    """A single paragraph longer than 2x target is split on sentence ends.

    Without this the 'never split a paragraph' rule produced an 8172-char chunk
    against a 900 target -- one runaway paragraph becomes a chunk no retriever
    can rank usefully and no context window wants.
    """
    if len(para) <= 2 * target:
        return [para]
    out, cur = [], ""
    for s in RE_SENT.split(para):
        if cur and len(cur) + len(s) + 1 > target:
            out.append(cur)
            cur = s
        else:
            cur = (cur + " " + s).strip()
    if cur:
        out.append(cur)
    return out


def chunk_body(body, target=900, hard_min=200):
    """Group paragraphs up to ~target chars, splitting only runaway ones."""
    paras = []
    for p in [p.strip() for p in body.split("\n") if p.strip()]:
        paras.extend(split_long(p, target))
    chunks, cur = [], ""
    for para in paras:
        if len(para) < 40 and not cur:
            continue                      # stray list fragment
        if cur and len(cur) + len(para) + 1 > target:
            chunks.append(cur)
            cur = para
        else:
            cur = (cur + "\n" + para).strip()
    if cur:
        chunks.append(cur)
    return [c for c in chunks if len(c) >= hard_min]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="0 = all articles")
    ap.add_argument("--min-chars", type=int, default=200)
    ap.add_argument("--target", type=int, default=900)
    args = ap.parse_args()
    os.makedirs(OUT, exist_ok=True)

    dumps = [f for f in os.listdir(RAW) if f.endswith("multistream.xml.bz2")]
    if not dumps:
        raise SystemExit("no multistream dump in %s" % RAW)
    dump = os.path.join(RAW, dumps[0])
    dump_date = re.search(r"lvwiki-(\d{8})-", dumps[0]).group(1)
    dest = os.path.join(OUT, "lvwiki_%s_chunks.jsonl" % dump_date)
    print("dump:  %s" % dump)
    print("out:   %s" % dest)

    ns = "{http://www.mediawiki.org/xml/export-0.11/}"
    pages = redirects = skipped = chunks = 0
    started = time.time()
    with bz2.open(dump, "rb") as fh, open(dest, "w", encoding="utf-8") as out:
        for event, elem in ET.iterparse(fh, events=("end",)):
            if not elem.tag.endswith("}page"):
                continue
            try:
                if (elem.findtext(ns + "ns") or "") != NS_MAIN:
                    continue
                title = elem.findtext(ns + "title") or ""
                rev = elem.find(ns + "revision")
                if rev is None:
                    continue
                text = rev.findtext(ns + "text") or ""
                if REDIRECT.match(text):
                    redirects += 1
                    continue
                pages += 1
                page_id = elem.findtext(ns + "id") or ""
                rev_id = rev.findtext(ns + "id") or ""
                cleaned = clean_wikitext(text)
                for section, body in split_sections(cleaned):
                    if section.strip().lower() in SKIP_SECTIONS:
                        continue
                    # The LEAD paragraph is kept at a much lower floor. A 200
                    # char minimum silently dropped 19,893 articles (13.8%) --
                    # all short stubs -- and a stub lead is exactly what answers
                    # "Kas ir X?". Body sections keep the higher floor, where a
                    # fragment really is noise.
                    floor = 80 if not section else args.min_chars
                    for c in chunk_body(body, args.target, floor):
                        if RE_LEFTOVER.search(c):
                            skipped += 1
                            continue
                        rec = {
                            "source": "lvwiki",
                            "language": "lv",
                            "title": title,
                            "section": section,
                            "text": c,
                            "page_id": page_id,
                            "revision_id": rev_id,
                            "dump_date": dump_date,
                            "canonical_source":
                                "https://lv.wikipedia.org/wiki/" + title.replace(" ", "_"),
                            "content_hash": hashlib.sha1(c.encode("utf-8")).hexdigest(),
                        }
                        out.write(json.dumps(rec, ensure_ascii=False) + "\n")
                        chunks += 1
                if pages % 20000 == 0:
                    print("  %6d articles, %8d chunks, %.0fs"
                          % (pages, chunks, time.time() - started), flush=True)
                if args.limit and pages >= args.limit:
                    break
            finally:
                elem.clear()

    size = os.path.getsize(dest)
    print("\narticles=%d redirects=%d chunks=%d dirty_skipped=%d  %.1f MB  %.0fs"
          % (pages, redirects, chunks, skipped, size / 1e6, time.time() - started))
    json.dump({"name": "lvwiki-chunks", "dump_date": dump_date,
               "built": time.strftime("%Y-%m-%d %H:%M"),
               "articles": pages, "redirects": redirects, "chunks": chunks,
               "dirty_skipped": skipped, "target_chars": args.target,
               "min_chars": args.min_chars, "path": dest, "bytes": size,
               "license": "CC BY-SA 4.0, see raw manifest"},
              open(os.path.join(MANIFESTS, "lvwiki_%s_chunks.json" % dump_date),
                   "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
