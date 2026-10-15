# -*- coding: utf-8 -*-
"""ss.com classifieds: search flats/houses/cars, and watch a search over time.

THE MODEL RESOLVES THE REQUEST, THIS MODULE FETCHES IT. "atrodi dažus dzīvokļus
Torņkalnā īrei" is not parsed here — the agent tool receives `kind="flats"`,
`area="Torņakalns"`, `deal="rent"` because the model worked that out. Nothing in
this file tries to understand a sentence.
"""
import difflib
import gzip
import html as _html
import json
import logging
import re
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

LOGGER = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
WATCH_PATH = ROOT / ".messaging_history" / "ss_watches.json"

BASE = "https://www.ss.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")
TIMEOUT = 25
# be a polite guest: one page at a time, and never hammer on a watch loop
_MIN_INTERVAL = 1.5
_last_fetch = [0.0]
_fetch_lock = threading.Lock()

# section -> (path, list-page suffix per deal)
_SECTIONS = {
    "flats": "/lv/real-estate/flats/riga/",
    "houses": "/lv/real-estate/homes-summer-residences/riga-region/",
    "land": "/lv/real-estate/plots-and-lands/riga/",
    "cars": "/lv/transport/cars/",
}
_DEALS = {"sell": "sell", "rent": "hand_over", "exchange": "exchange",
          "maina": "exchange", "buy": "buy"}

_cache = {"pages": {}, "lists": {}}
_CACHE_TTL = 300


class SSError(Exception):
    pass


class SSChoice(SSError):
    """ss.com publishes several pages for what was named and none of them is
    THE one. Carries the candidates so the caller picks, instead of this layer
    picking for it and quietly narrowing the question."""

    def __init__(self, message, needs, choices):
        SSError.__init__(self, message)
        self.needs = str(needs)
        self.choices = list(choices)


def _fetch(path, ttl=_CACHE_TTL):
    """One page, cached briefly and rate-limited. Read-only, always GET."""
    url = path if path.startswith("http") else BASE + path
    hit = _cache["pages"].get(url)
    now = time.time()
    if hit and now - hit[0] < ttl:
        return hit[1]
    with _fetch_lock:
        wait = _MIN_INTERVAL - (time.time() - _last_fetch[0])
        if wait > 0:
            time.sleep(wait)
        _last_fetch[0] = time.time()
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept-Language": "lv,en;q=0.8",
        "Accept-Encoding": "gzip"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            raw = r.read()
            if r.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            html = raw.decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        raise SSError("ss.com returned HTTP %s" % exc.code)
    except Exception as exc:
        raise SSError("ss.com unreachable: %s" % str(exc)[:90])
    _cache["pages"][url] = (now, html)
    return html


def _fold(value):
    """Latvian-insensitive compare key: Torņakalns == tornakalns == Tornjakalns."""
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower().replace("j", "").replace("ķ", "k")
    return re.sub(r"[^a-z0-9]+", "", text)


_NOT_A_CATEGORY = ("search", "today", "new", "rss")


def _options(section_path, pattern):
    """[(display name, slug)] read off the section's own page."""
    key = section_path + pattern
    hit = _cache["lists"].get(key)
    if hit and time.time() - hit[0] < 3600:
        return hit[1]
    html = _fetch(section_path, ttl=3600)
    out, seen = [], set()
    for slug, name in re.findall(pattern, html):
        name = " ".join(re.sub(r"<[^>]+>", " ", name).split())
        if not name or slug in seen or slug in _NOT_A_CATEGORY:
            continue
        seen.add(slug)
        out.append((name, slug))
    _cache["lists"][key] = (time.time(), out)
    return out


# ss.com runs two templates: the flats index and some make pages tag their
# category links class="a_category", the BMW-style pages do not. Anchoring on
# that class read zero models off /cars/bmw/, so links are taken by shape and
# the site's fixed utility endpoints are dropped by name in _options instead.
_CATEGORY = r'href="%s([a-z0-9\-]+)/"[^>]*>([^<]{1,60})<'


def districts(section="flats"):
    path = _SECTIONS.get(section) or _SECTIONS["flats"]
    return _options(path, _CATEGORY % re.escape(path))


def car_makes():
    return _options(_SECTIONS["cars"], _CATEGORY % re.escape(_SECTIONS["cars"]))


def car_models(make_slug):
    """The generations and variants ss.com actually publishes for one make.

    ss.com has no /volkswagen/golf/ page -- it has golf-1 through golf-8, plus
    Plus, Variant and Sportsvan. Folding "Golf" into a slug produced a 404 that
    parsed as zero rows, which read as "no Golfs for sale in Latvia".
    """
    path = "%s%s/" % (_SECTIONS["cars"], str(make_slug or "").strip("/"))
    return _options(path, _CATEGORY % re.escape(path))


def _vocab(options):
    """folded name -> (display, slug), for both what ss.com shows and what it
    puts in the URL, so either spelling of a name finds the page."""
    vocab = {}
    for display, slug in options:
        for key in (_fold(display), _fold(slug)):
            if key:
                vocab.setdefault(key, (display, slug))
    return vocab


def resolve_slug(name, options):
    """What the model named -> the site's own slug. AUTOCORRECT, not a ladder.

    One step: compare against the vocabulary ss.com itself publishes and take
    the nearest entry. An identical name scores 1.0 and wins on its own, so
    there is no separate exact case; "Torņkalns" lands on Torņakalns because it
    is the nearest real district, not because a rule was written for it.

    Returns "" when nothing is close enough, so the caller says "no such
    district" instead of fetching a 404 and reporting "no listings".
    """
    want = _fold(name)
    if not want:
        return ""
    vocab = _vocab(options)
    if want in vocab:
        return vocab[want][1]
    near = difflib.get_close_matches(want, list(vocab), n=1, cutoff=0.78)
    return vocab[near[0]][1] if near else ""


def resolve_choice(name, options, limit=8):
    """(slug, [candidate names]) -- a slug when ss.com has exactly one page for
    what was named, otherwise the pages it does publish.
    """
    want = _fold(name)
    if not want:
        return "", []
    vocab = _vocab(options)
    if want in vocab:
        return vocab[want][1], []
    near = difflib.get_close_matches(want, list(vocab), n=max(2, limit), cutoff=0.78)
    picked, names = [], []
    for key in near:
        display, slug = vocab[key]
        if slug not in picked:
            picked.append(slug)
            names.append(display)
    if len(picked) == 1:
        return picked[0], []
    return "", names[:limit]


_ROW_RE = re.compile(r'<tr id="tr_(\d+)".*?</tr>', re.S)
_CELL_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.S)
_HREF_RE = re.compile(r'href="(/msg/[^"]+)"')
_NUM_RE = re.compile(r"(\d[\d\s,]*)")


def _text(html):
    txt = _html.unescape(re.sub(r"<[^>]+>", " ", html))
    return " ".join(txt.replace("\xa0", " ").split())


def _price_eur(cell):
    """The number, when the cell carries one. 'no price' stays None, never 0."""
    txt = _text(cell).replace(" ", " ")
    m = _NUM_RE.search(txt.replace(" ", ""))
    if not m:
        return None
    try:
        return int(m.group(1).replace(",", ""))
    except ValueError:
        return None


def _post(path, data, referer="", expect="", tries=3):
    """A filtered search: ss.com keeps the filter in the session, so this is a
    POST on a cookie-carrying opener rather than a URL that can be linked.

    It drops the filter fairly often -- measured 2 kept out of 6 -- and answers
    with the plain listing, which looks like a good result and is not one. On
    the SAME session a repeat sticks (9 of 9), so `expect` names a fragment the
    landing URL must contain and the post is repeated on that one session until
    it does. Building a fresh session per attempt is what made it look hopeless.
    """
    import http.cookiejar

    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    opener.addheaders = [("User-Agent", UA), ("Accept-Language", "lv,en;q=0.8")]
    with _fetch_lock:
        wait = _MIN_INTERVAL - (time.time() - _last_fetch[0])
        if wait > 0:
            time.sleep(wait)
        _last_fetch[0] = time.time()
    body = urllib.parse.urlencode(data, doseq=True).encode()
    landed, html = "", ""
    try:
        opener.open(BASE + (referer or path), timeout=TIMEOUT).read()
        for attempt in range(max(1, int(tries))):
            req = urllib.request.Request(
                BASE + path, data=body,
                headers={"Content-Type": "application/x-www-form-urlencoded",
                         "Referer": BASE + (referer or path)})
            with opener.open(req, timeout=TIMEOUT) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                landed, html = resp.geturl(), raw.decode("utf-8", "replace")
            if not expect or expect in landed:
                break
            time.sleep(1.5)
        return landed, html
    except urllib.error.HTTPError as exc:
        raise SSError("ss.com returned %s" % exc.code)
    except Exception as exc:
        raise SSError("ss.com is not answering (%s)" % type(exc).__name__)


_CAT_RE = re.compile(r'<div class="ads_cat_names">(.*?)</div>', re.S)
_D1_RE = re.compile(r'<div class=d1>(.*?)</div>', re.S)


def _parse_any(html):
    """Rows from a search across sections, where there is no fixed column
    layout: a breadcrumb, the advert text, and a price that may read 'pērku'."""
    out = []
    for row in re.findall(r'<tr id="tr_\d+".*?</tr>', html, re.S):
        rid = re.search(r'<tr id="tr_(\d+)"', row)
        href = _HREF_RE.search(row)
        if not rid or not href:
            continue
        cells = _CELL_RE.findall(row)
        body = _D1_RE.search(row)
        cat = _CAT_RE.search(row)
        # /msg/lv/electronics/computers/pc/fx.html -> /lv/electronics/computers/pc/
        section = re.sub(r"^/msg", "", href.group(1))
        section = section[:section.rfind("/") + 1]
        out.append({
            "id": rid.group(1), "url": BASE + href.group(1), "section": section,
            "title": _text(body.group(1) if body else (cells[2] if len(cells) > 2 else ""))[:180],
            "category": _text(cat.group(1)) if cat else "",
            "price": _price_eur(cells[-1]) if cells else None,
            "price_text": _text(cells[-1]) if cells else "",
        })
    return out


def _parse_rows(html, kind):
    if kind == "any":
        return _parse_any(html)
    out = []
    for block in _ROW_RE.findall(html):
        full = re.search(r'<tr id="tr_%s".*?</tr>' % re.escape(block), html, re.S)
        if not full:
            continue
        row = full.group(0)
        cells = [c for c in _CELL_RE.findall(row)]
        if len(cells) < 6:
            continue
        href = _HREF_RE.search(row)
        item = {
            "id": block,
            "url": (BASE + href.group(1)) if href else "",
            "title": _text(cells[2])[:180],
        }
        if kind == "cars":
            _last = _text(cells[-1])
            _is_swap = "maiņ" in _last.lower()
            if _is_swap and len(cells) >= 7:
                item.update({
                    "model": _text(cells[3]) if len(cells) >= 9 else "",
                    "year": _text(cells[-5]),
                    "engine": _text(cells[-4]),
                    "mileage": _text(cells[-3]),
                    "wants_in_exchange": _text(cells[-2]),
                    "price": _price_eur(cells[-1]),
                    "price_text": _last.replace(" maiņai", "").strip(),
                    "deal_page": "exchange",
                })
            else:
                item.update({
                    "model": _text(cells[3]) if len(cells) >= 8 else "",
                    "year": _text(cells[-4]),
                    "engine": _text(cells[-3]),
                    "mileage": _text(cells[-2]),
                    "price": _price_eur(cells[-1]),
                    "price_text": _last,
                })
        else:
            item.update({
                "street": _text(cells[3]), "rooms": _text(cells[4]),
                "area_m2": _text(cells[5]), "floor": _text(cells[6]) if len(cells) > 6 else "",
                "series": _text(cells[7]) if len(cells) > 7 else "",
                "price": _price_eur(cells[9]) if len(cells) > 9 else None,
                "price_text": _text(cells[9]) if len(cells) > 9 else "",
            })
        out.append(item)
    return out


def filter_options(path):
    """Every choice ss.com's own filter form offers on this page.

    [(field, display, form field name, value)] -- read off the page, so a word
    the model uses ("Kabriolets", "Dīzelis", "Automāts") is matched against what
    the site actually publishes instead of against a table of ids kept here.
    """
    key = "filter:" + path
    hit = _cache["lists"].get(key)
    if hit and time.time() - hit[0] < 3600:
        return hit[1]
    html = _fetch(path, ttl=3600)
    out = []
    for m in re.finditer(r'<select[^>]*name="(opt\[\d+\])"[^>]*>(.*?)</select>',
                         html, re.S):
        window = html[max(0, m.start() - 120):m.start()]
        window = window[:window.rfind("<")] if "<" in window else window
        words = _text(window).rstrip(": ").split()
        field = words[-1].rstrip(":") if words else m.group(1)
        for value, display in re.findall(
                r'<option[^>]*value="([^"]*)"[^>]*>([^<]*)<', m.group(2)):
            display = " ".join(display.split())
            if value and display:
                out.append((field, display, m.group(1), value))
    _cache["lists"][key] = (time.time(), out)
    return out


def numeric_fields(path):
    """{label -> topt id} for the min/max boxes, e.g. Cena and Gads. The id
    differs per section, so it is read from the page rather than remembered."""
    html = _fetch(path, ttl=3600)
    found = {}
    for m in re.finditer(r'id="f_o_(\d+)_min"', html):
        window = html[max(0, m.start() - 200):m.start()]
        window = window[:window.rfind("<")] if "<" in window else window
        label = _text(window).rstrip(": ").split()
        if label:
            found[label[-1].rstrip(":")] = m.group(1)
    return found


def _filter_form(path, options=(), min_price=None, max_price=None,
                 min_year=None, max_year=None, min_area=None, max_area=None,
                 min_floor=None, max_floor=None):
    data = {}
    if options:
        published = filter_options(path)
        # every option on the page, keyed by the words ss.com shows for it, so
        # "kabriolets" finds the body-type select without this file knowing
        # which numbered select that is
        pairs = [(display, "%s=%s" % (name, value))
                 for _f, display, name, value in published]
        for want in options:
            hit = resolve_slug(want, pairs)
            if not hit:
                raise SSChoice(
                    "ss.com's filter has no option called %r" % str(want)[:30],
                    "option", sorted({d for _f, d, _n, _v in published}))
            name, value = hit.split("=", 1)
            data[name] = value
    _wanted = (min_price or max_price or min_year or max_year
               or min_area or max_area or min_floor or max_floor)
    nums = numeric_fields(path) if _wanted else {}
    for field_id, low, high in ((nums.get("Cena"), min_price, max_price),
                                (nums.get("Gads"), min_year, max_year),
                                (nums.get("Platība"), min_area, max_area),
                                (nums.get("Stāvs"), min_floor, max_floor)):
        if not field_id:
            continue
        if low:
            data["topt[%s][min]" % field_id] = str(int(low))
        if high:
            data["topt[%s][max]" % field_id] = str(int(high))
    return data


def site_search(text, path="", limit=8):
    """Search by words -- the only way to reach a Mac mini, a guitar or a
    fridge, none of which live under a make or a district.

    Every section publishes its own search, and scoping matters: "mac mini"
    across the whole site returns page after page of repair shops, while the
    same words under /electronics/computers/pc/ return the actual computer.
    Each result carries its category breadcrumb, so the caller can read where
    the real ones live and search there.
    """
    words = " ".join(str(text or "").split())
    if not words:
        raise SSError("nothing to search for")
    root = str(path or "/lv/")
    if not root.endswith("/"):
        root += "/"
    url = root + "search-result/?q=" + urllib.parse.quote(words)
    rows = _parse_any(_fetch(url))
    try:
        limit = max(1, min(int(limit), 20))
    except (TypeError, ValueError):
        limit = 8
    spots = {}
    for row in rows:
        spot = row.get("section") or ""
        if spot:
            spots.setdefault(spot, [0, row.get("category") or ""])
            spots[spot][0] += 1
    return {"url": BASE + url, "count": len(rows), "results": rows[:limit],
            "sections": [{"path": p, "adverts": n, "category": name}
                         for p, (n, name) in
                         sorted(spots.items(), key=lambda kv: -kv[1][0])][:8]}


def sections(path="/lv/"):
    """What ss.com publishes under a path, so the caller can walk its own
    taxonomy down to the section a thing actually lives in."""
    root = str(path or "/lv/")
    if not root.endswith("/"):
        root += "/"
    return [(display, root + slug + "/")
            for display, slug in _options(root, _CATEGORY % re.escape(root))]


def build_url(kind, area="", deal="", make="", model=""):
    """The listing URL, from slugs that were looked up — or SSError saying which
    name could not be found, so the reply can name it instead of guessing."""
    kind = str(kind or "flats").strip().lower()
    if kind == "cars":
        path = _SECTIONS["cars"]
        if make:
            slug = resolve_slug(make, car_makes())
            if not slug:
                raise SSError("ss.com has no car make called %r" % str(make)[:40])
            path += slug + "/"
            if model:
                mslug, options = resolve_choice(model, car_models(slug))
                if not mslug:
                    raise SSChoice(
                        "ss.com publishes %s %s as separate pages"
                        % (str(make)[:24], str(model)[:24]), "model",
                        options or [d for d, _s in car_models(slug)])
                path += mslug + "/"
        return path + _DEALS.get(str(deal or "sell").strip().lower(), "sell") + "/"
    section = kind if kind in _SECTIONS else "flats"
    path = _SECTIONS[section]
    if area:
        slug = resolve_slug(area, districts(section))
        if not slug:
            raise SSError("ss.com has no Riga district called %r" % str(area)[:40])
        path += slug + "/"
    want = _DEALS.get(str(deal or "").strip().lower(), "")
    if want:
        path += want + "/"
    return path


def missing_choice(kind, area="", make=""):
    """Which name ss.com still needs before there is a page of adverts to read."""
    kind = str(kind or "flats").strip().lower()
    if kind == "cars":
        return "" if str(make or "").strip() else "make"
    return "" if str(area or "").strip() else "area"


def choice_names(kind):
    """The site's own list of makes or districts, for the caller to choose from."""
    kind = str(kind or "flats").strip().lower()
    pairs = car_makes() if kind == "cars" else districts(kind)
    return [display for display, _slug in pairs]


def _merge_rows(rows, extra, deal_page=""):
    """Add rows from another deal page, keeping order and dropping repeats."""
    seen = {str(r.get("url") or "") for r in rows}
    for row in extra or []:
        url = str(row.get("url") or "")
        if not url or url in seen:
            continue
        seen.add(url)
        row = dict(row)
        if deal_page:
            # so an answer can say "this one is a trade advert"
            row["deal_page"] = deal_page
        rows.append(row)
    return rows


def search(kind="flats", area="", deal="", make="", model="",
           max_price=None, min_price=None, min_year=None, max_year=None,
           min_area=None, max_area=None, min_floor=None, max_floor=None,
           options=(), limit=8):
    """Listings, newest first as ss.com orders them. Never invents a row."""
    path = build_url(kind, area=area, deal=deal, make=make, model=model)
    _also = []
    if kind == "cars" and not str(deal or "").strip():
        _alt = build_url(kind, area=area, deal="exchange", make=make, model=model)
        if _alt and _alt != path:
            _also.append(_alt)
    try:
        limit = max(1, min(int(limit), 20))
    except (TypeError, ValueError):
        limit = 8
    form = _filter_form(path, options=options or (), min_price=min_price,
                        max_price=max_price, min_year=min_year, max_year=max_year,
                        min_area=min_area, max_area=max_area,
                        min_floor=min_floor, max_floor=max_floor)
    _asked = [str(o) for o in (options or []) if str(o or "").strip()]
    for _label, _lo, _hi, _unit in (("cena", min_price, max_price, "EUR"),
                                    ("gads", min_year, max_year, ""),
                                    ("platība", min_area, max_area, "m2"),
                                    ("stāvs", min_floor, max_floor, "")):
        if _lo and _hi:
            _asked.append("%s %s-%s %s" % (_label, _lo, _hi, _unit))
        elif _hi:
            _asked.append("%s līdz %s %s" % (_label, _hi, _unit))
        elif _lo:
            _asked.append("%s no %s %s" % (_label, _lo, _unit))
    # an index page is only unbrowsable when nothing is filtering it: ask
    # /transport/cars/ for cabriolets between 2000 and 3000 and ss.com answers
    # across every make at once, which is the whole point of asking that way
    need = "" if form else missing_choice(kind, area=area, make=make)
    if need:
        raise SSError("ss.com lists %s by %s, and none was given"
                      % ("cars" if str(kind).lower() == "cars" else "property", need))
    if form:
        # ss.com drops a filter now and then and answers with the plain listing
        # instead, which looks like a perfectly good result and is not one: a
        # 7,100 EUR BMW came back from a search capped at 4,000. The landing URL
        # is the tell -- a kept filter stays on .../filter/ -- so it is checked
        # rather than trusted, and _post retries on the same session until it
        # sticks.
        url, html = _post(path + "filter/", form, path, expect="/filter/")
        kept = "/filter/" in url
        rows = _parse_rows(html, "cars" if kind == "cars" else "flats") if kept else []
        if not kept:
            price_only = all(k.startswith("topt[") for k in form)
            if not price_only:
                # a body type or a fuel is not something this side can apply, so
                # saying so beats handing back a page of unrelated cars
                raise SSError("ss.com would not keep that filter just now")
            # walking pages instead only works where there ARE pages of adverts:
            # /transport/cars/sell/ is still an index, and reading it as an empty
            # market is the lie this whole path exists to avoid
            still_need = missing_choice(kind, area=area, make=make)
            if still_need:
                raise SSError(
                    "ss.com would not keep that filter, and it lists %s by %s"
                    % ("cars" if str(kind).lower() == "cars" else "property",
                       still_need))
            form = {}
        else:
            for _extra in _also:
                try:
                    _u2, _h2 = _post(_extra + "filter/", form, _extra,
                                     expect="/filter/")
                    if "/filter/" in _u2:
                        rows = _merge_rows(rows, _parse_rows(
                            _h2, "cars" if kind == "cars" else "flats"),
                            deal_page="exchange")
                except SSError:
                    pass
            return {"url": url, "count": len(rows), "results": rows[:limit],
                    "also_searched": _also,
                    "filtered": sorted(form),
                    "filter_applied": bool(form),
                    "filter_means": (
                        "ss.com applied this filter: %s. EVERY row below "
                        "already matches it — do not judge which ones do."
                        % ", ".join(_asked)) if form else
                        "no filter was applied: these are the newest adverts "
                        "on the page, in ss.com's own order"}
    cap = None
    if max_price:
        try:
            cap = int(max_price)
        except (TypeError, ValueError):
            cap = None
    floor = None
    if min_price:
        try:
            floor = int(min_price)
        except (TypeError, ValueError):
            floor = None
    rows = []
    page = 1
    while True:
        chunk = _parse_rows(
            _fetch(path if page == 1 else path + "page%d.html" % page),
            "cars" if kind == "cars" else "flats")
        raw = len(chunk)
        if cap is not None:
            chunk = [r for r in chunk if r.get("price") is None or r["price"] <= cap]
        if floor is not None:
            chunk = [r for r in chunk if r.get("price") is None or r["price"] >= floor]
        rows.extend(chunk)
        page += 1
        if (cap is None and floor is None) or not raw or len(rows) >= limit \
                or page > 8:
            break
    for _extra in _also:
        try:
            rows = _merge_rows(rows, _parse_rows(
                _fetch(_extra), "cars" if kind == "cars" else "flats"),
                deal_page="exchange")
        except SSError:
            pass
    if cap is not None:
        rows = [r for r in rows if not r.get("price") or int(r["price"]) <= cap]
    if floor is not None:
        rows = [r for r in rows if not r.get("price") or int(r["price"]) >= floor]
    return {"url": BASE + path, "count": len(rows), "results": rows[:limit],
            "also_searched": _also,
            "scanned_pages": page - 1, "whole_market": False}


# the body ends at the attributes table, NOT at the first </div> -- an empty
# nested div sits at the top of it, so a lazy match to </div> captured nothing
_DESC_RE = re.compile(
    r'<div id="msg_div_msg">(.*?)(?:<table[^>]*class="options_list"|<div id="mnu_)', re.S)
_OPT_ROW_RE = re.compile(
    r'class="ads_opt_name"[^>]*>(.*?)</td>.*?class="ads_opt"[^>]*>(.*?)</td>', re.S)
_PHOTO_RE = re.compile(r'https://i\.ss\.com/gallery/[^"\s]+\.800\.jpg')


def listing(url):
    """One advert in full: what it says, what it lists, and its photos."""
    path = str(url or "")
    if not path.startswith("http") and not path.startswith("/"):
        raise SSError("that is not an ss.com advert link")
    html = _fetch(path.replace(BASE, "") if path.startswith("http") else path)
    body = _DESC_RE.search(html)
    text = ""
    if body:
        text = _text(re.sub(r"<div[^>]*id=\"content_sys_div_msg\"[^>]*>.*?</div>",
                            " ", body.group(1), flags=re.S))
    attrs = {}
    for name, value in _OPT_ROW_RE.findall(html):
        key, val = _text(name).rstrip(":"), _text(value)
        if key and val and key not in attrs:
            attrs[key] = val[:80]
    price = re.search(r'class="ads_price"[^>]*>(.*?)</td>', html, re.S)
    title = re.search(r"<title>(.*?)</title>", html, re.S)
    # THE APRĪKOJUMS BLOCK. ss.com prints the whole option catalogue and marks
    # the ones this car has in bold; the plain ones are what it does NOT have.
    # Without the distinction every car looks like it has 4x4.
    equipment = []
    for name in re.findall(r'<b class="auto_c">(.*?)</b>', html, re.S):
        name = _text(name)
        if name and name not in equipment:
            equipment.append(name)
    photos = []
    for src in _PHOTO_RE.findall(html):
        if src not in photos:
            photos.append(src)
    return {
        "url": path if path.startswith("http") else BASE + path,
        "title": _text(title.group(1))[:140] if title else "",
        "price_text": _text(price.group(1))[:40] if price else "",
        "description": text[:1800],
        "attributes": attrs,
        "equipment": equipment,
        # the whole gallery: a flat's bathroom is photographed last, and the
        # old cap of 8 hid it on every advert with more than eight pictures
        "photos": photos[:30],
    }


def price_history(url):
    """What tirgusdati.lv remembers about an advert's price."""
    ad = str(url or "")
    if not ad.startswith("http"):
        raise SSError("that is not an advert link")
    page = "https://www.tirgusdati.lv/vesture?q=" + urllib.parse.quote(ad, safe="")
    try:
        html = _fetch(page, ttl=900)
    except SSError as exc:
        raise SSError("tirgusdati is not answering (%s)" % str(exc)[:40])
    flat = _text(re.sub(r"<script.*?</script>", " ", html, flags=re.S))

    def grab(label, _stop=0):
        """The number that follows a label, cut at its unit -- reading a fixed
        window instead swallowed the next label ("18 200 EUR Sākotnēj")."""
        i = flat.find(label)
        if i < 0:
            return ""
        tail = flat[i + len(label):i + len(label) + 40].strip()
        m = re.match(r"(-?\s*[\d\s]{1,12})(EUR|dienas|diena|dienu)", tail)
        if m:
            return " ".join(("%s %s" % (m.group(1), m.group(2))).split())
        return " ".join(tail.split())[:24]

    readings = re.findall(r"(\d{2}\.\d{2}\.\d{4})[^\d]{0,12}(\d[\d\s]*)\s*EUR", flat)
    return {
        "url": page, "advert": ad,
        "current": grab("Pašreizējā cena", 20),
        "first": grab("Sākotnējā cena", 20),
        "change": grab("Izmaiņa", 20),
        "days_listed": grab("Pārdošanā", 20),
        "readings": [{"date": d, "price": int(re.sub(r"\D", "", p) or 0)}
                     for d, p in readings[:12]],
        "note": ("tirgusdati starts tracking an advert when it is first looked "
                 "up, so a single reading means it is new to them, not that the "
                 "price never moved"),
    }


def run_query(**query):
    """One search, whichever kind it is."""
    words = str(query.pop("text", "") or "")
    where = str(query.pop("path", "") or "")
    if words:
        return site_search(words, path=where, limit=query.get("limit") or 8)
    return search(**query)


_SEEN_URLS = {}
_SEEN_ROWS = {}
_SEEN_TTL = 3600


def remember_urls(conversation_id, urls, rows=()):
    """Record the adverts a tool actually returned for this chat.

    Keeps the row, not just the link, so a later turn that talks about "the
    BMW 116" can be given that listing's real URL rather than a guess.
    """
    key = str(conversation_id or "")
    if not key:
        return
    now = time.time()
    bucket = _SEEN_URLS.setdefault(key, {})
    for url in urls or []:
        u = str(url or "").strip()
        if u.startswith("http"):
            bucket[u] = now
    seen_rows = _SEEN_ROWS.setdefault(key, [])
    for row in rows or []:
        if not isinstance(row, dict) or not str(row.get("url") or "").startswith("http"):
            continue
        seen_rows.append({
            "url": row.get("url"), "title": str(row.get("title") or "")[:120],
            "model": str(row.get("model") or ""), "year": str(row.get("year") or ""),
            "price": row.get("price"), "ts": now})
    _SEEN_ROWS[key] = [r for r in seen_rows if now - r["ts"] < _SEEN_TTL][-60:]
    for u, seen in list(bucket.items()):
        if now - seen > _SEEN_TTL:
            bucket.pop(u, None)
    if len(_SEEN_URLS) > 40:
        _SEEN_URLS.clear()
        _SEEN_URLS[key] = bucket


def recent_rows(conversation_id, max_age=None):
    """What this chat was shown from ss.com recently, newest first."""
    now = time.time()
    ttl = float(max_age or _SEEN_TTL)
    rows = [r for r in (_SEEN_ROWS.get(str(conversation_id or "")) or [])
            if now - float(r.get("ts") or 0) < ttl]
    return list(reversed(rows))


def listing_for(conversation_id, text):
    """The advert this sentence is talking about, if this chat has seen it.

    Matched on the things that identify a car in a text message and cannot be
    approximated: its price, then its model and year. Returns None rather than
    a guess -- a wrong link is worse than none.
    """
    rows = _SEEN_ROWS.get(str(conversation_id or "")) or []
    if not rows or not str(text or "").strip():
        return None
    body = str(text)
    numbers = {int(n.replace(" ", "").replace(",", ""))
               for n in re.findall(r"\d[\d\s,]{2,8}", body)}
    for row in reversed(rows):
        if row.get("price") and row["price"] in numbers:
            return row
    for row in reversed(rows):
        model, year = str(row.get("model") or ""), str(row.get("year") or "")
        if model and year and re.search(r"\b%s\b" % re.escape(model), body) \
                and year in body:
            return row
    return None


def url_was_fetched(conversation_id, url):
    """True when this exact link came back from a search in this chat."""
    bucket = _SEEN_URLS.get(str(conversation_id or "")) or {}
    u = str(url or "").strip().rstrip(").,")
    if u in bucket:
        return True
    return any(u.rstrip("/") == k.rstrip("/") for k in bucket)


ALIAS_PATH = ROOT / ".messaging_history" / "make_aliases.json"


def load_aliases():
    """{folded word -> the make ss.com knows it by}."""
    try:
        raw = json.loads(ALIAS_PATH.read_text(encoding="utf-8"))
    except Exception:
        raw = {}
    out = {}
    if isinstance(raw, dict):
        for word, make in raw.items():
            key = _fold(word)
            if key and str(make or "").strip():
                out[key] = str(make).strip()
    return out


def save_alias(word, make):
    key = str(word or "").strip()
    value = str(make or "").strip()
    if not key or not value:
        raise SSError("an alias needs both a word and a make")
    if not resolve_slug(value, car_makes()):
        raise SSError("ss.com has no car make called %r" % value[:30])
    with _watch_lock:
        try:
            raw = json.loads(ALIAS_PATH.read_text(encoding="utf-8"))
        except Exception:
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        raw[key] = value
        ALIAS_PATH.parent.mkdir(parents=True, exist_ok=True)
        ALIAS_PATH.write_text(json.dumps(raw, ensure_ascii=False, indent=1),
                              encoding="utf-8")
    return {key: value}


def make_he_meant(text):
    aliases = load_aliases()
    if not aliases:
        return ""
    keys = list(aliases)
    for token in re.findall(r"\w+", str(text or ""), re.UNICODE):
        folded = _fold(token)
        if len(folded) < 4:
            continue
        if folded in aliases:
            return aliases[folded]
        near = difflib.get_close_matches(folded, keys, n=1, cutoff=0.82)
        if near:
            return aliases[near[0]]
    return ""


_watch_lock = threading.Lock()


def _watches_load():
    try:
        raw = json.loads(WATCH_PATH.read_text(encoding="utf-8"))
    except Exception:
        raw = {}
    return raw if isinstance(raw, dict) else {}


def _watches_save(data):
    try:
        WATCH_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = WATCH_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(WATCH_PATH)
    except Exception:
        LOGGER.exception("could not save ss watches")


def watch_add(chat_guid, sender, label, **query):
    query = {k: v for k, v in query.items() if v not in (None, "", [])}
    found = run_query(**dict(query, limit=20))
    with _watch_lock:
        data = _watches_load()
        key = "w%d" % (int(time.time() * 1000) % 10_000_000)
        data[key] = {
            "chat_guid": str(chat_guid or ""), "sender": str(sender or ""),
            "label": str(label or "")[:80], "query": query,
            "seen": [r["id"] for r in found["results"]],
            "url": found["url"], "created": time.time(), "checked": time.time(),
        }
        _watches_save(data)
    return {"id": key, "label": label, "url": found["url"],
            "known": len(found["results"])}


def watch_list(chat_guid=""):
    data = _watches_load()
    return [dict(v, id=k) for k, v in sorted(data.items())
            if not chat_guid or v.get("chat_guid") == chat_guid]


def watch_move(watch_id, chat_guid, sender=""):
    """Report this watch somewhere else from now on ("send those to my chat")."""
    with _watch_lock:
        data = _watches_load()
        found = data.get(str(watch_id))
        if found is None:
            return None
        found["chat_guid"] = str(chat_guid or "")
        if sender:
            found["sender"] = str(sender)
        found["moved"] = time.time()
        _watches_save(data)
    return dict(found, id=str(watch_id))


def watch_remove(watch_id):
    with _watch_lock:
        data = _watches_load()
        gone = data.pop(str(watch_id), None)
        if gone is not None:
            _watches_save(data)
    return bool(gone)


def watch_check(watch_id=None):
    """[(watch, [new listings])] — and the seen set is updated as we go."""
    out = []
    with _watch_lock:
        data = _watches_load()
    for key, w in list(data.items()):
        if watch_id and key != watch_id:
            continue
        try:
            found = run_query(**dict(w.get("query") or {}, limit=20))
        except SSError as exc:
            LOGGER.info("ss watch %s: %s", key, str(exc)[:80])
            continue
        seen = set(w.get("seen") or [])
        fresh = [r for r in found["results"] if r["id"] not in seen]
        if fresh:
            out.append((dict(w, id=key), fresh))
        with _watch_lock:
            data = _watches_load()
            if key in data:
                data[key]["seen"] = ([r["id"] for r in found["results"]]
                                     + list(seen))[:200]
                data[key]["checked"] = time.time()
                _watches_save(data)
    return out
