"""A fix to a page the chat already has does not send both links again.

    python tests/build_report_no_repeat_cases.py
"""
import os
import sys
import time
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app.people import PARTNER_DAT
from app import agent_tools as A  # noqa: E402

CHAT = "iMessage;-;+37120000000"


def _row(age_s, chat=CHAT, slug="game-x", text=f"Atjaunināts: game-x.html. Tev: http://a {PARTNER_DAT}: http://b",
         status="sent", kind="antons_build_report"):
    return {"target": chat, "status": status, "text": text,
            "timestamp": datetime.fromtimestamp(time.time() - age_s).astimezone().isoformat(),
            "metadata": {"kind": kind, "slug": slug}}


def _check(rows):
    saved = A._M_outbox
    A._M_outbox = lambda limit: rows
    try:
        return A._links_sent_recently(CHAT, "game-x")
    finally:
        A._M_outbox = saved


def test_links_sent_minutes_ago_are_not_repeated():
    assert _check([_row(240)]) is True


def test_old_links_other_chats_other_pages_and_failures_do_not_count():
    assert _check([_row(30 * 3600)]) is False                       # a day and more ago
    assert _check([_row(240, chat="iMessage;-;+37129999999")]) is False
    assert _check([_row(240, slug="other-game")]) is False
    assert _check([_row(240, text="Pagaidām nesanāca strādājoša versija (game-x).")]) is False
    assert _check([_row(240, status="failed")]) is False


if __name__ == "__main__":
    test_links_sent_minutes_ago_are_not_repeated()
    test_old_links_other_chats_other_pages_and_failures_do_not_count()
    print("OK")
