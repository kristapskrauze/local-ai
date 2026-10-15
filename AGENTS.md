# AGENTS.md

Rules for the coding agents that edit this repository (the dashboard's Code
tab runs a local Ollama model through opencode, and Anton's self-heal loop
proposes patches). Read this fully before changing anything.

## Project
- Windows service `SysMonitor` (NSSM, LocalSystem), FastAPI on port 8765 only.
- Python 3.14. Run the tests with `python tests/run_offline.py`.
- Paths outside the repo come from `app/paths.py` and other machines from
  `app/hosts.py`; never hard-code a user folder or an IP address.

## RULE 1 - HOW TO EDIT FILES

Edit every file ONLY with the patch/file-edit tool. It writes bytes literally
and does not interpret `$` or backticks.

NEVER write, create or modify a source file (.js .py .html .css .toml .md)
through the shell. Forbidden for editing source, with no exceptions:

- Set-Content / Add-Content / Out-File / echo / printf > file
- PowerShell here-strings `@" ... "@` and `@' ... '@`
- PowerShell -replace, -split, or any regex patching of source
- Python `open(...).write(...)` one-liners that contain source code
- any command whose argument contains JavaScript, Python or HTML source

Why: in PowerShell the backtick is the escape character and `$` interpolates.
Source pushed through the shell comes out corrupted (`return "-";` becomes
``return `"-"`;``, `${x}` becomes `$\${x}`, code fences get written into the
file). The patch tool is the only safe path.

The shell IS allowed for running the app, `node --check`, git, reading files,
curl and pip. Never for writing source.

## RULE 2 - JAVASCRIPT SAFETY WORKFLOW

`app/static/app.js` is large and shared by two pages. To change it:

1. Copy `app/static/app.js` to `app/static/app.js.lastgood`.
2. Make the edit in `app/static/app.candidate.js` with the patch tool.
3. Validate: `node --check app\static\app.candidate.js`.
4. If it fails, stop, leave `app.js` alone and report the error.
5. If it passes, copy the candidate over `app.js` and run `node --check` again.
6. Never leave temp, `.broken` or scratch files behind.

`node --check` is necessary but not sufficient: shell corruption can produce
valid but dead JS. After any JS edit, also load the page or curl an endpoint
and confirm real values appear.

## RULE 3 - STOP CONDITION

If a file cannot be edited with the native patch tool, do not fall back to the
shell. Stop and output exactly:

    NATIVE_FILE_EDITING_NOT_AVAILABLE

## RULE 4 - DO NOT READ WHOLE FILES TO FIND THINGS

`app/messaging.py` and `app/agent_tools.py` are tens of thousands of tokens
each. Reading them whole fills the context and leaves no room for the work.
Use the `repo-lookup` MCP server (`_tools/mcp_repo_server.py`):

- `repo_symbols(name)`: where a thing is defined (file, line, kind), Python
  and JavaScript.
- `repo_refs(name)`: who uses it. Run it before changing any signature.
- `code_search(query)`: similar code when the name is unknown. Its hits are a
  path and a line to go and read, never an answer on their own.

Then read only those ranges. Grep is the fallback for strings, config keys and
URLs.

## RULE 4a - NO HARD WORD GATES

Never fix a bug or build a feature by adding a keyword list, regex or string
match that decides what a message MEANS or where it GOES: intent, routing,
lane or mode selection, "is this a request for X", relevance, whether an
answer is good enough, whether to search. That judgment belongs to the model.
Give the model the tools, the context and the state, and let it decide.

Allowed, and only these:
- mechanical parsing: dates, times, numbers, URLs, JSON, file paths, markup;
- SAFETY and PERMISSION boundaries that already exist: who the owner is, who
  may download or write, the owner's explicit yes before code is applied, SSRF
  host blocks. They stay deterministic and fail-closed.

When something is decided or routed wrongly, fix it upstream: what the model
is told and shown, or a model call that asks the real question. Do not add a
regex that catches the bad case, do not reword the input to slip past an
existing gate, and do not bolt a filter onto the reply path. If the upstream
fix is not visible, stop and report it. Details and how to test a removal:
`NO_HARD_GATES.md`.

## Stack (do not change without being asked)
- Backend: Python, FastAPI, Uvicorn, psutil, nvidia-smi, LibreHardwareMonitor
  (fan control and CPU temperatures).
- Frontend: static HTML/CSS/JS in `app/static/`, no build step.

## Two pages, one app.js

- `/` serves `index.html` (`body[data-app=console]`): rig operations.
- `/me` serves `me.html` (`body[data-app=personal]`): the personal assistant.

Every DOM lookup in `app.js` must be null-guarded, because a section that
exists on one page is absent on the other: `var el = $("#x"); if (el) ...`.
Render functions return early when their card is missing. The command palette
reads its tab list from the live nav buttons; do not hard-code tabs.

## Running and restarting

- Never start a manual `python -m uvicorn ... --port 8765` while the service
  exists: it takes the port, the service crash-loops, and fan control (which
  needs the service's elevation) stops.
- Frontend files are served from disk: changes are live on refresh. Bump the
  `?v=` cache tags.
- Python changes need a service restart: the dashboard's Terminal tab, or
  `_service\nssm.exe restart SysMonitor` from an elevated shell.
- If the port is taken or the service is paused, run `_service\revive.cmd`.
  If it reports missing service-control rights, run `_service\revive-admin.cmd`
  once from an Administrator shell.
- Never fix the dashboard by restarting Docker Desktop, WSL or their services;
  they are separate and started by hand. Report them as down and carry on.

## Models

Every lane uses one Ollama tag (`qwen3.8:latest` by default). Two large models
cannot share a 24 GB card, and a split lane costs a reload each way. Whatever
is configured must name a tag that `ollama list` shows, or the assistant
silently gets 404s.
