# No hard word gates

This is the standing rule for this repository, for every coding agent that
edits it, and for Anton, the assistant that runs in it.

Never fix a bug or implement a feature by adding a keyword list, regex or
string match that decides what a message MEANS or where it GOES: intent,
routing, lane or mode selection, "is this a request for X", relevance, whether
an answer is good enough, whether to search. That judgment belongs to the
model. Give the model the tools, the context and the state, and let it decide.

Every word list that was tried became the next silent failure: "palīdz"
silenced a homework question, and "pieliec" / "build" / "make" / "harder"
switched build mode on for ordinary messages until the mode was removed.

Allowed, and only these:
- mechanical parsing: dates, times, numbers, URLs, JSON, file paths, markup;
- SAFETY and PERMISSION boundaries that already exist: who the owner is, who may
  download or write, the owner's explicit yes before code is applied, SSRF host
  blocks. They stay deterministic and fail-closed. They are not a place for new
  filters.

When something is decided or routed wrongly:
- fix it upstream: what the model is told and shown, or a model call that asks
  the real question;
- do NOT add a regex that catches the bad case, on the input or on the model's
  answer;
- do NOT reword the owner's text to slip past an existing gate;
- do NOT bolt a filter onto the reply path;
- if the upstream fix is not visible, stop and say so; never ship a gate as a
  placeholder.

Removing a word gate in the code being touched is welcome. Test it the way
`tests/build_routing_cases.py` does: prove the old code fails the test and the
new code passes, with the model call faked.

The same rule is RULE 4a in `AGENTS.md`, which the coding agents load.
