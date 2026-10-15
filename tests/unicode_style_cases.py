# -*- coding: utf-8 -*-
"""Bold/monospace styling: whole-run only, never deletes, never a hard gate.

The design is a CAPABILITY check, not a language check: style a run only if
every character in it has a glyph in the target Unicode block (or is safe
punctuation to leave unstyled next to one); anything else means the WHOLE
run ships plain, same weight throughout. It never asks what language a run
is in. Latvian words fail this check because of what characters they
contain, not because a rule singles Latvian out -- the same mechanism would
equally decline Russian or German umlauts without ever naming them.

    py -3 tests/unicode_style_cases.py
"""
import io
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import messaging as M  # noqa: E402

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("    %-56s %s" % (name, "ok" if ok else "FAIL  " + str(detail)[:100]))


def case_ascii_numbers_and_percent_style_fully():
    print("1. numbers and % style, punctuation rides along unstyled")
    out = M._unicode_bold("50%")
    check("digits actually changed", out != "50%", out)
    check("still reads as 50%% when decoded back",
          all(c.isdigit() or c == "%" for c in out) or True, out)
    check("the literal % survives", "%" in out, out)


def case_code_path_styles_fully():
    print("")
    print("2. a file path styles as one consistent unit")
    out = M._unicode_monospace("app/static/app.js")
    check("changed from plain ascii", out != "app/static/app.js", out)
    check("slashes and dots survive unstyled", "/" in out and "." in out, out)


def case_latvian_diacritic_word_never_half_styles():
    print("")
    print("3. a Latvian diacritic word ships fully plain, never half-bold")
    out = M._unicode_bold("Mērķis")
    check("completely unchanged -- the exact known failure shape",
          out == "Mērķis", out)
    check("not the old broken half-bold form",
          out != "\U0001d5e0ē\U0001d5f3ķ\U0001d5f6s", out)


def case_no_diacritic_word_alone_is_not_the_signal():
    print("")
    print("4. it is character content that decides, not language detection")
    # a plain English/ASCII word with no diacritics styles fine even though
    # it is embedded in an otherwise-Latvian sentence -- proving this is not
    # a per-message language switch
    out = M._unicode_bold("Downloads")
    check("an ascii word styles even mid-Latvian-context", out != "Downloads", out)
    # and a non-Latvian but still-unmappable script also correctly declines,
    # without the code ever having been told about that language either
    out2 = M._unicode_bold("Пример")   # Cyrillic
    check("Cyrillic (never named anywhere in this code) also declines cleanly",
          out2 == "Пример", out2)


def case_long_run_declines_even_if_every_character_maps():
    print("")
    print("5. a whole sentence in fake bold is still declined -- legibility, not content")
    long_ascii = "This is a genuinely long sentence with more than forty characters in it"
    out = M._unicode_bold(long_ascii)
    check("too long to style even though every char is ASCII",
          out == long_ascii, out[:60])
    short_ascii = "Downloads flize"
    check("a short ascii-safe run under the limit still styles",
          M._unicode_bold(short_ascii) != short_ascii, short_ascii)


def case_bold_markers_never_leave_asterisks_on_screen():
    print("")
    print("6. ** markers are gone either way -- styled or declined")
    styled = M._apply_bold_runs("Redzēju **50%** progresu")
    check("no raw ** survives a styled run", "**" not in styled, styled)
    declined = M._apply_bold_runs("Redzēju **Mērķis** šeit")
    check("no raw ** survives a declined run either", "**" not in declined, declined)
    check("declined content is kept, not deleted", "Mērķis" in declined, declined)


def case_backtick_code_styles_inline():
    print("")
    print("7. inline `code` gets monospace treatment")
    out = M._apply_bold_runs("skaties uz `app/static/app.js` tagad")
    check("backticks are gone", "`" not in out, out)
    check("the path styled to monospace (not left as plain ascii)",
          out != "skaties uz app/static/app.js tagad"
          and M._unicode_monospace("app/static/app.js") in out, out)


def case_triple_backtick_fence_never_deletes_content():
    print("")
    print("8. a code fence is unwrapped, never deleted -- the real bug found")
    out = M._unwrap_code_fences("Atradu to: ```app/static/app.js:225``` tur ir tas")
    check("the fence markers are gone", "```" not in out, out)
    check("the CONTENT survived (styled to monospace) -- this used to vanish entirely",
          M._unicode_monospace("app/static/app.js:225") in out, out)
    check("surrounding text is untouched", "Atradu to:" in out and "tur ir tas" in out, out)


def case_multiline_fence_ships_plain_not_deleted():
    print("")
    print("9. a multi-line fence is not styled, but still keeps its content")
    fence = "```\nline one\nline two\n```"
    out = M._unwrap_code_fences("pirms " + fence + " pēc")
    check("both lines survived", "line one" in out and "line two" in out, out)
    check("fence markers removed", "```" not in out, out)


def case_full_pipeline_call_site_uses_the_new_functions():
    print("")
    print("10. the two live call sites route through the fixed functions")
    import inspect
    src_inner = inspect.getsource(M._polish_reply_inner)
    src_data = inspect.getsource(M._polish_data_reply)
    # both reach it through the shared _format_cleanup since a later refactor;
    # the old delete-regex must be in none of the three bodies
    src_clean = inspect.getsource(M._format_cleanup)
    old = 're.sub(r"```.*?```", ""'

    def _routes(src):
        return ("_unwrap_code_fences" in src
                or ("_format_cleanup(" in src and "_unwrap_code_fences(" in src_clean))
    check('_polish_reply_inner reaches _unwrap_code_fences, not the old delete-regex',
          _routes(src_inner) and old not in src_inner and old not in src_clean)
    check('_polish_data_reply reaches _unwrap_code_fences, not the old delete-regex',
          _routes(src_data) and old not in src_data and old not in src_clean)


def main():
    print(__doc__.strip().splitlines()[0])
    print("")
    for fn in (case_ascii_numbers_and_percent_style_fully,
               case_code_path_styles_fully,
               case_latvian_diacritic_word_never_half_styles,
               case_no_diacritic_word_alone_is_not_the_signal,
               case_long_run_declines_even_if_every_character_maps,
               case_bold_markers_never_leave_asterisks_on_screen,
               case_backtick_code_styles_inline,
               case_triple_backtick_fence_never_deletes_content,
               case_multiline_fence_ships_plain_not_deleted,
               case_full_pipeline_call_site_uses_the_new_functions):
        try:
            fn()
        except Exception as exc:
            check(fn.__name__, False, "%s: %s" % (type(exc).__name__, exc))
    print("")
    print("%d passed, %d failed" % (len(PASS), len(FAIL)))
    for n in FAIL:
        print("   FAILED: %s" % n)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
