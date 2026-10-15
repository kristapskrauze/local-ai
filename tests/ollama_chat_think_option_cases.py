# -*- coding: utf-8 -*-
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


def _res(content):
    return {"ok": True, "status_code": 200,
            "body": {"message": {"role": "assistant", "content": content},
                     "done_reason": "stop"}}


def _isolate_capability_probe():
    model = str({}.get("model") or M._default_chat_model())
    before = dict(M._MODEL_CAPS)
    M._MODEL_CAPS[model] = ("thinking",)
    return before


def _scripted_json_request():
    calls = {"n": 0, "payloads": []}

    def fn(url, method="POST", payload=None, timeout=None, headers=None):
        calls["payloads"].append(payload)
        calls["n"] += 1
        return _res("ok")
    fn.calls = calls
    return fn


def test_think_default_false():
    print("1. default call sends think=False")
    caps_before = _isolate_capability_probe()
    orig = M._json_request
    M._json_request = _scripted_json_request()
    try:
        M._ollama_chat({}, [{"role": "user", "content": "hi"}])
        p = M._json_request.calls["payloads"][0]
        check("think is False", p.get("think") is False, p.get("think"))
        check("model present", "model" in p, p)
        check("messages present", "messages" in p, p)
        check("stream is False", p.get("stream") is False, p.get("stream"))
    finally:
        M._json_request = orig
        M._MODEL_CAPS.clear()
        M._MODEL_CAPS.update(caps_before)


def test_think_true():
    print("2. think=True sends think=True, rest of payload identical")
    caps_before = _isolate_capability_probe()
    orig = M._json_request
    M._json_request = _scripted_json_request()
    try:
        M._ollama_chat({}, [{"role": "user", "content": "hi"}], think=True)
        p = M._json_request.calls["payloads"][0]
        check("think is True", p.get("think") is True, p.get("think"))
        check("model present", "model" in p, p)
        check("messages present", "messages" in p, p)
        check("stream is False", p.get("stream") is False, p.get("stream"))
    finally:
        M._json_request = orig
        M._MODEL_CAPS.clear()
        M._MODEL_CAPS.update(caps_before)


def main():
    test_think_default_false()
    test_think_true()
    print("")
    print("PASS %d  FAIL %d" % (len(PASS), len(FAIL)))
    if FAIL:
        sys.exit(1)


if __name__ == "__main__":
    main()
