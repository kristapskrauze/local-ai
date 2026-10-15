"""The system-prompt echo check leaves Anton's own earlier words alone.

    python tests/system_echo_own_replies_cases.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import messaging as M  # noqa: E402

INSTRUCTION = ("Atbildi tieši uz šo ziņu, ja tā min kaut ko konkrētu, un nekad "
               "nesāc ar sveicienu, ja saruna jau notiek.")
OWN = ('- Bomb It 2 — klasiskā Bomb It / Bomberman spēle, bet ar "2" nozīmi: '
       "režģis, bombas, ienaidnieki.")
SYSTEM = ("Tu esi Antons.\n" + INSTRUCTION + "\n\n" + M._OWN_REPLIES_HDR + OWN
          + M._OWN_REPLIES_END + " un neatkārto to saturu.")


def _check(reply):
    M._INFLIGHT_INCOMING.system_prompt = SYSTEM
    try:
        return M._drop_system_prompt_echo(reply)
    finally:
        M._INFLIGHT_INCOMING.system_prompt = ""


def test_own_earlier_words_are_kept():
    reply = "Bomb It 2 — klasiskā Bomb It spēle, adaptēta tavam telefonam."
    assert _check(reply) == reply, _check(reply)


def test_a_lifted_instruction_is_still_dropped():
    reply = INSTRUCTION + "\nTelefons A ir labāks."
    out = _check(reply)
    assert "Atbildi tieši" not in out and "Telefons A ir labāks." in out, out


if __name__ == "__main__":
    test_own_earlier_words_are_kept()
    test_a_lifted_instruction_is_still_dropped()
    print("OK")
