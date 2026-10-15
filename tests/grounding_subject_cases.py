# -*- coding: utf-8 -*-
"""The bug this pins down: the presence of any material was read as vouching for
the answer, so an article about a different person did not merely fail to
help — it switched off the check that would have stripped the invented claim.

Every material string is inlined, so this runs offline and deterministically.
Latvian stays inside this file: a shell strips the diacritics and then the
test is measuring the wrong input.

    python tests/grounding_subject_cases.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.messaging import (          # noqa: E402
    _material_on_subject, _question_subjects, _same_stem)


MAT_VIKSNE = (
    "- Jānis Vīksne (Vikipēdija): Jānis Vīksne bija latviešu ornitologs, "
    "Latvijas Universitātes Bioloģijas institūta vadošais pētnieks. "
    "Apbalvots ar Triju Zvaigžņu ordeni par ieguldījumu ornitoloģijā.")

# ...and what lvwiki returns for the same question today: the article about
# cosmonauts in general. Also on the wrong subject — and it contains the word
# "zvaigzne", which is the prefix collision the stem rule has to survive.
MAT_KOSMONAUTS = (
    "- Kosmonauts (Vikipēdija): Kosmonauts vai astronauts (no grieķu "
    "ἄστρον — zvaigzne) ir cilvēks, kas veicis kosmisko lidojumu. Pirmais "
    "bija Padomju Savienības kosmonauts Jurijs Gagarins 1961. gadā.")

MAT_ULMANIS = (
    "- Kārlis Ulmanis (Vikipēdija): Kārlis Ulmanis bija Latvijas valsts un "
    "ministru prezidents. 1934. gada 15. maijā viņš veica valsts apvērsumu "
    "un atlaida Saeimu.")

# Genitive in the material, dative in the question — the same noun.
MAT_ES = (
    "- Eiropas Savienība (Vikipēdija): Latvija Eiropas Savienības "
    "dalībvalsts ir kopš 2004. gada 1. maija.")

MAT_RIGA = (
    "- Rīga (Vikipēdija): Rīga ir Latvijas galvaspilsēta un lielākā pilsēta "
    "Baltijā, atrodas pie Daugavas upes grīvas.")

MAT_GOLF = (
    "- Volkswagen Golf Mk3: The Golf Mk3 fuel tank capacity is 55 litres on "
    "most variants, 60 litres on the syncro.")

MAT_MOON = (
    "- Mēness (Vikipēdija): Mēness ir Zemes vienīgais dabiskais pavadonis. "
    "1969. gadā uz Mēness pirmais nolaidās Nīls Ārmstrongs.")

MAT_PAMSE = (
    "- Kārlis Pamše: Kārlis Pamše (dzimis 1917. gada 31. martā Virānes "
    "pagastā, miris 2015. gada 9. septembrī) bija latviešu aktieris, "
    "režisors un rakstnieks. - Monika Zīle: Monika Zīle (dzimusi kā Monika "
    "Skrastiņa) ir latviešu rakstniece un žurnāliste.")

# ...and lvwiki keyword-matched a question about a car on its last content
# word: "tilpums" returned the article about VOLUME.
MAT_TILPUMS = (
    "- Tilpums: Tilpums ir kāda ģeometriska ķermeņa lielums, cik daudz "
    "trīsdimensiju telpas tas aizņem jeb telpas daļa, kas norobežota ar "
    "vismaz vienu noslēgtu virsmu.")


CASES = [
    ("Kas bija Jānis Zvaigžņkalns, Latvijas pirmais kosmonauts?",
     MAT_VIKSNE, False,
     "invented person, material is a different Jānis — must not vouch"),
    ("Kas bija Jānis Zvaigžņkalns, Latvijas pirmais kosmonauts?",
     MAT_KOSMONAUTS, False,
     "invented person, material is the generic article — must not vouch"),

    ("Kāda ir Latvijas galvaspilsēta?", MAT_RIGA, True,
     "no named subject (Latvijas is in every article) — nothing to check"),
    ("Cikos šodien lec saule?", MAT_RIGA, True,
     "no named subject at all — nothing to check"),
    ("Cik burtu ir latviešu alfabētā?", MAT_ULMANIS, True,
     "no named subject — this check must not be the one that fires"),

    ("Kurā gadā Kārlis Ulmanis veica valsts apvērsumu?", MAT_ULMANIS, True,
     "subject present — vouched exactly as before"),
    ("Kad Latvija pievienojās Eiropas Savienībai?", MAT_ES, True,
     "dative question against genitive material — same noun, vouched"),
    ("Vai Rīga atrodas pie Daugavas?", MAT_RIGA, True,
     "both named things present"),
    ("Kāds ir Golf 3 Cabrio degvielas bākas tilpums?", MAT_GOLF, True,
     "one of two named things is enough"),

    ("Kas ir Rundāles pils?", MAT_RIGA, False,
     "asked about Rundāle, given Rīga"),
    ("Ko Prāta Vētra izdeva 2019. gadā?", MAT_ULMANIS, False,
     "asked about a band, given a president"),
    ("Īsi pastāsti, kas bija Kārlis Ulmanis.", MAT_PAMSE, False,
     "LV-RAG-001 live: asked one Kārlis, given another — shared first name"),
    ("Kāds ir Golf 3 Cabrio degvielas bākas tilpums?", MAT_TILPUMS, False,
     "LV-RESEARCH-004 live: lvwiki matched the word 'tilpums', not the car"),

    # --- traps this check does NOT fix, asserted so it stays honest -------
    ("Kādu grāmatu 1987. gadā sarakstīja Kārlis Ulmanis par mākslīgo "
     "intelektu?", MAT_ULMANIS, True,
     "LV-HALL-002: real person, invented deed — retrieval is correct here"),
    ("Kurš Latvijas prezidents 1969. gadā pirmais nolaidās uz Mēness?",
     MAT_MOON, True,
     "LV-HALL-005: false premise about a real subject — not this check's job"),
]

# The stem rule is the load-bearing part: too loose and the invented name is
# vouched for by "zvaigzne", too tight and a real question loses its evidence.
STEM_CASES = [
    ("savienibai", "savienibas", True, "one noun, two cases"),
    ("rigas", "riga", True, "genitive vs nominative"),
    ("viksne", "viksnes", True, "surname inflected"),
    ("zvaigznkalns", "zvaigzne", False, "THE collision — different words"),
    ("janis", "jana", False, "too little in common to be the same word"),
    ("rundales", "riga", False, "unrelated"),
]

SUBJECT_CASES = [
    ("Kas bija Jānis Zvaigžņkalns, Latvijas pirmais kosmonauts?",
     ["zvaigznkalns"], "surname beats shared first name; Latvijas dropped"),
    ("Kāda ir Latvijas galvaspilsēta?", [], "opening word and Latvijas both out"),
    ("Kurā gadā Kārlis Ulmanis veica valsts apvērsumu?", ["ulmanis"], ""),
]


def main():
    fails = 0
    for a, b, want, why in STEM_CASES:
        got = _same_stem(a, b)
        ok = got is want
        fails += not ok
        print("%s  _same_stem(%s, %s) = %s  %s"
              % ("OK  " if ok else "FAIL", a, b, got, why))
    print()
    for question, want, why in SUBJECT_CASES:
        got = _question_subjects(question)
        ok = got == want
        fails += not ok
        print("%s  subjects(%s) = %s  %s"
              % ("OK  " if ok else "FAIL", question[:46], got, why))
        if not ok:
            print("      wanted %s" % (want,))
    print()
    for question, material, want, why in CASES:
        got = _material_on_subject(question, material=material, names=set())
        ok = got is want
        fails += not ok
        print("%s  on_subject=%-5s %s"
              % ("OK  " if ok else "FAIL", got, why))
        if not ok:
            print("      q: %s" % question)
    print()
    total = len(STEM_CASES) + len(SUBJECT_CASES) + len(CASES)
    print("%d/%d passed" % (total - fails, total))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
