# -*- coding: utf-8 -*-
"""These checks pin the replacement: park it, back off, redeliver once, never
duplicate, and give up loudly instead of silently.

Runs entirely on temp files — the live outbox and queue are never touched.

    py -3 tests/send_queue_cases.py
"""
import io
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import messaging as M  # noqa: E402

TARGET = "iMessage;-;+37120000001"
TEXT = "☀️ Labrīt! Otrdiena, 18. augusts"

TRANSPORT_DOWN = {"ok": False, "status_code": -1,
                  "body": "<urlopen error [WinError 10060] A connection attempt failed>"}
TRANSPORT_UP = {"ok": True, "status_code": 200, "body": {"status": 200}}
REJECTED = {"ok": False, "status_code": 500, "body": '{"message":"Message Send Error"}'}


def _rows(path):
    out = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


def main():
    checks = []

    def check(label, passed):
        checks.append((label, bool(passed)))

    tmp = Path(tempfile.mkdtemp(prefix="antons-queue-"))
    real = {"outbox": M.OUTBOX_PATH, "queue": M.SEND_QUEUE_PATH,
            "deliver": M._deliver, "config": M.load_config}
    calls = []
    behaviour = {"result": TRANSPORT_DOWN}

    def fake_deliver(selected, pcfg, target, body, metadata):
        calls.append({"target": target, "text": body,
                      "temp_guid": (metadata or {}).get("_temp_guid"),
                      "idem": (metadata or {}).get("idem")})
        return behaviour["result"]

    try:
        M.OUTBOX_PATH = tmp / "outbox.jsonl"
        M.SEND_QUEUE_PATH = tmp / "send_queue.json"
        M.OUTBOX_PATH.write_text("", encoding="utf-8")
        M._deliver = fake_deliver
        M.load_config = lambda: {
            "default_provider": "bluebubbles_http",
            "providers": {"bluebubbles_http": {
                "enabled": True, "server_url": "http://127.0.0.1:1",
                "password_env": "TEST_ONLY", "send_retries": 1,
                "include_temp_guid": True}},
        }
        os.environ.setdefault("TEST_ONLY", "x")

        # ---- a transport failure parks the message instead of losing it ------
        res = M.send_message(provider="bluebubbles_http", target=TARGET, text=TEXT,
                             metadata={"digest": True, "mode": "digest"},
                             real_send=True)
        check("a transport failure does not raise", isinstance(res, dict))
        check("...it reports queued", res.get("queued") and res.get("status") == "queued")
        queued = json.loads(M.SEND_QUEUE_PATH.read_text(encoding="utf-8"))
        check("...the message is on disk", len(queued) == 1)
        check("...with an idempotency key and a stable tempGuid",
              queued[0].get("idem") and (queued[0].get("metadata") or {}).get("_temp_guid"))
        check("...the outbox row says queued, not error",
              [r for r in _rows(M.OUTBOX_PATH) if r.get("status") == "queued"])
        check("...and keeps its lane",
              (_rows(M.OUTBOX_PATH)[-1].get("metadata") or {}).get("mode") == "digest")
        check("...with a digest-length TTL, not the chat one",
              queued[0]["expires_at"] - queued[0]["first_failed_at"]
              > M.SEND_QUEUE_TTL_SECONDS)

        M.send_message(provider="bluebubbles_http", target=TARGET, text=TEXT,
                       metadata={"digest": True, "mode": "digest"}, real_send=True)
        check("re-sending the same message does not queue a duplicate",
              len(json.loads(M.SEND_QUEUE_PATH.read_text(encoding="utf-8"))) == 1)

        before = len(calls)
        M.drain_send_queue()
        check("a queued message is not retried before its backoff elapses",
              len(calls) == before)

        rows = json.loads(M.SEND_QUEUE_PATH.read_text(encoding="utf-8"))
        original_guid = rows[0]["metadata"]["_temp_guid"]
        rows[0]["next_attempt_at"] = time.time() - 1
        M.SEND_QUEUE_PATH.write_text(json.dumps(rows), encoding="utf-8")
        behaviour["result"] = TRANSPORT_UP
        out = M.drain_send_queue()
        check("the queue redelivers when the transport recovers", out["sent"] == 1)
        check("...reusing the original tempGuid so BlueBubbles can dedupe",
              calls[-1]["temp_guid"] == original_guid)
        check("...the queue is then empty",
              json.loads(M.SEND_QUEUE_PATH.read_text(encoding="utf-8")) == [])
        sent_rows = [r for r in _rows(M.OUTBOX_PATH) if r.get("status") == "sent"]
        check("...and the delivery is recorded as recovered from the queue",
              sent_rows and (sent_rows[-1].get("metadata") or {}).get("recovered_from_queue"))

        behaviour["result"] = TRANSPORT_DOWN
        M.send_message(provider="bluebubbles_http", target=TARGET, text=TEXT,
                       metadata={"digest": True, "mode": "digest"}, real_send=True)
        rows = json.loads(M.SEND_QUEUE_PATH.read_text(encoding="utf-8"))
        rows[0]["next_attempt_at"] = time.time() - 1
        M.SEND_QUEUE_PATH.write_text(json.dumps(rows), encoding="utf-8")
        before = len(calls)
        M.drain_send_queue()
        check("a message already marked sent is dropped, not sent twice",
              len(calls) == before
              and json.loads(M.SEND_QUEUE_PATH.read_text(encoding="utf-8")) == [])

        behaviour["result"] = REJECTED
        raised = False
        try:
            M.send_message(provider="bluebubbles_http", target=TARGET,
                           text="a different message", metadata={"mode": "chat"},
                           real_send=True)
        except M.MessagingError:
            raised = True
        check("an HTTP 500 rejection still raises", raised)
        check("...and is NOT queued (retrying could duplicate a delivered message)",
              json.loads(M.SEND_QUEUE_PATH.read_text(encoding="utf-8")) == [])

        behaviour["result"] = TRANSPORT_DOWN
        M.send_message(provider="bluebubbles_http", target=TARGET, text="stale reply",
                       metadata={"mode": "chat"}, real_send=True)
        rows = json.loads(M.SEND_QUEUE_PATH.read_text(encoding="utf-8"))
        rows[0]["expires_at"] = time.time() - 1
        M.SEND_QUEUE_PATH.write_text(json.dumps(rows), encoding="utf-8")
        out = M.drain_send_queue()
        check("an expired message is dropped from the queue", out["expired"] == 1)
        expired_rows = [r for r in _rows(M.OUTBOX_PATH) if r.get("status") == "expired"]
        check("...and recorded with the original transport error",
              expired_rows and "10060" in str(expired_rows[-1].get("error")))
        check("send_queue_status reports an empty queue at the end",
              M.send_queue_status()["queued"] == 0)
    finally:
        M.OUTBOX_PATH = real["outbox"]
        M.SEND_QUEUE_PATH = real["queue"]
        M._deliver = real["deliver"]
        M.load_config = real["config"]
        shutil.rmtree(tmp, ignore_errors=True)

    ok = True
    for label, passed in checks:
        ok = ok and passed
        print(("OK   " if passed else "FAIL ") + label)
    print("\n%d checks, VERDICT: %s" % (len(checks), "PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
