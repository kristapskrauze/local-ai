# -*- coding: utf-8 -*-
"""Local speech-to-text backend for agent audio attachments.

This is intentionally a lazy CPU-first whisper.cpp wrapper.  It does not keep a
GPU model resident and it does not run unless `transcribe_audio` asks for it.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from app import paths

ROOT = Path(__file__).resolve().parent.parent
STT_ROOT = Path(os.environ.get(
    "ANTONS_STT_WHISPERCPP_ROOT",
    paths.ai("stt", "whispercpp"),
))
WHISPER_EXE = Path(os.environ.get(
    "ANTONS_STT_WHISPER_EXE",
    str(STT_ROOT / "Release" / "whisper-cli.exe"),
))
MODEL_PATH = Path(os.environ.get(
    "ANTONS_STT_MODEL",
    str(STT_ROOT / "models" / "ggml-small.bin"),
))
FFMPEG_EXE = Path(os.environ.get(
    "ANTONS_STT_FFMPEG",
    paths.ai("ffmpeg", "ffmpeg-8.1.2-essentials_build", "bin", "ffmpeg.exe"),
))
CACHE_DIR = ROOT / ".messaging_history" / "stt_cache"

MAX_AUDIO_BYTES = 96 * 1024 * 1024


def available() -> tuple[bool, str]:
    missing = []
    if not WHISPER_EXE.exists():
        missing.append(str(WHISPER_EXE))
    if not MODEL_PATH.exists():
        missing.append(str(MODEL_PATH))
    if not FFMPEG_EXE.exists():
        missing.append(str(FFMPEG_EXE))
    if missing:
        return False, "missing: " + "; ".join(missing)
    return True, "whisper.cpp ready: %s" % MODEL_PATH.name


def health() -> dict:
    ok, reason = available()
    return {
        "available": ok,
        "reason": reason,
        "backend": "whisper.cpp",
        "exe": str(WHISPER_EXE),
        "model": str(MODEL_PATH),
        "ffmpeg": str(FFMPEG_EXE),
        "cache": str(CACHE_DIR),
    }


def transcribe_attachment(att: dict, ctx=None, language: str = "") -> dict:
    ok, reason = available()
    if not ok:
        raise RuntimeError("local STT unavailable: %s" % reason)

    from app import agent_tools

    raw = agent_tools._attachment_bytes(att, ctx, MAX_AUDIO_BYTES)
    media_hash = hashlib.sha256(raw).hexdigest()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = CACHE_DIR / ("%s.json" % media_hash)
    cached = _read_cache(cache_path)
    if cached:
        cached["cache_hit"] = True
        return cached

    name = str((att or {}).get("name") or (att or {}).get("transferName") or "")
    suffix = _suffix_for(name, str((att or {}).get("mime") or (att or {}).get("mimeType") or ""))
    started = time.time()
    with tempfile.TemporaryDirectory(prefix="stt-", dir=str(CACHE_DIR)) as tdir:
        src = Path(tdir) / ("source" + suffix)
        wav = Path(tdir) / "audio.wav"
        out_base = Path(tdir) / "whisper"
        src.write_bytes(raw)
        _run_ffmpeg(src, wav)
        duration = _duration_seconds(wav)
        transcript, detected_language = _run_whisper(
            wav, out_base, language=language)

    if not transcript.strip():
        raise RuntimeError("local STT produced no transcript")
    result = {
        "transcript": transcript[:12000],
        "language": detected_language or _language_out(language),
        "duration_seconds": duration,
        "backend": "whisper.cpp",
        "model": MODEL_PATH.name,
        "cache_key": media_hash[:16],
        "cache_hit": False,
        "seconds": round(time.time() - started, 3),
    }
    _write_cache(cache_path, result)
    return result


def _suffix_for(name: str, mime: str) -> str:
    low = str(name or "").lower()
    for suffix in (".m4a", ".mp3", ".wav", ".caf", ".aac", ".ogg", ".opus", ".flac", ".amr"):
        if low.endswith(suffix):
            return suffix
    m = str(mime or "").lower()
    if "caf" in m:
        return ".caf"
    if "mpeg" in m or "mp3" in m:
        return ".mp3"
    if "wav" in m:
        return ".wav"
    if "ogg" in m or "opus" in m:
        return ".ogg"
    return ".m4a"


def _run_ffmpeg(src: Path, wav: Path) -> None:
    cmd = [
        str(FFMPEG_EXE), "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(src), "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le",
        str(wav),
    ]
    res = subprocess.run(
        cmd, capture_output=True, text=True, timeout=60,
        encoding="utf-8", errors="replace", creationflags=0x08000000,
    )
    if res.returncode != 0 or not wav.exists() or wav.stat().st_size < 1000:
        raise RuntimeError("ffmpeg audio decode failed: %s" % (res.stderr or res.stdout)[:300])


def _run_whisper(wav: Path, out_base: Path, language: str = "") -> tuple[str, str]:
    lang = _language_arg(language)
    threads = _threads()
    cmd = [
        str(WHISPER_EXE),
        "-m", str(MODEL_PATH),
        "-f", str(wav),
        "-l", lang,
        "-nt",
        "-np",
        "-oj",
        "-of", str(out_base),
        "-t", str(threads),
    ]
    res = subprocess.run(
        cmd, capture_output=True, text=True, timeout=180,
        encoding="utf-8", errors="replace", creationflags=0x08000000,
    )
    if res.returncode != 0:
        raise RuntimeError("whisper.cpp failed: %s" % (res.stderr or res.stdout)[:500])
    json_path = Path(str(out_base) + ".json")
    if json_path.exists():
        try:
            data = json.loads(json_path.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            data = {}
        transcript = _text_from_json(data)
        detected = str(data.get("language") or data.get("detected_language") or "").strip()
        if transcript:
            return transcript, detected
    return _clean_stdout(res.stdout), ""


def _text_from_json(data) -> str:
    if not isinstance(data, dict):
        return ""
    texts = []
    for key in ("transcription", "segments"):
        rows = data.get(key)
        if isinstance(rows, list):
            for row in rows:
                if isinstance(row, dict) and str(row.get("text") or "").strip():
                    texts.append(str(row.get("text")).strip())
    if texts:
        return " ".join(texts).strip()
    value = data.get("text") or data.get("result")
    return str(value or "").strip()


def _clean_stdout(text: str) -> str:
    lines = []
    for line in str(text or "").splitlines():
        line = re.sub(r"^\s*\[[^\]]+\]\s*", "", line).strip()
        if line and not line.startswith("whisper_") and not line.startswith("main:"):
            lines.append(line)
    return " ".join(lines).strip()


def _duration_seconds(wav: Path) -> float | None:
    ffprobe = FFMPEG_EXE.with_name("ffprobe.exe")
    if not ffprobe.exists():
        return None
    cmd = [
        str(ffprobe), "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(wav),
    ]
    try:
        res = subprocess.run(
            cmd, capture_output=True, text=True, timeout=10,
            encoding="utf-8", errors="replace", creationflags=0x08000000,
        )
        if res.returncode == 0:
            return round(float((res.stdout or "").strip()), 3)
    except Exception:
        return None
    return None


def _language_arg(language: str) -> str:
    value = str(language or "").strip().lower()
    if value in ("lv", "latvian", "en", "english"):
        return "lv" if value.startswith("lv") or value == "latvian" else "en"
    return "auto"


def _language_out(language: str) -> str:
    lang = _language_arg(language)
    return "" if lang == "auto" else lang


def _threads() -> int:
    try:
        return max(1, min(8, int(os.environ.get("ANTONS_STT_THREADS") or 4)))
    except Exception:
        return 4


def _read_cache(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if isinstance(data, dict) and str(data.get("transcript") or "").strip():
        return data
    return {}


def _write_cache(path: Path, result: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def cleanup_cache_temp() -> int:
    """Best-effort cleanup for abandoned temp dirs; useful for diagnostics."""
    if not CACHE_DIR.exists():
        return 0
    removed = 0
    for child in CACHE_DIR.iterdir():
        if child.is_dir() and child.name.startswith("stt-"):
            try:
                shutil.rmtree(child)
                removed += 1
            except Exception:
                pass
    return removed
