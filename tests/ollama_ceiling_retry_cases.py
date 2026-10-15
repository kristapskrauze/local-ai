# -*- coding: utf-8 -*-
"""_ollama_chat's ceiling-retry: must also fire when content is EMPTY.

Monkeypatches messaging._json_request to script controlled HTTP responses;
never touches a real Ollama instance.

    py -3 tests/ollama_ceiling_retry_cases.py
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


def _res(content, done_reason="length"):
    return {"ok": True, "status_code": 200,
            "body": {"message": {"role": "assistant", "content": content},
                     "done_reason": done_reason}}


def _isolate_capability_probe():
    """_model_capabilities makes its OWN _json_request call and does not
    reliably cache a probe with no `capabilities` key in the response --
    which consumed responses meant for the scripted chat sequence itself
    and made the FIRST test written against this look like it passed for
    the wrong reason. Pre-seeding the cache for whatever model
    _default_chat_model() resolves to removes that side channel entirely;
    the caller restores M._MODEL_CAPS in a finally.
    """
    model = str({}.get("model") or M._default_chat_model())
    before = dict(M._MODEL_CAPS)
    M._MODEL_CAPS[model] = ("thinking",)
    return before


def _scripted_json_request(responses):
    calls = {"n": 0, "payloads": []}

    def fn(url, method="POST", payload=None, timeout=None, headers=None):
        calls["payloads"].append(payload)
        i = calls["n"]
        calls["n"] += 1
        return responses[i] if i < len(responses) else responses[-1]
    fn.calls = calls
    return fn


def case_empty_content_at_the_ceiling_retries_instead_of_raising():
    print("1. empty content + done_reason=length retries, does not raise immediately")
    caps_before = _isolate_capability_probe()
    orig = M._json_request
    M._json_request = _scripted_json_request([
        _res(""),                                   # first call: nothing, ran out of room
        _res("Atradu to: app/static/app.js, rindas 225-226."),   # retry: real answer
    ])
    try:
        out = M._ollama_chat({}, [{"role": "user", "content": "hi"}],
                             options={"num_predict": 700})
        check("did not raise", True)
        check("used the retry's real content",
              out == "Atradu to: app/static/app.js, rindas 225-226.", out)
        check("actually made two HTTP calls (one retry)",
              M._json_request.calls["n"] == 2, M._json_request.calls["n"])
    except M.MessagingError as exc:
        check("did not raise", False, exc)
    finally:
        M._json_request = orig
        M._MODEL_CAPS.clear()
        M._MODEL_CAPS.update(caps_before)


def case_empty_content_that_stays_empty_still_raises_eventually():
    print("")
    print("2. empty content that STAYS empty after the retry still raises")
    caps_before = _isolate_capability_probe()
    orig = M._json_request
    M._json_request = _scripted_json_request([
        _res(""),   # first call: nothing
        _res(""),   # retry: still nothing -- genuinely stuck, not a room problem
    ])
    try:
        M._ollama_chat({}, [{"role": "user", "content": "hi"}],
                       options={"num_predict": 700})
        check("raised MessagingError", False, "did not raise")
    except M.MessagingError as exc:
        check("raised MessagingError", True)
        check("names the actual problem", "empty reply" in str(exc), exc)
    finally:
        M._json_request = orig
        M._MODEL_CAPS.clear()
        M._MODEL_CAPS.update(caps_before)


def case_retry_budget_is_never_smaller_than_the_original():
    print("")
    print("3. the widened retry budget is never smaller than the original")
    caps_before = _isolate_capability_probe()
    orig = M._json_request
    seen_options = []

    def fn(url, method="POST", payload=None, timeout=None, headers=None):
        seen_options.append(dict((payload or {}).get("options") or {}))
        if len(seen_options) == 1:
            return _res("")   # trigger the retry
        return _res("done.", done_reason="stop")
    M._json_request = fn
    try:
        M._ollama_chat({}, [{"role": "user", "content": "hi"}],
                       options={"num_predict": 700})
        check("two calls happened", len(seen_options) == 2, seen_options)
        if len(seen_options) == 2:
            original = seen_options[0].get("num_predict")
            retried = seen_options[1].get("num_predict")
            check("retry budget (%s) is at least double the original (%s)"
                  % (retried, original),
                  retried is not None and original is not None
                  and retried >= 2 * original, (original, retried))
    finally:
        M._json_request = orig
        M._MODEL_CAPS.clear()
        M._MODEL_CAPS.update(caps_before)


def case_short_num_predict_is_not_eligible_for_retry():
    print("")
    print("4. a small num_predict (e.g. a classifier) is never retried")
    caps_before = _isolate_capability_probe()
    orig = M._json_request
    M._json_request = _scripted_json_request([_res("")])
    try:
        try:
            M._ollama_chat({}, [{"role": "user", "content": "hi"}],
                           options={"num_predict": 4})
            check("raised immediately, no retry", False, "did not raise")
        except M.MessagingError:
            check("raised immediately, no retry",
                  M._json_request.calls["n"] == 1, M._json_request.calls["n"])
    finally:
        M._json_request = orig
        M._MODEL_CAPS.clear()
        M._MODEL_CAPS.update(caps_before)


def main():
    print(__doc__.strip().splitlines()[0])
    print("")
    for fn in (case_empty_content_at_the_ceiling_retries_instead_of_raising,
               case_empty_content_that_stays_empty_still_raises_eventually,
               case_retry_budget_is_never_smaller_than_the_original,
               case_short_num_predict_is_not_eligible_for_retry):
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
