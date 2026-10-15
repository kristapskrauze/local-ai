import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.messaging as M


class FakeModel:
    """Records the messages it is given and returns a fixed line."""
    def __init__(self, reply):
        self.reply = reply
        self.calls = []
        self.checks = []

    def __call__(self, acfg, messages, options=None, format_schema=None):
        if format_schema is not None:
            self.checks.append({"messages": messages})
            user = next(m["content"] for m in messages if m["role"] == "user")
            opts = [ln.split(". ", 1)[1] for ln in user.splitlines()
                    if ln[:1].isdigit() and ". " in ln]
            return '{"choice": %d}' % (opts.index(self.reply) + 1)
        self.calls.append({"acfg": acfg, "messages": messages, "options": options})
        return self.reply


def _last_user(fake):
    for c in reversed(fake.calls):
        for m in c["messages"]:
            if m["role"] == "user":
                return m["content"]
    return ""


def _last_system(fake):
    for c in reversed(fake.calls):
        for m in c["messages"]:
            if m["role"] == "system":
                return m["content"]
    return ""


def test_prompt_contents():
    # Sunday morning, rain expected, a headline is passed in.
    sunday = datetime(2026, 9, 6, 9, 0)
    fake = FakeModel("Mierīgu svētdienu ☕")
    M._ollama_chat = fake
    out = M._digest_signoff_model({"model": "x"}, hi=17, rain=60, mm=4.0,
                                 now=sunday, headline="Valdība pieņem jaunu likumu")
    assert out == "Mierīgu svētdienu ☕", "good ready line must be returned unchanged, got %r" % out
    user = _last_user(fake)
    assert "svētdiena" in user, "Sunday must be named in the facts, got %r" % user
    assert "lietus" in user, "expected rain must be mentioned, got %r" % user
    assert "Dienas ziņa" not in user, "headline must NOT be in the prompt, got %r" % user
    assert "Valdība" not in user, "headline text must NOT be in the prompt, got %r" % user
    sysp = _last_system(fake)
    assert "tikko beidzās" not in sysp, "prompt must not say the news just ended, got %r" % sysp
    assert "bez klišejām" not in sysp, "originality phrase must be removed, got %r" % sysp
    assert "katru dienu citu" not in sysp, "originality phrase must be removed, got %r" % sysp
    assert "korporatīvām" not in sysp, "originality phrase must be removed, got %r" % sysp
    assert "6. septembris" in user, "facts must contain Latvian month name, got %r" % user


def test_negative_temp_sign():
    # Negative temperature: the sign must be '-', never '+-' or unsigned.
    tuesday = datetime(2026, 9, 8, 9, 0)
    fake = FakeModel("Lai laba diena! ☀️")
    M._ollama_chat = fake
    out = M._digest_signoff_model({"model": "x"}, hi=-5, rain=0, mm=0.0,
                                 now=tuesday, headline="Kāds virsraksts")
    assert out == "Lai laba diena! ☀️", "good ready line must be returned unchanged, got %r" % out
    user = _last_user(fake)
    assert "-5°" in user, "facts must contain signed -5°, got %r" % user
    assert "+-" not in user, "facts must not contain '+-', got %r" % user


def test_no_rain_no_mention():
    # A day with no rain: the word lietus must not appear in the facts.
    tuesday = datetime(2026, 9, 8, 9, 0)
    fake = FakeModel("Lai laba diena! ☀️")
    M._ollama_chat = fake
    out = M._digest_signoff_model({"model": "x"}, hi=20, rain=0, mm=0.0,
                                 now=tuesday, headline="Kāds virsraksts")
    assert out == "Lai laba diena! ☀️", "good ready line must be returned unchanged, got %r" % out
    user = _last_user(fake)
    assert "lietus" not in user, "no rain expected, lietus must not be mentioned, got %r" % user
    assert "Kāds virsraksts" not in user, "headline must NOT be in the prompt, got %r" % user


if __name__ == "__main__":
    test_prompt_contents()
    test_negative_temp_sign()
    test_no_rain_no_mention()
    print("OK")