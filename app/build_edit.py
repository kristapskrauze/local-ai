"""Surgical HTML editing.

Instead of asking the model to reprint an entire page, ask it for a small
list of find/replace pairs and apply them to the base HTML. This keeps the
diff tiny and avoids the model drifting on unchanged regions.
"""

import json
import re

_JSON_ARRAY_RE = re.compile(r"\[\s*\{.*?\}\s*\]", re.DOTALL)


def _extract_pairs(text):
    """Pull a list of {find, replace} dicts out of a model reply.

    Returns a list of (find, replace) tuples, or None if no usable array was
    found. Malformed entries are skipped rather than raising.
    """
    if not text:
        return None
    match = _JSON_ARRAY_RE.search(text)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except (ValueError, TypeError):
        return None
    if not isinstance(data, list):
        return None
    pairs = []
    for item in data:
        if not isinstance(item, dict):
            continue
        find = item.get("find")
        replace = item.get("replace")
        if not isinstance(find, str) or not isinstance(replace, str):
            continue
        pairs.append((find, replace))
    return pairs


def _apply_pairs(base_html, pairs):
    """Apply find/replace pairs to base_html.

    Returns the edited string, or None if any find string is not present in
    the (progressively edited) document. Each pair is applied in order and
    the first occurrence is replaced.
    """
    result = base_html
    for find, replace in pairs:
        if result.count(find) != 1:
            return None
        result = result.replace(find, replace, 1)
    return result


def surgical(goal, base_html, change, chat=None, ios9=False):
    """Apply a surgical edit to an HTML page.

    Args:
        goal: The overall task description (context for the model).
        base_html: The current HTML to edit.
        change: The specific change requested.
        chat: A callable taking a prompt string and returning the model's
            reply string.
        ios9: Reserved for legacy rendering constraints; unused here.

    Returns:
        The edited HTML string, or None when a surgical edit could not be
        made (no usable pairs, or a find string was missing).
    """
    prompt = (
        "You are editing an existing HTML page. Do NOT reprint the whole file "
        "and do NOT redesign anything that already works.\n\n"
        "THE TASK -- do exactly this, all of it, and nothing else:\n%s\n\n"
        "Work through the task above one problem at a time. For each distinct "
        "problem it names, find the exact code responsible and fix THAT. If it "
        "says a control does nothing (\"the X button/poga does nothing\"), find "
        "that control's id in the HTML and its event binding in the JS and make "
        "the binding actually do the thing. If it says a reset/new-game leaves "
        "old state on screen, find the function that starts a round and make it "
        "redraw that state. Give one find/replace pair per fix; include a pair "
        "for EVERY problem the task names -- a reply that fixes only the first "
        "one is wrong.\n\n"
        "Background, for context only (this is what the page is; it is already "
        "built -- do not treat it as a to-do list): %s\n\n"
        "Current HTML:\n%s\n\n"
        "Return ONLY a JSON array of objects, each with keys \"find\" and "
        "\"replace\": \"find\" is an exact, unique substring of the page above, "
        "\"replace\" is what it becomes. JSON array only, no prose."
    ) % (change, goal, base_html)

    if chat is None:
        from app.agent_tools import _build_chat
        chat = lambda prompt: _build_chat([{"role": "user", "content": prompt}])

    reply = chat(prompt)
    pairs = _extract_pairs(reply)
    if not pairs:
        return None
    return _apply_pairs(base_html, pairs)
