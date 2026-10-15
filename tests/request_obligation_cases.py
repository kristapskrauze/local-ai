# -*- coding: utf-8 -*-
"""* a direct question could be suppressed by a generic silence heuristic;
  * a two-message request answered only the second message;
  * a clarification typed during generation read as a manual takeover, and the
    original request disappeared.

State lives in a temp dir — the live obligations store is never touched.

    py -3 tests/request_obligation_cases.py
"""
import io
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.people import PARTNER
from app import messaging as M      # noqa: E402
from app import obligations as O    # noqa: E402

ASSISTANT = "iMessage;-;owner@example.com"
CONTACT = "iMessage;-;+37120000001"
OWNER = "owner@example.com"
PARTNER_ID = "+37120000001"

_seq = [0]


def msg(text, chat=ASSISTANT, from_me=True, sender=None, guid=None):
    _seq[0] += 1
    return {"provider": "bluebubbles", "event_type": "new-message",
            "chat_guid": chat, "sender": sender or (OWNER if from_me else PARTNER_ID),
            "is_from_me": from_me, "text": text,
            "message_guid": guid or ("M%03d" % _seq[0]),
            "ts": time.time(), "attachments": [], "_arrived_at": time.time()}


def acfg():
    return {"enabled": True, "assistant_enabled": True,
            "assistant_chats": [ASSISTANT], "persona_test_chats": [],
            "allowed_senders": [OWNER, PARTNER_ID], "reply_persona": "ai",
            "partner_handles": [PARTNER_ID]}


def reset():
    O._state.clear()
    O._loaded[0] = True


def main():
    checks = []

    def check(label, passed):
        checks.append((label, bool(passed)))

    tmp = Path(tempfile.mkdtemp(prefix="antons-oblig-"))
    real = {"state": O.STATE_PATH, "log": O.LOG_PATH}
    O.STATE_PATH = tmp / "obligations.json"
    O.LOG_PATH = tmp / "obligations.jsonl"
    try:
        reset()
        check("naming Antons is a direct request",
              O.is_direct_request("Anton, pārbaudi kāpēc serveris lēns"))
        check("a rig command is a direct request",
              O.is_direct_request("cik grādi gpu", rig_skill="temps"))
        check("a question in his own assistant chat is a direct request",
              O.is_direct_request("kad Latvija ieviesa eiro?", is_from_me=True,
                                  assistant_chat=True))
        check("two humans chatting is NOT a direct request",
              not O.is_direct_request("Man liekas Latvija eiro ieviesa 2012",
                                      is_from_me=False, assistant_chat=False))
        check("owner answering a contact is NOT a direct request",
              not O.is_direct_request("Ap septiņiem", is_from_me=True,
                                      assistant_chat=False))
        check("a clarification is recognised as a continuation",
              O.looks_like_continuation("un paskaties arī disku")
              and O.looks_like_continuation("paga, domāju Cabrio"))

        reset()
        a = msg("Anton, pārbaudi kad Latvija ieviesa eiro.")
        b = msg("un kad iestājās ES")
        O.open_or_extend(ASSISTANT, a)
        req = O.open_or_extend(ASSISTANT, b)
        check("MSG-BATCH-001 both messages join ONE request",
              req["message_ids"] == [a["message_guid"], b["message_guid"]])
        check("MSG-BATCH-001 both texts are kept",
              "eiro" in " ".join(req["texts"]) and "ES" in " ".join(req["texts"]))
        merged = M._with_open_request_entries(ASSISTANT, [b])
        check("MSG-BATCH-001 the first message is re-attached to a later batch",
              [e["message_guid"] for e in merged]
              == [a["message_guid"], b["message_guid"]])
        batch_text, target_text, _e = M._incoming_turn_details(merged)
        check("MSG-BATCH-001 the burst text carries both asks",
              "eiro" in batch_text and "ES" in batch_text)

        reset()
        m1 = msg("Salīdzini Qwen un Gemma.")
        m2 = msg("tieši uz mana 3090")
        m3 = msg("Latvian arī svarīgs")
        for m in (m1, m2, m3):
            req = O.open_or_extend(ASSISTANT, m)
        check("MSG-BATCH-003 three messages, one request",
              len(req["message_ids"]) == 3 and req["version"] == 3)
        joined = " ".join(O.request_messages(ASSISTANT))
        check("MSG-BATCH-003 hardware + language constraints survive",
              "3090" in joined and "Latvian" in joined and "Gemma" in joined)

        reset()
        first = msg("Pastāsti par Golf 3 1.6 motoru.")
        O.open_or_extend(ASSISTANT, first)
        version = O.begin_generation(ASSISTANT)
        check("MSG-INTERRUPT-001 generation is bound to a version", version == 1)
        check("MSG-INTERRUPT-001 nothing is stale yet",
              not O.is_stale(ASSISTANT, version))
        O.open_or_extend(ASSISTANT, msg("paga, domāju Cabrio."))
        check("MSG-INTERRUPT-001 the running generation is now stale",
              O.is_stale(ASSISTANT, version))
        check("MSG-INTERRUPT-001 the original request message is retained",
              first["message_guid"] in O.current(ASSISTANT)["message_ids"])
        check("MSG-INTERRUPT-001 the cancelled generation is counted",
              O.current(ASSISTANT)["cancelled_generations"] == 1)
        # ...and the stale answer must not be sent
        M._TURN.chat_guid = ASSISTANT
        M._TURN.version = version
        with patch.object(M, "_append_jsonl") as appended:
            out = M._send_reply_parts(acfg(), ASSISTANT, "Golf 3 1.6 ir...",
                                      {"auto_reply": True, "mode": "chat"})
        check("MSG-INTERRUPT-001 the stale reply is NOT sent",
              out.get("skipped") == "superseded")
        check("MSG-INTERRUPT-001 the drop is recorded with its own lane",
              any((c.args[1].get("metadata") or {}).get("mode") == "superseded"
                  for c in appended.call_args_list))
        M._TURN.chat_guid = None
        M._TURN.version = None

        reset()
        O.open_or_extend(ASSISTANT, msg("Atrodi filmu."))
        v1 = O.begin_generation(ASSISTANT)
        O.open_or_extend(ASSISTANT, msg("nē, seriālu"))
        check("MSG-INTERRUPT-003 movie generation is superseded",
              O.is_stale(ASSISTANT, v1))
        check("MSG-INTERRUPT-003 the request is open again, not lost",
              O.current(ASSISTANT)["state"] == O.PENDING
              and len(O.current(ASSISTANT)["message_ids"]) == 2)

        reset()
        check("MSG-YIELD-001 no request is opened by two humans talking",
              O.current(CONTACT) is None)
        yielded = _run_fire(CONTACT, [msg("Cikos tiekamies?", chat=CONTACT,
                                          from_me=False)], owner_replied=True)
        check("MSG-YIELD-001 Antons still yields to a manual answer", yielded)

        reset()
        ask = msg("Anton, pārbaudi serveri.")
        O.open_or_extend(ASSISTANT, ask)
        O.open_or_extend(ASSISTANT, msg("tieši Antonu."))
        yielded = _run_fire(ASSISTANT, [msg("tieši Antonu.")], owner_replied=True)
        check("MSG-YIELD-004 a clarification is NOT a manual takeover", not yielded)
        check("MSG-YIELD-004 the original request is still open",
              ask["message_guid"] in (O.current(ASSISTANT) or {}).get("message_ids", []))

        reset()
        temp_ask = msg("Anton, pārbaudi servera temperatūru.")
        O.open_or_extend(ASSISTANT, temp_ask)
        # a message to ANOTHER human, in another chat, must not touch it
        O.open_or_extend(CONTACT, msg(f"{PARTNER}, es tūlīt būšu.", chat=CONTACT),
                         request_type="PASSIVE")
        still = O.current(ASSISTANT)
        check("a message aimed at a human does not close the Antons request",
              still and temp_ask["message_guid"] in still["message_ids"])

        reset()
        O.open_or_extend(ASSISTANT, msg("Anton, cik GPU grādi?"))
        O.begin_generation(ASSISTANT)
        O.resolve(ASSISTANT, O.ANSWERED, outbox_id="OB1")
        check("an answered request is closed, not left open",
              O.current(ASSISTANT) is None)
        stats = O.completion_stats(path=O.LOG_PATH)
        check("the lifecycle log yields a completion rate",
              stats.get("requests", 0) >= 1 and "completion_rate" in stats)
        reset()
        O.open_or_extend(ASSISTANT, msg("Anton, atrodi restorānu."))
        O.resolve(ASSISTANT, O.SUPERSEDED, reason="owner accepted a human suggestion")
        check("MSG-YIELD-003 a superseded request records WHY, never vanishes",
              any(r.get("state") == O.SUPERSEDED
                  for r in _log_rows(O.LOG_PATH)))
        reset()
        O.open_or_extend(ASSISTANT, msg("Anton, pārbaudi X."))
        req = O.current(ASSISTANT)
        req["created_at"] = req["updated_at"] = time.time() - (O.TTL_SECONDS + 10)
        O._state[ASSISTANT] = req
        O._prune()
        check("an expired request is recorded as FAILED, not forgotten",
              any(r.get("state") == O.FAILED for r in _log_rows(O.LOG_PATH)))
    finally:
        O.STATE_PATH, O.LOG_PATH = real["state"], real["log"]
        O._state.clear()
        O._loaded[0] = False
        M._TURN.chat_guid = None
        M._TURN.version = None
        shutil.rmtree(tmp, ignore_errors=True)

    ok = True
    for label, passed in checks:
        ok = ok and passed
        print(("OK   " if passed else "FAIL ") + label)
    print("\n%d checks, VERDICT: %s" % (len(checks), "PASS" if ok else "FAIL"))
    return 0 if ok else 1


def _log_rows(path):
    import json
    rows = []
    try:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
    except OSError:
        pass
    return rows


def _run_fire(chat_guid, entries, owner_replied=False):
    """Run the yield decision only; returns True when Antons stayed quiet."""
    slot = {"entries": entries}
    with patch.object(M, "load_config", return_value={"auto_reply": acfg(),
                                                      "providers": {}}), \
            patch.object(M, "_owner_replied_after", return_value=owner_replied), \
            patch.object(M, "_append_jsonl"), \
            patch.object(M, "_watcher_history", return_value=[]), \
            patch.object(M, "_semantic_gate_decision", return_value=("", None)), \
            patch.object(M, "_reply_to_batch") as replied:
        M._fire_auto_reply_inner(chat_guid, slot)
    return not replied.called


if __name__ == "__main__":
    raise SystemExit(main())
