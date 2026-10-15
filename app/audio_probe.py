# -*- coding: utf-8 -*-
"""What is actually in this audio file — from the signal, not from its name.

Deliberately the smallest stack that produces REAL evidence: ffprobe for
container facts, ffmpeg to decode, numpy for the analysis. librosa, essentia,
YAMNet and CLAP are all absent from this machine and every one of them would
be a new dependency, a model download and (for the neural ones) VRAM that the
resident Qwen currently owns. Everything here runs on the CPU in well under a
second for a chat-length clip.

WHAT THIS CAN HONESTLY TELL YOU
    duration, channels, sample rate, codec, container   (ffprobe: ground truth)
    loudness, silence/pause structure, zero-crossing    (measured)
    spectral centroid / flatness / rolloff              (measured, FFT)
    syllabic 2-8 Hz envelope modulation                 (measured — speech)
    beat periodicity and a tempo estimate               (measured — music)
    speech / music / mixed / silence / unknown          (INFERRED from the above)

The classifier is a small set of thresholds over measured features, and it
says so: every verdict carries the numbers that produced it and a confidence,
and it returns "unknown" rather than guessing when the evidence is thin. That
is the point — an honest "I can hear speech over music, roughly 100 BPM" beats
a confident "this is synthwave".
"""
import hashlib
import json
import logging
import os
import subprocess
import wave
from pathlib import Path
from app import paths

LOGGER = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT / ".messaging_history" / "audio_cache"
FFMPEG_EXE = Path(os.environ.get(
    "ANTONS_STT_FFMPEG",
    paths.ai("ffmpeg", "ffmpeg-8.1.2-essentials_build", "bin", "ffmpeg.exe")))
FFPROBE_EXE = Path(os.environ.get(
    "ANTONS_AUDIO_FFPROBE",
    str(Path(FFMPEG_EXE).parent / "ffprobe.exe")))

SR = 16000                 # analysis rate; whisper uses the same
MAX_SECONDS = 600          # analyse at most 10 minutes
FRAME = 512                # 32 ms at 16 kHz
ANALYSIS_VERSION = 2       # bump to invalidate cached analyses

SPEECH = "speech"
MUSIC = "music"
MIXED = "speech_over_music"
SILENCE = "silence"
NOISE = "noise"
UNKNOWN = "unknown"


def available():
    """(ok, why) — the tools this module needs."""
    if not Path(FFMPEG_EXE).exists():
        return False, "ffmpeg missing at %s" % FFMPEG_EXE
    if not Path(FFPROBE_EXE).exists():
        return False, "ffprobe missing at %s" % FFPROBE_EXE
    try:
        import numpy  # noqa: F401
    except Exception as exc:
        return False, "numpy unavailable: %s" % str(exc)[:60]
    return True, "ffmpeg+ffprobe+numpy"


def probe(path):
    """Container facts from ffprobe. These are GROUND TRUTH, not inference."""
    try:
        res = subprocess.run(
            [str(FFPROBE_EXE), "-v", "error", "-print_format", "json",
             "-show_format", "-show_streams", str(path)],
            capture_output=True, text=True, timeout=30,
            encoding="utf-8", errors="replace")
        data = json.loads(res.stdout or "{}")
    except Exception as exc:
        return {"error": "ffprobe failed: %s" % str(exc)[:120]}
    audio = next((s for s in (data.get("streams") or [])
                  if s.get("codec_type") == "audio"), {})
    fmt = data.get("format") or {}

    def _num(value, cast=float):
        try:
            return cast(value)
        except (TypeError, ValueError):
            return None
    return {
        "duration_seconds": _num(fmt.get("duration") or audio.get("duration")),
        "codec": audio.get("codec_name"),
        "container": (fmt.get("format_name") or "").split(",")[0] or None,
        "channels": _num(audio.get("channels"), int),
        "sample_rate": _num(audio.get("sample_rate"), int),
        "bit_rate": _num(fmt.get("bit_rate") or audio.get("bit_rate"), int),
        "size_bytes": _num(fmt.get("size"), int),
    }


def _decode(path, seconds=MAX_SECONDS):
    """Mono 16 kHz float array via ffmpeg, or None."""
    import numpy as np
    tmp = CACHE_DIR / ("decode-%s.wav" % os.getpid())
    tmp.parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            [str(FFMPEG_EXE), "-nostdin", "-y", "-i", str(path),
             "-t", str(int(seconds)), "-ac", "1", "-ar", str(SR),
             "-f", "wav", str(tmp)],
            capture_output=True, timeout=180, check=True)
        with wave.open(str(tmp), "rb") as wav:
            raw = wav.readframes(wav.getnframes())
        data = np.frombuffer(raw, dtype="<i2").astype("float32") / 32768.0
        return data
    except Exception as exc:
        LOGGER.info("audio decode failed: %s", str(exc)[:120])
        return None
    finally:
        try:
            tmp.unlink()
        except Exception:
            pass


def features(samples):
    """Measured properties of the waveform. No interpretation here."""
    import numpy as np
    n = len(samples)
    if n < SR // 4:
        return {"too_short": True, "samples": int(n)}
    frames = n // FRAME
    trimmed = samples[:frames * FRAME].reshape(frames, FRAME)

    rms = np.sqrt((trimmed ** 2).mean(axis=1) + 1e-12)
    peak = float(np.abs(samples).max())
    loud = float(rms.mean())
    # pauses: frames far below this clip's own speaking level. Speech is full
    # of them (between words); continuous music is not.
    thresh = max(loud * 0.25, peak * 0.02)
    quiet = rms < thresh
    silence_ratio = float(quiet.mean())

    # zero crossings: fricatives and cymbals push this up
    zcr = float((np.diff(np.sign(trimmed), axis=1) != 0).mean())

    # spectrum, averaged over frames
    win = np.hanning(FRAME)
    spec = np.abs(np.fft.rfft(trimmed * win, axis=1)) + 1e-12
    freqs = np.fft.rfftfreq(FRAME, 1.0 / SR)
    power = spec.mean(axis=0)
    total = power.sum()
    centroid = float((freqs * power).sum() / total)
    # flatness: 1.0 = noise-like, near 0 = tonal/harmonic
    flatness = float(np.exp(np.log(power).mean()) / (power.mean() + 1e-12))
    cumulative = np.cumsum(power)
    rolloff = float(freqs[int(np.searchsorted(cumulative, 0.85 * total))])

    # HOW MUCH THE LOUDNESS MOVES. Speech swings between syllables and pauses;
    # a sustained chord or steady noise barely moves at all. This is the
    # separator that actually works — the first version measured the SHARE of
    # envelope energy in 2-8 Hz, which is high for any steady signal because a
    # flat envelope spreads evenly across a band that covers most of the
    # available range. Measured on fixtures: pure tones scored 0.5+ on that,
    # the same as real speech.
    env_cov = float(rms.std() / (rms.mean() + 1e-12))

    # WHAT IS UNDERNEATH THE PAUSES. In plain speech the gaps between words
    # are near-silent and noise-like. If something tonal keeps playing through
    # them, there is a bed under the voice — which is the only cheap way to
    # catch speech-over-music, because a voice on top flattens the beat
    # autocorrelation that would otherwise reveal the music.
    order = np.argsort(rms)
    quiet_idx = order[:max(1, len(order) // 5)]          # quietest 20%
    loud_idx = order[-max(1, len(order) // 5):]          # loudest 20%
    quiet_energy_ratio = float(rms[quiet_idx].mean() / (rms[loud_idx].mean() + 1e-12))
    q_spec = np.abs(np.fft.rfft(trimmed[quiet_idx] * win, axis=1)) + 1e-12
    q_power = q_spec.mean(axis=0)
    quiet_flatness = float(np.exp(np.log(q_power).mean()) / (q_power.mean() + 1e-12))
    env = rms - rms.mean()
    env_rate = SR / float(FRAME)                    # ~31 frames/second
    mod_energy, mod_peak_hz = 0.0, None
    if len(env) >= 32:
        env_spec = np.abs(np.fft.rfft(env * np.hanning(len(env))))
        env_freqs = np.fft.rfftfreq(len(env), 1.0 / env_rate)
        band = (env_freqs >= 2.0) & (env_freqs <= 8.0)
        if band.any() and env_spec.mean() > 0:
            # PROMINENCE, not share: how far the strongest syllabic-rate peak
            # stands above the average modulation, in units of that average
            mod_energy = float(env_spec[band].max() / (env_spec.mean() + 1e-12))
            mod_peak_hz = round(float(env_freqs[band][int(np.argmax(env_spec[band]))]), 2)

    # BEAT PERIODICITY. Autocorrelate the onset envelope; a strong peak in the
    # 60-180 BPM lag range is what a rhythm section looks like from here.
    onset = np.diff(rms, prepend=rms[:1]).clip(min=0)
    tempo_bpm, beat_strength = None, 0.0
    if len(onset) >= 64:
        onset = onset - onset.mean()
        corr = np.correlate(onset, onset, mode="full")[len(onset) - 1:]
        corr = corr / (corr[0] + 1e-12)
        lo = max(1, int(env_rate * 60.0 / 180.0))   # 180 BPM
        hi = min(len(corr) - 1, int(env_rate * 60.0 / 60.0))  # 60 BPM
        if hi > lo:
            lag = int(np.argmax(corr[lo:hi]) + lo)
            beat_strength = float(corr[lag])
            tempo_bpm = round(60.0 * env_rate / lag, 1)
    return {
        "too_short": False,
        "loudness_rms": round(loud, 5),
        "peak": round(peak, 4),
        "silence_ratio": round(silence_ratio, 3),
        "zero_crossing_rate": round(zcr, 4),
        "spectral_centroid_hz": round(centroid, 1),
        "spectral_flatness": round(flatness, 4),
        "spectral_rolloff_hz": round(rolloff, 1),
        "envelope_variability": round(env_cov, 3),
        "quiet_energy_ratio": round(quiet_energy_ratio, 3),
        "quiet_flatness": round(quiet_flatness, 4),
        "modulation_peak_prominence": round(mod_energy, 2),
        "modulation_peak_hz": mod_peak_hz,
        "beat_strength": round(beat_strength, 3),
        "tempo_bpm_estimate": tempo_bpm,
    }


def classify(feat):
    """speech / music / mixed / noise / silence / unknown, WITH the numbers.

    Thresholds over measured features, not a trained model — so every verdict
    carries its evidence and a confidence, and returns `unknown` when nothing
    is decisive. Treat it as a strong hint, not an oracle.
    """
    if feat.get("too_short"):
        return {"type": UNKNOWN, "confidence": 0.0,
                "why": ["clip too short to analyse"]}
    loud = feat["loudness_rms"]
    if loud < 0.002:
        return {"type": SILENCE, "confidence": 0.9,
                "why": ["mean RMS %.5f is essentially silence" % loud]}

    cov = feat["envelope_variability"]
    flat = feat["spectral_flatness"]
    beat = feat["beat_strength"]
    pauses = feat["silence_ratio"]
    mod = feat["modulation_peak_prominence"]
    why = []

    # NOISE FIRST. A near-flat spectrum with a steady envelope is hiss, rain,
    # a fan or a room — neither speech nor music, and saying so is more useful
    # than forcing it into one of them.
    # 0.60, not 0.30: real speech measures 0.41-0.48 flatness on this rig, so
    # the first threshold would have called every voice note noise.
    if flat >= 0.60 and cov < 0.45 and beat < 0.35:
        return {"type": NOISE, "confidence": round(min(0.85, flat + 0.3), 2),
                "why": ["flat, noise-like spectrum (flatness %.2f)" % flat,
                        "steady level (variability %.2f)" % cov]}

    speech_score, music_score = 0.0, 0.0
    # The loudness actually moves — syllables and gaps. Measured on this rig:
    # real voice notes 0.33-0.59, sustained music 0.02-0.34. The ranges touch,
    # so a moving envelope only counts as speech when the spectrum is not the
    # near-pure tone of a synthetic pad and no strong beat is present.
    if cov >= 0.33 and flat > 0.10 and beat < 0.45:
        speech_score += 1.3
        why.append("loudness swings like speech (variability %.2f)" % cov)
    elif cov <= 0.30:
        music_score += 1.3
        why.append("sustained, steady level (variability %.2f)" % cov)
    if pauses >= 0.10:
        speech_score += 0.8
        why.append("quiet gaps in %.0f%% of frames" % (pauses * 100))
    elif pauses <= 0.02:
        music_score += 0.6
        why.append("almost no gaps (%.0f%% quiet)" % (pauses * 100))
    if mod >= 2.0 and cov >= 0.33:
        speech_score += 0.7
        why.append("syllabic peak at %.1f Hz (prominence %.1f)"
                   % (feat.get("modulation_peak_hz") or 0.0, mod))
    if beat >= 0.45 and feat["tempo_bpm_estimate"]:
        music_score += 1.2
        why.append("periodic beat, ~%.0f BPM (strength %.2f)"
                   % (feat["tempo_bpm_estimate"], beat))
    if flat <= 0.02:
        music_score += 0.7
        why.append("strongly tonal/harmonic (flatness %.3f)" % flat)

    total = speech_score + music_score
    if total < 1.0:
        return {"type": UNKNOWN, "confidence": 0.2,
                "why": why or ["no feature was decisive"]}
    # A BED UNDER THE VOICE. Speech scores high AND the quiet frames are both
    # loud enough to be something and tonal enough not to be room noise.
    # A BED UNDER THE VOICE, only when the pauses are clearly TONAL. Measured
    # honestly: a real music bed mixed under speech at moderate level leaves
    # quiet-frame flatness around 0.5, indistinguishable from a speech tail,
    # so this fires only on unmistakable evidence and otherwise the result
    # stays `speech` with a flag. Separating a voice from a bed properly needs
    # source separation, which is not installed — claiming MIXED on a hunch
    # would be exactly the overclaiming this tool exists to avoid.
    bed_strong = (feat["quiet_energy_ratio"] >= 0.12
                  and feat["quiet_flatness"] <= 0.25)
    bed_possible = (feat["quiet_energy_ratio"] >= 0.10
                    and feat["quiet_flatness"] <= 0.55)
    if speech_score >= 1.3 and bed_strong:
        why.append("tonal content continues through the pauses "
                   "(quiet frames at %.0f%% of peak, flatness %.3f)"
                   % (feat["quiet_energy_ratio"] * 100, feat["quiet_flatness"]))
        return {"type": MIXED, "confidence": 0.6, "why": why}
    if speech_score >= 1.3 and bed_possible:
        why.append("possible background audio under the voice: quiet frames "
                   "still at %.0f%% of peak (flatness %.2f) — not conclusive"
                   % (feat["quiet_energy_ratio"] * 100, feat["quiet_flatness"]))
        return {"type": SPEECH, "confidence": round(min(0.8, speech_score / 2.8), 2),
                "why": why, "possible_background_audio": True}
    # BOTH readings well supported = voice over a bed. Requires real evidence
    # of each, not merely a middling score.
    if speech_score >= 1.3 and music_score >= 1.3:
        return {"type": MIXED, "confidence": round(min(0.7, total / 5.0), 2),
                "why": why}
    if speech_score > music_score:
        return {"type": SPEECH,
                "confidence": round(min(0.9, speech_score / 2.8), 2), "why": why}
    return {"type": MUSIC,
            "confidence": round(min(0.9, music_score / 2.8), 2), "why": why}


def _cache_key(path):
    h = hashlib.sha256()
    h.update(b"v%d|" % ANALYSIS_VERSION)
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()[:32]


def analyse(path, use_cache=True):
    """Full analysis of one audio file, cached by CONTENT hash.

    Keyed on content, not on attachment id, so the same clip forwarded twice
    is analysed once — and so a re-download with a new id is a cache hit.
    """
    ok, why = available()
    if not ok:
        return {"ok": False, "error": why, "analysis_source": "unavailable"}
    path = Path(path)
    if not path.exists():
        return {"ok": False, "error": "file not found"}
    key = None
    if use_cache:
        try:
            key = _cache_key(path)
            cached = CACHE_DIR / ("%s.json" % key)
            if cached.exists():
                out = json.loads(cached.read_text(encoding="utf-8"))
                out["cache_hit"] = True
                return out
        except Exception:
            key = None
    meta = probe(path)
    samples = _decode(path)
    if samples is None:
        out = {"ok": False, "error": "could not decode audio", **meta,
               "metadata_source": "ffprobe"}
        return out
    feat = features(samples)
    verdict = classify(feat)
    out = {
        "ok": True,
        "type": verdict["type"],
        "type_confidence": verdict["confidence"],
        "why": verdict["why"],
        **meta,
        "features": feat,
        "cache_hit": False,
        "metadata_source": "ffprobe",
        "features_source": "numpy_dsp",
        "classification_source": "numpy_dsp_heuristic",
        "analysis_version": ANALYSIS_VERSION,
    }
    if key:
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            (CACHE_DIR / ("%s.json" % key)).write_text(
                json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception:
            pass
    return out


def content_key(path):
    """Stable content id, shared with the STT cache so both agree."""
    try:
        return _cache_key(path)
    except Exception:
        return ""
