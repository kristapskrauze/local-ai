"""Film / episode suggestions from the OWN Jellyfin library — no LLM in the path.

Jellyfin runs on a separate media box (see app/hosts.py for its addresses).

NB the Jellyfin host sits in Tailscale's 100.64/10 range, which
`messaging._PRIVATE_HOST_RE` blocks for SSRF reasons — so this deliberately does
NOT go through read_link/_link_is_safe. It has its own client, exactly as
rig_agent has its own path to main.api_stats().

Credentials: reuses the token the project already stores in
`.jellyfin-auth/storage-state.json`. Never logs or returns it.
"""
import json
import logging
import os
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path
from app import hosts
from app.people import OWNER

ROOT = Path(__file__).resolve().parent.parent
AUTH_PATH = ROOT / ".jellyfin-auth" / "storage-state.json"
JELLYFIN_URL = os.environ.get("JELLYFIN_URL", hosts.JELLYFIN_URL)
ANTON_URL = os.environ.get("ANTON_URL", "http://%s:8183" % hosts.MEDIA_BOX_LAN_IP)

_cache = {"auth": None, "lib": {}, "lib_ts": {}, "users": None}
CACHE_TTL = 300.0
# The account to answer as when the asker is unknown. NOT the token's own
# account: that is `k`, an admin with almost no watch history.
DEFAULT_USER = os.environ.get("JELLYFIN_DEFAULT_USER", OWNER)


class MediaError(Exception):
    pass


class NoAccount(MediaError):
    """The asker has no Jellyfin account, so the feature is off for them."""


def _auth():
    """(token, user_id) from the stored browser state. Cached; never printed."""
    if _cache["auth"]:
        return _cache["auth"]
    try:
        data = json.loads(AUTH_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        raise MediaError("no Jellyfin auth stored (%s)" % str(exc)[:60])
    creds = None
    for origin in data.get("origins") or []:
        for kv in origin.get("localStorage") or []:
            if kv.get("name") == "jellyfin_credentials":
                try:
                    creds = json.loads(kv.get("value") or "{}")
                except Exception:
                    creds = None
    servers = (creds or {}).get("Servers") or []
    if not servers or not servers[0].get("AccessToken"):
        raise MediaError("stored Jellyfin credentials have no AccessToken")
    out = (servers[0]["AccessToken"], servers[0].get("UserId") or "")
    _cache["auth"] = out
    return out


def users():
    """{display name: id} for every Jellyfin account. Cached."""
    if _cache.get("users"):
        return _cache["users"]
    try:
        out = {str(u.get("Name")): str(u.get("Id")) for u in _jf("/Users")}
    except Exception:
        out = {}
    if out:
        _cache["users"] = out
    return out


def resolve_user(who=None, contact_names=None):
    """Which Jellyfin account to answer AS."""
    known = users()
    if not known:
        return _auth()[1]
    want = str(who or "").strip()
    # handle -> display name
    if want and want not in known:
        for handle, name in (contact_names or {}).items():
            if str(handle).strip().lower() == want.lower():
                want = str(name)
                break
    for name, uid in known.items():
        if name.lower() == want.lower():
            return uid
    if want:
        return None
    # No asker given at all (dashboard, tests) -> the owner, never the admin.
    for name, uid in known.items():
        if name.lower() == str(DEFAULT_USER).lower():
            return uid
    return _auth()[1]


def _jf(path, timeout=20, **params):
    token, _uid = _auth()
    url = JELLYFIN_URL + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"X-Emby-Token": token,
                                               "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def available():
    """True when the media server answers at all — cheap, unauthenticated."""
    try:
        req = urllib.request.Request(JELLYFIN_URL + "/System/Info/Public")
        with urllib.request.urlopen(req, timeout=6) as r:
            return bool(json.loads(r.read().decode()).get("Id"))
    except Exception:
        return False


_FIELDS = ("Genres,CommunityRating,ProductionYear,UserData,RunTimeTicks,"
           "Overview,OfficialRating,DateCreated")


def _fetch(kind, user_id=None):
    """kind: 'Movie' | 'Series'. Cached per (kind, user) for CACHE_TTL."""
    uid = user_id or resolve_user()
    key = (kind, uid)
    now = time.time()
    if key in _cache["lib"] and now - _cache["lib_ts"].get(key, 0) < CACHE_TTL:
        return _cache["lib"][key]
    res = _jf("/Items", userId=uid, IncludeItemTypes=kind, Recursive="true",
              Fields=_FIELDS, Limit=2000, SortBy="SortName")
    items = res.get("Items") or []
    _cache["lib"][key] = items
    _cache["lib_ts"][key] = now
    return items


def owned_movies(user_id=None):
    """[(normalised title, year)] already in the Jellyfin library.

    Returns None when the library cannot be read. None is NOT "you own nothing":
    the caller must fall back to acquiring everything rather than silently
    skipping films because Jellyfin was down.
    """
    try:
        items = _fetch("Movie", user_id)
    except Exception:
        logging.getLogger(__name__).info("library check unavailable", exc_info=True)
        return None
    out = []
    for it in items or []:
        name = str(it.get("Name") or "").strip()
        if not name:
            continue
        year = it.get("ProductionYear") or ""
        out.append((name, str(year or "")))
    return out


def owned_series(user_id=None):
    """[(title, year)] of SHOWS already in the library, or None if unreadable."""
    try:
        items = _fetch("Series", user_id)
    except Exception:
        logging.getLogger(__name__).info("series library check unavailable",
                                         exc_info=True)
        return None
    out = []
    for it in items or []:
        name = str(it.get("Name") or "").strip()
        if name:
            out.append((name, str(it.get("ProductionYear") or "")))
    return out


def resume_items(user_id=None):
    """Half-watched things — the strongest signal there is."""
    uid = user_id or resolve_user()
    try:
        return (_jf("/Users/%s/Items/Resume" % uid, Limit=20,
                    Fields=_FIELDS).get("Items") or [])
    except Exception:
        return []


def next_up(user_id=None):
    """Next unwatched episode of a series already started."""
    uid = user_id or resolve_user()
    try:
        return (_jf("/Shows/NextUp", userId=uid, Limit=20,
                    Fields=_FIELDS).get("Items") or [])
    except Exception:
        return []


_GENRE_WORDS = {
    "Comedy": ("komēdij", "smiekl", "jautr", "funny", "comedy", "pasmiet",
               "uzjautrin", "smieties"),
    # stems, not whole words: "baisi" failed to match "kaut ko baisu"
    "Horror": ("šausm", "biedē", "horror", "bais", "spok", "briesm"),
    "Thriller": ("trilleri", "trilleris", "thriller", "spriedz", "saspringt"),
    # "asa siž" missed "kaut ko ar asu sižetu" — decline the adjective too
    "Action": ("asa siž", "asu siž", "asāk", "action", "šaušan", "boeviks",
               "darbīb"),
    "Drama": ("drām", "drama", "nopietn", "smagāk"),
    "Romance": ("romant", "mīlas", "romance", "mīlest"),
    "Science Fiction": ("fantastik", "zinātnisk", "sci-fi", "scifi", "kosmos",
                        "space"),
    "Documentary": ("dokument", "documentary", "par dzīvi", "reāl"),
    "Animation": ("multen", "multfilm", "animācij", "animation", "anime"),
    "Family": ("ģimen", "bērn", "family", "kids"),
    "Crime": ("krimin", "crime", "detektīv", "slepkav"),
    "Adventure": ("piedzīvoj", "adventure"),
    "Fantasy": ("fantāzij", "fantasy", "maģij"),
    "Mystery": ("mistik", "mystery", "noslēp"),
    "War": ("kara ", "karš", "war "),
    "Music": ("mūzik", "music", "koncert", "muzikl"),
    "Reality": ("reality", "šovs", "realit"),
}

_GENRE_ALIASES = {
    "Science Fiction": {"Science Fiction", "Sci-Fi & Fantasy"},
    "Fantasy": {"Fantasy", "Sci-Fi & Fantasy"},
    "Action": {"Action", "Action & Adventure"},
    "Adventure": {"Adventure", "Action & Adventure"},
}


def expand_genres(names):
    out = set()
    for g in names or ():
        out |= _GENRE_ALIASES.get(g, {g})
    return out


_SHORT_WORDS = ("īsu", "īss", "ātri", "nav laika", "short", "quick", "īsāk")
_LONG_WORDS = ("gar", "epi", "long", "visu vakaru", "maratons")
_SERIES_WORDS = ("seriāl", "sēriju", "series", "show", "episode", "epizod",
                 "turpin", "skatām tālāk", "next episode")
_MOVIE_WORDS = ("filmu", "filma", "movie", "film", "kino")


_NEGATION_RE = re.compile(
    r"\b(ne|nevis|tikai ne|bez|negribu|nav noskaņ\w*|not|no more|except)\b"
    r"[^.!?]{0,24}$", re.I)


def read_taste(context):
    low = " ".join(str(context or "").lower().split())
    genres, avoid = [], []
    for g, words in _GENRE_WORDS.items():
        for w in words:
            i = low.find(w)
            if i < 0:
                continue
            # "tikai ne šausmas" / "negribu komēdiju" is the OPPOSITE request.
            # Look only at the short run of text immediately before the word.
            before = low[max(0, i - 26):i]
            (avoid if _NEGATION_RE.search(before + " ") else genres).append(g)
            break
    kind = None
    if any(w in low for w in _SERIES_WORDS):
        kind = "Series"
    elif any(w in low for w in _MOVIE_WORDS):
        kind = "Movie"
    length = None
    if any(w in low for w in _SHORT_WORDS):
        length = "short"
    elif any(w in low for w in _LONG_WORDS):
        length = "long"
    return {"genres": genres, "avoid": avoid, "length": length, "kind": kind,
            # every Jellyfin spelling, so callers never re-derive it
            "want_set": expand_genres(genres), "avoid_set": expand_genres(avoid)}


def _minutes(item):
    ticks = item.get("RunTimeTicks") or 0
    return int(ticks / 10_000_000 / 60) if ticks else 0


def score(item, taste):
    """Deterministic and explainable — every term is a reason we can print."""
    why = []
    s = 0.0
    played = bool((item.get("UserData") or {}).get("Played"))
    if not played:
        s += 3.0
        why.append("vēl neskatīts")
    else:
        s -= 4.0
    igen = set(item.get("Genres") or [])
    hit = igen & (taste.get("want_set") or expand_genres(taste.get("genres")))
    if hit:
        s += 4.0 * len(hit)
        why.append("žanrs: " + ", ".join(sorted(hit)))
    dodge = igen & (taste.get("avoid_set") or expand_genres(taste.get("avoid")))
    if dodge:
        s -= 12.0
    rating = item.get("CommunityRating")
    if isinstance(rating, (int, float)):
        s += min(float(rating), 10.0) / 4.0
        if rating >= 7.5:
            why.append("vērtējums %.1f" % rating)
    mins = _minutes(item)
    if taste.get("length") == "short" and 0 < mins <= 100:
        # scale the bonus so 23 min really does beat 95 min, and only CALL it
        # short when it is — "īsa (76 min)" next to a 23-min pick read wrong
        s += 3.0 * (1.0 - min(mins, 100) / 100.0) + 0.5
        if mins <= 60:
            why.append("īsa (%d min)" % mins)
    elif taste.get("length") == "long" and mins >= 130:
        s += 1.5
        why.append("gara (%d min)" % mins)
    year = item.get("ProductionYear") or 0
    if year >= 2015:
        s += 0.4
    return s, why


def merge_taste(question, history):
    """The QUESTION wins; history only fills what the question left unsaid."""
    q = read_taste(question)
    if not history:
        return q
    h = read_taste(history)
    out = dict(q)
    for key in ("genres", "avoid", "kind", "length"):
        if not out.get(key):
            out[key] = h.get(key)
    asked = set(out.get("genres") or [])
    out["avoid"] = [g for g in (out.get("avoid") or []) if g not in asked]
    out["want_set"] = expand_genres(out.get("genres"))
    out["avoid_set"] = expand_genres(out.get("avoid")) - expand_genres(asked)
    return out


def suggest(context="", kind=None, limit=3, include_resume=True, history="",
            who=None, contact_names=None):
    """Ranked picks. Returns dicts; the caller formats them.

    `context` is the question itself; `history` is recent conversation, used
    only to fill gaps. `who` selects WHOSE Jellyfin account the watched /
    continue-watching state comes from. Nothing here calls a model.
    """
    taste = merge_taste(context, history)
    want = kind or taste.get("kind")
    uid = resolve_user(who, contact_names)
    if uid is None:
        raise NoAccount("no Jellyfin account for %r" % (who,))
    personal = True

    # Genres live on the SERIES, not the episode, so a NextUp episode looks
    # genre-less and can never match a request. Look them up once.
    series_genres = {}
    try:
        for s in _fetch("Series", uid):
            series_genres[s.get("Name")] = set(s.get("Genres") or [])
    except Exception:
        pass

    out = []
    if include_resume:
        seen_ids, seen_shows, queue = set(), set(), []
        for it in (next_up(uid) + resume_items(uid)):
            key = it.get("Id") or (it.get("SeriesName"), it.get("Name"))
            show = it.get("SeriesName") or it.get("Name")
            if key in seen_ids or show in seen_shows:
                continue
            seen_ids.add(key)
            seen_shows.add(show)
            queue.append(it)
        for it in queue:
            if want == "Movie" and it.get("Type") != "Movie":
                continue
            if want == "Series" and it.get("Type") == "Movie":
                continue
            label = it.get("SeriesName") or it.get("Name")
            genres = set(it.get("Genres") or []) or series_genres.get(label, set())
            if genres & (taste.get("avoid_set") or set()):
                continue                       # explicitly not wanted
            asked = taste.get("want_set") or set()
            if asked and not (genres & asked):
                continue
            ep = ""
            if it.get("Type") == "Episode":
                ep = "S%02dE%02d" % (it.get("ParentIndexNumber") or 0,
                                     it.get("IndexNumber") or 0)
            base, why = score(dict(it, Genres=sorted(genres)), taste)
            out.append({"title": label, "episode": ep, "name": it.get("Name"),
                        "year": it.get("ProductionYear"), "kind": "continue",
                        "rating": it.get("CommunityRating"),
                        "minutes": _minutes(it), "score": base + 5.0,
                        "why": (["nākamā sērija (%s)" % ep] if ep
                                else ["turpini iesākto"]) + [w for w in why
                                                             if "neskatīts" not in w],
                        "genres": sorted(genres)})
    kinds = [want] if want else ["Movie", "Series"]
    pool = []
    for k in kinds:
        try:
            pool.extend(_fetch(k, uid))
        except MediaError:
            raise
        except Exception:
            continue
    scored = []
    for it in pool:
        sc, why = score(it, taste)
        scored.append({"title": it.get("Name"), "episode": "", "name": it.get("Name"),
                       "year": it.get("ProductionYear"), "kind": it.get("Type"),
                       "rating": it.get("CommunityRating"),
                       "minutes": _minutes(it), "score": sc, "why": why,
                       "genres": it.get("Genres") or []})
    scored.sort(key=lambda x: (-x["score"], str(x["title"])))
    seen = {p["title"] for p in out}
    for p in scored:
        if p["title"] in seen:
            continue
        seen.add(p["title"])
        out.append(p)
        if len(out) >= limit * 3:
            break
    out.sort(key=lambda x: (-x["score"], str(x["title"])))
    return {"taste": taste, "picks": out[:max(1, limit)]}


_GENRE_LV = {
    "Comedy": "komēdija", "Horror": "šausmene", "Thriller": "trilleris",
    "Action": "asa sižeta", "Action & Adventure": "asa sižeta",
    "Drama": "drāma", "Romance": "romantiska", "Crime": "krimiķis",
    "Science Fiction": "fantastika", "Sci-Fi & Fantasy": "fantastika",
    "Fantasy": "fantāzija", "Documentary": "dokumentāla",
    "Animation": "multene", "Family": "ģimenes", "Adventure": "piedzīvojumu",
    "Mystery": "mistika", "War": "par karu", "Music": "par mūziku",
}
# Which genre to NAME when a title has several. Jellyfin returns them
# alphabetically, so Rick and Morty (Action & Adventure, Animation, Comedy,
# Sci-Fi & Fantasy) was described as "asa sižeta" — true, and the least useful
# of the four. Most-telling first.
_GENRE_PRIORITY = ("Horror", "Documentary", "Comedy", "Animation", "Romance",
                   "Mystery", "Thriller", "Science Fiction", "Sci-Fi & Fantasy",
                   "Fantasy", "War", "Music", "Crime", "Family", "Adventure",
                   "Action & Adventure", "Action", "Drama")


def _headline_genre(genres):
    for g in _GENRE_PRIORITY:
        if g in (genres or ()) and g in _GENRE_LV:
            return _GENRE_LV[g]
    for g in (genres or ()):
        if g in _GENRE_LV:
            return _GENRE_LV[g]
    return ""


def _dur_lv(mins):
    """'23 min' / '2h 21min' — nobody says '141 min'."""
    if not mins:
        return ""
    if mins < 90:
        return "%d min" % mins
    h, m = divmod(int(mins), 60)
    return "%dh %02dmin" % (h, m) if m else "%dh" % h


def format_reply(result, lang="lv"):
    picks = (result or {}).get("picks") or []
    if not picks:
        return "Neatradu neko, ko vērts skatīties 😕"
    asked = (result or {}).get("taste", {}).get("want_set") or set()

    lines = []
    for p in picks:
        bits = []
        if p.get("episode"):
            head = "%s — nākamā sērija %s" % (p["title"], p["episode"])
            if p.get("name") and p["name"] != p["title"]:
                head += ' "%s"' % p["name"]
        else:
            head = p["title"]
            if p.get("kind") == "continue":
                bits.append("jau iesākta")
            elif not p.get("unknown_user"):
                bits.append("neskatīta")
        if not asked:
            gl = _headline_genre(p.get("genres"))
            if gl:
                bits.append(gl)
        dur = _dur_lv(p.get("minutes"))
        if dur:
            bits.append(dur)
        r = p.get("rating")
        if isinstance(r, (int, float)):
            if r >= 8.3:
                bits.append("ļoti laba")
            elif r >= 7.5:
                bits.append("laba")
        lines.append(head + ((", " + ", ".join(bits)) if bits else ""))

    if len(lines) == 1:
        p = picks[0]
        # "Noskaties X, jau iesākta" is contradictory — you resume what you began
        verb = "Turpini" if (p.get("kind") == "continue" or p.get("episode")) \
            else "Noskaties"
        return "%s %s" % (verb, lines[0].replace(", jau iesākta", "", 1))
    return "Varētu paskatīties:\n" + "\n".join(lines)
