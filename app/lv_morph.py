# -*- coding: utf-8 -*-
r"""Latvian agreement, checked against what each word actually is.

The grammar layer before this was 33 written rules plus the chat model judging
its own Latvian, and it scored "Šodien tika labota atbilžu ceļš" as 5/5 with no
issues. `ceļš` is masculine, `labota` is feminine. That error cannot be found by
retrieving grammar text, because agreement is not a fact to look up — it is a
comparison between two words' features, and nothing in the stack knew what the
features were.

UDPipe (Latvian-LVTB) knows:

    labota  VERB Gender=Fem  Number=Sing VerbForm=Part  head=0
    ceļš    NOUN Gender=Masc Number=Sing                 head=3 nsubj:pass

so the check is a comparison, and — the useful half — so is the verification of
a proposed fix. A correction is accepted only when the parser says the mismatch
is gone and no new one appeared.

Nothing here blocks a reply. It reports what disagrees; the model rewrites.
Daemon: AI_HOME\lv_morph\daemon.py on 127.0.0.1:8126.
"""
import json
import logging
import os
import subprocess
import time
import urllib.request
from app import paths

LOGGER = logging.getLogger("app.lv_morph")

URL = "http://127.0.0.1:8126"
PORT = 8126
DAEMON = paths.ai("lv_morph", "daemon.py")
PY = paths.COMFY_PYTHON

_state = {"last_start": 0.0, "down_until": 0.0}

# The predicate agrees with its subject in gender and number; an attribute
# agrees with its noun in gender, number and case as well.
_SUBJECT_RELS = ("nsubj", "nsubj:pass")
_ATTRIBUTE_RELS = ("amod", "det")


def _ensure_daemon():
    """True when the parser is serving. A cold start is detached and this turn
    goes on without it — the same contract as the voice daemons."""
    if time.time() < _state["down_until"]:
        return False
    try:
        with urllib.request.urlopen(URL + "/health", timeout=2) as r:
            h = r.read()
        # "ok": true only means the HTTP server is up. A daemon that cannot
        # import ufal or load the model answers /health 200 with the failure
        # in last_error and never parses anything -- that is DOWN, and it has
        # to be replaced, not left to answer /parse with 500s forever. This
        # was live for ~2 weeks after D:\jods-data was renamed to
        # D:\antons-data and the daemon's hard-coded path went stale.
        broken = (b'"loaded": false' in h and b'"last_error": ""' not in h
                  and (b'ModuleNotFoundError' in h or b'ImportError' in h
                       or b'could not load' in h or b'No such file' in h))
        if b'"ok": true' in h and not broken:
            _state["down_until"] = 0.0
            return True
        if broken:
            _kill_stale_daemon()
    except Exception:
        pass
    _state["down_until"] = time.time() + 30
    if (time.time() - _state["last_start"] > 120
            and os.path.exists(PY) and os.path.exists(DAEMON)):
        _state["last_start"] = time.time()
        try:    # only if none is there -- see app/sidecar_launch.py
            from app import sidecar_launch
            sidecar_launch.start_once(PY, DAEMON, PORT)
        except Exception:
            LOGGER.info("lv morph daemon would not start", exc_info=True)
    return False


def _kill_stale_daemon():
    """Kill whatever is answering on PORT so a fresh daemon can take it.

    The old process is detached (survives a service restart) and may run as
    LocalSystem; this runs inside the service, which is also LocalSystem, so
    it can. Without this a daemon broken at import time is immortal -- nothing
    restarts it because /health still says "ok": true.
    """
    try:
        out = subprocess.run(
            ["netstat", "-ano", "-p", "tcp"], capture_output=True, text=True,
            creationflags=0x08000000).stdout
    except Exception:
        return
    pids = {ln.split()[-1] for ln in out.splitlines()
            if (":%d " % PORT) in ln and "LISTENING" in ln
            and ln.split()[-1].isdigit() and ln.split()[-1] != "0"}
    for pid in pids:
        try:
            subprocess.run(["taskkill", "/F", "/PID", pid],
                           capture_output=True, creationflags=0x08000000)
            LOGGER.info("lv morph: killed stale daemon pid %s on :%d", pid, PORT)
        except Exception:
            pass
    if pids:
        time.sleep(1.0)


def parse(text):
    """Tokens with lemma, upos, feats, head and deprel — or [] if unavailable."""
    body = " ".join(str(text or "").split())
    if not body or not _ensure_daemon():
        return []
    try:
        req = urllib.request.Request(
            URL + "/parse", data=json.dumps({"text": body}).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode("utf-8")).get("tokens") or []
    except Exception as exc:
        LOGGER.info("lv morph parse failed: %s", str(exc)[:120])
        return []


def _by_id(tokens):
    out = {}
    for t in tokens:
        out[(t.get("sent"), t.get("id"))] = t
    return out


def agreement_issues(text, tokens=None):
    """Predicates whose gender disagrees with their subject."""
    tokens = tokens if tokens is not None else parse(text)
    if not tokens:
        return []
    index = _by_id(tokens)
    issues = []
    for tok in tokens:
        if tok.get("deprel") not in _SUBJECT_RELS:
            continue
        head = index.get((tok.get("sent"), tok.get("head")))
        if not head:
            continue
        hf = head.get("feats") or {}
        tf = tok.get("feats") or {}
        if hf.get("VerbForm") != "Part" and head.get("upos") != "ADJ":
            continue
        # a predicate is nominative; a dative adjective is a complement
        # ("tam zaļajam ir panorāmas lūka")
        if hf.get("Case") and hf["Case"] != "Nom":
            continue
        if tf.get("Case") and tf["Case"] != "Nom":
            continue
        after = index.get((tok.get("sent"), (tok.get("id") or 0) + 1))
        if tok.get("id") == 1 and after and after.get("form") == ",":
            continue
        if hf.get("Gender") and tf.get("Gender") and hf["Gender"] != tf["Gender"]:
            issues.append({
                "word": head.get("form"), "lemma": head.get("lemma"),
                "must_agree_with": tok.get("form"),
                "wrong": {"Gender": {"has": hf["Gender"], "expected": tf["Gender"]}},
                "why": "a predicate agrees with its subject in gender",
            })
    return issues


def describe(issues):
    """The issues as one line a model can act on."""
    parts = []
    for it in issues or []:
        wrong = ", ".join("%s should be %s, not %s"
                          % (k.lower(), v["expected"], v["has"])
                          for k, v in (it.get("wrong") or {}).items())
        parts.append("%s (with %s): %s" % (it.get("word"), it.get("must_agree_with"), wrong))
    return "; ".join(parts)
