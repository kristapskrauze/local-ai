# -*- coding: utf-8 -*-
"""The morning digest picture is ANTON's in chat history, and says what it shows.

No network, no live files: BlueBubbles, inbox and outbox are stand-ins and
the PNG lives in a temp dir. Placeholder handle only.

    python tests/digest_picture_history_cases.py
"""
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app.people import OWNER
from app import messaging as M  # noqa: E402

CHAT = "iMessage;-;+37120000000"
HANDLE = "+37120000000"
FULL = ("Riga old town rooftops at sunrise, autumn, golden leaves, long shadows, "
        "mist, soft overcast, diffuse light, vibrant overhead shot of seven "
        "distinct Latvian home-cooked dishes, including cold beetroot soup, "
        "meatballs with brown gravy, boiled potatoes, pickled cucumbers and rye "
        "bread, atmospheric photography, no people, no text")
T = time.time() - 2 * 3600          # the digest went out two hours ago


def _iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def _att(guid, name="rits.png", mime="image/png"):
    return {"guid": guid, "mimeType": mime, "transferName": name}


ROWS = [
    {"id": "old", "timestamp": _iso(T - 86400 + 3), "status": "sent",
     "target": "iMessage;-;self," + CHAT, "metadata": {"mode": "digest_image"},
     "text": "[digest image: Riga old town rooftops at sunrise, misty]"},
    {"id": "new", "timestamp": _iso(T + 4), "status": "sent",
     "target": "iMessage;-;self," + CHAT,
     "metadata": {"mode": "digest_image",
                  "illustrates": "7 dīvainas ēdienu kombinācijas"},
     "text": "[digest image: %s]" % FULL},
]
BB = [   # BlueBubbles answers newest first
    {"guid": "g3", "text": "Amazing bilde, Anton🤩", "isFromMe": False,
     "dateCreated": int((T + 3600) * 1000), "handle": {"address": HANDLE}},
    {"guid": "g2", "text": "", "isFromMe": True, "dateCreated": int(T * 1000),
     "attachments": [_att("a2")], "handle": {"address": HANDLE}},
    {"guid": "g1", "text": "", "isFromMe": True,
     "dateCreated": int((T - 86400) * 1000), "attachments": [_att("a1")],
     "handle": {"address": HANDLE}},
    {"guid": "g0", "text": "", "isFromMe": True,
     "dateCreated": int((T - 3 * 3600) * 1000),
     "attachments": [_att("a0", "IMG_0001.jpg", "image/jpeg")],
     "handle": {"address": HANDLE}},
]


class _Patched:
    def __init__(self, **names):
        self.names = names
        self.saved = {}

    def __enter__(self):
        names = dict({"_nmt_lv": _fake_lv,
                      "_lv_unknown_words": lambda t, *a, **k: (
                          ["saltibarsciai"] if "saltibarsciai" in str(t) else [])},
                     **self.names)
        for k, v in names.items():
            self.saved[k] = getattr(M, k)
            setattr(M, k, v)
        M._png_prompt_cache.clear()
        M._picture_lv_cache.clear()
        return self

    def __exit__(self, *exc):
        for k, v in self.saved.items():
            setattr(M, k, v)
        M._png_prompt_cache.clear()
        M._picture_lv_cache.clear()


def _fake_lv(text, *a, **k):
    """Stand-in translator: marks what it translated, keeps a foreign word."""
    return "LV[%s] saltibarsciai" % text


def _check(entries):
    by = {e["guid"]: e for e in entries}
    pic = by["g2"]
    assert pic["origin"] == "bot" and pic["mode"] == "digest_image", pic
    assert not pic["attachments"], "a described picture must not also be an invisible photo"
    # described in LATVIAN (the translator's output), never the English prompt
    assert "LV[" in pic["content"] and "cold beetroot soup" in pic["content"], pic["content"]
    assert "saltibarsciai" not in pic["content"], "unknown words are left out"
    assert "ilustrē ziņu «7 dīvainas" in pic["content"], pic["content"]
    assert "atmospheric photography" not in pic["content"], pic["content"]
    assert M._history_author_label({}, pic) == "Antons"
    assert by["g1"]["origin"] == "bot" and "attēlā" not in by["g1"]["content"], by["g1"]
    assert by["g0"]["origin"] == "manual" and by["g0"]["attachments"], \
        "his own photo must stay his"
    assert by["g3"]["origin"] == "contact"
    assert not any("_digest_picture" in e for e in entries)


def test_bb_history_shows_the_picture_as_antons_with_what_it_shows():
    with tempfile.TemporaryDirectory() as d:
        with _Patched(_bb_get=lambda *a, **k: {"ok": True, "body": {"data": BB}},
                      outbox=lambda limit=50: list(ROWS),
                      _STUDIO_DIR=__import__("pathlib").Path(d)):
            entries = M._bb_fetch_history({}, CHAT, 20)
            _check(entries)
            line = M._watcher_transcript({}, {"chat_guid": CHAT, "text": "Amazing bilde, Anton🤩",
                                              "sender": HANDLE}, entries)
    assert "Antons: [rīta ziņu apkopojuma bilde" in line, line
    assert f"{OWNER}: [media" not in line.split("Antons: [rīta ziņu apkopojuma bilde")[1], line


def test_local_history_does_the_same():
    inbox_rows = []
    for m in reversed(BB):
        inbox_rows.append({"chat_guid": CHAT, "message_guid": m["guid"],
                           "text": m.get("text") or "", "is_from_me": m["isFromMe"],
                           "sender": HANDLE, "ts": m["dateCreated"],
                           "attachments": [{"guid": a["guid"], "mime": a["mimeType"],
                                            "name": a["transferName"]}
                                           for a in m.get("attachments") or []]})
    with tempfile.TemporaryDirectory() as d:
        with _Patched(inbox=lambda limit=50: list(inbox_rows),
                      outbox=lambda limit=50: list(ROWS),
                      _STUDIO_DIR=__import__("pathlib").Path(d)):
            _check(M._chat_history_messages(CHAT, 20))


def test_a_row_cut_at_110_characters_reads_the_rest_from_the_png():
    from PIL import Image
    from PIL.PngImagePlugin import PngInfo
    with tempfile.TemporaryDirectory() as d:
        made = T - 4
        name = time.strftime("%Y%m%d-%H%M%S", time.localtime(made)) + "-console_00001_.png"
        info = PngInfo()
        info.add_text("prompt", json.dumps({
            "4": {"class_type": "CLIPTextEncode", "inputs": {"text": FULL + ", high detail"}},
            "5": {"class_type": "CLIPTextEncode",
                  "inputs": {"text": "people, faces, text, watermark"}}}))
        Image.new("RGB", (4, 4)).save(os.path.join(d, name), pnginfo=info)
        row = {"timestamp": _iso(T), "status": "sent", "target": CHAT,
               "metadata": {"mode": "digest_image"},
               "text": "[digest image: %s]" % FULL[:110]}
        with _Patched(_STUDIO_DIR=__import__("pathlib").Path(d)):
            text = M._digest_picture_text(row)
    assert "cold beetroot soup" in text and "high detail" not in text, text


def test_no_translator_means_no_description_never_english():
    row = dict(ROWS[1])
    with tempfile.TemporaryDirectory() as d:
        with _Patched(_nmt_lv=lambda *a, **k: None,
                      _STUDIO_DIR=__import__("pathlib").Path(d)):
            text = M._digest_picture_text(row)
    assert "cold beetroot soup" not in text and "Riga old town" not in text, text
    assert "ilustrē ziņu «7 dīvainas" in text, text


if __name__ == "__main__":
    test_bb_history_shows_the_picture_as_antons_with_what_it_shows()
    test_local_history_does_the_same()
    test_a_row_cut_at_110_characters_reads_the_rest_from_the_png()
    test_no_translator_means_no_description_never_english()
    print("OK")
