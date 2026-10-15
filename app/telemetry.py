"""End-to-end latency instrumentation for the reply pipeline.

One TRACE per answered turn. Stages are marked as the pipeline passes them, and
every Ollama call inside the turn is recorded with the server's own performance
counters (load_duration, prompt_eval_*, eval_*), so "the model was still
loading" and "the prompt was enormous" are separable facts rather than theories.

    trace = telemetry.start("chat", chat_guid, t0=earliest_arrival)
    with telemetry.stage("context"):
        ...
    telemetry.finish(status="sent")

Everything is best-effort: instrumentation must never break a reply, so every
public function swallows its own errors. Traces are appended to
`.messaging_history/traces.jsonl`; tests/latency_battery.py reads them.

NOTE none of this stores prompt or reply TEXT — only counts, ids and timings.
"""
import json
import logging
import os
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

LOGGER = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
TRACE_PATH = ROOT / ".messaging_history" / "traces.jsonl"
MAX_TRACE_BYTES = 40 * 1024 * 1024

_local = threading.local()
_write_lock = threading.Lock()


def enabled():
    """On by default; ANTONS_TELEMETRY=0 turns it off without a code change."""
    return str(os.environ.get("ANTONS_TELEMETRY", "1")).strip().lower() not in (
        "0", "false", "off", "no")


class Trace(object):
    def __init__(self, kind, chat_guid="", t0=None, meta=None):
        now = time.time()
        self.id = uuid.uuid4().hex[:12]
        self.kind = str(kind or "chat")
        self.chat_guid = str(chat_guid or "")
        # T0 is when the EVENT arrived, not when we started working on it:
        # the gap between them is the debounce/queue wait, which is exactly
        # the number the old logs could not show.
        self.t0 = float(t0 or now)
        self.started = now
        self.marks = {"T0_event_received": self.t0, "T_work_started": now}
        self.stages = {}
        self.llm = []
        self.meta = dict(meta or {})
        self.status = "open"
        self._stack = []

    def mark(self, name, when=None):
        self.marks[str(name)] = float(when or time.time())

    def add_stage(self, name, seconds, extra=None):
        row = self.stages.setdefault(str(name), {"seconds": 0.0, "calls": 0})
        row["seconds"] = round(row["seconds"] + float(seconds), 4)
        row["calls"] += 1
        if extra:
            row.update(extra)

    def add_llm(self, record):
        self.llm.append(record)

    def set(self, **kwargs):
        self.meta.update(kwargs)

    def answer_seconds(self):
        sent = (self.marks.get("T15_send_acknowledged")
                or self.marks.get("T13_send_started"))
        if not sent:
            return None
        return round(float(sent) - self.t0, 3)

    def payload(self, status=None, ended=None):
        ended = float(ended or time.time())
        llm_total = sum(float(c.get("seconds") or 0) for c in self.llm)
        first_token = next((c.get("ttft_seconds") for c in self.llm
                            if c.get("purpose") not in ("gate", "watcher")
                            and c.get("ttft_seconds")), None)
        answered = self.answer_seconds()
        load_total = sum(float(c.get("load_seconds") or 0) for c in self.llm
                         if float(c.get("load_seconds") or 0) >= 1.0)
        return {
            "trace_id": self.id,
            "kind": self.kind,
            "chat_guid": self.chat_guid,
            "status": str(status or self.status),
            "ts": self.t0,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(self.t0)),
            "e2e_seconds": round(ended - self.t0, 3),
            "work_seconds": round(ended - self.started, 3),
            "wait_before_work_seconds": round(self.started - self.t0, 3),
            "llm_seconds": round(llm_total, 3),
            "llm_calls": len(self.llm),
            "ttft_seconds": first_token,
            "answered": answered is not None,
            "answer_seconds": answered,
            "model_load_seconds": round(load_total, 3),
            "models_used": sorted({str(c.get("model") or "") for c in self.llm}
                                  - {""}),
            "stages": self.stages,
            "marks": {k: round(v - self.t0, 3) for k, v in sorted(
                self.marks.items(), key=lambda kv: kv[1])},
            "llm": self.llm,
            "meta": self.meta,
        }


def current():
    return getattr(_local, "trace", None)


def start(kind, chat_guid="", t0=None, meta=None):
    if not enabled():
        return None
    try:
        trace = Trace(kind, chat_guid, t0=t0, meta=meta)
        _local.trace = trace
        return trace
    except Exception:
        return None


def adopt(trace):
    """Attach an existing trace to THIS thread (generation runs on a worker)."""
    try:
        _local.trace = trace
    except Exception:
        pass


def clear():
    try:
        _local.trace = None
    except Exception:
        pass


def answered():
    trace = current()
    if trace is None:
        return False
    try:
        return bool(trace.marks.get("T15_send_acknowledged")
                    or trace.marks.get("T13_send_started"))
    except Exception:
        return False


def elapsed():
    """Callers put this on the outbox row, so the wait survives next to the
    message itself even if the trace file is rotated or telemetry is off.
    """
    trace = current()
    if trace is None:
        return None
    try:
        return round(time.time() - trace.t0, 1)
    except Exception:
        return None


def mark(name, when=None):
    trace = current()
    if trace is not None:
        try:
            trace.mark(name, when)
        except Exception:
            pass


def set_meta(**kwargs):
    trace = current()
    if trace is not None:
        try:
            trace.set(**kwargs)
        except Exception:
            pass


def add_wait(name, seconds):
    """Record a blocking wait measured elsewhere (a lock, a queue)."""
    trace = current()
    if trace is not None:
        try:
            trace.add_stage(str(name), float(seconds))
        except Exception:
            pass


@contextmanager
def stage(name, **extra):
    """Time one pipeline stage. Safe when no trace is active."""
    started = time.time()
    try:
        yield
    finally:
        trace = current()
        if trace is not None:
            try:
                trace.add_stage(name, time.time() - started, extra or None)
                trace.mark("T_%s_done" % name)
            except Exception:
                pass


def snapshot_ollama(url="http://127.0.0.1:11434", timeout=0.8):
    """Fast /api/ps snapshot for explaining a later load_duration spike."""
    if current() is None:
        return None
    return ollama_ps(url=url, timeout=timeout)


def _norm_model_name(value):
    text = str(value or "").strip()
    if not text:
        return ""
    return text if ":" in text else text + ":latest"


def _int_or_none(value):
    try:
        if value is None or value == "":
            return None
        return int(value)
    except Exception:
        return None


def _models(snapshot):
    if not isinstance(snapshot, dict) or not snapshot.get("ok"):
        return []
    return [m for m in (snapshot.get("models") or []) if isinstance(m, dict)]


def _compact_models(snapshot):
    if not isinstance(snapshot, dict):
        return None
    if not snapshot.get("ok"):
        return {"ok": False, "error": str(snapshot.get("error") or "")[:120]}
    out = []
    for row in _models(snapshot):
        item = {"model": row.get("model") or row.get("name")}
        if row.get("context_length") is not None:
            item["ctx"] = row.get("context_length")
        if row.get("size_vram_gb") is not None:
            item["vram_gb"] = row.get("size_vram_gb")
        if row.get("size_gb") is not None:
            item["size_gb"] = row.get("size_gb")
        out.append(item)
    return out


def _find_model(snapshot, model):
    wanted = _norm_model_name(model)
    for row in _models(snapshot):
        if _norm_model_name(row.get("model") or row.get("name")) == wanted:
            return row
    return None


def _runner_names(snapshot):
    names = []
    for row in _models(snapshot):
        name = str(row.get("model") or row.get("name") or "").strip()
        if not name:
            continue
        ctx = row.get("context_length")
        vram = row.get("size_vram_gb")
        suffix = []
        if ctx:
            suffix.append("ctx=%s" % ctx)
        if vram is not None:
            suffix.append("vram=%.2fGB" % float(vram))
        names.append("%s%s" % (name, " (" + ", ".join(suffix) + ")"
                               if suffix else ""))
    return names


def _model_device(row):
    if not row:
        return "unknown"
    try:
        vram = float(row.get("size_vram_gb") or 0)
        size = float(row.get("size_gb") or 0)
    except Exception:
        return "unknown"
    if vram <= 0.05:
        return "cpu"
    if size and vram + 0.10 < size:
        return "partial_gpu"
    return "gpu"


def _load_reason(model, options, ps_before):
    if not isinstance(ps_before, dict):
        return "no pre-call /api/ps snapshot"
    if not ps_before.get("ok"):
        return "pre-call /api/ps failed: %s" % str(ps_before.get("error") or "")[:80]
    before = _models(ps_before)
    if not before:
        return "no resident runner before call"
    same = _find_model(ps_before, model)
    requested_ctx = _int_or_none((options or {}).get("num_ctx"))
    if same:
        resident_ctx = _int_or_none(same.get("context_length"))
        if requested_ctx and resident_ctx and requested_ctx != resident_ctx:
            return ("same model had different context_length before call "
                    "(resident %s, requested %s)" % (resident_ctx, requested_ctx))
        return "same model was listed before call; Ollama still reloaded it"
    names = _runner_names(ps_before)
    embed_rows = [r for r in before
                  if "embed" in str(r.get("model") or r.get("name") or "").lower()]
    if embed_rows and all(_model_device(r) == "cpu" for r in embed_rows):
        return ("requested model absent; only CPU embedding runner was resident "
                "(not a GPU-offload eviction): %s" % ", ".join(names))
    if embed_rows:
        return "requested model absent; embedding runner used GPU: %s" % ", ".join(names)
    return "requested model absent; resident runners before call: %s" % ", ".join(names)


def record_llm(purpose, model, seconds, body=None, options=None, messages=None,
               error="", ps_before=None, ps_after=None):
    """One Ollama call, with the server's own counters where available.

    Ollama returns nanosecond fields on a non-streamed /api/chat:
      load_duration        — >0 means the model was NOT resident (a reload)
      prompt_eval_count    — input tokens actually evaluated
      prompt_eval_duration — prompt processing time (TTFT floor)
      eval_count/duration  — generated tokens and their time
      done_reason          — "stop" | "length" | ...
    """
    trace = current()
    if trace is None:
        return None
    try:
        body = body if isinstance(body, dict) else {}
        ns = 1e9
        prompt_tokens = body.get("prompt_eval_count")
        out_tokens = body.get("eval_count")
        prompt_secs = float(body.get("prompt_eval_duration") or 0) / ns
        eval_secs = float(body.get("eval_duration") or 0) / ns
        load_secs = float(body.get("load_duration") or 0) / ns
        record = {
            "purpose": str(purpose or "unlabeled"),
            "model": str(model or ""),
            "seconds": round(float(seconds), 3),
            "load_seconds": round(load_secs, 3),
            "model_was_resident": load_secs < 1.0,
            "prompt_tokens": prompt_tokens,
            "output_tokens": out_tokens,
            "prompt_eval_seconds": round(prompt_secs, 3),
            "generation_seconds": round(eval_secs, 3),
            # TTFT = everything before the first generated token: load + queue
            # inside ollama + prompt evaluation.
            "ttft_seconds": round(load_secs + prompt_secs, 3) or None,
            "prompt_tokens_per_sec": (round(prompt_tokens / prompt_secs, 1)
                                      if prompt_tokens and prompt_secs else None),
            "output_tokens_per_sec": (round(out_tokens / eval_secs, 1)
                                      if out_tokens and eval_secs else None),
            "done_reason": str(body.get("done_reason") or ""),
            "num_predict": (options or {}).get("num_predict"),
            "num_ctx": (options or {}).get("num_ctx"),
            "messages": len(messages or []),
            "error": str(error or "")[:200],
        }
        if ps_before is not None:
            record["resident_before"] = _compact_models(ps_before)
        if ps_after is not None:
            record["resident_after"] = _compact_models(ps_after)
        if load_secs >= 1.0:
            record["load_reason"] = _load_reason(model, options, ps_before)
        after_row = _find_model(ps_after, model)
        if after_row:
            record["runner_device"] = _model_device(after_row)
            if after_row.get("context_length") is not None:
                record["runner_context_length"] = after_row.get("context_length")
        # a generation that stopped because it ran out of room is a TRUNCATED
        # answer, not a finished one — flag it here so the report can count it
        if (record["done_reason"] == "length"
                or (record["num_predict"] and out_tokens
                    and int(out_tokens) >= int(record["num_predict"]))):
            record["hit_output_ceiling"] = True
        trace.add_llm(record)
        return record
    except Exception:
        return None


def record_embed(model, seconds, count=0, error="", options=None,
                 ps_before=None, ps_after=None):
    """An /api/embed call. Same trace, same accounting as a chat call."""
    trace = current()
    if trace is None:
        return None
    try:
        record = {
            "purpose": "embed",
            "model": str(model or ""),
            "seconds": round(float(seconds), 3),
            "load_seconds": 0.0,
            "model_was_resident": None,
            "prompt_tokens": None,
            "output_tokens": None,
            "inputs": int(count or 0),
            "error": str(error or "")[:200],
        }
        if options:
            record["embedding_options"] = {
                "num_gpu": options.get("num_gpu"),
            }
        if ps_before is not None:
            record["resident_before"] = _compact_models(ps_before)
        if ps_after is not None:
            record["resident_after"] = _compact_models(ps_after)
        row = _find_model(ps_after, model)
        if row:
            record["embedding_device"] = _model_device(row)
            record["embedding_vram_gb"] = row.get("size_vram_gb")
        if (row
                and record.get("embedding_device") != "cpu"
                and options
                and _int_or_none(options.get("num_gpu")) == 0):
            record["embedding_offload_warning"] = (
                "requested num_gpu=0 but /api/ps did not show a CPU-only runner")
            LOGGER.warning(
                "embedding runner %s requested num_gpu=0 but device=%s vram=%sGB",
                model, record.get("embedding_device"),
                record.get("embedding_vram_gb"))
        trace.add_llm(record)
        return record
    except Exception:
        return None


def finish(status="done", **meta):
    trace = current()
    clear()
    if trace is None:
        return None
    try:
        trace.set(**meta)
        payload = trace.payload(status=status)
        _append(payload)
        _log_line(payload)
        return payload
    except Exception:
        return None


def _log_line(payload):
    """The turn in one line of service.out.log.

    traces.jsonl is complete but nobody reads 2 MB of JSON at 23:00 when a
    reply feels slow. This puts the answer time where the rest of the
    diagnosis already is, in the same words the trace uses.
    """
    try:
        if str(payload.get("kind")) != "chat":
            return
        answered = payload.get("answer_seconds")
        llm = payload.get("llm") or []
        slowest = max(llm, key=lambda c: float(c.get("seconds") or 0), default=None)
        tail = ("slowest %s %.1fs" % (slowest.get("purpose"),
                                      float(slowest.get("seconds") or 0))
                if slowest else "no model calls")
        load = float(payload.get("model_load_seconds") or 0)
        if load >= 1.0:
            # the models that actually LOADED, not every model the turn used
            swapped = sorted({str(c.get("model") or "") for c in llm
                              if float(c.get("load_seconds") or 0) >= 1.0} - {""})
            tail += ", %.0fs of it loading %s into VRAM" % (
                load, "/".join(swapped) or "a model")
            reasons = sorted({str(c.get("load_reason") or "") for c in llm
                              if float(c.get("load_seconds") or 0) >= 1.0
                              and c.get("load_reason")})
            if reasons:
                tail += " (%s)" % "; ".join(reasons)
        if answered is None:
            LOGGER.info("turn %s: no reply sent (%s) after %.1fs — %s",
                        payload.get("trace_id"), payload.get("status"),
                        float(payload.get("e2e_seconds") or 0), tail)
            return
        LOGGER.info(
            "ANSWERED in %.1fs (waited %.1fs for the burst to end, worked "
            "%.1fs, %d model call(s) = %.1fs) — %s",
            answered,
            float(payload.get("wait_before_work_seconds") or 0),
            float(payload.get("work_seconds") or 0),
            int(payload.get("llm_calls") or 0),
            float(payload.get("llm_seconds") or 0),
            tail)
    except Exception:
        pass


def _append(payload):
    try:
        TRACE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _write_lock:
            if TRACE_PATH.exists() and TRACE_PATH.stat().st_size > MAX_TRACE_BYTES:
                TRACE_PATH.rename(TRACE_PATH.with_suffix(".jsonl.1"))
            with TRACE_PATH.open("a", encoding="utf-8", newline="\n") as fh:
                fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception as exc:
        LOGGER.debug("trace not written: %s", exc)


def read(limit=500, path=None):
    rows = []
    try:
        with open(str(path or TRACE_PATH), encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except Exception:
                        pass
    except OSError:
        return []
    return rows[-limit:] if limit else rows


def ollama_ps(url="http://127.0.0.1:11434", timeout=3):
    """What is resident RIGHT NOW, straight from Ollama."""
    import urllib.request
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/api/ps", timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:120], "models": []}
    out = []
    for row in (data.get("models") or []):
        out.append({
            "model": row.get("name") or row.get("model"),
            "size_vram_gb": round(float(row.get("size_vram") or 0) / 1e9, 2),
            "size_gb": round(float(row.get("size") or 0) / 1e9, 2),
            "context_length": row.get("context_length"),
            "expires_at": row.get("expires_at"),
        })
    return {"ok": True, "models": out}


def _answer_seconds(row):
    """This turn's answer time, including rows written before the field existed."""
    value = row.get("answer_seconds")
    if value is not None:
        return float(value)
    if not (row.get("meta") or {}).get("sent_parts"):
        return None                      # nothing was sent: no answer to time
    marks = row.get("marks") or {}       # marks are offsets from T0 already
    for name in ("T15_send_acknowledged", "T13_send_started"):
        if marks.get(name) is not None:
            return float(marks[name])
    return float(row.get("e2e_seconds") or 0)


def _pct(values, p):
    values = sorted(values)
    if not values:
        return None
    return round(values[min(len(values) - 1, int(len(values) * p))], 1)


def status_payload(limit=200):
    """What the last `limit` turns actually cost, answered ones separately."""
    rows = read(limit=limit)
    if not rows:
        return {"ok": True, "traces": 0, "path": str(TRACE_PATH),
                "resident": ollama_ps()}
    answers = [(r, _answer_seconds(r)) for r in rows]
    answered = [(r, v) for r, v in answers if v is not None]
    loads = [c for r in rows for c in (r.get("llm") or [])
             if float(c.get("load_seconds") or 0) >= 1.0]
    slow = {}
    for r, _v in answered:
        for c in (r.get("llm") or []):
            key = str(c.get("purpose") or "?")
            slow[key] = round(slow.get(key, 0.0) + float(c.get("seconds") or 0), 1)
    e2e = [float(r.get("e2e_seconds") or 0) for r in rows]
    return {
        "ok": True,
        "path": str(TRACE_PATH),
        "traces": len(rows),
        "answered_turns": len(answered),
        "silent_turns": len(rows) - len(answered),
        # the number the question actually asks for
        "median_answer_seconds": _pct([v for _r, v in answered], 0.5),
        "p90_answer_seconds": _pct([v for _r, v in answered], 0.9),
        "worst_answer_seconds": _pct([v for _r, v in answered], 1.0),
        "median_e2e_seconds": _pct(e2e, 0.5),
        "p90_e2e_seconds": _pct(e2e, 0.9),
        "median_llm_calls": sorted(int(r.get("llm_calls") or 0)
                                   for r in rows)[len(rows) // 2],
        "reloads": len(loads),
        "reload_seconds": round(sum(float(c.get("load_seconds") or 0)
                                    for c in loads), 1),
        # a swapped model is the single most expensive thing a turn can do
        "reload_models": sorted({str(c.get("model") or "") for c in loads} - {""}),
        "llm_seconds_by_purpose": dict(sorted(slow.items(),
                                              key=lambda kv: -kv[1])[:12]),
        "resident": ollama_ps(),
    }
