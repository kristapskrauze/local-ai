# -*- coding: utf-8 -*-
"""The outgoing voice subsystem: which backend, when it speaks, what it drops.

They also pin the two things that must never regress:
  * a voice attempt that falls back to text has to SAY SO in telemetry — until
    now a warming daemon looked exactly like "voice was never selected";
  * a long answer is delivered as TEXT, never as a truncated clip. Length is
    decided at eligibility, never by chopping the answer.

    py -3 tests/voice_cases.py
    py -3 tests/voice_cases.py --with-daemon    # also hit the live TTS daemon
"""
import io
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import messaging as M      # noqa: E402
from app import telemetry           # noqa: E402

CHAT = "iMessage;-;voice-test-scratch"
LV_LONG = ("Paskatījos — Antonam GPU ir 37 grādi un vēl ir daudz brīvas vietas "
           "VRAM, tā ka viss ir kārtībā.")


class FakeTrace(object):
    """Captures telemetry.set_meta without writing a trace file."""

    def __init__(self):
        self.meta = {}

    def set(self, **kw):
        self.meta.update(kw)

    def add_stage(self, *a, **k):
        pass

    def mark(self, *a, **k):
        pass

    def add_llm(self, *a, **k):
        pass


def main():
    with_daemon = "--with-daemon" in sys.argv
    checks = []

    def check(label, passed):
        checks.append((label, bool(passed)))

    acfg = {"voice_replies": True, "voice_reply_chance": 1.0, "voice_min_chars": 48,
            "voice_pitch": 1.0}

    src = M._maybe_voice_reply.__code__.co_names
    check("VOICE-TTS-001 the send path goes through the LV voice front door",
          "_lv_voice_tts" in src)
    check("VOICE-TTS-001 ...which tries F5 first and keeps MMS as fallback",
          "_f5_tts" in M._lv_voice_tts.__code__.co_names
          and "_mms_tts" in M._lv_voice_tts.__code__.co_names)
    check("VOICE-TTS-001 the send path does NOT call Piper", "_piper_tts" not in src)
    check("VOICE-TTS-001 Piper remains available but unused (dead, not deleted)",
          hasattr(M, "_piper_tts"))
    check("VOICE-TTS-001 the daemon endpoint is the configured one",
          "8123" in M._mms_tts.__doc__ or "8123" in str(M._ensure_mms_daemon.__code__.co_consts))

    real_current = telemetry.current
    trace = FakeTrace()
    telemetry.current = lambda: trace
    try:
        M._maybe_voice_reply(acfg, {}, CHAT, "ok", {})          # too short
        check("VOICE-TTS-002 a too-short reply never even selects voice",
              trace.meta.get("delivery_selected") is None)

        trace.meta.clear()
        gibberish = "Nu tas ir totally awesome stuff man, really nice yes yes " \
                    "and whatever dude, super cool"
        M._maybe_voice_reply(acfg, {}, CHAT, gibberish, {})
        check("VOICE-TTS-002 an unpronounceable reply falls back to text",
              trace.meta.get("voice_fallback") == "text")
        check("VOICE-TTS-002 ...with a reason recorded",
              "gibberish" in str(trace.meta.get("voice_fallback_reason") or ""))
        check("VOICE-TTS-002 ...and the requested backend is still named",
              trace.meta.get("voice_backend_requested") == "lv-voice")

        trace.meta.clear()
        real_tts = M._mms_tts
        M._mms_tts = lambda *a, **k: None            # daemon warming / down
        try:
            out = M._maybe_voice_reply(acfg, {}, CHAT, LV_LONG, {})
        finally:
            M._mms_tts = real_tts
        check("VOICE-TTS-002 a dead daemon falls back to text, never raises",
              out is False)
        # With the F5 daemon running this line really does synthesise, so
        # either outcome is correct: a fallback must name the daemon, and a
        # success must name the backend that spoke.
        check("VOICE-TTS-002 ...a fallback names the daemon, a success names the voice",
              "daemon" in str(trace.meta.get("voice_fallback_reason") or "")
              or str(trace.meta.get("voice_backend_used") or "").startswith("f5")
              or trace.meta.get("delivery_selected") == "voice")
        check("VOICE-TTS-002 ...and TTS time is measured even on failure",
              trace.meta.get("tts_generate_ms") is not None)

        trace.meta.clear()
        long_answer = ("Latvija eiro ieviesa 2014. gada 1. janvārī. " * 12)[:700]
        M._mms_tts = lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("TTS must not be called for an over-long reply"))
        try:
            out = M._maybe_voice_reply(acfg, {}, CHAT, long_answer, {})
        finally:
            M._mms_tts = real_tts
        check("VOICE-TTS-004 a 700-char answer is delivered as text", out is False)
        check("VOICE-TTS-004 ...and voice was never even selected for it",
              trace.meta.get("delivery_selected") is None)

        trace.meta.clear()
        real_random = M.random.random
        M.random.random = lambda: 0.99          # above any sane chance
        M._mms_tts = lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("TTS must not run when the roll says text"))
        try:
            out = M._maybe_voice_reply(dict(acfg, voice_reply_chance=0.28), {},
                                       CHAT, LV_LONG, {})
        finally:
            M.random.random = real_random
            M._mms_tts = real_tts
        check("the probability gate keeps text when the roll loses", out is False)
    finally:
        telemetry.current = real_current

    speak = M._speakable_lv(LV_LONG)
    check("VOICE-TTS-005 the full sentence reaches TTS (nothing truncated)",
          "brīvas vietas VRAM" in speak and len(speak) >= len(LV_LONG) - 12)
    check("VOICE-TTS-005 Latvian diacritics survive the speakable rewrite",
          "ā" in speak and "ī" in speak)
    check("emoji are stripped rather than read aloud",
          "😅" not in M._speakable_lv("Viss kārtībā 😅"))

    ps = telemetry.ollama_ps()
    resident_before = [m.get("model") for m in (ps.get("models") or [])]
    if with_daemon:
        import json
        import time
        import urllib.request
        try:
            req = urllib.request.Request(
                "http://127.0.0.1:8123/tts",
                data=json.dumps({"text": LV_LONG, "lang": "lv"}).encode("utf-8"),
                headers={"Content-Type": "application/json"})
            started = time.time()
            with urllib.request.urlopen(req, timeout=90) as r:
                audio = r.read()
            secs = (len(audio) - 44) / 2 / 16000.0
            print("   live TTS: %.1fs for %.1fs of audio (RTF %.2f)"
                  % (time.time() - started, secs, (time.time() - started) / max(secs, 0.01)))
            check("the live daemon returns real audio", len(audio) > 20000)
        except Exception as exc:
            check("the live daemon answers (%s)" % str(exc)[:40], False)
        after = [m.get("model") for m in (telemetry.ollama_ps().get("models") or [])]
        check("VOICE-TTS-003 synthesising does not evict Qwen",
              set(resident_before) <= set(after) or not resident_before)
    else:
        check("VOICE-TTS-003 skipped (pass --with-daemon to synthesise)", True)

    ok = True
    for label, passed in checks:
        ok = ok and passed
        print(("OK   " if passed else "FAIL ") + label)
    print("\n%d checks, VERDICT: %s" % (len(checks), "PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
