# -*- coding: utf-8 -*-
"""Outgoing Latvian language engine for detection, repair, and verification."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import time
from typing import Callable

from app import lv_grammar, lv_validate


ROOT = Path(__file__).resolve().parent.parent
LOG_PATH = ROOT / ".messaging_history" / "lv_corrections.jsonl"


def analyze_reply(reply: str, latest_message: str = "",
                  conversation_text: str = "", contacts=(),
                  extra_vocab=(), style_examples=(), judge=None) -> dict:
    """Return lexical/register/relevance verdict plus contextual grammar issues."""
    text = str(reply or "")
    verdict = lv_validate.validate_latvian_reply(
        text,
        latest_message=latest_message,
        conversation_text=conversation_text,
        contacts=contacts,
        extra_vocab=extra_vocab,
        style_examples=style_examples,
        judge=judge,
    )
    contextual = lv_grammar.contextual_diagnostics(text)
    scores = dict(verdict.get("scores") or {})
    if contextual:
        scores["latvian"] = min(int(scores.get("latvian", 5)), 2)
    return {
        "pass": bool(verdict.get("pass")) and not contextual,
        "scores": scores,
        "verdict": verdict,
        "contextual_issues": contextual,
        "needs_edit": bool(contextual),
        "coverage": lv_grammar.coverage_summary(),
    }


def _unknown_words(text: str, contacts=(), extra_vocab=()) -> set[str]:
    try:
        return set(lv_validate.check_words(text, contacts=contacts,
                                           extra_vocab=extra_vocab))
    except Exception:
        return set()


def _words(text: str) -> set[str]:
    return {w.lower() for w in lv_validate.WORD_RE.findall(str(text or ""))}


def _rule_suggestion_vocab(applied: list[dict]) -> set[str]:
    out = set()
    for issue in applied or []:
        out |= _words(issue.get("suggestion") or "")
    return out


def _integrity_errors(text: str) -> list[str]:
    try:
        return list(lv_validate.check_integrity(text))
    except Exception as exc:
        return ["integrity check unavailable: %s" % exc]


def _too_different(original: str, candidate: str) -> bool:
    if not candidate.strip():
        return True
    if original.count("\n") != candidate.count("\n"):
        return True
    max_growth = max(80, int(len(original) * 0.75))
    return len(candidate) > len(original) + max_growth


def _verified_improvement(original: str, candidate: str, before: dict,
                          after: dict, contacts=(), extra_vocab=()) -> tuple[bool, str]:
    if candidate == original:
        return False, "unchanged"
    before_issues = before.get("contextual_issues") or []
    after_issues = after.get("contextual_issues") or []
    if len(after_issues) >= len(before_issues):
        return False, "contextual issues did not decrease"
    if _too_different(original, candidate):
        return False, "candidate changed structure too much"
    before_unknown = _unknown_words(original, contacts=contacts,
                                    extra_vocab=extra_vocab)
    after_unknown = _unknown_words(candidate, contacts=contacts,
                                   extra_vocab=extra_vocab)
    new_unknown = sorted(after_unknown - before_unknown)
    if new_unknown:
        return False, "candidate introduced unknown words: %s" % ", ".join(new_unknown[:4])
    integrity = _integrity_errors(candidate)
    if integrity:
        return False, "candidate failed integrity: %s" % "; ".join(integrity[:2])
    second_pass, _second_issues = lv_grammar.apply_deterministic_fixes(candidate)
    if second_pass != candidate:
        return False, "candidate was not stable under a second deterministic pass"
    return True, "accepted"


def _write_log(record: dict) -> None:
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    except Exception:
        pass


def edit_reply(reply: str, latest_message: str = "", conversation_text: str = "",
               contacts=(), extra_vocab=(), style_examples=(), judge=None,
               editor: Callable | None = None, log: bool = True) -> dict:
    """Fix high-confidence contextual issues, then verify no regression."""
    started = time.perf_counter()
    original = str(reply or "")
    before = analyze_reply(
        original,
        latest_message=latest_message,
        conversation_text=conversation_text,
        contacts=contacts,
        extra_vocab=extra_vocab,
        style_examples=style_examples,
        judge=judge,
    )
    candidate, applied = lv_grammar.apply_deterministic_fixes(original)
    verified_extra_vocab = tuple(extra_vocab or ()) + tuple(
        sorted(_rule_suggestion_vocab(applied)))

    remaining = lv_grammar.contextual_diagnostics(candidate)
    editor_used = False
    if remaining and callable(editor):
        try:
            edited = editor(candidate, remaining, {
                "latest_message": latest_message,
                "conversation_text": conversation_text,
                "contacts": contacts,
                "extra_vocab": verified_extra_vocab,
                "style_examples": style_examples,
            })
            if isinstance(edited, str) and edited.strip():
                candidate = edited.strip()
                editor_used = True
        except Exception:
            pass

    after = analyze_reply(
        candidate,
        latest_message=latest_message,
        conversation_text=conversation_text,
        contacts=contacts,
        extra_vocab=verified_extra_vocab,
        style_examples=style_examples,
        judge=judge,
    )
    accepted, reason = _verified_improvement(
        original, candidate, before, after, contacts=contacts,
        extra_vocab=verified_extra_vocab)
    final = candidate if accepted else original
    latency_ms = int((time.perf_counter() - started) * 1000)
    verification = {
        "contextual_before": len(before.get("contextual_issues") or []),
        "contextual_after": len(after.get("contextual_issues") or []),
        "unknown_before": sorted(_unknown_words(original, contacts=contacts,
                                                extra_vocab=extra_vocab)),
        "unknown_after": sorted(_unknown_words(candidate, contacts=contacts,
                                               extra_vocab=verified_extra_vocab)),
        "integrity_after": _integrity_errors(candidate),
        "stable_second_pass": lv_grammar.apply_deterministic_fixes(candidate)[0] == candidate,
        "rule_suggestion_vocab": sorted(_rule_suggestion_vocab(applied)),
    }
    result = {
        "original": original,
        "corrected": candidate,
        "final": final,
        "accepted": accepted,
        "reason": reason,
        "latency_ms": latency_ms,
        "before": before,
        "after": after,
        "rules_used": lv_grammar.rule_ids(applied),
        "editor_used": editor_used,
        "verification": verification,
        "log_path": str(LOG_PATH),
    }
    if log and (original != candidate or before.get("contextual_issues")):
        _write_log({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "original": original,
            "corrected": candidate,
            "final": final,
            "changed": original != candidate,
            "accepted": accepted,
            "reason": reason,
            "latency_ms": latency_ms,
            "rules_triggered": result["rules_used"],
            "rules_used": result["rules_used"],
            "editor_used": editor_used,
            "issues": before.get("contextual_issues") or [],
            "before_issues": before.get("contextual_issues") or [],
            "after_issues": after.get("contextual_issues") or [],
            "verification": verification,
        })
    return result


def correction_frequency(log_path: Path | str = LOG_PATH) -> dict:
    """Aggregate local correction audit logs by category and rule."""
    path = Path(log_path)
    counts = {
        "total_records": 0,
        "accepted": 0,
        "rejected": 0,
        "by_category": {},
        "by_rule": {},
        "avg_latency_ms": 0.0,
    }
    if not path.exists():
        return counts
    category_counter = {}
    rule_counter = {}
    latencies = []
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except Exception:
        return counts
    for line in lines:
        try:
            rec = json.loads(line)
        except Exception:
            continue
        counts["total_records"] += 1
        if rec.get("accepted"):
            counts["accepted"] += 1
        else:
            counts["rejected"] += 1
        try:
            latencies.append(float(rec.get("latency_ms", 0)))
        except Exception:
            pass
        for issue in rec.get("issues") or rec.get("before_issues") or []:
            cat = str(issue.get("category") or "other")
            category_counter[cat] = category_counter.get(cat, 0) + 1
            rid = str(issue.get("rule_id") or "unknown")
            rule_counter[rid] = rule_counter.get(rid, 0) + 1
        for rid in rec.get("rules_triggered") or rec.get("rules_used") or []:
            rid = str(rid)
            rule_counter[rid] = rule_counter.get(rid, 0) + 1
    counts["by_category"] = dict(sorted(category_counter.items(),
                                        key=lambda kv: (-kv[1], kv[0])))
    counts["by_rule"] = dict(sorted(rule_counter.items(),
                                    key=lambda kv: (-kv[1], kv[0])))
    if latencies:
        counts["avg_latency_ms"] = sum(latencies) / len(latencies)
    return counts
