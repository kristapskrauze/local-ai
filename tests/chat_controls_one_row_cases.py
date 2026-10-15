"""The dashboard's chat controls show one row per person.

    python tests/chat_controls_one_row_cases.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import messaging as M  # noqa: E402

CFG = {"assistant_chats": ["iMessage;-;owner@example.com"],
       "partner_handles": ["+37120000001"],
       "contact_names": {"+37120000001": "Partnere", "+37120000002": "Draugs",
                         "draugs@example.com": "Draugs", "+37120000003": "Cits"}}


def test_two_handles_of_one_person_are_one_row():
    rows = M.chat_controls_summary(CFG)
    names = [r["name"] for r in rows]
    assert names.count("Draugs") == 1, names
    row = next(r for r in rows if r["name"] == "Draugs")
    assert row["handles"] == ["+37120000002", "draugs@example.com"], row
    assert names.count("Cits") == 1 and names.count("Partnere") == 1, names


if __name__ == "__main__":
    test_two_handles_of_one_person_are_one_row()
    print("OK")
