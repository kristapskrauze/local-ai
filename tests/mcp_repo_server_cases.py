# -*- coding: utf-8 -*-
"""mcp_repo_server: the repo lookups, exposed to a coding agent over MCP/stdio.

It wraps app.selfinspect read-only (repo_symbols, repo_refs, code_search) so an
agent can ask "where is X", "who uses it", or "what's like X" without reading a
3700-line file. This test drives the server exactly as a client would -- a
subprocess, one JSON-RPC message per line on stdin/out -- and checks:

  * initialize answers with the server's capabilities
  * tools/list advertises all three tools, each with a usable inputSchema
  * a tools/call on repo_symbols returns the file AND line for a symbol that
    really is in this repo
  * a bad request -- an unknown tool, and a name that is too short -- comes back
    as a JSON-RPC error, and the server is still alive afterwards (no traceback
    ever hits the protocol stream on stdout)

    py tests\\mcp_repo_server_cases.py
"""
import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(ROOT, "_tools", "mcp_repo_server.py")
KNOWN_SYMBOL = "select_best"      # app/street_imagery.py:647
KNOWN_FILE = "app/street_imagery.py"
PASS, FAIL = [], []
_next_id = [100]


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("    %-56s %s" % (name[:56],
                            "ok" if ok else "FAIL  " + str(detail)[:70]))
    return ok


class Conn(object):
    """One live server process, spoken to line-by-line like a real MCP client."""

    def __init__(self):
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(
            [ROOT] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
        self.proc = subprocess.Popen(
            [sys.executable, SERVER],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=ROOT, env=env, bufsize=0, universal_newlines=False)

    def _send(self, obj):
        self.proc.stdin.write(json.dumps(obj).encode("utf-8") + b"\n")
        self.proc.stdin.flush()

    def _read_reply(self, timeout=120):
        deadline = time.time() + timeout
        while time.time() < deadline:
            line = self.proc.stdout.readline()
            if not line:
                if self.proc.poll() is not None:
                    raise RuntimeError("server exited while waiting for a reply")
                continue
            try:
                return json.loads(line.decode("utf-8", "replace"))
            except ValueError:
                raise RuntimeError("non-JSON bytes hit the protocol stream: %r"
                                   % line[:120])
        raise RuntimeError("no reply within %ss" % timeout)

    def request(self, method, params=None):
        rid = _next_id[0]
        _next_id[0] += 1
        self._send({"jsonrpc": "2.0", "id": rid, "method": method,
                    "params": params or {}})
        msg = self._read_reply()
        # a reply shaped like a reply: 2.0, our id, and exactly one of
        # result/error set (so neither a crash nor a second response)
        check("%s -> JSON-RPC reply with our id" % method,
              isinstance(msg, dict) and msg.get("jsonrpc") == "2.0"
              and msg.get("id") == rid, msg)
        check("%s -> one response, server still up" % method,
              ("result" in msg or "error" in msg) and self.proc.poll() is None,
              "process exited: %r" % (self.proc.poll(),))
        return msg

    def close(self):
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.kill()
        except Exception:
            pass
        self.proc.wait()


def _payload(msg):
    content = (msg.get("result") or {}).get("content") or []
    text = content[0].get("text", "") if content else ""
    try:
        return json.loads(text)
    except ValueError:
        return None


def case_initialize(c):
    print("\n1. initialize")
    msg = c.request("initialize", {"protocolVersion": "2024-11-05",
                                   "clientInfo": {"name": "t", "version": "0"}})
    r = msg.get("result") or {}
    check("answers the protocol handshake", "protocolVersion" in r, msg)
    check("advertises itself",
          isinstance(r.get("serverInfo"), dict) and r["serverInfo"].get("name"),
          r.get("serverInfo"))
    caps = r.get("capabilities") or {}
    check("declares the tools capability",
          isinstance(caps.get("tools"), dict), caps)


def case_tools_list(c):
    print("\n2. tools/list advertises the three lookups")
    msg = c.request("tools/list")
    tools = {t.get("name"): t for t in (msg.get("result") or {}).get("tools", [])}
    for name in ("repo_symbols", "repo_refs", "code_search"):
        check("%s is on the menu" % name, name in tools, list(tools))
        if name in tools:
            check("%s has a description for the agent" % name,
                  len(tools[name].get("description", "")) > 40,
                  tools[name].get("description", "")[:50])
            check("%s has an inputSchema object" % name,
                  isinstance(tools[name].get("inputSchema"), dict),
                  tools[name].get("inputSchema"))


def case_repo_symbols_call(c):
    print("\n3. tools/call repo_symbols -> file and line of a real symbol")
    msg = c.request("tools/call", {"name": "repo_symbols",
                                   "arguments": {"name": KNOWN_SYMBOL}})
    check("not flagged as an error", (msg.get("result") or {}).get("isError") is not True, msg)
    payload = _payload(msg) or {}
    syms = payload.get("symbols")
    check("the reply payload is JSON with symbols", isinstance(syms, list) and syms,
          payload)
    hit = next((s for s in (syms or []) if s.get("name") == KNOWN_SYMBOL), None)
    check("the answer contains the file",
          bool(hit and hit.get("path", "").endswith(KNOWN_FILE)), hit)
    check("the answer contains a line number",
          bool(hit and isinstance(hit.get("line"), int) and hit["line"] > 0), hit)


def case_repo_refs_call(c):
    print("\n4. tools/call repo_refs -> who uses it")
    msg = c.request("tools/call", {"name": "repo_refs",
                                   "arguments": {"name": KNOWN_SYMBOL}})
    check("not flagged as an error", (msg.get("result") or {}).get("isError") is not True, msg)
    payload = _payload(msg) or {}
    check("reports a total of uses",
          isinstance(payload.get("total"), int) and payload["total"] > 0, payload)
    check("groups the uses by file",
          isinstance(payload.get("files"), dict) and payload["files"], payload)


def case_bad_requests(c):
    print("\n5. bad requests are JSON-RPC errors, not crashes")
    msg = c.request("tools/call",
                    {"name": "definitely_not_a_tool", "arguments": {}})
    err = msg.get("error") or {}
    check("unknown tool -> an error object", "error" in msg and "result" not in msg, msg)
    check("the error has a code and a reason",
          isinstance(err.get("code"), int) and bool(err.get("message")), err)
    check("the server is still alive", c.proc.poll() is None,
          "process exited: %r" % (c.proc.poll(),))

    msg = c.request("tools/call", {"name": "repo_refs",
                                   "arguments": {"name": "x"}})
    err = msg.get("error") or {}
    check("a too-short symbol name -> an error object (Refused, not a crash)",
          "error" in msg and "result" not in msg, msg)
    check("the refusal carries a readable reason", bool(err.get("message")), err)
    check("the server is still alive", c.proc.poll() is None,
          "process exited: %r" % (c.proc.poll(),))

    msg = c.request("no/such/method")
    check("an unknown method -> a clean error, server still up",
          "error" in msg and "result" not in msg and c.proc.poll() is None, msg)


def case_code_search_reachable(c):
    print("\n6. code_search is reachable through the same path")
    msg = c.request("tools/call",
                    {"name": "code_search",
                     "arguments": {"query": "decide whether to reply"}})
    # The semantic corpus may be unbuilt in this sandbox (then a clean
    # Refused -> error) or present (then a result). Both are legitimate; what we
    # must never see is the server dying or the reply being malformed.
    check("returns either a result or a clean error (never a crash)",
          ("result" in msg or "error" in msg) and c.proc.poll() is None, msg)


def main():
    print(__doc__.strip().splitlines()[0])
    t0 = time.time()
    c = Conn()
    cases = [case_initialize, case_tools_list, case_repo_symbols_call,
             case_repo_refs_call, case_bad_requests, case_code_search_reachable]
    for fn in cases:
        try:
            fn(c)
        except Exception as exc:
            check(fn.__name__, False, "%s: %s" % (type(exc).__name__, exc))
    c.close()
    print("\n%d passed, %d failed  (%.0fs)" % (len(PASS), len(FAIL), time.time() - t0))
    for name in FAIL:
        print("   FAILED: %s" % name)
    return 1 if FAIL else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        raise
    except Exception:
        import traceback
        traceback.print_exc()
        sys.exit(2)
