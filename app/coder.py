"""OpenCode runner/status helpers for the local dashboard.

The preferred live runner is WSL OpenCode. Windows OpenCode is kept as a
compatibility fallback because prior PowerShell-mediated edits corrupted source
files. Manual/task-file mode remains available when live runs are disabled.
"""
import logging
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import uuid
import base64
import collections
import random
import html
import json
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

from app import people
from app import paths

LOGGER = logging.getLogger(__name__)

WSL_EXE = r"C:\Windows\System32\wsl.exe"
WSL_OPENCODE = "/usr/local/bin/opencode"
WSL_CONFIG_HOME = paths.WSL_CONFIG_HOME

DOBBY_HOME = paths.USER_HOME
MEDIA_WORKSPACE = r"M:\AI\media-server-workspace"
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
TASK_DIR_NAME = ".opencode_tasks"
WSL_BRIDGE_DIR = os.path.join(PROJECT_ROOT, ".wsl_bridge")
WSL_BRIDGE_REQUEST_DIR = os.path.join(WSL_BRIDGE_DIR, "requests")
WSL_BRIDGE_RUN_DIR = os.path.join(WSL_BRIDGE_DIR, "runs")
WSL_BRIDGE_CANCEL_DIR = os.path.join(WSL_BRIDGE_DIR, "cancel")
WSL_BRIDGE_HEARTBEAT = os.path.join(WSL_BRIDGE_DIR, "heartbeat.json")

RUNNER_MANUAL = "manual_task_file"
RUNNER_WSL = "wsl"
RUNNER_ALIASES = {
    "manual": RUNNER_MANUAL,
    "manual_task_file": RUNNER_MANUAL,
    "manual/task_file": RUNNER_MANUAL,
    "task_file": RUNNER_MANUAL,
    "wsl": RUNNER_WSL,
}

MAX_SECONDS = 1800  # hard cap on a single run (30 min)
_CREATE_NO_WINDOW = 0x08000000
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")

CODE_MODES = {
    "hybrid": {
        "label": "Hybrid",
        "description": "Local agent edits the selected project and uses fresh web research only when useful.",
        "uses_web": True,
        "instruction": (
            "Use the selected local project as the source of truth. Inspect files before changing them. "
            "Use the supplied web research only to resolve current API, package, or docs uncertainty; "
            "do not let web context override repository-specific behavior."
        ),
    },
    "local": {
        "label": "Local only",
        "description": "Fully local agentic coding with no web research preflight.",
        "uses_web": False,
        "instruction": (
            "Stay fully local. Do not use web or network research. Rely on the selected project, "
            "installed tools, and local documentation only."
        ),
    },
    "local_web": {
        "label": "Local + Web",
        "description": "Local opencode edits the selected project after a web search preflight.",
        "uses_web": True,
        "instruction": (
            "Keep editing local and project-scoped. Use the supplied web research as current docs/API "
            "context before making changes, then verify against the repository's actual code."
        ),
    },
}


def normalize_mode(mode):
    key = str(mode or "hybrid").strip().lower().replace("-", "_")
    return key if key in CODE_MODES else "hybrid"


def mode_uses_web(mode):
    return bool(CODE_MODES[normalize_mode(mode)]["uses_web"])


def code_mode_options():
    return [
        {
            "id": key,
            "label": cfg["label"],
            "description": cfg["description"],
            "uses_web": cfg["uses_web"],
        }
        for key, cfg in CODE_MODES.items()
    ]


def _env_int(name, default, lo, hi):
    try:
        value = int(os.environ.get(name, default))
    except Exception:
        value = default
    return max(lo, min(hi, value))


def _clean_space(value):
    return " ".join(str(value or "").split())


def _request_json(url, timeout):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 SysMonitor/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    return json.loads(raw)


# Local SearXNG (AI Hub service on :8888) is the default web-search backend;
# RIG_WEB_SEARCH_URL overrides it. DuckDuckGo HTML is the fallback but its
# bot detection makes it unreliable.
DEFAULT_SEARXNG_URL = "http://127.0.0.1:8888/search"


def _searxng_search(query, limit):
    base = os.environ.get("RIG_WEB_SEARCH_URL", "").strip() or DEFAULT_SEARXNG_URL
    sep = "&" if "?" in base else "?"
    url = base.rstrip("/") + sep + urllib.parse.urlencode(
        {
            "q": query,
            "format": "json",
            # NOT "en". See the module docstring on this function: forcing en
            # made Bing answer Latvian queries with unrelated English and
            # Chinese pages. "auto" lets the engine read the query itself.
            "language": "auto",
            "safesearch": "1",
        }
    )
    data = _request_json(url, _env_int("RIG_WEB_SEARCH_TIMEOUT", 8, 2, 30))
    results = []
    for item in data.get("results", []):
        title = _clean_space(item.get("title"))
        link = _clean_space(item.get("url") or item.get("link"))
        snippet = _clean_space(item.get("content") or item.get("snippet"))
        if title and link:
            results.append({"title": title, "url": link, "snippet": snippet})
        if len(results) >= limit:
            break
    return results


def _decode_ddg_url(href):
    href = html.unescape(href or "")
    if href.startswith("//"):
        href = "https:" + href
    parts = urllib.parse.urlparse(href)
    qs = urllib.parse.parse_qs(parts.query)
    if qs.get("uddg"):
        return _clean_space(qs["uddg"][0])
    return _clean_space(href)


class _DuckDuckGoParser(HTMLParser):
    def __init__(self, limit):
        super().__init__(convert_charrefs=True)
        self.limit = limit
        self.results = []
        self._active = None
        self._buf = []
        self._href = ""

    def handle_starttag(self, tag, attrs):
        if len(self.results) >= self.limit:
            return
        attrs = dict(attrs)
        cls = attrs.get("class", "")
        if tag == "a" and ("result__a" in cls or "result-link" in cls):
            self._active = "title"
            self._buf = []
            self._href = attrs.get("href", "")
        elif tag in ("a", "div") and ("result__snippet" in cls or "result-snippet" in cls):
            self._active = "snippet"
            self._buf = []

    def handle_data(self, data):
        if self._active:
            self._buf.append(data)

    def handle_endtag(self, tag):
        if self._active == "title" and tag == "a":
            title = _clean_space(html.unescape("".join(self._buf)))
            url = _decode_ddg_url(self._href)
            if title and url and not any(item["url"] == url for item in self.results):
                self.results.append({"title": title, "url": url, "snippet": ""})
            self._active = None
            self._buf = []
            self._href = ""
        elif self._active == "snippet" and tag in ("a", "div"):
            snippet = _clean_space(html.unescape("".join(self._buf)))
            if snippet:
                for item in reversed(self.results):
                    if not item.get("snippet"):
                        item["snippet"] = snippet
                        break
            self._active = None
            self._buf = []


# Same shape SearXNG's duckduckgo engine sends. The Referer is the endpoint's
# OWN origin and Sec-Fetch-Site is same-origin: with duckduckgo.com as referer
# and no Sec-Fetch headers the request works once and is then served the
# anomaly page. See docs.searxng.org/dev/engines/online/duckduckgo.html
_DDG_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Accept-Encoding": "identity",
    "Referer": "https://html.duckduckgo.com/",
    "Origin": "https://html.duckduckgo.com",
    "Content-Type": "application/x-www-form-urlencoded",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}
_DDG_RESULT = re.compile(
    r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.S)
_DDG_SNIPPET = re.compile(
    r'class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</a>', re.S)


_DDG_MAX_PER_MIN = 10
_DDG_MIN_GAP = 4.0
_DDG_JITTER = 1.5
_DDG_LAST = [0.0]
_DDG_BLOCKED_UNTIL = [0.0]
_DDG_CALLS = collections.deque(maxlen=200)     # timestamps, sliding window
_SEARCH_COUNTS = collections.Counter()


def _ddg_window():
    """Calls in the last 60 seconds."""
    cutoff = time.time() - 60.0
    while _DDG_CALLS and _DDG_CALLS[0] < cutoff:
        _DDG_CALLS.popleft()
    return len(_DDG_CALLS)


def _ddg_pace():
    """Wait long enough to stay under the ceiling. Returns False if blocked."""
    now = time.time()
    if now < _DDG_BLOCKED_UNTIL[0]:
        _SEARCH_COUNTS["skipped_while_blocked"] += 1
        return False
    used = _ddg_window()
    if used >= _DDG_MAX_PER_MIN:
        wait = 60.0 - (time.time() - _DDG_CALLS[0]) + 0.5
        if wait > 0:
            _SEARCH_COUNTS["throttled"] += 1
            LOGGER.info("search: %d calls in the last minute, waiting %.1fs",
                        used, wait)
            time.sleep(min(wait, 30.0))
        _ddg_window()
    gap = _DDG_MIN_GAP + random.uniform(0, _DDG_JITTER) - (time.time() - _DDG_LAST[0])
    if gap > 0:
        time.sleep(gap)
    _DDG_LAST[0] = time.time()
    _DDG_CALLS.append(_DDG_LAST[0])
    _SEARCH_COUNTS["requests"] += 1
    return True


def search_stats():
    """What the search backend has been doing -- for the dashboard and for me.

    Every time search "stopped working" today the first thing missing was a
    number: how many calls, how many blocks, how long since one succeeded.
    """
    now = time.time()
    return {
        "calls_last_minute": _ddg_window(),
        "ceiling_per_minute": _DDG_MAX_PER_MIN,
        "blocked": now < _DDG_BLOCKED_UNTIL[0],
        "blocked_for_seconds": max(0, round(_DDG_BLOCKED_UNTIL[0] - now, 1)),
        "counts": dict(_SEARCH_COUNTS),
        "cached_queries": len(_SEARCH_CACHE),
    }


_DDG_VQD_RE = (r'vqd=""([^"]+)""', r'vqd="([^"]+)"', r"vqd='([^']+)'",
               r"vqd=([0-9-]+)&")


def _ddg_vqd(query):
    """The per-query token DuckDuckGo's own frontend fetches before searching."""
    page = _ddg_fetch("https://duckduckgo.com/?" +
                      urllib.parse.urlencode({"q": query, "ia": "web"}))
    for pat in _DDG_VQD_RE:
        m = re.search(pat, page)
        if m:
            return m.group(1)
    return ""


def _ddg_fetch(url, accept="text/html,application/xhtml+xml,*/*;q=0.8"):
    req = urllib.request.Request(url, headers={
        "User-Agent": _DDG_HEADERS["User-Agent"],
        "Accept": accept,
        "Accept-Language": "en-US,en;q=0.9,lv;q=0.8",
        "Accept-Encoding": "identity",
        "Referer": "https://duckduckgo.com/"})
    with urllib.request.urlopen(
            req, timeout=_env_int("RIG_WEB_SEARCH_TIMEOUT", 8, 2, 30)) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _duckduckgo_json(query, limit):
    """Results from links.duckduckgo.com/d.js -- no key, no account."""
    if not _ddg_pace():
        return []
    vqd = _ddg_vqd(query)
    if not vqd:
        _DDG_BLOCKED_UNTIL[0] = time.time() + 120.0
        return []
    body = _ddg_fetch(
        "https://links.duckduckgo.com/d.js?" + urllib.parse.urlencode(
            {"q": query, "vqd": vqd, "kl": "wt-wt", "l": "wt-wt", "o": "json"}),
        accept="application/json, text/javascript, */*")
    # its rate-limit reply, not an empty result set
    if "is506" in body or len(body) < 200:
        _DDG_BLOCKED_UNTIL[0] = time.time() + 300.0
        return []
    m = re.search(r"DDG\.pageLayout\.load\('d',(\[.*?\])\);", body, re.S)
    raw = m.group(1) if m else body[body.find("["):]
    try:
        rows = json.loads(raw)
    except Exception:
        return []
    out = []
    for row in rows:
        if not isinstance(row, dict) or not row.get("u"):
            continue
        out.append({
            "title": _clean_space(re.sub(r"<[^>]+>", "", row.get("t") or "")),
            "url": str(row.get("u")),
            "snippet": _clean_space(re.sub(r"<[^>]+>", "", row.get("a") or ""))[:300],
        })
        if len(out) >= limit:
            break
    return out


def _duckduckgo_search(query, limit):
    """POST to html.duckduckgo.com with browser headers, paced.

    A GET with a thin User-Agent times out, which is how this was written off
    as unreachable. It also rate limits within a few requests and answers with
    a short page carrying no results at all, so calls are spaced and a block
    is remembered rather than retried into.
    """
    if not _ddg_pace():
        return []
    data = urllib.parse.urlencode(
        {"q": query, "kl": "wt-wt", "b": "", "df": ""}).encode("utf-8")
    req = urllib.request.Request("https://html.duckduckgo.com/html/",
                                 data=data, headers=_DDG_HEADERS)
    with urllib.request.urlopen(
            req, timeout=_env_int("RIG_WEB_SEARCH_TIMEOUT", 8, 2, 30)) as resp:
        page = resp.read().decode("utf-8", errors="replace")

    def _clean(fragment):
        return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", fragment)).split())

    # SearXNG detects a block by the challenge form or an HTTP 303; the
    # anomaly modal is the same thing rendered.
    if ("challenge-form" in page or "anomaly-modal" in page
            or "result__a" not in page):
        _SEARCH_COUNTS["blocked"] += 1
        _DDG_BLOCKED_UNTIL[0] = time.time() + 90.0
        LOGGER.info("search: DuckDuckGo blocked us; backing off 90s "
                    "(%d calls in the last minute)", _ddg_window())
        return []
    _SEARCH_COUNTS["answered"] += 1
    snippets = [_clean(m) for m in _DDG_SNIPPET.findall(page)]
    out, seen = [], set()
    for i, (href, title) in enumerate(_DDG_RESULT.findall(page)):
        url = _decode_ddg_url(href)
        name = _clean(title)
        if not url or not name or url in seen:
            continue
        seen.add(url)
        out.append({"title": name, "url": url,
                    "snippet": snippets[i][:300] if i < len(snippets) else ""})
        if len(out) >= limit:
            break
    return out


_SEARCH_CACHE = {}          # query -> (fetched_at, results, hosts)
_SEARCH_CACHE_TTL = 900.0   # a schedule or a price does not move in 15 min
_SEARCH_LOCK = threading.Lock()
_SEARCH_LAST_CALL = [0.0]   # wall clock of the last network search
_SEARCH_MIN_GAP = 0.4       # seconds between network searches
_SEARCH_BACKOFF_UNTIL = [0.0]


def _result_hosts(results):
    out = []
    for item in results[:5]:
        try:
            host = urllib.parse.urlparse(item.get("url") or "").hostname or ""
        except Exception:
            host = ""
        if host:
            out.append(host.lower())
    return set(out)


def _engine_once(query, limit):
    """One spaced network search: DuckDuckGo, then the local SearXNG."""
    with _SEARCH_LOCK:
        gap = _SEARCH_MIN_GAP - (time.time() - _SEARCH_LAST_CALL[0])
        if gap > 0:
            time.sleep(gap)
        _SEARCH_LAST_CALL[0] = time.time()
    for backend in (_duckduckgo_search, _duckduckgo_json):
        try:
            results = backend(query, limit)
            if results:
                return results
        except Exception:
            continue
    return []


def _wikipedia_search(query, limit, langs=("lv", "en")):
    """Wikipedia's own search, for when the engines are refusing us.

    Not a general web index and not a replacement for one: it cannot answer
    what time a radio programme starts. It answers the encyclopedic half --
    what a thing is, where it is, what was discovered -- and it answers it
    while every crawler-facing engine is serving a captcha.
    """
    out = []
    for lang in langs:
        if len(out) >= limit:
            break
        try:
            url = ("https://%s.wikipedia.org/w/api.php?" % lang) + urllib.parse.urlencode(
                {"action": "query", "list": "search", "srsearch": query,
                 "format": "json", "srlimit": max(1, limit)})
            data = _request_json(url, _env_int("RIG_WEB_SEARCH_TIMEOUT", 8, 2, 30))
        except Exception:
            continue
        for hit in ((data.get("query") or {}).get("search") or []):
            title = _clean_space(hit.get("title"))
            if not title:
                continue
            snippet = _clean_space(re.sub(r"<[^>]+>", "", hit.get("snippet") or ""))
            out.append({"title": title,
                        "url": "https://%s.wikipedia.org/wiki/%s"
                               % (lang, urllib.parse.quote(title.replace(" ", "_"))),
                        "snippet": snippet})
            if len(out) >= limit:
                break
    return out



_BRAVE_MIN_GAP = 1.1
_BRAVE_JITTER = 0.3
_BRAVE_LAST = [0.0]
_BRAVE_BLOCKED_UNTIL = [0.0]
_BRAVE_CACHE = {}  # (query, limit) -> (fetched_at, results)


def _brave_pace():
    """True if a Brave call may go out now; sleeps out the grace period first."""
    if time.time() < _BRAVE_BLOCKED_UNTIL[0]:
        return False
    gap = _BRAVE_MIN_GAP + random.uniform(0, _BRAVE_JITTER) - (time.time() - _BRAVE_LAST[0])
    if gap > 0:
        time.sleep(gap)
    _BRAVE_LAST[0] = time.time()
    return True


def _brave_search(query, limit):
    """One paced call to Brave's Web Search API.

    Raises on network/HTTP failure so callers can tell "broke" from "no key
    configured yet" (empty list, no exception) -- the caller decides whether
    that is worth falling back on.
    """
    key = os.environ.get("BRAVE_SEARCH_API_KEY", "").strip()
    if not key:
        return []
    if not _brave_pace():
        return []
    url = "https://api.search.brave.com/res/v1/web/search?" + urllib.parse.urlencode(
        {"q": query, "count": max(1, min(limit, 20)), "safesearch": "moderate"})
    req = urllib.request.Request(url, headers={
        "Accept": "application/json",
        "X-Subscription-Token": key,
    })
    try:
        with urllib.request.urlopen(
                req, timeout=_env_int("RIG_WEB_SEARCH_TIMEOUT", 8, 2, 30)) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            # A bad/revoked key will not fix itself by retrying sooner.
            _BRAVE_BLOCKED_UNTIL[0] = time.time() + 3600.0
        elif exc.code == 429:
            _BRAVE_BLOCKED_UNTIL[0] = time.time() + 60.0
        raise
    out = []
    for item in ((data.get("web") or {}).get("results") or []):
        title = _clean_space(item.get("title"))
        link = _clean_space(item.get("url"))
        snippet = _clean_space(item.get("description"))
        if title and link:
            out.append({"title": title, "url": link, "snippet": snippet[:300]})
        if len(out) >= limit:
            break
    return out


def _brave_cached(query, limit):
    """Brave is a real keyed index, not a scraped page -- one sample is
    trusted, unlike _engine_cached's two-call consistency check that exists
    only for DuckDuckGo's unrelated-page problem. Just cache it.
    """
    now = time.time()
    key = (query, int(limit))
    cached = _BRAVE_CACHE.get(key)
    if cached and now - cached[0] < _SEARCH_CACHE_TTL:
        return cached[1]
    results = _brave_search(query, limit)
    if results:
        _BRAVE_CACHE[key] = (now, results)
    return results


def _engine_cached(query, limit):
    """Cached search, at most two network calls, backing off when refused.

    The second call is not a vote for its own sake: this backend answers the
    same query correctly most of the time and with unrelated pages the rest
    (measured 4/6 on "lr1 programma"), and a caller cannot tell which it got.
    When a previous sample for this query is already in hand the comparison is
    free, so the extra call only happens on a genuinely cold query.
    """
    now = time.time()
    key = (query, int(limit))
    cached = _SEARCH_CACHE.get(key)
    if cached and now - cached[0] < _SEARCH_CACHE_TTL:
        return cached[1]
    if now < _SEARCH_BACKOFF_UNTIL[0]:
        return cached[1] if cached else []

    try:
        first = _engine_once(query, limit)
    except Exception:
        # The backend is refusing; stop asking for a bit.
        _SEARCH_BACKOFF_UNTIL[0] = time.time() + 30.0
        raise
    if not first:
        # No global backoff here: this query found nothing, which says nothing
        # about the next one.
        return []

    hosts = _result_hosts(first)
    # A stale sample of the same query is still a second opinion, for free.
    if cached and len(hosts & cached[2]) >= 2:
        _SEARCH_CACHE[key] = (now, first, hosts)
        return first

    try:
        second = _engine_once(query, limit)
    except Exception:
        second = []
    if second:
        second_hosts = _result_hosts(second)
        if len(hosts & second_hosts) >= 2:
            _SEARCH_CACHE[key] = (now, first, hosts)
            return first
        # They disagree, so one of them is the intermittent junk. The newer
        # sample is no more trustworthy than the older; keep the newer and let
        # the next call for this query settle it against the cache.
        _SEARCH_CACHE[key] = (now, second, second_hosts)
        return second
    _SEARCH_CACHE[key] = (now, first, hosts)
    return first


# Names in the conversation and discourse filler: never what the question is
# about. Kept small and explicit rather than guessed at.
_QUERY_NOISE = people.known_names() | {
    "anton", "antons", "antonam", "antona",
    "piemeram", "piem\u0113ram", "varb\u016bt", "varbut", "tie\u0161\u0101m", "tiesam",
    "l\u016bdzu", "ludzu", "paldies", "sveiks", "sveika",
}


def _keyword_query(query, keep=3):
    """A long question reduced to the words an engine can actually match."""
    tokens = [t for t in re.findall(r"\w+", str(query or ""), re.U) if len(t) > 2]
    tokens = [t for t in tokens if not t.isdigit()]
    if len(tokens) <= 5:
        return ""
    tokens = [t for t in tokens if t.lower() not in _QUERY_NOISE]
    if len(tokens) <= keep:
        return ""
    proper = [t for t in tokens if t[:1].isupper()]
    rest = sorted((t for t in tokens if t not in proper), key=len, reverse=True)
    reduced = " ".join((proper + rest)[:keep])
    return reduced if reduced.lower() != str(query or "").strip().lower() else ""


def web_search(query, limit=None):
    q = _clean_space(query)[:500]
    lim = limit or _env_int("RIG_WEB_SEARCH_LIMIT", 5, 1, 10)
    if not q:
        return {"ok": False, "results": [], "error": "empty query"}

    if os.environ.get("BRAVE_SEARCH_API_KEY", "").strip():
        try:
            results = _brave_cached(q, lim)
            if results:
                return {"ok": True, "results": results, "error": ""}
        except Exception:
            LOGGER.info("search: Brave failed, falling back", exc_info=True)

    reduced = _keyword_query(q)
    if reduced:
        try:
            results = _engine_cached(reduced, lim)
            if results:
                return {"ok": True, "results": results, "error": ""}
        except Exception:
            pass

    errors = []
    try:
        results = _engine_cached(q, lim)
        if results:
            return {"ok": True, "results": results, "error": ""}
        errors.append("no results from any engine")
    except Exception as exc:
        errors.append("search engines failed: %s" % exc)

    # No third engine attempt: _engine_cached -> _engine_once already tried
    # DuckDuckGo twice, and asking again only spends another request against
    # the engine that is rate limiting us, which is what earns the 90s block
    # that then empties every other query.
    #
    # Wikipedia instead. It is a narrower index, so it is last and it is
    # labelled, but it is reachable when nothing else is.
    try:
        results = _wikipedia_search(q, lim)
        if results:
            return {"ok": True, "results": results,
                    "error": "web engines unavailable; Wikipedia only"}
        errors.append("Wikipedia returned no results")
    except Exception as exc:
        errors.append("Wikipedia: %s" % exc)

    return {"ok": False, "results": [], "error": "; ".join(errors) or "search unavailable"}


def _format_research(research):
    if not research:
        return "Web research context: none supplied."
    lines = ["Web research context:"]
    for idx, item in enumerate(research, 1):
        title = _clean_space(item.get("title")) or _clean_space(item.get("url")) or "Untitled"
        url = _clean_space(item.get("url"))
        snippet = _clean_space(item.get("snippet"))
        lines.append("%d. %s" % (idx, title))
        if url:
            lines.append("   URL: %s" % url)
        if snippet:
            lines.append("   Snippet: %s" % snippet)
    return "\n".join(lines)


def build_coding_task(task, mode="hybrid", research=None):
    key = normalize_mode(mode)
    cfg = CODE_MODES[key]
    research_block = (
        _format_research(research)
        if cfg["uses_web"]
        else "Web research context: disabled by Local only mode."
    )
    return (
        "Agentic coding mode: %s\n\n"
        "%s\n\n"
        "Shared operating rules: inspect the repository first; keep edits scoped to the user task; "
        "preserve existing user changes; prefer project-local patterns; run focused checks when practical "
        "and report any checks that cannot be run.\n"
        "Make the SMALLEST diff that satisfies the task. Do NOT rename CSS classes, CSS custom "
        "properties, JS identifiers, HTML ids, or files unless the task explicitly asks for a code "
        "identifier rename. For branding/name/text changes, edit user-visible strings (wordmarks, "
        "titles, labels) only - renaming identifiers breaks selector/JS coupling - and treat the "
        "name case-insensitively: stylized variants (UPPERCASE wordmarks, lowercase slugs in "
        "visible text) must be updated too. Do not reformat, re-indent, or 'improve' code you are "
        "not changing.\n"
        "Before declaring done, VERIFY completeness: search (e.g. grep -in) for every term the "
        "task asked to change and fix any missed user-visible occurrence; list the remaining "
        "occurrences you intentionally left (identifiers, file names) in your final answer.\n\n"
        "%s\n\n"
        "User task:\n%s"
    ) % (cfg["label"], cfg["instruction"], research_block, task.strip())



_RENAME_RE = re.compile(
    r"from\s*[\"']?([A-Za-z0-9][\w-]{0,40})[\"']?\s*to\s*[\"']?([A-Za-z0-9][\w-]{0,40})[\"']?",
    re.I,
)
_GREP_EXTS = {
    ".html", ".htm", ".css", ".js", ".mjs", ".ts", ".jsx", ".tsx", ".py",
    ".json", ".md", ".txt", ".xml", ".yml", ".yaml", ".toml", ".vue", ".svelte",
}
_GREP_SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".opencode_tasks", ".terminal_logs",
    "_old_backups", ".venv", "venv", "dist", "build", "media-ai-safety",
}


def rename_terms(task):
    """(old, new) when the task reads like a rename/branding change, else None."""
    m = _RENAME_RE.search(task or "")
    if not m:
        return None
    old, new = m.group(1), m.group(2)
    if old.lower() == new.lower():
        return None
    return old, new


def grep_term(project_dir, term, limit=40, visible_only=False, files=None):
    """Case-insensitive file:line hits for term in project text files.

    visible_only=True skips identifier-context matches (luma-*, LUMA_*,
    jellyfin-luma-home.js, ...) so expected identifier leftovers after a
    branding rename don't trigger pointless follow-up passes.
    files, when given, restricts the scan to those project-relative paths."""
    if visible_only:
        pattern = re.compile(r"(?<![A-Za-z0-9_./-])" + re.escape(term) + r"(?![A-Za-z0-9_-])", re.I)
    else:
        pattern = re.compile(re.escape(term), re.I)

    _COMMENT_PREFIXES = ("/*", "*", "//", "#", "<!--", "rem ", "REM ")

    def _scan(path, rel, hits):
        try:
            if os.path.getsize(path) > 2_000_000:
                return
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for i, line in enumerate(f, 1):
                    if visible_only and line.lstrip().startswith(_COMMENT_PREFIXES):
                        continue
                    if pattern.search(line):
                        hits.append("%s:%d: %s" % (rel, i, line.strip()[:160]))
                        if len(hits) >= limit:
                            return
        except OSError:
            pass

    hits = []
    if files:
        for rel in files:
            if os.path.splitext(rel)[1].lower() not in _GREP_EXTS:
                continue
            _scan(os.path.join(project_dir, rel), rel, hits)
            if len(hits) >= limit:
                break
        return hits

    for root, dirs, names in os.walk(project_dir):
        dirs[:] = [d for d in dirs if d not in _GREP_SKIP_DIRS and not d.startswith(".")]
        for fn in names:
            if os.path.splitext(fn)[1].lower() not in _GREP_EXTS:
                continue
            _scan(os.path.join(root, fn), os.path.relpath(os.path.join(root, fn), project_dir), hits)
            if len(hits) >= limit:
                return hits
    return hits


def build_rename_followup(old, new, hits):
    return (
        "Follow-up verification pass for a rename task (%s -> %s). A mechanical "
        "grep found these remaining occurrences of '%s' (file:line: text):\n%s\n\n"
        "Update ONLY the occurrences a user can see (displayed text, titles, labels, "
        "wordmarks - including stylized/UPPERCASE variants). Do NOT rename code "
        "identifiers, CSS classes, CSS custom properties, HTML ids, or file names. "
        "If every remaining occurrence is a code identifier or file name, make no "
        "changes and reply exactly: NO CHANGES NEEDED."
    ) % (old, new, old, "\n".join(hits))


def _build_env():
    """Env that makes opencode resolve the dobby profile + its config."""
    env = dict(os.environ)
    env["USERPROFILE"] = DOBBY_HOME
    env["HOME"] = DOBBY_HOME
    env["HOMEDRIVE"], env["HOMEPATH"] = os.path.splitdrive(DOBBY_HOME)
    env["APPDATA"] = os.path.join(DOBBY_HOME, "AppData", "Roaming")
    env["LOCALAPPDATA"] = os.path.join(DOBBY_HOME, "AppData", "Local")
    env["XDG_CONFIG_HOME"] = os.path.join(DOBBY_HOME, ".config")
    # opencode is non-interactive here; make sure it never tries to page/prompt.
    env["CI"] = "1"
    env["NO_COLOR"] = "1"
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["LC_ALL"] = "C.UTF-8"
    env["LANG"] = "C.UTF-8"
    return env


# Markers that make a directory look like a real project worth listing.
_PROJECT_MARKERS = (
    ".git", "package.json", "pyproject.toml", "requirements.txt", "AGENTS.md",
    "go.mod", "Cargo.toml", "pom.xml", "CMakeLists.txt", "compose.yaml",
    "docker-compose.yml", ".opencode", "tsconfig.json",
)
# Never list/scan these (system/cache/heavy dirs under the home folder);
# dot-folders are skipped wholesale where this is used.
_SKIP_DIRS = {
    "AppData", "Application Data", "Local Settings", "NetHood", "PrintHood",
    "Recent", "SendTo", "Templates", "Cookies", "My Documents", "Start Menu",
    "node_modules", "Saved Games", "Searches", "Contacts", "Favorites",
    "Links", "OneDrive", "__pycache__",
}


def _looks_like_project(path):
    try:
        for marker in _PROJECT_MARKERS:
            if os.path.exists(os.path.join(path, marker)):
                return True
    except OSError:
        pass
    return False


def _extra_project_roots():
    """Operator-configured extra roots (RIG_CODE_PROJECTS=path1;path2)."""
    raw = os.environ.get("RIG_CODE_PROJECTS", "")
    roots = []
    for part in raw.replace(",", ";").split(";"):
        p = part.strip().strip('"')
        if p and os.path.isdir(p):
            roots.append(os.path.abspath(p))
    return roots


def allowed_project_roots():
    """All directories the dashboard is allowed to run a coding agent in:
    this project, the media workspace, any RIG_CODE_PROJECTS entries, and
    project-looking immediate subdirs of the user's home folder."""
    roots = [os.path.abspath(PROJECT_ROOT)]
    if os.path.isdir(MEDIA_WORKSPACE):
        roots.append(os.path.abspath(MEDIA_WORKSPACE))
    roots.extend(_extra_project_roots())
    try:
        for name in sorted(os.listdir(DOBBY_HOME)):
            if name in _SKIP_DIRS or name.startswith("."):
                continue
            full = os.path.join(DOBBY_HOME, name)
            if os.path.isdir(full) and _looks_like_project(full):
                roots.append(os.path.abspath(full))
    except OSError:
        pass
    # de-dupe, keep order
    seen, out = set(), []
    for r in roots:
        key = r.lower()
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


def _project_allowed(project_dir):
    try:
        requested = os.path.abspath(project_dir or PROJECT_ROOT)
    except Exception:
        return False

    for root in allowed_project_roots():
        try:
            root = os.path.abspath(root)
            common = os.path.commonpath([root, requested])
            if os.path.normcase(common) == os.path.normcase(root):
                return True
        except Exception:
            continue
    return False


def _windows_to_wsl_path(path):
    abs_path = os.path.abspath(path)
    drive, tail = os.path.splitdrive(abs_path)
    if not drive:
        return abs_path.replace("\\", "/")
    tail = tail.replace("\\", "/").lstrip("/")
    return "/mnt/%s/%s" % (drive[:1].lower(), tail)


def list_projects():
    """Project allow-list exposed to the dashboard."""
    return allowed_project_roots()


def project_mapping():
    return {
        "windows": PROJECT_ROOT,
        "wsl": _windows_to_wsl_path(PROJECT_ROOT),
        "note": "WSL is the required live runner. Windows OpenCode is disabled for runs on this rig.",
    }


def normalize_runner(runner):
    key = str(runner or RUNNER_MANUAL).strip().lower().replace("-", "_")
    return RUNNER_ALIASES.get(key, RUNNER_MANUAL)


def runner_status(wsl_run_enabled=True):
    wsl_direct_ready = _wsl_direct_ready()
    wsl_bridge = _wsl_bridge_status()
    wsl_ready = wsl_direct_ready or bool(wsl_bridge.get("ready"))
    live_ready = bool(wsl_run_enabled and wsl_ready)
    wsl_status = "ready_preferred" if wsl_direct_ready else (
        "ready_bridge" if wsl_bridge.get("ready") else "not_ready"
    )
    return {
        "recommended_runner": RUNNER_WSL if wsl_run_enabled and wsl_ready else RUNNER_MANUAL,
        "auto_run_default": live_ready,
        "manual_task_file": {
            "id": RUNNER_MANUAL,
            "label": "manual/task_file",
            "status": "safe_default",
            "ready": True,
            "task_dir": os.path.join(PROJECT_ROOT, TASK_DIR_NAME),
            "description": "Writes a reviewable task file and does not launch OpenCode.",
        },
        "wsl": {
            "id": RUNNER_WSL,
            "label": "WSL OpenCode",
            "status": wsl_status,
            "ready": wsl_ready,
            "enabled_for_run": bool(wsl_run_enabled and wsl_ready),
            "path": WSL_OPENCODE,
            "bridge": wsl_bridge,
            "description": "Preferred live runner; edits Windows/shared projects through WSL.",
        },
        "project_mapping": project_mapping(),
    }


def _wsl_available():
    return os.path.isfile(WSL_EXE) or bool(shutil.which("wsl"))


def _wsl_cmd():
    return WSL_EXE if os.path.isfile(WSL_EXE) else "wsl"


def _wsl_direct_ready():
    if not _wsl_available():
        return False
    try:
        res = subprocess.run(
            [_wsl_cmd(), "--", "bash", "-lc", "test -x /usr/local/bin/opencode && command -v git >/dev/null"],
            capture_output=True,
            text=True,
            timeout=12,
            creationflags=_CREATE_NO_WINDOW,
        )
        return res.returncode == 0
    except Exception:
        return False


def _wsl_bridge_status():
    try:
        with open(WSL_BRIDGE_HEARTBEAT, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {"ready": False, "status": "not_running"}
    age = time.time() - float(data.get("time") or 0)
    if age > 10:
        return {"ready": False, "status": "stale", "age_seconds": round(age, 1)}
    return {
        "ready": True,
        "status": "ready_bridge",
        "pid": data.get("pid"),
        "age_seconds": round(age, 1),
    }


def _wsl_bridge_ready():
    return bool(_wsl_bridge_status().get("ready"))


def _wsl_ready():
    return _wsl_direct_ready() or _wsl_bridge_ready()


def _prepare_wsl_path(project_dir):
    wsl_path = _windows_to_wsl_path(project_dir)
    drive = os.path.splitdrive(os.path.abspath(project_dir))[0][:1].lower()
    if drive and drive != "c":
        mount_point = "/mnt/%s" % drive
        drive_name = "%s:" % drive.upper()
        script = (
            "mkdir -p {mount}; "
            "if ! mountpoint -q {mount}; then "
            "mount -t drvfs {drive} {mount} -o uid=1000,gid=1000,metadata; "
            "fi"
        ).format(mount=mount_point, drive=drive_name)
        res = subprocess.run(
            [_wsl_cmd(), "-u", "root", "--", "bash", "-lc", script],
            capture_output=True,
            text=True,
            timeout=30,
            creationflags=_CREATE_NO_WINDOW,
        )
        if res.returncode != 0:
            out = ((res.stdout or "") + (res.stderr or "")).strip()
            raise RuntimeError(out or "could not mount %s in WSL" % drive_name)
    safe = subprocess.run(
        [_wsl_cmd(), "--", "git", "config", "--global", "--add", "safe.directory", wsl_path],
        capture_output=True,
        text=True,
        timeout=12,
        creationflags=_CREATE_NO_WINDOW,
    )
    if safe.returncode != 0:
        out = ((safe.stdout or "") + (safe.stderr or "")).strip()
        raise RuntimeError(out or "could not mark WSL git safe.directory")
    return wsl_path


def _write_manual_task(project_dir, task):
    if not _project_allowed(project_dir):
        raise ValueError("project outside allowed folder: %s" % project_dir)
    project_dir = os.path.abspath(project_dir or PROJECT_ROOT)
    task_dir = os.path.join(project_dir, TASK_DIR_NAME)
    os.makedirs(task_dir, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    path = os.path.join(task_dir, "opencode_task_%s.txt" % stamp)
    body = (
        "OpenCode manual task file\n"
        "Created: %s\n"
        "Project: %s\n"
        "Runner: manual/task_file\n\n"
        "OpenCode was not launched by the dashboard. Review this file before running any agent manually.\n\n"
        "Task:\n%s\n"
    ) % (time.strftime("%Y-%m-%d %H:%M:%S"), project_dir, task.strip())
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(body)
    try:
        from app import messaging
        messaging.send_dashboard_notification(
            "opencode_task_created",
            "OpenCode task file created: %s" % path,
            {"path": path, "project": project_dir},
        )
    except Exception:
        pass
    return path


def _kill_tree(proc):
    try:
        import psutil
        p = psutil.Process(proc.pid)
        for ch in p.children(recursive=True):
            try:
                ch.kill()
            except Exception:
                pass
        p.kill()
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _stream_proc_lines(proc, tick_seconds=5):
    """Yield {'type':'out'} events per stdout line, plus {'type':'tick'}
    heartbeats while the child is silent (model load / long inference can be
    minutes with zero output; without ticks the UI looks frozen)."""
    q = queue.Queue()

    def _reader():
        try:
            for line in proc.stdout:
                q.put(line)
        except Exception:
            pass
        q.put(None)

    t = threading.Thread(target=_reader, daemon=True)
    t.start()
    start = time.time()
    while True:
        try:
            line = q.get(timeout=tick_seconds)
        except queue.Empty:
            yield {"type": "tick", "seconds": int(time.time() - start)}
            continue
        if line is None:
            break
        yield {"type": "out", "text": _ANSI.sub("", line).rstrip("\n")}


def _write_json_atomic(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp.%s" % uuid.uuid4().hex
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=True)
    os.replace(tmp, path)


def _run_wsl_bridge_opencode(project_dir, task):
    bridge = _wsl_bridge_status()
    if not bridge.get("ready"):
        yield {"type": "error", "text": "WSL bridge is not running in the user session."}
        yield {"type": "done", "code": -1}
        return
    run_id = time.strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:8]
    req_path = os.path.join(WSL_BRIDGE_REQUEST_DIR, run_id + ".json")
    log_path = os.path.join(WSL_BRIDGE_RUN_DIR, run_id + ".ndjson")
    cancel_path = os.path.join(WSL_BRIDGE_CANCEL_DIR, run_id + ".cancel")
    done_path = os.path.join(WSL_BRIDGE_RUN_DIR, run_id + ".done.json")
    _write_json_atomic(req_path, {
        "id": run_id,
        "project_dir": os.path.abspath(project_dir),
        "task": task,
        "max_seconds": MAX_SECONDS,
    })

    yield {"type": "start", "project": project_dir, "runner": RUNNER_WSL, "task": task}
    yield {"type": "out", "text": "wsl bridge queued request %s" % run_id}
    offset = 0
    start = time.time()
    saw_done = False
    try:
        while True:
            if os.path.exists(log_path):
                try:
                    with open(log_path, "r", encoding="utf-8") as f:
                        f.seek(offset)
                        chunk = f.read()
                        offset = f.tell()
                except Exception as exc:
                    yield {"type": "error", "text": "WSL bridge log read failed: %s" % exc}
                    yield {"type": "done", "code": -1}
                    return
                for line in chunk.splitlines():
                    if not line.strip():
                        continue
                    try:
                        evt = json.loads(line)
                    except Exception:
                        evt = {"type": "out", "text": line}
                    if evt.get("type") == "done":
                        saw_done = True
                    yield evt
                if saw_done:
                    return
            if os.path.exists(done_path) and not saw_done:
                try:
                    with open(done_path, "r", encoding="utf-8") as f:
                        done = json.load(f)
                    yield {"type": "done", "code": int(done.get("code") or 0)}
                except Exception:
                    yield {"type": "done", "code": -1}
                return
            if time.time() - start > MAX_SECONDS + 30:
                _write_json_atomic(cancel_path, {"time": time.time(), "reason": "dashboard timeout"})
                yield {"type": "error", "text": "WSL bridge run timed out and was cancelled."}
                yield {"type": "done", "code": -1}
                return
            time.sleep(0.5)
    except GeneratorExit:
        _write_json_atomic(cancel_path, {"time": time.time(), "reason": "dashboard stop"})
        raise


def dispatch_bridge_run(project_dir, task, max_seconds=MAX_SECONDS):
    """Queue a WSL-bridge OpenCode run and return its run_id without waiting.

    run_opencode()/_run_wsl_bridge_opencode() stream events back to an HTTP
    response; this just drops the request file and returns, for callers that
    have already answered and cannot block on a 10-30 minute agent run (see
    agent_tools._tool_antons_build). Poll bridge_run_state(run_id) or tail
    .wsl_bridge/runs/<run_id>.ndjson. Returns None if the bridge is not up or
    the project is off the allow-list.
    """
    if not _wsl_bridge_ready():
        return None
    project_dir = os.path.abspath(project_dir or PROJECT_ROOT)
    if not _project_allowed(project_dir):
        return None
    if not str(task or "").strip():
        return None
    os.makedirs(WSL_BRIDGE_REQUEST_DIR, exist_ok=True)
    run_id = time.strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:8]
    _write_json_atomic(os.path.join(WSL_BRIDGE_REQUEST_DIR, run_id + ".json"), {
        "id": run_id,
        "project_dir": project_dir,
        "task": str(task),
        "max_seconds": int(max_seconds or MAX_SECONDS),
    })
    return run_id


def bridge_run_state(run_id):
    """{run_id, done, code, queued, events, tail} for a dispatch_bridge_run id."""
    run_id = str(run_id or "").strip()
    log_path = os.path.join(WSL_BRIDGE_RUN_DIR, run_id + ".ndjson")
    done_path = os.path.join(WSL_BRIDGE_RUN_DIR, run_id + ".done.json")
    events = []
    try:
        with open(log_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except Exception:
                    events.append({"type": "out", "text": line})
    except FileNotFoundError:
        pass
    except Exception:
        pass
    done = None
    code = None
    for evt in events:
        if evt.get("type") == "done":
            done = True
            code = evt.get("code")
    if done is None and os.path.exists(done_path):
        try:
            with open(done_path, "r", encoding="utf-8") as f:
                payload = json.load(f)
            done, code = True, payload.get("code")
        except Exception:
            done, code = True, -1
    text_lines = [str(evt.get("text") or "") for evt in events
                  if evt.get("type") in ("out", "error")
                  and str(evt.get("text") or "").strip()]
    return {
        "run_id": run_id,
        "done": bool(done),
        "code": code,
        "queued": bool(events),
        "events": events,
        "tail": "\n".join(text_lines[-14:]),
    }


def _run_wsl_opencode(project_dir, task):
    if not _wsl_direct_ready():
        if _wsl_bridge_ready():
            for evt in _run_wsl_bridge_opencode(project_dir, task):
                yield evt
            return
        yield {"type": "error", "text": "WSL OpenCode is not ready. Install opencode in WSL and retry."}
        yield {"type": "done", "code": -1}
        return
    try:
        wsl_project = _prepare_wsl_path(project_dir)
    except Exception as exc:
        yield {"type": "error", "text": "WSL prepare failed: %s" % exc}
        yield {"type": "done", "code": -1}
        return

    shell_task = task.replace("'", "'\"'\"'")
    shell_dir = wsl_project.replace("'", "'\"'\"'")
    script = (
        "cd '%s' && "
        "XDG_CONFIG_HOME='%s' HOME='%s' NO_COLOR=1 CI=1 "
        "%s run '%s' --dir '%s' --dangerously-skip-permissions"
    ) % (shell_dir, WSL_CONFIG_HOME, paths.WSL_HOME, WSL_OPENCODE, shell_task, shell_dir)

    try:
        proc = subprocess.Popen(
            [_wsl_cmd(), "--", "bash", "-lc", script],
            cwd=project_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            encoding="utf-8",
            errors="replace",
            creationflags=_CREATE_NO_WINDOW,
        )
    except Exception as e:
        yield {"type": "error", "text": "WSL launch failed: %s" % e}
        yield {"type": "done", "code": -1}
        return

    timed_out = {"v": False}

    def _watchdog():
        timed_out["v"] = True
        _kill_tree(proc)

    timer = threading.Timer(MAX_SECONDS, _watchdog)
    timer.daemon = True
    timer.start()

    yield {"type": "start", "project": project_dir, "runner": RUNNER_WSL, "task": task}
    yield {"type": "out", "text": "wsl runner: live agent"}
    try:
        for evt in _stream_proc_lines(proc):
            yield evt
        proc.wait(timeout=10)
        if timed_out["v"]:
            yield {"type": "error", "text": "run exceeded %ds and was stopped" % MAX_SECONDS}
        yield {"type": "done", "code": proc.returncode}
    except GeneratorExit:
        _kill_tree(proc)
        raise
    finally:
        timer.cancel()
        if proc.poll() is None:
            _kill_tree(proc)


def run_opencode(project_dir, task, runner=None, explicit_request=False):
    """Yield event dicts for manual/task-file, native_windows, or wsl runners."""
    runner_key = normalize_runner(runner)
    project_dir = os.path.abspath(project_dir or PROJECT_ROOT)
    if not _project_allowed(project_dir):
        yield {"type": "error", "text": "project outside allowed folder: %s" % project_dir}
        yield {"type": "done", "code": -1}
        return

    if runner_key == RUNNER_WSL:
        if not explicit_request:
            yield {"type": "error", "text": "WSL OpenCode requires live-run confirmation."}
            yield {"type": "done", "code": -1}
            return
        for evt in _run_wsl_opencode(project_dir, task):
            yield evt
        return

    try:
        task_file = _write_manual_task(project_dir, task)
    except Exception as e:
        yield {"type": "error", "text": "manual task file failed: %s" % e}
        yield {"type": "done", "code": -1}
        return

    yield {"type": "start", "project": project_dir, "runner": RUNNER_MANUAL, "task": task}
    yield {"type": "out", "text": "manual/task_file is the safe default; OpenCode was not launched."}
    yield {"type": "out", "text": "task file: %s" % task_file}
    yield {"type": "out", "text": "review the task file before running any agent manually."}
    yield {"type": "done", "code": 0}
