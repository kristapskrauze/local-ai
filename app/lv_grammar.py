# -*- coding: utf-8 -*-
"""High-confidence Latvian contextual grammar diagnostics and fixes.

This is intentionally not a broad Latvian parser. The live validator already
checks Unicode integrity, invented words, register, and relevance. This module
adds the missing contextual layer for cases where every token is a real Latvian
word, but the form is wrong in context: agreement, case, verb government, and
reflexive possessives.

Rules are loaded from ``config/lv_grammar_rules.json`` and mapped to the topic
taxonomy in ``config/lv_grammar_ontology.json``. The public Uzdevumi.lv pages
are used as a grammar-topic map, not as copied exercise data.
"""

from __future__ import annotations

from dataclasses import dataclass
import copy
import json
from pathlib import Path
import re
from typing import Iterable


ROOT = Path(__file__).resolve().parent.parent
RESOURCE_DIR = ROOT / "resources" / "latvian" / "grammar"
RULES_PATHS = (
    RESOURCE_DIR / "lv_grammar_rules.json",
    ROOT / "config" / "lv_grammar_rules.json",
)
ONTOLOGY_PATHS = (
    RESOURCE_DIR / "lv_grammar_ontology.json",
    ROOT / "config" / "lv_grammar_ontology.json",
)

SOURCE_LABEL = "Uzdevumi.lv Latvian grammar topic taxonomy"
SOURCE_URLS = (
    "https://www.uzdevumi.lv/p/latviesu-valoda",
    "https://www.uzdevumi.lv/p/latviesu-valoda-pec-skola2030-paraugprogrammas",
)


_FALLBACK_RULES = [
    {
        "id": "lv.agreement.subject_verb.mes_aizgaja",
        "category": "agreement.subject_verb",
        "pattern": r"\b(Mēs|mēs)\s+aizgāja\b",
        "replacement": r"\1 aizgājām",
        "message": "The subject 'mēs' needs the 1st person plural verb form.",
        "concepts": ["Darbības vārds", "Darbības vārda konjugācijas"],
        "confidence": 0.99,
    },
    {
        "id": "lv.agreement.adjective_number.vini_priecigs",
        "category": "agreement.adjective_number",
        "pattern": r"\b(Viņi|viņi)\s+(bija|ir|būs)\s+ļoti\s+priecīgs\b",
        "replacement": r"\1 \2 ļoti priecīgi",
        "message": "The plural pronoun 'viņi' needs the plural adjective form.",
        "concepts": ["Īpašības vārds", "Vārdu saskaņojums"],
        "confidence": 0.98,
    },
    {
        "id": "lv.case.preposition.ar_masina",
        "category": "case.preposition",
        "pattern": r"\bar\s+mašīna\b",
        "replacement": "ar mašīnu",
        "message": "After 'ar', this phrase needs the governed case form.",
        "concepts": ["Lietvārds", "Locīšana", "Prievārds"],
        "confidence": 0.98,
    },
    {
        "id": "lv.case.direct_object.redzeju_vins",
        "category": "case.direct_object",
        "pattern": r"\bredzēju\s+viņš\b",
        "replacement": "redzēju viņu",
        "message": "The direct object of 'redzēju' needs accusative.",
        "concepts": ["Vietniekvārds", "Teikuma locekļi"],
        "confidence": 0.99,
    },
    {
        "id": "lv.case.preposition.ar_mans_draugs",
        "category": "case.preposition",
        "pattern": r"\bar\s+mans\s+draugs\b",
        "replacement": "ar savu draugu",
        "message": "This phrase needs a governed case form and reflexive possessive.",
        "concepts": ["Vietniekvārds", "Lietvārds", "Prievārds"],
        "confidence": 0.97,
    },
    {
        "id": "lv.government.verb_dative.piezvaniju_draugam",
        "category": "government.verb_dative",
        "pattern": r"\bpiezvanīju\s+uz\s+savu\s+draugu\b",
        "replacement": "piezvanīju savam draugam",
        "message": "The verb 'piezvanīt' takes a dative complement.",
        "concepts": ["Darbības vārds", "Vietniekvārds", "Locīšana"],
        "confidence": 0.99,
    },
    {
        "id": "lv.reflexive_possessive.vina_sava_mamma",
        "category": "reflexive_possessive",
        "pattern": r"\b(Viņa|viņa)\s+aizgāja\s+pie\s+viņas\s+mammas\b",
        "replacement": r"\1 aizgāja pie savas mammas",
        "message": "Use reflexive possessive 'savas' when the subject owns the object.",
        "concepts": ["Vietniekvārds", "Valodas kultūra", "Sintakse"],
        "confidence": 0.96,
    },
    {
        "id": "lv.case.preposition.pie_draugs",
        "category": "case.preposition",
        "pattern": r"\bpie\s+draugs\b",
        "replacement": "pie drauga",
        "message": "After 'pie', this everyday phrase needs genitive.",
        "concepts": ["Lietvārds", "Locīšana", "Prievārds"],
        "confidence": 0.98,
    },
    {
        "id": "lv.case.direct_object.nopirku_jauns_telefons",
        "category": "case.direct_object",
        "pattern": r"\bnopirku\s+jauns\s+telefons\b",
        "replacement": "nopirku jaunu telefonu",
        "message": "The direct object after 'nopirku' needs accusative forms.",
        "concepts": ["Lietvārds", "Īpašības vārds", "Teikuma locekļi"],
        "confidence": 0.98,
    },
]


@dataclass(frozen=True)
class GrammarRule:
    id: str
    category: str
    pattern_text: str
    replacement: str
    message: str
    concepts: tuple[str, ...]
    confidence: float
    source: str = SOURCE_LABEL
    source_urls: tuple[str, ...] = SOURCE_URLS
    source_type: str = "taxonomy"
    auto_fix: bool = True
    ignore_case: bool = True
    blocked_by: tuple[str, ...] = ()

    @property
    def pattern(self) -> re.Pattern[str]:
        flags = re.UNICODE
        if self.ignore_case:
            flags |= re.IGNORECASE
        return re.compile(self.pattern_text, flags)


def _as_tuple(value: object) -> tuple[str, ...]:
    if isinstance(value, (list, tuple)):
        return tuple(str(v) for v in value if str(v).strip())
    if value:
        return (str(value),)
    return ()


_CACHE = {
    "rules_path": None,
    "rules_mtime": None,
    "rule_dicts": None,
    "rules": None,
    "ontology_path": None,
    "ontology_mtime": None,
    "ontology": None,
}


def _first_existing(paths: Iterable[Path]) -> Path | None:
    for path in paths:
        try:
            if path.exists():
                return path
        except OSError:
            continue
    return None


def _expanded_rule_dicts(raw_rules: list[dict]) -> list[dict]:
    expanded = []
    for raw in raw_rules:
        forms = raw.get("forms")
        if not isinstance(forms, list):
            expanded.append(raw)
            continue
        pattern_template = str(raw.get("pattern_template") or "")
        replacement_template = str(raw.get("replacement_template") or "")
        if "{bad}" not in pattern_template or "{good}" not in replacement_template:
            continue
        for idx, form in enumerate(forms, 1):
            if not isinstance(form, dict) or not form.get("bad") or not form.get("good"):
                continue
            item = copy.deepcopy(raw)
            item.pop("forms", None)
            item.pop("pattern_template", None)
            item.pop("replacement_template", None)
            bad = str(form["bad"])
            good = str(form["good"])
            item["id"] = "%s.%03d" % (str(raw.get("id") or "lv.generated"), idx)
            item["pattern"] = pattern_template.replace("{bad}", re.escape(bad))
            item["replacement"] = replacement_template.replace("{good}", good)
            item["message"] = str(raw.get("message") or "Contextual grammar fix")
            if form.get("message"):
                item["message"] = str(form["message"])
            expanded.append(item)
    return expanded


def _load_rule_dicts() -> list[dict]:
    path = _first_existing(RULES_PATHS)
    if path is not None:
        try:
            mtime = path.stat().st_mtime
            if (_CACHE["rule_dicts"] is not None
                    and _CACHE["rules_path"] == path
                    and _CACHE["rules_mtime"] == mtime):
                return list(_CACHE["rule_dicts"])
            raw = json.loads(path.read_text(encoding="utf-8"))
            rules_raw = raw.get("rules") if isinstance(raw, dict) else raw
            if isinstance(rules_raw, list) and rules_raw:
                rules_out = _expanded_rule_dicts(
                    [r for r in rules_raw if isinstance(r, dict)])
                _CACHE["rules_path"] = path
                _CACHE["rules_mtime"] = mtime
                _CACHE["rule_dicts"] = rules_out
                _CACHE["rules"] = None
                return list(rules_out)
        except Exception:
            pass
    return list(_FALLBACK_RULES)


def _make_rule(raw: dict) -> GrammarRule | None:
    try:
        return GrammarRule(
            id=str(raw["id"]),
            category=str(raw["category"]),
            pattern_text=str(raw["pattern"]),
            replacement=str(raw["replacement"]),
            message=str(raw.get("message") or raw["id"]),
            concepts=_as_tuple(raw.get("concepts")),
            confidence=float(raw.get("confidence", 0.95)),
            source=str(raw.get("source") or SOURCE_LABEL),
            source_urls=_as_tuple(raw.get("source_urls")) or SOURCE_URLS,
            source_type=str(raw.get("source_type") or "taxonomy"),
            auto_fix=bool(raw.get("auto_fix", True)),
            ignore_case=bool(raw.get("ignore_case", True)),
            blocked_by=_as_tuple(raw.get("blocked_by")),
        )
    except Exception:
        return None


def rules() -> tuple[GrammarRule, ...]:
    """Return configured grammar rules, skipping malformed entries safely."""
    if _CACHE["rules"] is not None:
        _load_rule_dicts()
        if _CACHE["rules"] is not None:
            return _CACHE["rules"]
    out = []
    for raw in _load_rule_dicts():
        rule = _make_rule(raw)
        if rule is not None:
            try:
                rule.pattern.search("")
            except re.error:
                continue
            out.append(rule)
    _CACHE["rules"] = tuple(out)
    return _CACHE["rules"]


def ontology() -> dict:
    path = _first_existing(ONTOLOGY_PATHS)
    if path is None:
        return {}
    try:
        mtime = path.stat().st_mtime
        if (_CACHE["ontology"] is not None
                and _CACHE["ontology_path"] == path
                and _CACHE["ontology_mtime"] == mtime):
            return dict(_CACHE["ontology"])
        raw = json.loads(path.read_text(encoding="utf-8"))
        obj = raw if isinstance(raw, dict) else {}
        _CACHE["ontology_path"] = path
        _CACHE["ontology_mtime"] = mtime
        _CACHE["ontology"] = obj
        return dict(obj)
    except Exception:
        return {}


def _replacement(rule: GrammarRule, match: re.Match[str]) -> str:
    observed = match.group(0)
    fixed = match.expand(rule.replacement)
    if observed[:1].isupper() and fixed[:1].islower():
        fixed = fixed[:1].upper() + fixed[1:]
    return fixed


def _issue(rule: GrammarRule, match: re.Match[str]) -> dict:
    return {
        "rule_id": rule.id,
        "category": rule.category,
        "message": rule.message,
        "span": [match.start(), match.end()],
        "observed": match.group(0),
        "suggestion": _replacement(rule, match),
        "confidence": rule.confidence,
        "concepts": list(rule.concepts),
        "source": rule.source,
        "source_urls": list(rule.source_urls),
        "source_type": rule.source_type,
        "auto_fix": rule.auto_fix,
    }


def _inside_quotes(text: str, start: int, end: int) -> bool:
    left_single = text[:start].count("'") - text[:start].count("\\'")
    right_single = text[end:].count("'") - text[end:].count("\\'")
    left_double = text[:start].count('"') - text[:start].count('\\"')
    right_double = text[end:].count('"') - text[end:].count('\\"')
    return ((left_single % 2 == 1 and right_single > 0)
            or (left_double % 2 == 1 and right_double > 0))


def _blocked(rule: GrammarRule, text: str, match: re.Match[str]) -> bool:
    if _inside_quotes(text, match.start(), match.end()):
        return True
    if not rule.blocked_by:
        return False
    window = text[max(0, match.start() - 80): min(len(text), match.end() + 80)]
    flags = re.IGNORECASE | re.UNICODE
    return any(re.search(pattern, window, flags) for pattern in rule.blocked_by)


def contextual_diagnostics(text: str, min_confidence: float = 0.90) -> list[dict]:
    """Find high-confidence contextual grammar issues in ``text``."""
    s = str(text or "")
    if not s.strip():
        return []
    found = []
    seen = set()
    for rule in rules():
        if rule.confidence < min_confidence:
            continue
        for match in rule.pattern.finditer(s):
            if _blocked(rule, s, match):
                continue
            key = (match.start(), match.end(), rule.id)
            if key in seen:
                continue
            seen.add(key)
            found.append(_issue(rule, match))
    found.sort(key=lambda item: (item["span"][0], item["span"][1], item["rule_id"]))
    return found


def apply_deterministic_fixes(text: str,
                              min_confidence: float = 0.95) -> tuple[str, list[dict]]:
    """Apply only exact high-confidence fixes and return ``(fixed, issues)``."""
    fixed = str(text or "")
    applied: list[dict] = []
    for rule in rules():
        if rule.confidence < min_confidence or not rule.auto_fix:
            continue

        def repl(match: re.Match[str]) -> str:
            if _blocked(rule, fixed, match):
                return match.group(0)
            issue = _issue(rule, match)
            applied.append(issue)
            return str(issue["suggestion"])

        fixed = rule.pattern.sub(repl, fixed)
    return fixed, applied


def coverage_summary() -> dict:
    """Small serializable summary for diagnostics and setup reports."""
    by_category: dict[str, int] = {}
    concept_counts: dict[str, int] = {}
    for rule in rules():
        by_category[rule.category] = by_category.get(rule.category, 0) + 1
        for concept in rule.concepts:
            concept_counts[concept] = concept_counts.get(concept, 0) + 1
    return {
        "rule_count": len(rules()),
        "categories": by_category,
        "concepts": concept_counts,
        "sources": list(SOURCE_URLS),
        "rules_path": str(_first_existing(RULES_PATHS) or ""),
        "ontology_path": str(_first_existing(ONTOLOGY_PATHS) or ""),
        "ontology_loaded": bool(ontology()),
    }


def rule_ids(issues: Iterable[dict]) -> list[str]:
    return [str(item.get("rule_id")) for item in issues if item.get("rule_id")]
