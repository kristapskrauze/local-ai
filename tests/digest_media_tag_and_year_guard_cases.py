# -*- coding: utf-8 -*-
"""MEDIA-TAG   publisher format prefixes ("Video: ...", "(FOTO)") never ship
  YEAR-GUARD  _drop_unvouched_years reaches the assistant-agent lane's evidence

    py -3 tests/digest_media_tag_and_year_guard_cases.py
"""
import io
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import messaging as M  # noqa: E402


def main():
    checks = []

    def check(label, ok):
        checks.append((label, bool(ok)))
        print(("  ok  " if ok else "FAIL  ") + label)

    strip = M._strip_media_tag
    check("MEDIA-001 leading 'Video:' removed",
          strip("Video: ASV veikušas triecienus trīs Irānas naftas tankkuģiem")
          == "ASV veikušas triecienus trīs Irānas naftas tankkuģiem")
    check("MEDIA-002 leading '(VIDEO)' removed",
          strip("(VIDEO) Zelenskis uzstājas ANO") == "Zelenskis uzstājas ANO")
    check("MEDIA-003 leading 'FOTO:' removed",
          strip("FOTO: Rīgā atklāj jaunu tiltu") == "Rīgā atklāj jaunu tiltu")
    check("MEDIA-004 trailing '(FOTO)' removed",
          strip("Ministrs atkāpjas no amata (FOTO)") == "Ministrs atkāpjas no amata")
    check("MEDIA-005 'Tiešraide:' removed",
          strip("Tiešraide: Saeimas sēde") == "Saeimas sēde")
    check("MEDIA-006 an ordinary headline is untouched",
          strip("Latvija zaudē kontroli pār airBaltic: daudz kas var noiet greizi")
          == "Latvija zaudē kontroli pār airBaltic: daudz kas var noiet greizi")
    check("MEDIA-007 a colon inside the headline is not a media tag",
          strip("Semjonovs: nekas nav jāgaida")
          == "Semjonovs: nekas nav jāgaida")
    check("MEDIA-008 stripping that would leave one word is refused",
          strip("Video: sniegs") == "Video: sniegs")

    IN = M._INFLIGHT_INCOMING
    for k in ("material_years", "material_text", "local_text"):
        try:
            delattr(IN, k)
        except AttributeError:
            pass

    findings = ("web_search -> Edgars Rinkēvičs ir Latvijas Valsts prezidents. "
                "Rinkēvičs (dzimis 1973. gadā) agrāk bija ārlietu ministrs.")
    IN.material_years = set(M._YEAR_RE.findall(findings))
    IN.material_text = findings
    IN.local_text = "kas ir latvijas prezidents"
    wrong = ("Latvijas prezidents ir Edgars Rinkēvičs. "
             "Viņš ir amatā kopš 2019. gada.")
    out = M._drop_unvouched_years(wrong, "iMessage;-;t")
    check("YEAR-001 unvouched 2019 sentence dropped", "2019" not in out)
    check("YEAR-001 the correct name is kept", "Rinkēvičs" in out)

    IN.local_text = "kopš kura gada rinkēvičs ir amatā"
    out2 = M._drop_unvouched_years(wrong, "iMessage;-;t")
    check("YEAR-002 a date question keeps its own answer year", "2019" in out2)

    IN.local_text = "kas ir latvijas prezidents"
    IN.material_years = {"1973", "2023"}
    IN.material_text = findings + " Amatā stājās 2023. gadā."
    ok_reply = "Prezidents ir Edgars Rinkēvičs. Amatā kopš 2023. gada."
    check("YEAR-003 a vouched year survives",
          "2023" in M._drop_unvouched_years(ok_reply, "iMessage;-;t"))

    IN.material_years = set()
    IN.material_text = ""
    check("YEAR-004 with no retrieved material the guard is a no-op",
          M._drop_unvouched_years(wrong, "iMessage;-;t") == wrong)

    for k in ("material_years", "material_text", "local_text"):
        try:
            delattr(IN, k)
        except AttributeError:
            pass

    passed = sum(1 for _, ok in checks if ok)
    print("\n%d/%d passed" % (passed, len(checks)))
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
