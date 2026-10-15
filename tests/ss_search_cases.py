# -*- coding: utf-8 -*-
r"""ss.com: slug autocorrect, row parsing, and watches that only report NEW.

* ss.com files Torņkalns as `tornjakalns` and Jugla as `yugla`, so slugs are
  matched against the site's own published list by similarity, never guessed.
  A name that is not a district must come back empty, or the caller fetches a
  404 page and reports "no listings" for a place that exists.
* the same applies one level down: there is no /volkswagen/golf/ page, only
  golf-1 .. golf-8. Folding the name into a slug 404'd, and eight generations
  are a choice for the MODEL to make, not for this layer to make silently.
* /transport/cars/ and /real-estate/flats/riga/ are index pages that parse to
  zero rows. Reporting that as "nothing for sale" sent the agent hunting
  through six blind searches, so the missing name is named instead.
* the car make page carries a model column and the model page does not (8 cells
  vs 7), so fixed indexes read the year as the model and dropped the price.
* a watch records what exists at creation time; only later arrivals are news,
  and moving a watch to another chat must not replay them.

Network-free: the fetch layer is stubbed with real captured markup.

    py -3 tests/ss_search_cases.py
"""
import io
import os
import sys
import tempfile
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import ss_search as S  # noqa: E402

DISTRICTS = [("Centrs", "centre"), ("Āgenskalns", "agenskalns"),
             ("Torņakalns", "tornjakalns"), ("Jugla", "yugla"),
             ("Purvciems", "purvciems"), ("Ķengarags", "kengarags")]
MAKES = [("Audi", "audi"), ("BMW", "bmw"), ("Volkswagen", "volkswagen")]
MODELS = {
    "audi": [("Audi A4", "a4"), ("Audi A6", "a6"), ("Audi A6 Allroad", "a6-allroad")],
    "volkswagen": [("Volkswagen Golf 3", "golf-3"), ("Volkswagen Golf 4", "golf-4"),
                   ("Volkswagen Polo", "polo")],
    "bmw": [("BMW 3. sērija", "3-series"), ("BMW 5. sērija", "5-series")],
}

FLAT_ROW = (
    '<tr id="tr_123"><td></td><td></td>'
    '<td><a href="/msg/lv/real-estate/flats/riga/tornjakalns/abc.html">'
    '2 istabu dzīvoklis Jelgavas ielā</a></td>'
    '<td>Jelgavas 51</td><td>2</td><td>43</td><td>1/6</td><td>Jaun.</td>'
    '<td>15.12 €</td><td>650 €/mēn.</td></tr>')
CAR_MAKE_ROW = (
    '<tr id="tr_777"><td></td><td></td>'
    '<td><a href="/msg/lv/transport/cars/audi/a6/x.html">Audi A6C6 s-line</a></td>'
    '<td>A6</td><td>2011</td><td>3.0D</td><td>330 tūkst.</td><td>6,000 €</td></tr>')
CAR_MODEL_ROW = (
    '<tr id="tr_888"><td></td><td></td>'
    '<td><a href="/msg/lv/transport/cars/audi/a6/y.html">Audi A6C6 quattro</a></td>'
    '<td>2005</td><td>3.0D</td><td>353 tūkst.</td><td>1,900 €</td></tr>')


def _car_row(rid, price):
    return ('<tr id="tr_%s"><td></td><td></td>'
            '<td><a href="/msg/lv/transport/cars/volkswagen/golf-4/%s.html">Golf</a></td>'
            '<td>2002</td><td>1.9D</td><td>300 tūkst.</td><td>%d €</td></tr>'
            % (rid, rid, price))


def main():
    checks = []

    def check(label, passed):
        checks.append((label, bool(passed)))

    for name, want in (("Torņakalns", "tornjakalns"),   # exact
                       ("Torņkalns", "tornjakalns"),    # how people say it
                       ("tornkalns", "tornjakalns"),    # no diacritics
                       ("Agenskalns", "agenskalns"),
                       ("purvciem", "purvciems"),       # truncated
                       ("Jugla", "yugla")):             # transliterated slug
        check("%-12s -> %s" % (name, want),
              S.resolve_slug(name, DISTRICTS) == want)
    for name in ("Mēness pilsēta", "asdfgh", ""):
        check("%r is not a district" % name[:14], S.resolve_slug(name, DISTRICTS) == "")

    slug, options = S.resolve_choice("A6", MODELS["audi"])
    check("an exact model resolves outright", slug == "a6" and not options)
    slug, options = S.resolve_choice("Golf", MODELS["volkswagen"])
    check("an ambiguous model resolves to nothing", slug == "")
    check("...and hands back the generations",
          sorted(options) == ["Volkswagen Golf 3", "Volkswagen Golf 4"])
    slug, options = S.resolve_choice("3. sērija", MODELS["bmw"])
    check("a name only the display carries still resolves", slug == "3-series")

    check("cars need a make", S.missing_choice("cars") == "make")
    check("...and a named make is enough", S.missing_choice("cars", make="BMW") == "")
    check("property needs a district", S.missing_choice("flats") == "area")
    check("...and a named district is enough",
          S.missing_choice("flats", area="Jugla") == "")

    real_d, real_m, real_mm = S.districts, S.car_makes, S.car_models
    S.districts = lambda section="flats": DISTRICTS
    S.car_makes = lambda: MAKES
    S.car_models = lambda make_slug: MODELS.get(make_slug, [])
    try:
        check("rent url uses ss.com's own word (hand_over)",
              S.build_url("flats", area="Torņkalns", deal="rent")
              == "/lv/real-estate/flats/riga/tornjakalns/hand_over/")
        check("sale url",
              S.build_url("flats", area="Āgenskalns", deal="sell")
              == "/lv/real-estate/flats/riga/agenskalns/sell/")
        check("car make url names the deal ss.com needs for filters", S.build_url("cars", make="BMW") == "/lv/transport/cars/bmw/sell/")
        check("car model url is looked up, not folded",
              S.build_url("cars", make="Audi", model="A6") == "/lv/transport/cars/audi/a6/sell/")
        check("a model spelled only in the display resolves",
              S.build_url("cars", make="BMW", model="3 sērija")
              == "/lv/transport/cars/bmw/3-series/sell/")
        bad = ""
        try:
            S.build_url("flats", area="Mēness pilsēta", deal="rent")
        except S.SSError as exc:
            bad = str(exc)
        check("an unknown district is named, not silently searched",
              "Mēness" in bad or "no Riga district" in bad)
        picked = None
        try:
            S.build_url("cars", make="Volkswagen", model="Golf")
        except S.SSChoice as exc:
            picked = exc
        check("an ambiguous model raises a CHOICE, not an error",
              picked is not None and picked.needs == "model")
        check("...carrying the pages ss.com does publish",
              picked is not None and "Volkswagen Golf 4" in picked.choices)
        blind = ""
        try:
            S.search(kind="cars", limit=3)
        except S.SSError as exc:
            blind = str(exc)
        check("an all-cars search is refused, not read as an empty market",
              "make" in blind)
    finally:
        S.districts, S.car_makes, S.car_models = real_d, real_m, real_mm

    flat = S._parse_rows("<table>%s</table>" % FLAT_ROW, "flats")[0]
    check("flat rooms/area/floor read", (flat["rooms"], flat["area_m2"], flat["floor"])
          == ("2", "43", "1/6"))
    check("flat price parsed to a number", flat["price"] == 650)
    check("flat keeps the human price text", "€/mēn" in flat["price_text"])
    check("flat links the listing", flat["url"].endswith("abc.html"))

    car_a = S._parse_rows("<table>%s</table>" % CAR_MAKE_ROW, "cars")[0]
    car_b = S._parse_rows("<table>%s</table>" % CAR_MODEL_ROW, "cars")[0]
    check("make page keeps the model column", car_a["model"] == "A6")
    check("make page year/price", (car_a["year"], car_a["price"]) == ("2011", 6000))
    check("model page has no model column, and does not shift",
          car_b["model"] == "" and car_b["year"] == "2005")
    check("model page price still parsed", car_b["price"] == 1900)

    pages = {
        "/lv/transport/cars/volkswagen/golf-4/sell/":
            "<table>%s</table>" % "".join(_car_row(90 + i, 9000) for i in range(4)),
        "/lv/transport/cars/volkswagen/golf-4/sell/page2.html":
            "<table>%s</table>" % (_car_row(201, 1200) + _car_row(202, 8000)),
        "/lv/transport/cars/volkswagen/golf-4/sell/page3.html":
            "<table>%s</table>" % _car_row(301, 900),
    }
    real_fetch, real_mm2, real_mk2 = S._fetch, S.car_models, S.car_makes
    S._fetch = lambda path, ttl=None: pages.get(path, "<table></table>")
    S.car_models = lambda make_slug: MODELS.get(make_slug, [])
    S.car_makes = lambda: MAKES
    try:
        got = S.search(kind="cars", make="Volkswagen", model="Golf 4",
                       max_price=1500, limit=5)
        check("a price cap keeps reading past page 1",
              [r["id"] for r in got["results"]] == ["201", "301"])
        plain = S.search(kind="cars", make="Volkswagen", model="Golf 4", limit=99)
        check("...and without a cap only page 1 is fetched", plain["count"] == 4)
    finally:
        S._fetch, S.car_models, S.car_makes = real_fetch, real_mm2, real_mk2

    filter_page = (
        '<td class="filter_name">Cena:<br><span><input id="f_o_8_min" '
        'name="topt[8][min]"><input id="f_o_8_max" name="topt[8][max]"></span></td>'
        '<td class="filter_name">Gads:<br><span><input id="f_o_18_min" '
        'name="topt[18][min]"><input id="f_o_18_max" name="topt[18][max]"></span></td>'
        '<td nowrap>Virsbuve:<select name="opt[32]">'
        '<option value="">-</option><option value="486">Hečbeks</option>'
        '<option value="488">Kabriolets</option></select></td>'
        '<td nowrap>Dzinējs:<select name="opt[34]">'
        '<option value="494">Dīzelis</option></select></td>')
    real_fetch3 = S._fetch
    S._fetch = lambda path, ttl=None: filter_page
    S._cache["lists"].clear()
    try:
        check("the price and year boxes are found by their own labels",
              S.numeric_fields("/x/") == {"Cena": "8", "Gads": "18", "Virsbuve": "32"}
              or S.numeric_fields("/x/").get("Cena") == "8")
        form = S._filter_form("/x/", options=["kabriolets"], min_price=2000,
                              max_price=3000)
        check("a body type becomes the select ss.com uses",
              form.get("opt[32]") == "488")
        check("a budget becomes the price boxes",
              form.get("topt[8][min]") == "2000" and form.get("topt[8][max]") == "3000")
        check("an empty bound is not posted at all",
              "topt[18][min]" not in S._filter_form("/x/", max_year=2010))
        check("a diacritic-free word still finds the option",
              S._filter_form("/x/", options=["dizelis"]).get("opt[34]") == "494")
        unknown = None
        try:
            S._filter_form("/x/", options=["kosmoss"])
        except S.SSChoice as exc:
            unknown = exc
        check("an option ss.com does not offer comes back as a choice",
              unknown is not None and "Kabriolets" in unknown.choices)
    finally:
        S._fetch = real_fetch3
        S._cache["lists"].clear()

    dropped = ["https://www.ss.com/lv/transport/cars/sell/",
               "https://www.ss.com/lv/transport/cars/sell/"]
    real_post, real_mk3, real_mm3 = S._post, S.car_makes, S.car_models
    S.car_makes = lambda: MAKES
    S.car_models = lambda make_slug: MODELS.get(make_slug, [])
    S._fetch = lambda path, ttl=None: filter_page
    S._cache["lists"].clear()
    S._post = lambda path, data, referer="", expect="", tries=3: (
        dropped[0], "<table>%s</table>" % CAR_MAKE_ROW)
    try:
        refused = ""
        try:
            S.search(kind="cars", options=["Kabriolets"], limit=3)
        except S.SSError as exc:
            refused = str(exc)
        check("a dropped body-type filter is refused, not answered with anything",
              "would not keep" in refused)
    finally:
        S._post, S.car_makes, S.car_models = real_post, real_mk3, real_mm3
        S._fetch = real_fetch3
        S._cache["lists"].clear()

    search_row = (
        '<tr id="tr_5555"><td></td><td></td>'
        '<td class=msg2><div class="ads_cat_names">Elektrotehnika : Datori</div>'
        '<div class=d1><a href="/msg/lv/electronics/computers/pc/fx.html">'
        '<b>Pārdodu Apple Mac mini 2014 Late</b></a></div></td>'
        '<td>150 €</td></tr>')
    found = S._parse_any("<table>%s</table>" % search_row)
    check("a search row keeps the advert text", found
          and "Mac mini" in found[0]["title"])
    check("...and the category it came from",
          found[0]["category"].startswith("Elektrotehnika"))
    check("...and its price", found[0]["price"] == 150)

    asked = {}
    S._fetch = lambda path, ttl=None: (asked.setdefault("path", path),
                                       "<table>%s</table>" % search_row)[1]
    try:
        got = S.site_search("mac mini", path="/lv/electronics/computers/pc/")
        check("a scoped words-search asks the section's own search page",
              asked["path"] == "/lv/electronics/computers/pc/search-result/?q=mac%20mini")
        check("...and finds the advert", got["count"] == 1)
    finally:
        S._fetch = real_fetch3

    ad_page = (
        "<title>SS.COM Datori, Cena 150 </title>"
        '<div id="msg_div_msg">\r\n<div id="content_sys_div_msg"></div>\r\n'
        "Pārdodu Apple Mac mini 2014 Late. Atrodas Rīga-Teikā.<br />"
        '<table class="options_list"><tr><td class="ads_opt_name">Procesors:</td>'
        '<td class="ads_opt" id="tdo_116">I5</td></tr>'
        '<tr><td class="ads_opt_name">Stāvoklis:</td>'
        '<td class="ads_opt">lietota</td></tr></table></div>'
        '<td class="ads_price" id="tdo_8">150 €</td>'
        '<img src="https://i.ss.com/gallery/7/1/1/electronics-computers-pc-1.800.jpg">'
        '<img src="https://i.ss.com/gallery/7/1/1/electronics-computers-pc-1.t.jpg">')
    S._fetch = lambda path, ttl=None: ad_page
    try:
        ad = S.listing("/msg/lv/electronics/computers/pc/fx.html")
        check("the advert body is read past the empty div at its top",
              ad["description"].startswith("Pārdodu Apple Mac mini"))
        check("...and stops before the attributes table",
              "Procesors" not in ad["description"])
        check("the listed attributes are read",
              ad["attributes"].get("Procesors") == "I5"
              and ad["attributes"].get("Stāvoklis") == "lietota")
        check("the price is read", ad["price_text"].startswith("150"))
        check("only full-size photos are returned, not the thumbnails",
              ad["photos"] == ["https://i.ss.com/gallery/7/1/1/"
                               "electronics-computers-pc-1.800.jpg"])
    finally:
        S._fetch = real_fetch3

    tmp = Path(tempfile.mkdtemp(prefix="ss-watch-")) / "watches.json"
    real_path, real_search = S.WATCH_PATH, S.search
    S.WATCH_PATH = tmp
    page = {"n": 1}

    def fake_search(**kw):
        rows = [{"id": "1", "title": "first", "url": "u1", "price": 500,
                 "price_text": "500 €", "rooms": "2", "area_m2": "40"}]
        if page["n"] > 1:
            rows.insert(0, {"id": "2", "title": "brand new", "url": "u2",
                            "price": 600, "price_text": "600 €", "rooms": "3",
                            "area_m2": "60"})
        return {"url": "https://www.ss.com/x/", "count": len(rows), "results": rows}

    S.search = fake_search
    try:
        made = S.watch_add("chat1", "+371", "Torņkalna īre", kind="flats",
                           area="Torņkalns", deal="rent")
        check("a watch records what already exists", made["known"] == 1)
        check("...and nothing is reported on creation", S.watch_check() == [])
        page["n"] = 2
        fresh = S.watch_check()
        check("a NEW listing is reported once",
              len(fresh) == 1 and [i["id"] for i in fresh[0][1]] == ["2"])
        check("...and not reported again", S.watch_check() == [])
        check("the watch is listed for its own chat",
              len(S.watch_list("chat1")) == 1 and S.watch_list("other") == [])

        # "send those to my chat from now on"
        moved = S.watch_move(made["id"], "chat2", "owner@x")
        check("a watch can be told to report elsewhere",
              moved and moved["chat_guid"] == "chat2")
        check("...and it reports there now", len(S.watch_list("chat2")) == 1
              and S.watch_list("chat1") == [])
        check("...without replaying what it already sent", S.watch_check() == [])
        check("moving a watch that does not exist says so",
              S.watch_move("nope", "chat2") is None)
        check("removing it works", S.watch_remove(made["id"])
              and S.watch_list("chat2") == [])
    finally:
        S.WATCH_PATH, S.search = real_path, real_search

    ok = all(p for _l, p in checks)
    for label, passed in checks:
        print(("OK   " if passed else "FAIL ") + label)
    print("\n%d checks, VERDICT: %s" % (len(checks), "PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
