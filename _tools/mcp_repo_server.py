# -*- coding: utf-8 -*-
"""Read-only MCP server over stdio for looking up code in this repository.

Why it exists: app/agent_tools.py is ~3.7k lines / ~48k tokens. A coding agent
that must add a tool there has two bad options -- read the whole file (spends
most of its context just getting there) or grep blindly and hit the wrong
`search` of the five. The small facts it actually needs -- where a function is
defined, who calls it, what the registration block looks like -- are three
lines of answer. They already live in app/selfinspect.py; this process just
makes them reachable from a coding agent over MCP, with no SDK, stdlib only.

Protocol: Model Context Protocol over stdio, line-delimited JSON-RPC 2.0.
The client opens this process, sends one JSON object per line on stdin
(initialize, tools/list, tools/call), and gets one JSON object per line back on
stdout. Diagnostics go to stderr, never to the protocol stream.

Three tools, all READ-ONLY (app/selfinspect has no write tools at all):
  repo_symbols   where a NAMED thing is defined
  repo_refs      who USES it -- run before changing a signature
  code_search    conceptually similar code -- the last resort; its hits are a
                 path + line to go and READ, not an answer

stdout discipline: the protocol stream *is* stdout, so a stray print() from
anywhere would corrupt every message after it. main() swaps sys.stdout for a
guard that raises instead of writing, and the only thing ever written is by
_send, which goes to the real stream kept in _OUT. Logging is wired to stderr
before the app is imported.
"""
import json
import logging
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

PROTOCOL = "2024-11-05"
SERVER_INFO = {"name": "repo-lookup", "version": "1.0.0"}
CAPABILITIES = {"tools": {}}

# app.selfinspect, bound in main() only after the stdout guard is in place.
SI = None
# The real stdout, written by _send and by nothing else.
_OUT = None

# JSON-RPC 2.0 error codes.
PARSE = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL = -32603

# The tools. The descriptions are written for the agent deciding whether to
# call them -- when to use it, and when NOT to -- in the same spirit as the
# tool descriptions in app/agent_tools.py. The distinction that makes them
# useful is the ordering: name first (repo_symbols), impact second
# (repo_refs), fuzzy last (code_search), and its hits are a place to go.
TOOLS = [
    {
        "name": "repo_symbols",
        "description": (
            "Where a NAMED thing is DEFINED: a function, class, or module "
            "constant, with its file and line. Use this the moment you know "
            "what the thing is called (\"where is select_best defined?\"). "
            "Prefer it over reading a whole file just to find a definition, "
            "and to find the small registration block around a handler. Do NOT "
            "use it to read the code itself -- take the path and line it "
            "returns and go read that. It answers location, not behaviour."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string",
                         "description": "symbol to locate; an exact match wins, "
                                        "otherwise it is a substring"},
                "kind": {"type": "string",
                         "enum": ["function", "class", "constant", "method"],
                         "description": "optional: only this kind of definition"},
                "path": {"type": "string",
                         "description": "optional: only definitions under this "
                                        "repo-relative path"},
                "limit": {"type": "integer", "minimum": 1},
            },
            "description": "Give a name, or (instead) a path to list one "
                           "file's symbols.",
        },
    },
    {
        "name": "repo_refs",
        "description": (
            "Who USES it: every import, call, and reference to a named symbol, "
            "with file and line, counted by kind. Use this BEFORE you change a "
            "function's signature or rename it, to see exactly what would "
            "break -- and before you say nothing calls something. AST-backed, "
            "so a name that only appears in a string or a comment is NOT a "
            "reference. Do NOT use it to find where a thing is defined (use "
            "repo_symbols)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string",
                         "description": "the symbol whose uses you want "
                                        "(at least 2 characters)"},
                "limit": {"type": "integer", "minimum": 1},
            },
            "required": ["name"],
        },
    },
    {
        "name": "code_search",
        "description": (
            "Code that is CONCEPTUALLY similar, for when you cannot name the "
            "thing you are looking for (\"the bit that decides whether to "
            "reply\"). This is the last resort and the weakest of the three: "
            "if you have a name, use repo_symbols; if you have an exact "
            "string, that is a lookup, not a search. Similarity is not an "
            "answer -- every hit is a PATH and a LINE to go and READ, never a "
            "conclusion to trust."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": "a plain-language description "
                                         "(at least 3 characters)"},
                "limit": {"type": "integer", "minimum": 1},
                "kind": {"type": "string",
                         "description": "optional: restrict to a kind"},
                "path": {"type": "string",
                         "description": "optional: restrict to a repo path"},
            },
            "required": ["query"],
        },
    },
]

# The argument names each tool forwards to app.selfinspect, in order. Two jobs:
# a client can't send a key we don't forward (so a typo'd argument cannot
# become a crash), and we expose only what selfinspect already validates,
# sandboxes and redacts -- nothing is forwarded that selfinspect does not own.
_ARGS = {
    "repo_symbols": ("name", "kind", "path", "limit"),
    "repo_refs": ("name", "limit"),
    "code_search": ("query", "limit", "kind", "path"),
}


class _RPCError(Exception):
    """A request that must come back as a JSON-RPC error, not a crash."""

    def __init__(self, code, message, data=None):
        super(_RPCError, self).__init__(message)
        self.code = code
        self.message = message
        self.data = data


class _StdoutLocked(Exception):
    """A handler tried to write something that is not a protocol message."""


class _WriteGuard(object):
    """Stands in for sys.stdout and refuses every write.

    print() routes through sys.stdout.write, so this raises the moment
    anything tries to send a non-protocol byte to the stream, instead of
    corrupting the messages after it. The real stdout lives in _OUT.
    """

    def write(self, text):
        if text:
            raise _StdoutLocked(
                "stdout is the MCP protocol stream; only _send may write it")
        return 0

    def flush(self):
        if _OUT is not None:
            _OUT.flush()

    def fileno(self):
        if _OUT is not None:
            return _OUT.fileno()
        raise _StdoutLocked("stdout is locked to the protocol stream")

    def isatty(self):
        return False


def _send(obj):
    """The only legitimate write to the protocol stream."""
    _OUT.write(json.dumps(obj) + "\n")
    _OUT.flush()


def _error_body(code, message, data=None):
    err = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return err


def _response(rpc_id, result):
    return {"jsonrpc": "2.0", "id": rpc_id, "result": result}


def _error_response(rpc_id, code, message, data=None):
    return {"jsonrpc": "2.0", "id": rpc_id,
            "error": _error_body(code, message, data)}


def _text_result(payload):
    try:
        text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    except (TypeError, ValueError):
        text = repr(payload)
    return {"content": [{"type": "text", "text": text}], "isError": False}


def _tool_payload(name, args):
    kwargs = {k: args[k] for k in _ARGS[name] if k in args}
    fn = getattr(SI, name)            # name is one of _ARGS's three keys only
    return fn(**kwargs)


def _tools_call(params):
    if not isinstance(params, dict):
        raise _RPCError(INVALID_PARAMS, "tools/call params must be an object")
    name = params.get("name")
    if not isinstance(name, str) or not name:
        raise _RPCError(INVALID_PARAMS, "tools/call needs a string tool name")
    args = params.get("arguments")
    if args is None:
        args = params.get("args") or {}
    if not isinstance(args, dict):
        raise _RPCError(INVALID_PARAMS, "arguments must be an object")
    if name not in _ARGS:
        raise _RPCError(
            INVALID_PARAMS, "unknown tool: %r (have: %s)"
            % (name, ", ".join(sorted(_ARGS))))
    try:
        payload = _tool_payload(name, args)
    except SI.Refused as exc:
        raise _RPCError(INVALID_PARAMS, str(exc))
    except TypeError as exc:
        raise _RPCError(INVALID_PARAMS,
                        "bad arguments for %s: %s" % (name, str(exc)[:160]))
    except Exception as exc:
        raise _RPCError(INTERNAL, "tool %s failed: %s: %s"
                        % (name, type(exc).__name__, str(exc)[:160]))
    return _text_result(payload)


def _handle(method, params):
    """Return the `result` payload for one request, or raise _RPCError."""
    if method == "initialize":
        p = params or {}
        return {"protocolVersion": p.get("protocolVersion") or PROTOCOL,
                "serverInfo": dict(SERVER_INFO),
                "capabilities": dict(CAPABILITIES)}
    if method == "tools/list":
        return {"tools": [{"name": t["name"], "description": t["description"],
                           "inputSchema": t["inputSchema"]} for t in TOOLS]}
    if method == "tools/call":
        return _tools_call(params)
    if method == "ping":
        return {}
    raise _RPCError(METHOD_NOT_FOUND, "unknown method: %r" % (method,))


def _dispatch(line):
    """Read one input line and, if it needs one, send exactly one response."""
    try:
        msg = json.loads(line)
    except (ValueError, TypeError) as exc:
        _send(_error_response(None, PARSE, "parse error: %s" % str(exc)[:120]))
        return
    if not isinstance(msg, dict):
        _send(_error_response(None, INVALID_REQUEST, "not a JSON-RPC object"))
        return
    method = msg.get("method")
    rpc_id = msg.get("id")
    if "id" not in msg:                # a notification never gets a response
        return
    try:
        result = _handle(method, msg.get("params"))
        _send(_response(rpc_id, result))
    except _RPCError as e:
        _send(_error_response(rpc_id, e.code, e.message, e.data))
    except _StdoutLocked as e:
        # A handler tried to print into the protocol stream. Report it on
        # stderr and answer as an error; the stream itself stays clean.
        logging.getLogger(__name__).exception("a tool tried to write stdout")
        _send(_error_response(rpc_id, INTERNAL,
                              "a tool tried to write to the protocol stream"))
    except Exception as exc:
        _send(_error_response(rpc_id, INTERNAL,
                              "internal error: %s: %s"
                              % (type(exc).__name__, str(exc)[:160])))


def _serve():
    log = logging.getLogger(__name__)
    while True:
        line = sys.stdin.readline()
        if not line:
            break                                  # EOF: the client is done
        line = line.strip()
        if not line:
            continue
        try:
            _dispatch(line)
        except (BrokenPipeError, OSError) as exc:
            sys.stderr.write(
                "mcp_repo_server: write side broken (%s), exiting\n" % exc)
            break
        except Exception as exc:                   # the server must never die
            log.exception("mcp_repo_server: dispatch error")
            sys.stderr.write("mcp_repo_server: dispatch error: %r\n" % (exc,))


def _main():
    global SI, _OUT
    _OUT = sys.stdout                    # keep the real stream before we lock it
    sys.stdout = _WriteGuard()           # lock it: only _OUT may be written
    logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                        format="mcp_repo_server: %(message)s")
    from app import selfinspect as _si
    SI = _si
    logging.getLogger(__name__).info("repo-lookup ready (read-only, stdio)")
    _serve()
    return 0


if __name__ == "__main__":
    sys.exit(_main())
