# -*- coding: utf-8 -*-
"""How long a message waits before Antons answers it.

Offline. No timers, no model, no network.

    py -3 tests/debounce_cases.py
"""
import io
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import messaging as M  # noqa: E402


def main():
    checks = []

    def check(label, passed):
        checks.append((label, bool(passed)))

    live = dict(M.DEFAULT_CONFIG["auto_reply"])

    # ---- DEB-001: a message aimed at Antons does not wait out the burst window
    direct = M._debounce_delay(live, True, 0.0)
    passive = M._debounce_delay(live, False, 0.0)
    check("DEB-001 a direct request waits seconds, not the burst window",
          0 < direct <= 4.0)
    check("DEB-001 passive watching waits a burst, not a conversation",
          5.0 <= passive <= 12.0)
    check("DEB-001 ...and the two are not the same number",
          direct < passive)

    # ---- DEB-002: the batch is answered at the cap, however it continues ----
    check("DEB-002 a direct batch still open at the cap fires now",
          M._debounce_delay(live, True, M.DIRECT_BATCH_CAP_SECONDS) == 0.0)
    check("DEB-002 ...and one second before the cap still waits",
          M._debounce_delay(live, True, M.DIRECT_BATCH_CAP_SECONDS - 1) == direct)
    check("DEB-002 a passive batch has its own, longer cap",
          M._debounce_delay(live, False, M.BATCH_CAP_SECONDS) == 0.0
          and M.BATCH_CAP_SECONDS > M.DIRECT_BATCH_CAP_SECONDS)

    # ---- DEB-003: the caps are inside what the corpus actually shows --------
    # 97% of direct-address messages have no same-author follow-up within 4 s,
    # so a cap far above that is answering messages nobody is still typing.
    check("DEB-003 the direct cap is a burst, not a conversation",
          4.0 <= M.DIRECT_BATCH_CAP_SECONDS <= 20.0)
    check("DEB-003 the direct delay is under the fastest realistic follow-up",
          M.DIRECT_REPLY_DELAY_SECONDS <= 4.0)

    tuned = dict(live, direct_reply_delay_seconds=1.0,
                 direct_batch_cap_seconds=6.0)
    check("DEB-004 a configured direct delay is honoured",
          M._debounce_delay(tuned, True, 0.0) == 1.0)
    check("DEB-004 a configured cap is honoured",
          M._debounce_delay(tuned, True, 6.0) == 0.0)
    check("DEB-004 zero delay stays zero",
          M._debounce_delay(dict(live, direct_reply_delay_seconds=0), True, 0.0) == 0.0)

    silly = dict(live, direct_reply_delay_seconds=30.0, direct_batch_cap_seconds=1.0)
    check("DEB-005 a cap below the delay does not make every reply instant",
          M._debounce_delay(silly, True, 0.0) == 30.0)

    # ---- DEB-006: the scheduler uses this function, not its own copy --------
    consts = " ".join(str(c) for c in M._schedule_auto_reply.__code__.co_names)
    check("DEB-006 the scheduler asks _debounce_delay",
          "_debounce_delay" in consts)

    ok = True
    for label, passed in checks:
        ok = ok and passed
        print(("OK   " if passed else "FAIL ") + label)
    print("\n%d checks, VERDICT: %s" % (len(checks), "PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
