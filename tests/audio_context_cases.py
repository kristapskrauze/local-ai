# -*- coding: utf-8 -*-
"""Audio understanding, and the context bugs found by runtime evidence.

Audio cases run against REAL files: F5 speech, the mined film VO, and
generated music/noise/silence fixtures under D:\\antons-data\\audio. They skip
cleanly when that drive or ffmpeg is not present, so the suite still runs off
the rig.

    py -3 tests/audio_context_cases.py
"""
import io
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.people import PARTNER, PARTNER_DAT  # noqa: E402

FIX = r"D:/antons-data/audio/fixtures/"
GEN = r"D:/antons-data/audio/generated/"
REF = r"D:/antons-data/audio/references/how_high_lv_vo/candidates_v2/listen/"


def main():
    checks = []

    def check(label, passed):
        checks.append((label, bool(passed)))

    from app import audio_probe as A

    ok, why = A.available()
    have_fixtures = ok and os.path.isdir(FIX)

    check("AUD-000 the backend reports its own availability honestly",
          isinstance(ok, bool) and isinstance(why, str) and why)

    if not have_fixtures:
        check("AUD-* skipped (no ffmpeg or no D: fixtures: %s)" % why, True)
    else:
        labelled = [
            (GEN + "f5_cand_002_002257.wav", "speech"),
            (REF + "cand_002.wav", "speech"),
            (REF + "cand_004.wav", "speech"),
            (FIX + "music_beat.wav", "music"),
            (FIX + "music_pad.wav", "music"),
            (FIX + "noise.wav", "noise"),
            (FIX + "silence.wav", "silence"),
        ]
        present = [(p, w) for p, w in labelled if os.path.exists(p)]
        hits = 0
        for path, want in present:
            got = A.analyse(path, use_cache=False)
            if got.get("type") == want:
                hits += 1
            else:
                print("     AUD miss: %s want=%s got=%s"
                      % (os.path.basename(path), want, got.get("type")))
        check("AUD-001 speech/music/noise/silence from the signal (%d/%d)"
              % (hits, len(present)), present and hits == len(present))

        one = A.analyse(present[0][0], use_cache=False)
        check("AUD-002 the verdict carries the numbers that produced it",
              bool(one.get("why")) and isinstance(one["why"], list))
        check("AUD-003 container facts come from ffprobe, not guesswork",
              one.get("metadata_source") == "ffprobe"
              and (one.get("duration_seconds") or 0) > 0)
        check("AUD-004 provenance is explicit for every layer",
              one.get("features_source") and one.get("classification_source"))
        feats = one.get("features") or {}
        check("AUD-005 the measured features are actually present",
              all(k in feats for k in ("loudness_rms", "silence_ratio",
                                       "spectral_flatness",
                                       "envelope_variability")))
        # a second identical call must be served from the content cache
        A.analyse(present[0][0], use_cache=True)
        again = A.analyse(present[0][0], use_cache=True)
        check("AUD-006 repeat analysis is cached by content hash",
              bool(again.get("cache_hit")))
        check("AUD-007 the cache key is content, not path",
              len(A.content_key(present[0][0])) == 32)

        music = A.analyse(FIX + "music_beat.wav", use_cache=False)
        check("AUD-008 music gets a tempo estimate from real autocorrelation",
              (music.get("features") or {}).get("tempo_bpm_estimate"))
        check("AUD-009 nothing claims a song title, artist or genre",
              not any(k in music for k in ("title", "artist", "genre", "song")))

        missing = A.analyse(FIX + "does_not_exist.wav")
        check("AUD-010 a missing file fails truthfully, not silently",
              missing.get("ok") is False and missing.get("error"))

    from app import personal_assistant as PA
    strip = PA._current_action_text
    kept = strip(f"I'm going to be late - tell {PARTNER} I'll pick her up at 8")
    check("QUOTE-001 an apostrophe does not delete the verb",
          "tell" in kept)
    check("QUOTE-001 ...nor the recipient", PARTNER in kept)
    kept2 = strip(f"don't forget, let's tell {PARTNER} 19:00")
    check("QUOTE-002 two contractions leave the sentence intact",
          "tell" in kept2 and PARTNER in kept2 and "19:00" in kept2)
    dropped = strip(f'he said "forward this to {PARTNER}" earlier')
    check("QUOTE-003 a genuine double-quoted span is still removed",
          "forward this" not in dropped)
    dropped2 = strip("she wrote \u201csend it to K\u0101rlis\u201d yesterday")
    check("QUOTE-004 a curly-quoted span is still removed",
          "send it" not in dropped2)
    lv = strip(f"Anton, pasaki {PARTNER_DAT} ka b\u016b\u0161u 19:00")
    check("QUOTE-005 Latvian without quotes is untouched",
          "pasaki" in lv and PARTNER_DAT in lv)

    from app import agent_tools as T
    src = io.open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "app", "agent_tools.py"),
        encoding="utf-8").read()
    check("ATTR-001 chat-history rows carry a speaker label",
          '"speaker": speaker,' in src and 'speaker = "contact"' in src
          and 'speaker = "owner"' in src)
    check("ATTR-002 ...and the transport's own direction flag",
          '"is_from_me": from_me or source == "outbox"' in src)
    check("ATTR-003 outbox rows are attributed to the assistant",
          'source == "outbox"' in src)

    reg = T.default_registry()
    by_name = {t.name: t for t in reg.all()}
    check("TOOL-001 inspect_audio is registered", "inspect_audio" in by_name)
    check("TOOL-002 transcribe_audio still exists and stays narrow",
          "transcribe_audio" in by_name)
    if "inspect_audio" in by_name:
        desc = by_name["inspect_audio"].description
        check("TOOL-003 its description says it cannot identify a song",
              "cannot identify a song" in desc)
        check("TOOL-004 ...and points at transcribe_audio for words only",
              "transcribe_audio" in desc)

    ok_all = True
    for label, passed in checks:
        ok_all = ok_all and passed
        print(("OK   " if passed else "FAIL ") + label)
    print("\n%d checks, VERDICT: %s" % (len(checks), "PASS" if ok_all else "FAIL"))
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
