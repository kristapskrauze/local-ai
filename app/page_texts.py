# -*- coding: utf-8 -*-
"""Page text in real Latvian: the model says what a player sees and what it
means, the translator writes the Latvian.

Here the strings are found mechanically -- HTML text, a few visible
attributes, JS string literals, each with its exact position. The model only
says which of them a player sees and what each means in plain English; the
translator turns that English into Latvian; each string is replaced where it
stands, escaped for its context. Code, ids, the selftest block and anything
the model does not mark visible are never touched.
"""
import html as _html
import json
import re

try:
    from app.logging_setup import get_logger  # type: ignore
    LOGGER = get_logger(__name__)
except Exception:  # pragma: no cover
    import logging
    LOGGER = logging.getLogger(__name__)

_ASK_CHUNK = 150
_LETTER_RE = re.compile(r"[^\W\d_]", re.U)
_JS_ESC = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "v": "\v", "0": "\0"}
_HEX4_RE = re.compile(r"[0-9a-fA-F]{4}")
_HEX2_RE = re.compile(r"[0-9a-fA-F]{2}")
_SCRIPT_RE = re.compile(r"<script\b([^>]*)>(.*?)</script>", re.S | re.I)
_STYLE_RE = re.compile(r"<style\b[^>]*>.*?</style>", re.S | re.I)
_TAG_RE = re.compile(r"<[^>]*>", re.S)
_ATTR_RE = re.compile(
    r"\b(title|placeholder|alt|aria-label)\s*=\s*(\"([^\"]*)\"|'([^']*)')", re.I)
_META_CONTENT_RE = re.compile(
    r"<meta\b[^>]*\bname\s*=\s*[\"'](?:game-name|game-desc|description)[\"'][^>]*"
    r"\bcontent\s*=\s*(\"([^\"]*)\"|'([^']*)')", re.I)
_JSON_TYPE_RE = re.compile(r"\btype\s*=\s*[\"']?(application/json|text/template)", re.I)


def _worth(text):
    t = text.strip()
    return len(t) >= 2 and bool(_LETTER_RE.search(t))


def _js_unescape(raw):
    """The value a JS string literal's source text stands for."""
    out, i, n = [], 0, len(raw)
    while i < n:
        c = raw[i]
        if c == "\\" and i + 1 < n:
            d = raw[i + 1]
            if d == "u" and raw[i + 2:i + 3] == "{":
                j = raw.find("}", i + 3)
                try:
                    out.append(chr(int(raw[i + 3:j], 16)))
                    i = j + 1
                    continue
                except Exception:
                    pass
            elif d == "u" and _HEX4_RE.match(raw, i + 2):
                out.append(chr(int(raw[i + 2:i + 6], 16)))
                i += 6
                continue
            elif d == "x" and _HEX2_RE.match(raw, i + 2):
                out.append(chr(int(raw[i + 2:i + 4], 16)))
                i += 4
                continue
            elif d == "\n":
                i += 2                         # a line continuation
                continue
            out.append(_JS_ESC.get(d, d))
            i += 2
            continue
        out.append(c)
        i += 1
    s = "".join(out)
    try:
        return s.encode("utf-16", "surrogatepass").decode("utf-16")
    except Exception:
        return s


def _js_strings(src, offset):
    """(start, end, quote, value) of each string literal in JS source. Comments
    are skipped; a string never spans a line, so a regex literal that confuses
    the scan recovers at the next newline."""
    out, i, n = [], 0, len(src)
    while i < n:
        ch = src[i]
        if ch == "/" and i + 1 < n and src[i + 1] == "/":
            j = src.find("\n", i)
            i = n if j < 0 else j
            continue
        if ch == "/" and i + 1 < n and src[i + 1] == "*":
            j = src.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        if ch in "\"'`":
            j, buf, ok = i + 1, [], False
            while j < n:
                c = src[j]
                if c == "\\" and j + 1 < n:
                    buf.append(src[j:j + 2])
                    j += 2
                    continue
                if c == ch:
                    ok = True
                    break
                if c == "\n" and ch != "`":
                    break
                if ch == "`" and c == "$" and j + 1 < n and src[j + 1] == "{":
                    break                      # a template with ${} -- leave it
                buf.append(c)
                j += 1
            if ok:
                out.append((offset + i + 1, offset + j, ch, _js_unescape("".join(buf))))
                i = j + 1
                continue
            i = j + 1 if j < n else n
            continue
        i += 1
    return out


def candidates(page):
    """Every string a player MIGHT see, with where it stands. Mechanical only."""
    cands = []
    # <script> bodies: JS literals from executable scripts; JSON/template blocks
    # (the selftest block) are skipped entirely.
    blocked = []
    for m in _SCRIPT_RE.finditer(page):
        blocked.append((m.start(), m.end()))
        if _JSON_TYPE_RE.search(m.group(1) or ""):
            continue
        for s, e, q, val in _js_strings(m.group(2), m.start(2)):
            if _worth(val):
                cands.append({"start": s, "end": e, "kind": "js", "quote": q, "text": val})
    for m in _STYLE_RE.finditer(page):
        blocked.append((m.start(), m.end()))

    def _inside(pos):
        return any(a <= pos < b for a, b in blocked)

    # text between tags (outside script/style), and the visible attributes
    last = 0
    for m in _TAG_RE.finditer(page):
        seg_s, seg_e = last, m.start()
        last = m.end()
        if seg_e > seg_s and not _inside(seg_s):
            seg = page[seg_s:seg_e]
            if _worth(seg):
                lead = len(seg) - len(seg.lstrip())
                trail = len(seg.rstrip())
                cands.append({"start": seg_s + lead, "end": seg_s + trail, "kind": "html",
                              "quote": "", "text": _html.unescape(seg.strip())})
        if _inside(m.start()):
            continue
        for a in _ATTR_RE.finditer(m.group(0)):
            g = 3 if a.group(3) is not None else 4
            val = a.group(g)
            if _worth(val):
                s = m.start() + a.start(g)
                cands.append({"start": s, "end": s + len(val), "kind": "attr",
                              "quote": a.group(2)[0], "text": _html.unescape(val)})
    for mm in _META_CONTENT_RE.finditer(page):
        g = 2 if mm.group(2) is not None else 3
        val = mm.group(g)
        if _worth(val):
            cands.append({"start": mm.start(g), "end": mm.end(g), "kind": "attr",
                          "quote": mm.group(1)[0], "text": _html.unescape(val)})
    cands.sort(key=lambda c: c["start"])
    # overlapping spans (a meta found twice) -- keep the first
    uniq, end = [], -1
    for c in cands:
        if c["start"] >= end:
            uniq.append(c)
            end = c["end"]
    for i, c in enumerate(uniq):
        c["i"] = i
    return uniq


_ASK_SYS = (
    "You are given the strings found in a web page or game -- its HTML text, a "
    "few attributes and every string literal in its JavaScript -- each with a "
    "number and the line it sits on. Many are code: ids, class names, CSS, "
    "event names, keys, URLs. Pick ONLY the ones a person using the page SEES "
    "(on screen, in a button, a message, a title, a score label), and for each "
    "say in English what it should say to them -- keep the meaning, the tone, "
    "emoji and numbers, and keep what the page itself calls things (its "
    "creatures, items, the game's name). A translation program turns your "
    "English into Latvian ONE TEXT AT A TIME, without the page around it, and "
    "it leaves game slang untranslated. So write plain, literal English that "
    "describes what the person does on the phone ('tap the lizards when they "
    "appear', 'several in a row', 'play the game again') -- not slang "
    "('whack', 'hit', 'pop up', 'streak', 'combo') -- and make every text "
    "clear on its own: 'main menu' not 'menu', 'new best score' not 'new "
    "record', 'continue the game' not 'resume'. Leave out code and anything "
    "that is only a number or an emoji. Answer only with JSON.")

_RECHECK_SYS = (
    "A translation program turned each English text below, from a phone game, "
    "into Latvian -- one text at a time, without the game around it. Some came "
    "back wrong: with English words left in them, untranslated, or with a word "
    "read in the wrong sense because the program saw the text alone (a "
    "restaurant menu instead of the game's menu, a 'record' as in a recording). "
    "List ONLY those. For each, `new_english` is a NEW English wording of the "
    "same meaning, in OTHER words than the English above -- plain, clear on its "
    "own, describing what the person does or the result ('Whack the lizards' "
    "-> 'Tap the lizards'; 'Great smashing!' -> 'Great job!'; 'Play again' -> "
    "'Start the game again'; 'Menu' -> 'Main menu'). Slang verbs such as "
    "whack, smash, hit or combo come back untranslated too -- do not use them. "
    "Answer only with JSON.")
_RECHECK_SCHEMA = {"type": "object", "properties": {"fix": {"type": "array", "items": {
    "type": "object", "properties": {"i": {"type": "integer"},
                                     "what_is_wrong": {"type": "string"},
                                     "new_english": {"type": "string"}},
    "required": ["i", "what_is_wrong", "new_english"]}}}, "required": ["fix"]}
_ASK_SCHEMA = {"type": "object", "properties": {"texts": {"type": "array", "items": {
    "type": "object", "properties": {"i": {"type": "integer"}, "en": {"type": "string"}},
    "required": ["i", "en"]}}}, "required": ["texts"]}


def _default_chat():
    """chat(messages, schema) -> str on the assistant model, temperature 0."""
    from app import messaging as _M
    acfg = dict(_M.load_config().get("auto_reply") or {})
    acfg["model"] = (acfg.get("assistant_model") or acfg.get("model")
                     or _M._default_chat_model())

    def chat(msgs, schema):
        return _M._ollama_chat(acfg, msgs, options={"temperature": 0, "num_predict": 6000},
                               format_schema=schema)
    return chat


def recheck(rows, chat=None):
    """{i: plainer English} for the translations that kept English words.
    `rows` = [(i, english, latvian)]. One model call; nothing when it fails."""
    if not rows:
        return {}
    listing = "\n".join("%d. EN: %s\n   LV: %s" % (i, json.dumps(en, ensure_ascii=False),
                                                  json.dumps(lv, ensure_ascii=False))
                        for i, en, lv in rows)
    try:
        said = json.loads((chat or _default_chat())(
            [{"role": "system", "content": _RECHECK_SYS},
             {"role": "user", "content": listing}], _RECHECK_SCHEMA) or "{}")
    except Exception:
        LOGGER.exception("page_texts: recheck failed")
        return {}
    was = {i: en for i, en, _ in rows}
    out = {}
    for row in (said.get("fix") or []) if isinstance(said, dict) else []:
        try:
            i = int(row.get("i"))
            en = str(row.get("new_english") or row.get("en") or "").strip()
        except Exception:
            continue
        # the same English again is no rewording: the model named the bad
        # translation and handed back the text that produced it (measured)
        if i in was and en and _worth(en) and en.lower() != was[i].strip().lower():
            out[i] = en
    return out


def ask_meanings(page, cands, chat=None):
    """{i: English meaning} for the candidates a person sees. One model call.
    `chat(messages, schema) -> str` is injectable for tests."""
    lines = page.split("\n")
    starts, pos = [], 0
    for ln in lines:
        starts.append(pos)
        pos += len(ln) + 1

    def _line_of(offset):
        lo, hi = 0, len(starts) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if starts[mid] <= offset:
                lo = mid
            else:
                hi = mid - 1
        return lines[lo].strip()[:160]

    chat = chat or _default_chat()
    out = {}
    for k in range(0, len(cands), _ASK_CHUNK):
        part = cands[k:k + _ASK_CHUNK]
        listing = "\n".join("%d. %s  ||  line: %s" % (
            c["i"], json.dumps(c["text"], ensure_ascii=False), _line_of(c["start"]))
            for c in part)
        said = json.loads(chat([{"role": "system", "content": _ASK_SYS},
                                {"role": "user", "content": "STRINGS:\n" + listing}],
                               _ASK_SCHEMA) or "{}")
        known = {c["i"] for c in part}
        for row in (said.get("texts") or []) if isinstance(said, dict) else []:
            try:
                i, en = int(row.get("i")), str(row.get("en") or "")
            except Exception:
                continue
            if i in known and en.strip() and _worth(en):
                out[i] = en
    return out


def _escape_for(kind, quote, text):
    if kind == "js":
        esc = text.replace("\\", "\\\\").replace("\n", "\\n")
        if quote == "`":
            return esc.replace("`", "\\`").replace("${", "$\\{")
        return esc.replace(quote, "\\" + quote)
    if kind == "attr":
        return _html.escape(text, quote=True)
    return _html.escape(text, quote=False)


def _keep_shape(src, out):
    """The translator drops edge spaces and shouts differently: keep the
    original's leading/trailing spaces, and its ALL-CAPS when it had them."""
    lead = src[:len(src) - len(src.lstrip())]
    trail = src[len(src.rstrip()):]
    body = out.strip()
    letters = [ch for ch in src if ch.isalpha()]
    if letters and all(ch.isupper() for ch in letters):
        body = body.upper()
    return lead + body + trail


def retext(page, meanings=None, translate=None, chat=None, recheck_texts=None):
    """(new page, changes). `translate(en) -> lv` defaults to the chat lane's
    translator; `meanings` / `chat` are injectable for tests. The recheck pass
    runs in the real flow, and in a test only when a stand-in `chat` is given."""
    cands = candidates(page)
    if not cands:
        return page, []
    asked = meanings is None
    meanings = dict(ask_meanings(page, cands, chat=chat) if asked else meanings)
    if translate is None:
        from app import messaging as _M
        translate = _M._nmt_lv
    cache = {}

    def _tr(en):
        key = en.strip()
        if key not in cache:
            try:
                cache[key] = translate(key) or ""
            except Exception:
                LOGGER.exception("page_texts: translate failed for %r", key[:60])
                cache[key] = ""
        return cache[key]

    by_i = {c["i"]: c for c in cands}
    lv_of = {i: _tr(en) for i, en in meanings.items() if i in by_i}
    if recheck_texts is None:
        recheck_texts = asked or chat is not None
    if recheck_texts:
        # two rounds at most; the second reads only what the first reworded --
        # a rewording can land on another slang word ("smashing") the
        # translator keeps in English too (measured)
        pending = {i: (meanings[i].strip(), lv) for i, lv in lv_of.items() if lv.strip()}
        for _round in range(2):
            fixes = recheck([(i, en, lv) for i, (en, lv) in pending.items()], chat=chat)
            pending = {}
            for i, en2 in fixes.items():
                lv2 = _tr(en2)
                if lv2.strip():
                    meanings[i], lv_of[i] = en2, lv2
                    pending[i] = (en2, lv2)
            if not pending:
                break
    changes = []
    for i, lv in lv_of.items():
        c = by_i[i]
        if not lv.strip():
            continue
        lv = _keep_shape(c["text"], lv)
        if lv == c["text"]:
            continue
        changes.append({"i": i, "start": c["start"], "end": c["end"], "kind": c["kind"],
                        "quote": c["quote"], "was": c["text"], "en": meanings[i], "lv": lv})
    out = page
    for ch in sorted(changes, key=lambda x: x["start"], reverse=True):
        out = out[:ch["start"]] + _escape_for(ch["kind"], ch["quote"], ch["lv"]) + out[ch["end"]:]
    return out, [{k: ch[k] for k in ("was", "en", "lv")} for ch in changes]
