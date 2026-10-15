"""Structured Latvian reply validation.

Returns a structured verdict so the caller can repair rather than guess:

    {"pass": bool,
     "scores": {"latvian","relevance","register","persona","coherence"},  # 0-5
     "bad_spans": [...],
     "repair_instruction": "..."}

Hard fails (pass=False regardless of scores): an invented or malformed word,
control characters, truncation, a non-sequitur, a contradiction, or the wrong
register. Any score below 4 also fails.
"""
import json
import os
import re
import unicodedata
from pathlib import Path
from app import paths

try:
    from app import lv_grammar
except Exception:  # pragma: no cover - validator must still load alone
    lv_grammar = None

ROOT = Path(__file__).resolve().parent.parent

_VOCAB_PATH = ROOT / "config" / "lv_vocab.txt"
_WHITELIST_PATH = ROOT / "config" / "lv_whitelist.txt"
_HUNSPELL_DIR = Path(os.environ.get("LV_DICT_DIR", paths.ai("lv_dict")))

_cache = {"vocab": None, "vocab_mtime": 0.0, "white": None, "white_mtime": 0.0,
          "tezaurs": None, "tezaurs_mtime": 0.0,
          "speller": "unset"}

WORD_RE = re.compile(r"[A-Za-zĀČĒĢĪĶĻŅŠŪŽāčēģīķļņšūž]+")

ANY_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)
_LV_DIACRITICS = set("āčēģīķļņšūžĀČĒĢĪĶĻŅŠŪŽ")


def foreign_letters(word):
    """Letters in `word` that are neither ASCII nor Latvian diacritics.

    ASCII stays legal on purpose: film titles, brands and model numbers are
    written in it constantly ("The Waterboy", "Iron Man", "RTX 3090") and
    flagging those would be noise. What this catches is a Latin letter wearing
    the wrong diacritic — ō õ ä ö ü é ñ — which in a Latvian reply is a
    misspelling, not a borrowing.
    """
    return {c for c in str(word or "")
            if c.isalpha() and not c.isascii() and c not in _LV_DIACRITICS}
URL_RE = re.compile(r"https?://\S+|\b\w+\.(lv|com|org|net|io|ai)\b", re.I)
EMOJI_RE = re.compile("[\U0001F300-\U0001FAFF\u2600-\u27BF\U0001F1E6-\U0001F1FF]")

# Texting forms a dictionary will never contain. Kept explicit and small: a
# permissive slang list is how invented words sneak through.
SLANG = {
    "vnk", "moš", "karoč", "nja", "aijjj", "aij", "labiņi", "biku", "blin",
    "ķip", "sviests", "jap", "nop", "oki", "ok", "haha", "hah", "hehe", "btw",
    "nice", "true", "crazy", "yikes", "sorry", "okay", "yeah", "wtf", "lol",
    "api", "build", "check", "commit", "deployoju", "deployo", "deployot",
    "deploys", "fix", "full", "garbage", "merge", "online", "pipeline", "quick",
    "research", "response", "sanity", "sketchy", "hvz", "davai", "bāc", "bļin",
    "frontendā", "backend", "logs",
    "čau", "sveiks", "sveika", "labrīt", "labvakar", "arlabunakti", "mīļā",
    "mīļais", "bučas", "buča", "pff", "pfff", "hmm", "mhm", "aha", "jā", "nē",
}

FORMAL_RE = re.compile(r"\b(jūs|jums|jūsu|jūties|esat|varat|vēlaties|gribat|"
                       r"varētu jūs|lūdzu uzgaidiet|cienījam\w+)\b", re.I)
INFORMAL_RE = re.compile(r"\b(tu|tev|tevi|tavs|tava|tavu|tevis)\b", re.I)

# fragments that answer nothing on their own
STUB_RE = re.compile(r"^\s*(nu\s+kā\s+jau|tā\s+jau|"
                     r"neatceros|nezinu)\s*[.!]?\s*$", re.I)

# Individually valid dictionary words used in a demonstrably wrong collocation.
# The word checker cannot catch these because both forms exist in Latvian.
WRONG_SENSE_RE = re.compile(r"\bes\s+labi\s+guvēju\b", re.I)

TRUNCATED_TAIL = re.compile(
    r"\b(bet|un|ka|kaut|jo|vai|ar|uz|no|par|pie|lai|tad|kad|ja|tik|pēc|"
    r"pirms|līdz|kā|ko|kur)\s*$", re.I)
COMPLETE_SHORT = re.compile(r"^\s*(nu ja|vai ne)\s*[.!?]*\s*$", re.I)


def _load_words(path, cache_key, mtime_key):
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return set()
    if _cache[cache_key] is not None and _cache[mtime_key] == mtime:
        return _cache[cache_key]
    words = set()
    try:
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            w = line.strip().lower()
            if w and not w.startswith("#"):
                words.add(w)
    except Exception:
        pass
    _cache[cache_key] = words
    _cache[mtime_key] = mtime
    return words


def vocab():
    return _load_words(_VOCAB_PATH, "vocab", "vocab_mtime")


# TĒZAURS IS THE LEXICON. The corpus vocab held 143,198 forms; Tēzaurs 2026
# holds 4,553,655 — the speller was deciding what Latvian is from 3% of the
# available lexicon, which is why it rejected ordinary words like "jautrums",
# "avatārs" and "treniņzāle" as unknown.
#
# It is on D: on purpose (66 MB of text, and C: has little room), loaded lazily
# through the same cache as every other list, and its ABSENCE is not an error:
# if the file is missing this returns an empty set and word_known falls back to
# exactly the behaviour it had before. Nothing here should ever hard-fail a
# reply because a dictionary is not installed.
#
# Source: Tēzaurs.lv 2026 Spring Edition, AiLab IMCS UL, CC BY-SA 4.0,
# http://hdl.handle.net/20.500.12574/156
_TEZAURS_PATH = Path(os.environ.get(
    "ANTONS_TEZAURS_WORDFORMS",
    r"D:\antons-data\latvian\tezaurs_2026_02_wordforms.txt"))


def tezaurs():
    return _load_words(_TEZAURS_PATH, "tezaurs", "tezaurs_mtime")


def whitelist():
    return _load_words(_WHITELIST_PATH, "white", "white_mtime")


def speller():
    """Hunspell lv_LV via spylls (pure python). None when unavailable."""
    if _cache["speller"] != "unset":
        return _cache["speller"]
    obj = None
    try:
        from spylls.hunspell import Dictionary
        base = str(_HUNSPELL_DIR / "lv_LV")
        if os.path.exists(base + ".dic") and os.path.exists(base + ".aff"):
            obj = Dictionary.from_files(base)
    except Exception:
        obj = None
    _cache["speller"] = obj
    return obj


def word_known(word, contacts=(), extra=(), sentence_start=False):
    """Morphology check: dictionary, corpus vocab, slang, names, whitelist.

    sentence_start matters: "Vakarsietums" is capitalised only because it opens
    a sentence, and blanket-accepting capitals let that invented word ship.
    """
    lw = word.lower()
    if len(lw) <= 2:
        return True
    if lw in SLANG or lw in whitelist() or lw in {c.lower() for c in contacts}:
        return True
    if lw in {e.lower() for e in extra}:
        return True
    if lw in vocab():
        return True
    if lw in tezaurs():
        return True
    sp = speller()
    if sp is not None:
        try:
            if sp.lookup(word) or sp.lookup(lw) or sp.lookup(word.capitalize()):
                return True
        except Exception:
            pass
    # A capitalised word MID-SENTENCE is a proper noun. At a sentence start
    # capitalisation says nothing, and blanket-accepting it let the invented
    # "Vakarsietums" ship straight through the gate.
    if word[:1].isupper() and not word.isupper() and not sentence_start:
        return True
    return False


_LV_DIACRITIC = "āčēģīķļņšūžĀČĒĢĪĶĻŅŠŪŽ"


def repair_mojibake(text):
    """Correctly-decoded text, or the original when it is not mojibake."""
    s = str(text or "")
    if not s or all(ord(c) < 0x80 for c in s):
        return s
    for codec in ("latin-1", "cp1252"):
        try:
            fixed = s.encode(codec).decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
        if fixed == s or "�" in fixed:
            continue
        if any(c in _LV_DIACRITIC for c in fixed):
            return fixed
    return s


def is_mojibake(text):
    return repair_mojibake(text) != str(text or "")


def check_integrity(reply):
    """Control chars, mojibake, truncation, repeated fragments, broken lines."""
    bad = []
    if not str(reply or "").strip():
        return ["empty reply"]
    for ch in reply:
        if ch in "\n\t":
            continue
        if unicodedata.category(ch) in ("Cc", "Cf", "Co", "Cs"):
            bad.append("control character %r" % ch)
            break
    if "�" in reply:
        bad.append("replacement character (lost bytes)")
    if is_mojibake(reply):
        bad.append("corrupted unicode (mojibake)")
    if re.search(r"\S{40,}", reply):
        bad.append("unbroken 40+ char run")
    stripped = reply.rstrip()
    if TRUNCATED_TAIL.search(stripped) and not COMPLETE_SHORT.match(stripped):
        bad.append("ends mid-thought on a conjunction")
    if stripped.endswith(("…", "...")) and len(stripped) < 60:
        bad.append("truncated with ellipsis")
    # repeated fragment: same 4+ word run twice
    words = WORD_RE.findall(reply.lower())
    for n in (5, 4):
        seen = set()
        for i in range(len(words) - n + 1):
            gram = " ".join(words[i:i + n])
            if gram in seen:
                bad.append("repeated fragment: %s" % gram)
                break
            seen.add(gram)
        if bad and bad[-1].startswith("repeated"):
            break
    if re.search(r"\n{3,}", reply):
        bad.append("damaged line breaks")
    # a word carrying a letter the Latvian alphabet does not have
    for token in ANY_WORD_RE.findall(URL_RE.sub(" ", reply)):
        wrong = foreign_letters(token)
        if wrong:
            bad.append("non-Latvian letter %s in %r"
                       % ("".join(sorted(wrong)), token))
            break
    return bad


def check_words(reply, contacts=(), extra_vocab=()):
    clean = URL_RE.sub(" ", reply)
    clean = EMOJI_RE.sub(" ", clean)
    bad = []
    for mt in WORD_RE.finditer(clean):
        before = clean[:mt.start()].rstrip()
        at_start = (not before) or before[-1] in ".!?\n"
        if not word_known(mt.group(0), contacts, extra_vocab, at_start):
            bad.append(mt.group(0))
    return bad


def check_register(reply, conversation_text=""):
    """Formal jūs/jums is wrong unless the conversation already uses it."""
    convo_formal = bool(FORMAL_RE.search(conversation_text or ""))
    convo_informal = bool(INFORMAL_RE.search(conversation_text or ""))
    hits = FORMAL_RE.findall(reply)
    if hits and convo_informal and not convo_formal:
        return ["formal address to a close contact: %s" % ", ".join(sorted(set(
            h if isinstance(h, str) else h[0] for h in hits))[:3])]
    return []


_QUESTION_RE = re.compile(r"\?|^\s*(cik|kāds|kāda|kā|kas|vai|kur|kad|kāpēc|ko)\b",
                          re.I)


def check_relevance(reply, latest_message):
    """Cheap structural checks. The judge does the semantic work."""
    bad = []
    # The stub may be only the FIRST sentence: "Nu kā jau. Tu?" is still a
    # non-answer with a question bolted on.
    first = re.split(r"[.!?\n]", reply.strip())[0].strip()
    if STUB_RE.match(reply.strip()) or STUB_RE.match(first):
        bad.append("stub reply that answers nothing")
    if (re.match(r"^nu\s+labi(?:,?\s+paldies)?$", first, re.I)
            and re.search(r"čuč|gul|sleep", latest_message or "", re.I)):
        bad.append("stub reply that answers nothing")
    if WRONG_SENSE_RE.search(reply):
        bad.append("wrong word sense: 'guvēju' where 'gulēju' is intended")
    asked = bool(_QUESTION_RE.search(latest_message or ""))
    if asked:
        # answering a question with only a counter-question
        # A reply may both answer AND ask: "Jā, ideāli! Cikos braucam?" is
        # correct. Only flag when nothing before the question mark is an answer.
        stripped = reply.strip()
        answer_part = re.split(r"\?", stripped)[0].strip(" ,.!\n")
        has_answer = bool(re.match(
            r"^(j[aā]|n[eē]|labi|labdien|oki|ok|noteikti|protams|varbūt|moš|"
            r"nezinu|neko|droši|skaidrs|sviests|super|forši|čau|sveiks)\b",
            answer_part, re.I))
        if not has_answer and not WORD_RE.findall(answer_part):
            bad.append("answers a question with only a counter-question")
        elif (not has_answer and stripped.endswith("?")
              and _QUESTION_RE.match(stripped)):
            bad.append("answers a question with only a counter-question")
    return bad


def validate_latvian_reply(reply, latest_message="", conversation_text="",
                           contacts=(), extra_vocab=(), style_examples=(),
                           judge=None, min_score=4):
    """Full verdict. `judge` is an optional callable for semantic scoring."""
    reply = str(reply or "")
    scores = {"latvian": 5, "relevance": 5, "register": 5,
              "persona": 5, "coherence": 5}
    bad_spans, reasons = [], []

    integrity = check_integrity(reply)
    if integrity:
        scores["coherence"] = 0
        reasons.extend(integrity)
        bad_spans.extend(integrity)

    invented = check_words(reply, contacts, extra_vocab)
    if invented:
        scores["latvian"] = 0
        reasons.append("invented/unknown words: %s" % ", ".join(invented[:5]))
        bad_spans.extend(invented)

    reg = check_register(reply, conversation_text)
    if reg:
        scores["register"] = 0
        reasons.extend(reg)
        bad_spans.extend(reg)

    rel = check_relevance(reply, latest_message)
    if rel:
        scores["relevance"] = min(scores["relevance"], 2)
        reasons.extend(rel)
        bad_spans.extend(rel)

    contextual = []
    if lv_grammar is not None:
        try:
            contextual = lv_grammar.contextual_diagnostics(reply)
        except Exception:
            contextual = []
    if contextual:
        scores["latvian"] = min(scores["latvian"], 2)
        reasons.append("contextual grammar: %s" % "; ".join(
            "%s -> %s" % (issue.get("observed"), issue.get("suggestion"))
            for issue in contextual[:4]))
        bad_spans.extend(str(issue.get("observed")) for issue in contextual[:4])

    if judge is not None:
        try:
            jr = judge(reply, latest_message, conversation_text, style_examples)
            for key in ("latvian", "relevance", "register", "persona", "coherence"):
                if key in (jr.get("scores") or {}):
                    scores[key] = min(scores[key], int(jr["scores"][key]))
            if jr.get("reason"):
                reasons.append(str(jr["reason"]))
        except Exception as exc:
            reasons.append("judge unavailable: %s" % exc)

    hard_fail = bool(integrity or invented or reg or rel or contextual)
    low = [k for k, v in scores.items() if v < min_score]
    ok = (not hard_fail) and not low

    instruction = ""
    if not ok:
        parts = []
        if invented:
            parts.append("Neizmanto vārdus, kas nav latviešu valodā: %s."
                         % ", ".join(invented[:4]))
        if reg:
            parts.append("Runā uz 'tu', nevis 'jūs' — tas ir tuvs draugs.")
        if rel:
            parts.append("Vispirms ATBILDI uz pēdējo ziņu, tikai tad jautā pretī.")
        if contextual:
            parts.append("Izlabo locījumu vai saskaņojumu: %s."
                         % "; ".join("%s -> %s" % (
                             issue.get("observed"), issue.get("suggestion"))
                                      for issue in contextual[:3]))
        if integrity:
            parts.append("Uzraksti pilnu, nesabojātu teikumu.")
        if low and not parts:
            parts.append("Uzlabo: %s." % ", ".join(low))
        instruction = " ".join(parts)

    return {"pass": ok, "scores": scores, "bad_spans": bad_spans,
            "reasons": reasons, "repair_instruction": instruction,
            "grammar_issues": contextual}
