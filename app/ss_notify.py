# -*- coding: utf-8 -*-
import logging
import threading
import time

LOGGER = logging.getLogger(__name__)

INTERVAL = 900          # 15 minutes: classifieds move in hours, not seconds
MAX_PER_SWEEP = 3       # never turn a quiet morning into a wall of messages


def _line(item, kind):
    if kind == "cars":
        bits = [item.get("title") or "", item.get("year") or "",
                item.get("engine") or "", item.get("price_text") or ""]
    else:
        bits = [item.get("title") or "", (item.get("rooms") or "") + " ist.",
                (item.get("area_m2") or "") + " m2", item.get("price_text") or ""]
    head = " · ".join(b for b in bits if str(b).strip() not in ("", "ist.", "m2"))
    return "• %s\n%s" % (head[:110], item.get("url") or "")


def _send(chat_guid, sender, text):
    """send_message takes provider/target/text, not (chat, body); passing them
    positionally would file the chat guid as the provider and nothing would
    arrive."""
    from app import messaging as M
    acfg = (M.load_config().get("auto_reply") or {})
    return M.send_message(
        provider=str(acfg.get("provider") or "bluebubbles_http"),
        target=chat_guid, text=text,
        metadata={"auto_reply": True, "mode": "ss_watch", "sender": sender},
        real_send=True)


def sweep(send=None):
    """One pass. Returns how many messages were sent."""
    from app import ss_search

    if send is None:
        send = _send
    sent = 0
    try:
        fresh = ss_search.watch_check()
    except Exception:
        LOGGER.exception("ss watch sweep failed")
        return 0
    for watch, items in fresh:
        chat = watch.get("chat_guid")
        if not chat or not items:
            continue
        kind = (watch.get("query") or {}).get("kind") or "flats"
        label = watch.get("label") or "sludinājumi"
        shown = items[:MAX_PER_SWEEP]
        more = len(items) - len(shown)
        body = ("Jauns sludinājums — %s:\n%s" if len(shown) == 1
                else "Jauni sludinājumi — %s:\n%s") % (
            label, "\n".join(_line(i, kind) for i in shown))
        if more > 0:
            body += "\nUn vēl %d: %s" % (more, watch.get("url") or "")
        try:
            send(chat, watch.get("sender") or "", body)
            sent += 1
        except Exception as exc:
            LOGGER.warning("ss_notify send failed: %s", str(exc)[:120])
    return sent


def loop(interval=INTERVAL):
    """Background sweep. Never raises out; a bad pass just waits for the next."""
    def _run():
        while True:
            try:
                sweep()
            except Exception:
                LOGGER.exception("ss_notify loop error")
            time.sleep(max(120, int(interval)))

    thread = threading.Thread(target=_run, name="ss-notify", daemon=True)
    thread.start()
    return thread
