"""Who the assistant works for and who it talks to, read from config/messaging.json.

Prompts are written in Latvian and English, and Latvian declines names, so
the owner and the partner each carry their case forms under "people":

    "people": {
      "owner":   {"nom": "Jānis", "gen": "Jāņa", "dat": "Jānim", "acc": "Jāni",
                  "full": "Jānis Bērziņš", "full_gen": "Jāņa Bērziņa",
                  "about_lv": "...", "about_en": "..."},
      "partner": {"nom": "Anna", "gen": "Annas", "dat": "Annai", "acc": "Annu"},
      "aliases": ["Pēteris", "Pēteri"]
    }

A missing form falls back to the nominative. Other contacts come from
auto_reply.contact_names; "aliases" adds nicknames and declined forms the
name matching should also recognise.
"""
import json
import re
import unicodedata
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "messaging.json"
_FORMS = ("nom", "gen", "dat", "acc")


def _load_config():
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}


def _forms(entry, fallback):
    entry = entry or {}
    nom = entry.get("nom") or fallback
    out = {f: entry.get(f) or nom for f in _FORMS}
    out["full"] = entry.get("full") or nom
    out["full_gen"] = entry.get("full_gen") or out["gen"]
    return out


_cfg = _load_config()
_people = _cfg.get("people") or {}
_auto = _cfg.get("auto_reply") or {}

owner = _forms(_people.get("owner"), _auto.get("owner_name") or "Owner")
partner = _forms(_people.get("partner"), "Partner")

OWNER, OWNER_GEN, OWNER_DAT, OWNER_ACC = (owner[f] for f in _FORMS)
OWNER_FULL_GEN = owner["full_gen"]
PARTNER, PARTNER_GEN, PARTNER_DAT, PARTNER_ACC = (partner[f] for f in _FORMS)

# one sentence about the owner, for prompts that pick what would interest them
OWNER_ABOUT_LV = (_people.get("owner") or {}).get("about_lv") or ""
OWNER_ABOUT_EN = (_people.get("owner") or {}).get("about_en") or ""


def ascii_fold(text):
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def contact_names():
    """Display names of everyone in auto_reply.contact_names, owner and partner first."""
    names = [OWNER, PARTNER]
    for name in (_auto.get("contact_names") or {}).values():
        if name and name not in names:
            names.append(name)
    return names


def known_names():
    """Every form of every known name, lowercased, with and without diacritics."""
    forms = set(owner.values()) | set(partner.values())
    forms.update(contact_names())
    forms.update(a for a in (_people.get("aliases") or []) if a)
    out = set()
    for f in forms:
        for word in str(f).split():
            low = word.lower()
            out.add(low)
            out.add(ascii_fold(low))
    return out


def names_pattern():
    """Regex alternation (no group) matching any known name form, longest first."""
    forms = set()
    for f in set(owner.values()) | set(partner.values()) | set(contact_names()) | set(_people.get("aliases") or []):
        forms.update(str(f).split())
        forms.update(ascii_fold(w) for w in str(f).split())
    return "|".join(re.escape(f) for f in sorted(forms, key=len, reverse=True) if f)
