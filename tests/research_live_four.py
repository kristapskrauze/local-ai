# -*- coding: utf-8 -*-
"""The four research questions, through the live service. Raw output only.

    py tests\research_live_four.py

Sends nothing: /api/messaging/test-reply generates and discards.
"""
import io
import json
import sys
import time
import urllib.request

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

API = "http://127.0.0.1:8765/api/messaging/test-reply"
STATS = "http://127.0.0.1:8765/api/search/stats"
CHAT = "iMessage;-;+37120000001"
SENDER = "+37120000001"

QUESTIONS = [
    (u"anthem", u"Anton, vai Latvijas himnu laiž caur radio?"),
    (u"schedule", u"Anton, cikos LR9 sākas rīta signāls?"),
    (u"organ", u"Anton, kādu slepenu orgānu zinātnieki atklāja?"),
    (u"lithuania", u"Anton, ko tu ieteiktu apskatīt Lietuvā?"),
]


def post(url, payload, timeout=600):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def searches_done():
    try:
        with urllib.request.urlopen(STATS, timeout=10) as r:
            d = json.loads(r.read().decode("utf-8"))
        return int(d.get("total") or d.get("count") or 0)
    except Exception:
        return -1


def main():
    started = time.time()
    for name, q in QUESTIONS:
        before = searches_done()
        t0 = time.time()
        try:
            res = post(API, {"text": q, "sender": SENDER, "chat_guid": CHAT,
                             "from_me": False})
            reply = (res.get("reply") or "") if isinstance(res, dict) else str(res)
            trace = res.get("trace") if isinstance(res, dict) else None
        except Exception as exc:
            reply, trace = "<<ERROR %s: %s>>" % (type(exc).__name__, str(exc)[:200]), None
        after = searches_done()
        took = time.time() - t0
        print("=" * 72)
        print("[%s]  Q: %s" % (name, q))
        print("-" * 72)
        print(reply or "<EMPTY>")
        print("-" * 72)
        used = (after - before) if (before >= 0 and after >= 0) else -1
        print("%.0fs | %d chars | web searches this question: %s"
              % (took, len(reply or ""), used if used >= 0 else "?"))
        if isinstance(trace, dict):
            for k in ("tools_used", "tool_calls", "lane", "agent"):
                if trace.get(k):
                    print("   %s: %s" % (k, str(trace[k])[:200]))
        print(flush=True)
    print("total %.0fs" % (time.time() - started))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
