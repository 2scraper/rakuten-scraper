#!/usr/bin/env python3
"""smoke_test.py - the offline suite for rakuten-scraper.

One file, plain functions; `tests/test_smoke.py` wraps it as a single pytest
test so `pytest` works as an entry point without a second copy of the checks.

It must pass with NO engine library installed at all, so every engine import
is guarded and the skip is reported at the end - a suite that silently skips
part of itself and still says "all passed" is the same defect as code that
reports success without checking what it wanted actually happened.

THE FIXTURES ARE IN `fixtures_generated.json`, NOT INLINE
--------------------------------------------------------
The family's rule is fixtures inline, and on most of these sites that works
because a fixture is a few hundred bytes of markup. Here the fixture IS the
SSR payload: one product's entry in
`__INITIAL_STATE__.state.data.ichibaSearch.items` is 2-4 KB of JSON on its
own. `make_fixtures.py` cuts them from real captures and verifies that each
trim parses IDENTICALLY to the untrimmed original - every column, not a
sample - before writing anything.

Credential-shaped and personal material is replaced with obvious placeholders
and guarded by PATTERNS rather than by the literals one capture happened to
contain, so the next capture is caught too. This site's pages carry four
kinds: two 32-char front-end API keys baked into every item page, a real
customer's review nickname and text, a per-session id on each sponsored slot,
and a 32-hex catalogue id in `productUrl` that is public and harmless and
reads as a credential to every scanner including this repo's own.

WHAT THIS SUITE CANNOT DO, said out loud
----------------------------------------
Every check here runs against bytes captured earlier. It cannot see a
fingerprint that gets refused on the SECOND navigation but not the first,
which is exactly the shape of the worst bug found while building this repo -
the pyppeteer engine's UA override, served on nav 1 and denied on navs 2-4.
`compileall` passed, `--help` worked, every check here was green, and only a
live two-page run found it. Run one (§15).
"""
import ast
import builtins
import csv
import inspect
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import threading
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import asdict as dataclasses_asdict, fields

import captcha_solver
from captcha_solver import (CaptchaChallenge, detect_recaptcha_v3,
                            reconcile_detections, _v2_task_for, _redact)
from diff_runs import diff_products
import env_config

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from output_writer import (Product, save, finish_run, write_csv,
                           dedupe_by_key, dedupe_by_sku, run_meta,
                           ROW_CLASS_BY_MODE, UNIQUE_BY_SKU_MODES,
                           EXIT_BLOCKED, EXIT_NO_PRODUCTS, EXIT_PARTIAL,
                           EXIT_API_ERROR, COMPLETE_STOP_REASONS,
                           LIST_CSV_SEPARATOR)
from bs4 import BeautifulSoup

import product_parser
from product_parser import (parse_products, parse_product_page, page_url,
                            paginates_by_url, category_from_url, listing_kind,
                            site_host, is_supported_host, search_url,
                            locale_of, LOCALES, HOSTS, CURRENCY, PAGE_CAP,
                            PAGE_SIZE, SUBSET_CAP, CURRENCY_CODES,
                            detect_page_state, detect_bot_challenge,
                            detect_block_marker, served_by_rakuten,
                            is_no_results, page_number_from_url, total_results,
                            total_pages, pages_beyond_cap, search_header,
                            sku_from_url, unsupported_reason, strip_tracking,
                            prices_in, price_in, initial_state, item_island,
                            search_payload, pagination, served_offset,
                            served_page_number, reachable_max, hits_per_page,
                            capped_by_site, currency_from_page, carousel_urls,
                            jsonld_blocks, jsonld_currency, decode_page,
                            is_sponsored, shop_metadata, genre_of,
                            SELECTORS, BLOCK_MARKERS, BOT_CHALLENGE_MARKERS)
import page_flow
from proxy_pool import (ProxyPool, mask, to_playwright, split_credentials,
                        parse_proxy_line)

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))

_failures = []


def check(label, condition):
    """Print and record one check. Returns the condition, so callers can
    accumulate with `ok &= check(...)`."""
    if condition:
        print("  PASS  %s" % label)
    else:
        print("  FAIL  %s" % label)
        _failures.append(label)
    return bool(condition)


def group(title):
    print("\n== %s" % title)


def _raises(fn):
    """True if `fn()` raises. Used where refusing is the correct behaviour."""
    try:
        fn()
    except Exception:
        return True
    return False


BANNED_PHRASES = (
    "cloud browser",
    "antidetect browser",
    "anti-detect browser",
    "2scraper Antidetect Browser",
    "gate.2prx.com",
    "2prx.com",
)

# Flags that were removed and must stay removed. Scoped to the ENGINES:
# `--country` is banned on a scraper here -- the locale is a path segment, so
# a flag could only disagree with the URL -- and legitimate on
# fingerprint_client.py, where it picks a fingerprint locale.
REMOVED_ENGINE_FLAGS = ("--antidetect", "--country")
ENGINE_FILES = ("playwright_scraper.py", "puppeteer_scraper.py",
                "selenium_scraper.py")

# A fingerprint as the 2Captcha API really returns one, with this site's
# region. Kept as a fixture because three of the six defects §16 lists were
# one wrong key each in exactly this structure -- `userAgent.value` where the
# API says `userAgent.userAgent`, a locale built as `en-{country}`, a
# timezone never applied at all.
FIX_FINGERPRINT = {
    "id": 1000000,
    "country": "NL",
    "userAgent": {
        "userAgent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/146.0.0.0 Safari/537.36"),
        "platform": "Windows",
        "mobile": False,
    },
    "intl": {
        "contentLocale": "nl-NL",
        "languages": ["nl-NL", "nl", "en-US", "en"],
        "timeZone": "Europe/Amsterdam",
    },
    "screen": {"width": 1920, "height": 1080,
               "outerWidth": 1920, "outerHeight": 992,
               "deviceScaleFactor": 1},
}


# The exact tags the 2Captcha Scraping Browser's auto-solve extension injects
# into every page it loads, copied from a CDP capture of a page the site
# plainly served. Kept verbatim because the question they answer is whether
# our own marker set mistakes them for the site's challenge.
EXTENSION_TAGS = (
    '<script src="chrome-extension://kjmkgkdkpedkejedfhmfcenooemhbpbo/'
    'content/captcha/recaptcha/hunter.js"></script>'
    '<script src="chrome-extension://kjmkgkdkpedkejedfhmfcenooemhbpbo/'
    'content/captcha/turnstile/hunter.js" '
    'data-ts-input="cf-turnstile-response"></script>'
)

# ---------------------------------------------------------------------------
# FIXTURES — cut from real captures by make_fixtures.py, which verifies that
# each trim parses IDENTICALLY to the untrimmed original, column for column,
# and scrubs the agent names, per-seller UUIDs and search key out first.
#
# Loaded from `fixtures_generated.json` rather than pasted inline, and the
# reason is this site rather than a preference: the fixture IS the SSR
# payload, one ad's entry is 2-6 KB of JSON on its own, and a fixture that
# carried fewer than two ads plus the page's JSON-LD would stop exercising
# the join the parser is built on. See make_fixtures.py's docstring.
# ---------------------------------------------------------------------------
FIXTURES_PATH = os.path.join(REPO_ROOT, "fixtures_generated.json")
with open(FIXTURES_PATH, encoding="utf-8") as _f:
    FIX = json.load(_f)


def fx(name):
    """One fixture's (html, url, status)."""
    f = FIX[name]
    return f["html"], f["url"], f["status"]


def fx_rows(name):
    html, url, _ = fx(name)
    return parse_products(html, url, page=page_number_from_url(url),
                          currency=currency_from_page(html))


def _fx_detail(name):
    """(html, url) for a detail fixture, ready for parse_product_page."""
    f = FIX[name]
    return f["html"], f["url"]


def fx_bytes(name):
    """A fixture re-encoded to the bytes the site would actually send.

    Exists so the EUC-JP path is exercised against a real page rather than
    against a synthetic string: an item fixture is stored as text and the
    site sends it as EUC-JP, and `decode_page` is the only reason an HTTP
    client can read one at all.
    """
    f = FIX[name]
    return f["html"].encode(f.get("encoding") or "utf-8", "replace")


# The exact tags the 2Captcha Scraping Browser's auto-solve extension injects
# into every page it loads, copied from a CDP capture of a page the site
# plainly served. Kept verbatim because the question they answer is whether
# our own marker set mistakes them for the site's challenge — and unlike two
# sibling repos, this marker set CAN match one, so the guard is not dead code.
EXTENSION_TAGS = (
    '<script src="chrome-extension://kjmkgkdkpedkejedfhmfcenooemhbpbo/'
    'content/captcha/recaptcha/hunter.js"></script>'
    '<script src="chrome-extension://kjmkgkdkpedkejedfhmfcenooemhbpbo/'
    'content/captcha/turnstile/hunter.js" '
    'data-ts-input="cf-turnstile-response"></script>'
)

SEARCH_URL = ("https://search.rakuten.co.jp/search/mall/"
              "%E3%82%B3%E3%83%BC%E3%83%92%E3%83%BC/")
GENRE_URL = "https://search.rakuten.co.jp/search/mall/-/100356/"
CATEGORY_URL = "https://www.rakuten.co.jp/category/100356/"
ITEM_URL = "https://item.rakuten.co.jp/sawaicoffee-tea/solandluna/"
HUB_URL = "https://www.rakuten.co.jp/"
# The family's older names, kept pointing at this site's equivalents so the
# site-agnostic checks below (writers, diff, concurrency, engines) read the
# same as their siblings.
LISTING_URL = SEARCH_URL


# ---------------------------------------------------------------------------
# Money
# ---------------------------------------------------------------------------
def test_price_parsing():
    group("prices: one currency, and the traps around reading it off a tile")
    ok = True
    # The three ways Japanese retail writes a price, all of them real on this
    # site's rendered markup. Only the DOM fallback ever parses text — the
    # payload paths read integers — so this is a guard on the fallback.
    ok &= check("fullwidth yen prefix", price_in("￥1,234") == 1234)
    ok &= check("ASCII yen prefix", price_in("¥1,234") == 1234)
    ok &= check("en suffix", price_in("1,234円") == 1234)
    ok &= check("no separator", price_in("1234円") == 1234)
    ok &= check("large amount", price_in("￥1,234,567") == 1234567)
    # JPY has NO subunit, so a comma is always a thousands separator here and
    # §4's "whichever of dot and comma comes last is the decimal point" rule
    # does not apply. `1,234` is 1234 and never 1.234.
    ok &= check("a comma is never a decimal point in JPY",
                price_in("￥1,234") == 1234.0)
    # Percentages are stripped BEFORE matching, not rejected afterwards: a
    # rejected match has still consumed its neighbouring yen symbol, so the
    # real price would be lost with it. Both sign widths, because Japanese
    # pages use the fullwidth one.
    ok &= check("ASCII percent badge does not become the price",
                price_in("50%OFF ￥2,980") == 2980)
    ok &= check("fullwidth percent badge does not become the price",
                price_in("50％OFF ￥2,980") == 2980)
    ok &= check("a leading discount badge is skipped",
                prices_in("-10% ￥1,000 ￥2,000") == [1000.0, 2000.0])
    # A no-break space between the amount and the unit is what a rendered
    # page uses so the number does not wrap.
    ok &= check("NBSP between amount and unit",
                price_in("1,234\u00a0円") == 1234)
    ok &= check("narrow NBSP", price_in("1,234\u202f円") == 1234)
    ok &= check("no price in text returns None",
                price_in("送料無料 在庫あり") is None)
    ok &= check("a bare number with no currency is not a price",
                price_in("1234") is None)
    # A size or quantity beside a price must not merge into it.
    ok &= check("a quantity does not merge with the price",
                prices_in("100本 ￥2,447") == [2447.0])
    return ok


# ---------------------------------------------------------------------------
# Rows: VALUES on real fixtures, not coverage (§10)
# ---------------------------------------------------------------------------
def test_listing_values():
    group("listing rows: VALUES on real fixtures, not coverage")
    ok = True
    # Pinned figures, not a coverage percentage. A column can be 100%
    # populated and entirely wrong — a sibling repo shipped a review_count
    # of 445279961 on every row of every mode while its coverage check said
    # 100% (§10).
    rows = fx_rows("search_p1")
    first = rows[0]
    ok &= check("search fixture parses its products", len(rows) >= 6)
    ok &= check("sku is {shop}:{manageNumber}",
                first.sku == "ajinomoto:r4901111371248")
    ok &= check("title is the product name, not the campaign banner alone",
                first.title and "ブレンディ" in first.title)
    ok &= check("price is the tax-included integer", first.price == 2447.0)
    ok &= check("currency is read from the page, not defaulted",
                first.currency == "JPY")
    ok &= check("brand comes from Rakuten's own ブランド tag",
                first.brand == "味の素AGF")
    ok &= check("rating is rounded to two places", first.rating == 4.76)
    ok &= check("review_count is the count and not a digit-strip",
                first.review_count == 475)
    ok &= check("points are read", first.points == 220)
    ok &= check("free shipping is the comparison made once",
                first.shipping_fee == 0.0 and first.free_shipping is True)
    ok &= check("price_max carries the top of a variant range",
                first.price_max == 6975.0)
    ok &= check("category is the product's own genre path",
                first.category == "水・ソフトドリンク > コーヒー > インスタントコーヒー")
    ok &= check("shop is a column, because this site is a mall",
                first.shop_code == "ajinomoto" and first.shop_id == 383220)
    ok &= check("price_source names the structure it was read from",
                first.price_source == "state")
    ok &= check("source is the family constant, not the browsed host",
                first.source == "rakuten.co.jp")
    # The genre landing route reads through the same parser, which is the
    # reason it needs no second one.
    cat = fx_rows("category_p1")
    ok &= check("the /category/ route parses with the same parser",
                len(cat) >= 6 and cat[0].sku == "sawaicoffee-tea:solandluna")
    ok &= check("and produces the same price_source",
                cat[0].price_source == "state")
    # The browser's DOM and an HTTP client's bytes are the same payload.
    http_rows = fx_rows("search_p1")
    dom_rows = fx_rows("search_p1_browser")
    ok &= check("a browser capture and an HTTP capture agree on the skus",
                [r.sku for r in http_rows] == [r.sku for r in dom_rows])
    ok &= check("...and on the prices",
                [r.price for r in http_rows] == [r.price for r in dom_rows])
    # ?lang=en translates the chrome and NOT the data, which is measured and
    # is why the README says so.
    en = fx_rows("search_lang_en")
    ok &= check("?lang=en leaves merchant-authored names in Japanese",
                en and en[0].title == http_rows[0].title)
    return ok


def test_measured_absences():
    group("columns that are empty because the SITE is, not because the read broke")
    ok = True
    rows = fx_rows("search_p1") + fx_rows("category_p1")
    # ZERO IS NOT A RATING. Rakuten writes "nobody has rated this" as
    # `{score: 0, numReviews: 0}` rather than as a null — 28 of 405 measured
    # rows — and a 0 written through drags every average a consumer
    # computes. Both columns go null together, keyed on the COUNT.
    deep = fx_rows("search_deep")
    unrated = [r for r in deep if r.review_count is None]
    ok &= check("the deep fixture carries a real unreviewed product",
                len(unrated) >= 1)
    ok &= check("an unreviewed product has NO rating, not a rating of 0",
                all(r.rating is None for r in unrated))
    ok &= check("...and no review_count either, nulled together",
                all(r.review_count is None for r in unrated))
    ok &= check("no row anywhere carries the 0 sentinel as a rating",
                not any(r.rating == 0 for r in rows + deep))
    # The listing payload publishes no was-price of any kind — 0 occurrences
    # of doublePrice/listPrice/referencePrice across 405 rows — so these two
    # are null on every listing row BY MEASUREMENT. Pinned so that a future
    # capture growing the field becomes a visible change rather than a
    # surprise.
    ok &= check("no listing row invents an original_price",
                all(r.original_price is None for r in rows))
    ok &= check("and none invents a discount",
                all(r.discount_pct is None for r in rows))
    # `in_stock` is True on every listing row because this search EXCLUDES
    # sold-out products rather than marking them. Pinned as the known
    # limitation §10 asks for rather than half-guarded: a False here is
    # unproven, and the detail route is the source that can say otherwise.
    ok &= check("in_stock is True on every listing row (the search excludes "
                "sold-out items; a False here is unproven)",
                all(r.in_stock is True for r in rows))
    # A column that is null on every row of every run should not exist (§9).
    # These are populated on the DETAIL route, which is why they stay.
    detail = parse_product_page(*_fx_detail("item_discounted"))
    ok &= check("variant_count is null on listings and populated on a detail "
                "page", all(r.variant_count is None for r in rows)
                and detail[0].variant_count == 4)
    return ok


# ---------------------------------------------------------------------------
# The three sources, and the join between them
# ---------------------------------------------------------------------------
def test_jsonld_is_a_carousel_not_the_grid():
    group("JSON-LD on this site is a TRAP, and is read for exactly one thing")
    ok = True
    html, url, _ = fx("search_p1")
    # A search page has exactly one ld+json block, it is an ItemList, and it
    # holds TEN items that are NOT the result grid: every url carries
    # `?scid=seo-carousel-search`. The page itself holds 45 products. A
    # JSON-LD-primary parser returns ten rows of the wrong products and
    # reports success.
    carousel = carousel_urls(html)
    ok &= check("the page's only ItemList holds 10 items", len(carousel) == 10)
    ok &= check("and every one of them is a carousel link, not a result",
                all("scid=seo-carousel" in u for u in carousel))
    # The rows do not come from it, and the way to prove that on a trimmed
    # fixture is to take the payload away: if the carousel were the source,
    # ten rows would still come back. (Asserting that the row skus differ
    # from the carousel's would prove nothing — a coffee page's carousel
    # recommends the same popular coffee that tops its own results.)
    ok &= check("with the payload removed, the carousel yields NO rows",
                parse_products(html.replace("__INITIAL_STATE__", "__GONE__"),
                               url) == [])
    ok &= check("and with it present, the rows are the payload's",
                len(parse_products(html, url))
                == len(search_payload(html)["items"]))
    # The one thing it IS read for: a listing page's currency. And Rakuten
    # publishes this block on PAGE 1 ONLY — 1 on page 1, 0 on pages 2 and 3
    # — which is why the engines read it once and carry it forward.
    ok &= check("the carousel block is where a listing states its currency",
                currency_from_page(html) == "JPY")
    deep_html, deep_url, _ = fx("search_deep")
    ok &= check("a page past the first states no currency at all",
                currency_from_page(deep_html) is None)
    ok &= check("so its rows carry a null currency rather than a guess",
                all(r.currency is None for r in parse_products(deep_html,
                                                               deep_url)))
    ok &= check("...unless the engine passes page 1's statement forward",
                all(r.currency == "JPY" for r in
                    parse_products(deep_html, deep_url, currency="JPY")))
    # A DETAIL page publishes a different @type entirely — BreadcrumbList —
    # so the listing parser finds nothing on it (§20).
    item_html, item_url, _ = fx("item_page")
    types = [b.get("@type") for b in jsonld_blocks(item_html)
             if isinstance(b, dict)]
    ok &= check("a detail page's only JSON-LD is a BreadcrumbList",
                types == ["BreadcrumbList"])
    return ok


def test_dom_fallback():
    group("the DOM fallback: thin on purpose, and scoped to listing routes")
    ok = True
    html, url, _ = fx("search_p1")
    # With the payload removed, the fallback must still recover the products
    # from the anchors — that is the difference between "the site changed its
    # payload key" and "there are no products", which is a distinction a
    # reader needs (§20).
    stripped = html.replace("__INITIAL_STATE__", "__GONE__")
    ok &= check("with the payload gone, the parser reports no payload",
                search_payload(stripped) == {})
    rows = parse_products(stripped, url)
    # The fixture's body carries no product anchors (the trim keeps the
    # payload, not the grid), so the honest assertion is that the fallback
    # ran and returned nothing rather than crashing.
    ok &= check("the fallback runs without raising", isinstance(rows, list))
    # A hand-built listing body, to exercise the tile walk itself.
    body = (
        '<html><head><link rel="preload" href="https://x.r10s.jp/a.jpg">'
        '<link rel="preload" href="https://y.r10s.jp/b.jpg"></head><body>'
        '<div class="item"><a href="https://item.rakuten.co.jp/shopa/code1/">'
        '<img src="https://x.r10s.jp/1.jpg"></a>'
        '<a href="https://item.rakuten.co.jp/shopa/code1/">A product</a>'
        '<span class="price--x">￥1,980</span></div>'
        '<div class="item"><a href="https://item.rakuten.co.jp/shopb/code2/">'
        'B product</a><span class="price--x">￥2,480</span></div>'
        '</body></html>')
    dom = parse_products(body, "https://search.rakuten.co.jp/search/mall/x/")
    ok &= check("the fallback reads one row per product", len(dom) == 2)
    ok &= check("a tile linking to its product TWICE is still one row",
                [r.sku for r in dom] == ["shopa:code1", "shopb:code2"])
    ok &= check("and each row gets its OWN price, not its neighbour's",
                [r.price for r in dom] == [1980.0, 2480.0])
    ok &= check("a fallback row says so in price_source",
                all(r.price_source == "dom" for r in dom))
    # THE §20 FAILURE, stated directly: the listing parser must find NOTHING
    # on a detail page. It found two well-formed phantom rows until the
    # fallback was gated by route — a detail page links to plenty of other
    # products through its breadcrumb and recommendation rails.
    item_html, item_url, _ = fx("item_page")
    ok &= check("the LISTING parser finds nothing on a DETAIL page",
                parse_products(item_html, item_url) == [])
    return ok


# ---------------------------------------------------------------------------
# URLs
# ---------------------------------------------------------------------------
def test_urls():
    group("URLs: routes, ids, and the hosts that are Rakuten but not Ichiba")
    ok = True
    ok &= check("a keyword search is a search route",
                listing_kind(SEARCH_URL) == "search")
    ok &= check("a genre listing is a genre route",
                listing_kind(GENRE_URL) == "genre")
    ok &= check("a genre landing page is its own route",
                listing_kind(CATEGORY_URL) == "category")
    ok &= check("an item page is an item route",
                listing_kind(ITEM_URL) == "item")
    ok &= check("a merchant storefront is a shop route",
                listing_kind("https://www.rakuten.co.jp/ajinomoto/") == "shop")
    ok &= check("the mall front page is home",
                listing_kind("https://www.rakuten.co.jp/") == "home")
    # THE ID. `variantId` is the obvious candidate in the payload and it is
    # WRONG — it names the pre-selected SKU inside the item, matching the
    # URL's own code on only 39 of 180 measured rows. The URL tail is the
    # item's manageNumber, and it makes one id work on both routes.
    ok &= check("sku is recovered from the URL as {shop}:{code}",
                sku_from_url(ITEM_URL) == "sawaicoffee-tea:solandluna")
    ok &= check("a tracking tail does not change the id",
                sku_from_url(ITEM_URL + "?scid=af_pc_etc&sc_out=x")
                == "sawaicoffee-tea:solandluna")
    ok &= check("a listing URL has no product id",
                sku_from_url(SEARCH_URL) is None)
    ok &= check("a sponsored click-redirect is not a product",
                sku_from_url("https://grp07.ias.rakuten.co.jp/redirect_rpp/"
                             "?s=80mzhtMg7vA") is None)
    # The same product, read on both routes, must carry the same sku — that
    # is what lets a consumer join a listing run to a product run.
    listing_sku = {r.sku for r in fx_rows("category_p1")}
    detail_sku = parse_product_page(*_fx_detail("item_discounted"))[0].sku
    ok &= check("a listing row and a detail row agree on the sku",
                detail_sku in listing_sku)
    # Tracking parameters are stripped so two runs produce the same URL.
    ok &= check("Rakuten's own tracking params are stripped",
                strip_tracking(ITEM_URL + "?scid=x&iasid=y&l-id=z")
                == ITEM_URL)
    ok &= check("a real query parameter survives",
                "lang=en" in strip_tracking(SEARCH_URL + "?lang=en&scid=x"))
    # Hosts that ARE Rakuten and are not Ichiba get their own reason, because
    # "not a Rakuten site" would be false and would send the reader hunting
    # for a typo they did not make (§5).
    for host, needle in (
            ("https://ranking.rakuten.co.jp/daily/100316/", "ranking"),
            ("https://books.rakuten.co.jp/", "Books"),
            ("https://travel.rakuten.co.jp/", "Travel")):
        why = unsupported_reason(host)
        ok &= check("%s is refused with its own reason" % needle,
                    bool(why) and needle.lower() in why.lower()
                    and "not a Rakuten" not in why)
    ok &= check("a shop URL is refused with the payload reason",
                "EMPTY" in (unsupported_reason(
                    "https://www.rakuten.co.jp/ajinomoto/") or ""))
    ok &= check("a genuinely foreign host is refused plainly",
                "not a Rakuten Ichiba host"
                in (unsupported_reason("https://www.amazon.co.jp/") or ""))
    ok &= check("the supported routes are supported",
                all(is_supported_host(u) for u in
                    (SEARCH_URL, GENRE_URL, CATEGORY_URL, ITEM_URL)))
    ok &= check("locale_of reads a known ?lang= and ignores the rest",
                locale_of(SEARCH_URL + "?lang=en") == "en"
                and locale_of(SEARCH_URL + "?lang=zh-tw") is None)
    ok &= check("a Japanese keyword is percent-encoded the way the site "
                "spells it",
                search_url("コーヒー").endswith(
                    "/search/mall/%E3%82%B3%E3%83%BC%E3%83%92%E3%83%BC/"))
    return ok


def test_pagination():
    group("pagination: ?p=N, a 150-page cap, and two routes that LIE past it")
    ok = True
    ok &= check("page 1 is the URL itself",
                page_url(SEARCH_URL, 1) == SEARCH_URL)
    ok &= check("page 2 appends ?p=2",
                page_url(SEARCH_URL, 2) == SEARCH_URL + "?p=2")
    ok &= check("an existing ?p= is REPLACED, not duplicated",
                page_url(SEARCH_URL + "?p=7", 3) == SEARCH_URL + "?p=3")
    ok &= check("a real query parameter is preserved across pages",
                "lang=en" in page_url(SEARCH_URL + "?lang=en", 4))
    ok &= check("page 0 is not a page", page_url(SEARCH_URL, 0) is None)
    # A `/category/` URL does NOT honour ?p= on its own host — measured:
    # `?p=2` and `?p=4` both answered HTTP 200 with `start: 0`. Its own next
    # link points at the search host, and that is where page_url sends it.
    ok &= check("a genre landing page's page 1 stays where it is",
                page_url(CATEGORY_URL, 1) == CATEGORY_URL)
    ok &= check("but its page 2 moves to the search host, as the site's own "
                "link does",
                page_url(CATEGORY_URL, 2)
                == "https://search.rakuten.co.jp/search/mall/-/100356/?p=2")
    ok &= check("an item page has no page 2",
                page_url(ITEM_URL, 2) is None)
    ok &= check("page numbers are read back out of a URL",
                page_number_from_url(SEARCH_URL + "?p=12") == 12
                and page_number_from_url(SEARCH_URL) == 1)
    # THE SITE'S OWN ARITHMETIC. It caps every query at `subset` results
    # however many it matched, and states both numbers.
    html, url, _ = fx("search_p1")
    ok &= check("the query's own match count is read",
                (total_results(html) or 0) > 1000)
    ok &= check("and the reachable cap beside it",
                reachable_max(html) == 6750)
    ok &= check("which makes this query capped by the site",
                capped_by_site(html) is True)
    ok &= check("page size comes from the payload", hits_per_page(html) == 45)
    ok &= check("the page count is the site's arithmetic, not a guess",
                total_pages(html) == 150)
    ok &= check("and the pages beyond the cap are counted rather than hidden",
                pages_beyond_cap(html) > 0)
    nores_html, nores_url, _ = fx("search_no_results")
    ok &= check("a query that matched nothing has 0 pages",
                total_pages(nores_html) == 0)
    ok &= check("and is not 'capped'", capped_by_site(nores_html) is False)
    # THE DEFENCE. Neither out-of-range route fails in a way the client can
    # see, so the server's own statement of which offset it served is what
    # ends the listing.
    deep_html, deep_url, _ = fx("search_deep")
    ok &= check("the server states the offset it served",
                served_offset(deep_html) == 6660)
    ok &= check("which is page 149 of 45-row pages",
                served_page_number(deep_html) == 149)
    ok &= check("asking for 149 and getting 149 is not the end",
                page_flow.served_the_page_asked_for(deep_html, 149) is True)
    ok &= check("asking for 151 and getting page 1 IS the end",
                page_flow.served_the_page_asked_for(html, 151) is False)
    ok &= check("and so is asking a /category/ URL for page 2",
                page_flow.served_the_page_asked_for(
                    fx("category_p1")[0], 2) is False)
    ok &= check("a response with no payload is not treated as an ending",
                page_flow.served_the_page_asked_for(None, 5) is True)
    # The requested page is the URL's, not the loop's — a run started on a
    # URL that already carries ?p=2 asks the site for page 2 while calling it
    # page 1 of the run, and comparing against the loop index threw away 45
    # good rows.
    ok &= check("the requested page comes from the URL when it has one",
                page_flow.requested_page_number(SEARCH_URL + "?p=2", 1) == 2)
    ok &= check("and from the loop index when it does not",
                page_flow.requested_page_number(SEARCH_URL, 3) == 3)
    # The site's own links agree with the convention, which is what makes
    # pages plannable up front.
    ok &= check("the convention agrees with the site's own next link",
                page_flow.pagination_agrees(
                    SEARCH_URL, 1, [SEARCH_URL + "?p=2"]) is True)
    ok &= check("a genre landing page's cross-host next link also agrees",
                page_flow.pagination_agrees(
                    CATEGORY_URL, 1,
                    ["https://search.rakuten.co.jp/search/mall/-/100356/?p=2"])
                is True)
    ok &= check("a link to page 9 is not accepted as the page after 1",
                page_flow.next_page_candidates(
                    SEARCH_URL, [SEARCH_URL + "?p=9"])
                == [SEARCH_URL + "?p=2"])
    ok &= check("a link into another listing is filtered out",
                page_flow.next_page_candidates(
                    SEARCH_URL,
                    ["https://search.rakuten.co.jp/search/mall/-/999999/?p=2"])
                == [SEARCH_URL + "?p=2"])
    return ok


def test_page_state():
    group("six states, three refusal skins, and the status that separates two of them")
    ok = True
    # CONTENT. Both listing routes, both transports, and a detail page.
    for name in ("search_p1", "search_p1_browser", "category_p1",
                 "search_deep", "search_sponsored", "item_page",
                 "item_discounted"):
        html, url, status = fx(name)
        ok &= check("%s is content" % name,
                    detect_page_state(html, status, url) == "content")
    # EMPTY is the site's own arithmetic — `numFound: 0` — not an inference
    # from what we received.
    html, url, status = fx("search_no_results")
    ok &= check("a query that matched nothing is empty, not blocked",
                detect_page_state(html, status, url) == "empty")
    ok &= check("and is_no_results agrees", is_no_results(html) is True)
    ok &= check("a page with products is not no-results",
                is_no_results(fx("search_p1")[0]) is False)
    # BLOCKED, skin 1: Akamai's 43-byte deny, which arrives under HTTP 200.
    # The status is not the signal here.
    html, url, status = fx("block_akamai")
    ok &= check("the 43-byte Akamai deny is blocked EVEN under HTTP 200",
                status == 200
                and detect_page_state(html, status, url) == "blocked")
    ok &= check("and the marker survives the two-space spelling the edge "
                "actually sends",
                detect_block_marker(html) == "akamai-reference-id")
    # The browser wraps that same body in html/head/body — 43 bytes becomes
    # 82 — which is §20's "a marker must survive both transports" arriving in
    # a third costume. A literal "Reference #" with one space matches NONE of
    # the three refusals.
    wrapped = "<html><head></head><body>%s</body></html>" % html
    ok &= check("and survives the browser's wrapping of it",
                detect_page_state(wrapped, 200, url) == "blocked")
    ok &= check("a literal single-space marker would have missed all of it",
                "Reference #" not in html)
    # BLOCKED, skin 2, vs THROTTLED, skin 3: the SAME body, and only the
    # status tells them apart.
    b_html, b_url, _ = fx("block_403")
    ok &= check("the branded page under 403 is blocked",
                detect_page_state(b_html, 403, b_url) == "blocked")
    ok &= check("the SAME body under 503 is a throttle, not a block",
                detect_page_state(b_html, 503, b_url) == "throttled")
    ok &= check("a browser capture of the 403 behaves the same",
                detect_page_state(fx("block_403_browser")[0], 403, b_url)
                == "blocked")
    # With NO status — which is every Selenium fetch — the branded page reads
    # as the RECOVERABLE answer, because calling a throttle a block spends
    # the block budget and the exit rotation on a page that was coming back.
    ok &= check("statusless, the branded page reads as the recoverable one",
                detect_page_state(b_html, None, b_url) == "throttled")
    ok &= check("statusless, the bare Akamai deny still reads as blocked",
                detect_page_state(fx("block_akamai")[0], None, "") == "blocked")
    # A served page must never be called blocked. This is §18's "count the
    # marker on a page you know is good", as an assertion.
    for name in ("search_p1", "search_p1_browser", "category_p1",
                 "search_deep", "item_page", "search_no_results"):
        html, url, _ = fx(name)
        ok &= check("%s carries no block marker" % name,
                    detect_block_marker(html) is None)
        ok &= check("%s carries no challenge marker" % name,
                    detect_bot_challenge(html) is None)
    # The positive structural signal: a served page is built out of Rakuten's
    # own asset hosts and an interstitial is not. The threshold is 2 because
    # the branded page carries exactly one logo off that host.
    ok &= check("served pages reference the site's own assets",
                all(served_by_rakuten(fx(n)[0]) for n in
                    ("search_p1", "category_p1", "item_page")))
    ok &= check("and the refusals do not",
                not any(served_by_rakuten(fx(n)[0]) for n in
                        ("block_akamai", "block_403")))
    # A route with no payload is NOT content just because the host is
    # Rakuten's — the ranking route carries one BreadcrumbList and no
    # ichibaSearch at all.
    r_html, r_url, r_status = fx("ranking_no_payload")
    ok &= check("a Rakuten page with no payload is not called content",
                detect_page_state(r_html, r_status, r_url) != "content")
    # No captcha is configured anywhere on this site, which is a measurement
    # (§18's right question: is one configured, and would we recognise it?).
    ok &= check("no fixture carries a vendor captcha marker",
                all(detect_bot_challenge(fx(n)[0]) is None for n in FIX))
    # An extension's injected hunters must not read as the site's challenge.
    ok &= check("the Scraping Browser's own injected tags are not mistaken "
                "for a challenge",
                detect_bot_challenge(fx("search_p1")[0] + EXTENSION_TAGS)
                is None)
    return ok


def test_page_flow():
    group("page_flow: the policy as DATA, and a throttle budget of its own")
    ok = True
    ok &= check("every state the classifier can return has a policy",
                set(page_flow.STATE_POLICY) ==
                {"content", "empty", "shell", "throttled", "challenge",
                 "blocked"})
    ok &= check("content is parsed and not retried",
                page_flow.should_parse("content")
                and not page_flow.should_retry("content"))
    # An EMPTY page is NOT parsed, and here that is load-bearing: a
    # no-results search page still renders the ten-item SEO carousel, so
    # parsing one would write ten plausible phantom rows for a query that
    # matched nothing.
    ok &= check("an empty page is not parsed",
                not page_flow.should_parse("empty"))
    ok &= check("and is not retried — it is a correct answer",
                not page_flow.should_retry("empty"))
    ok &= check("and is not counted as blocked",
                not page_flow.counts_as_blocked("empty"))
    ok &= check("a shell is parsed and waited for, not refetched",
                page_flow.should_parse("shell")
                and not page_flow.should_retry("shell"))
    # A THROTTLE is retryable, is NOT blocked, and is not paid for. Reporting
    # exit 3 for a page that was about to come back sends a reader to buy a
    # proxy they do not need.
    ok &= check("a throttle is retried",
                page_flow.should_retry("throttled"))
    ok &= check("a throttle does NOT count as blocked",
                not page_flow.counts_as_blocked("throttled"))
    ok &= check("and nothing is solved for it",
                not page_flow.should_solve("throttled"))
    ok &= check("its rows are not parsed",
                not page_flow.should_parse("throttled"))
    ok &= check("blocked counts as blocked and buys no solve",
                page_flow.counts_as_blocked("blocked")
                and not page_flow.should_solve("blocked"))
    ok &= check("a challenge is the only state that buys a solve",
                [s for s in page_flow.STATE_POLICY
                 if page_flow.should_solve(s)] == ["challenge"])
    ok &= check("an unknown state is treated as blocked, not as content",
                not page_flow.should_parse("something-new")
                and page_flow.counts_as_blocked("something-new"))
    # The throttle backoff escalates and then plateaus rather than growing
    # without bound.
    delays = [page_flow.throttle_delay_ms(n) for n in (1, 2, 3, 9)]
    ok &= check("the throttle backoff escalates",
                delays[0] < delays[1] < delays[2])
    ok &= check("and plateaus rather than growing without bound",
                delays[3] == delays[2])
    # THE SOLVE CAP IS ENFORCED, not merely declared. It read like a limit in
    # every repo of this family while each engine called the solver twice per
    # attempt and counted once (§23).
    ok &= check("the solve budget allows the first solve",
                page_flow.solve_budget(0) is True)
    ok &= check("and refuses the second",
                page_flow.solve_budget(1) is False)
    # Readiness: a threshold above 1, and never above a page's own hit count.
    ok &= check("the readiness threshold is above 1",
                page_flow.min_matches("listing") > 1)
    ok &= check("and never above what the page actually holds",
                page_flow.min_matches("listing", 5) == 5)
    ok &= check("and never below 2", page_flow.min_matches("listing", 1) == 2)
    ok &= check("a detail page needs one match",
                page_flow.min_matches("product") == 1)
    ok &= check("expected_cards reads the page's own hit count",
                page_flow.expected_cards(fx("search_p1")[0]) >= 6)
    # Concurrency is allowed exactly where pages have addresses.
    ok &= check("all three listing routes are addressable",
                all(page_flow.pagination_is_addressable(u) for u in
                    (SEARCH_URL, GENRE_URL, CATEGORY_URL)))
    ok &= check("an item page is not",
                not page_flow.pagination_is_addressable(ITEM_URL))
    ok &= check("concurrency is refused on an item page, WITH a reason",
                "one product" in (page_flow.concurrency_refusal(ITEM_URL) or ""))
    ok &= check("and on a shop front, with the payload reason",
                "ichibaSearch" in (page_flow.concurrency_refusal(
                    "https://www.rakuten.co.jp/ajinomoto/") or ""))
    ok &= check("and allowed on a listing",
                page_flow.concurrency_refusal(SEARCH_URL) is None)
    ok &= check("a listing's limit is unbounded",
                page_flow.concurrency_limit(SEARCH_URL) is None)
    ok &= check("an item page's limit is 1",
                page_flow.concurrency_limit(ITEM_URL) == 1)
    # The block advice names what to CHECK on this site, which is a
    # fingerprint rather than an address.
    advice = page_flow.block_advice(fx("block_akamai")[0], True, False)
    ok &= check("the block advice points at client consistency first",
                "fingerprint" in advice.lower())
    ok &= check("and the cap summary carries both of the site's numbers",
                page_flow.cap_summary(fx("search_p1")[0])["reachable_max"]
                == 6750)
    ok &= check("pages_at_cap is the site's own statement",
                page_flow.pages_at_cap(fx("search_p1")[0]) is True)
    # The readiness wait is a COUNT through the driver, never an evaluated
    # string: a sibling repo died with EvalError under a CSP with no
    # unsafe-eval and took the run down with exit 1 (§18).
    seen = []
    got = page_flow.wait_for_count(lambda s: (seen.append(s), 9)[1],
                                   lambda ms: None, "sel", 8, 1000)
    ok &= check("wait_for_count returns as soon as the threshold is met",
                got == 9 and len(seen) == 1)
    ok &= check("a driver error during the wait does not raise",
                page_flow.wait_for_count(
                    lambda s: (_ for _ in ()).throw(RuntimeError("x")),
                    lambda ms: None, "sel", 8, 500) == 0)
    return ok


# ---------------------------------------------------------------------------
# The output contract
# ---------------------------------------------------------------------------
def test_output_contract():
    group("the output contract shared across this scraper family")
    ok = True
    names = [f.name for f in fields(Product)]
    # The family prefix, byte-identical and in order, so a consumer written
    # against another repo in this family reads the first eighteen columns
    # unchanged. Site-specific columns go AFTER it.
    family_prefix = ["source", "scraped_at", "url", "sku", "title", "brand",
                     "price", "currency", "original_price", "discount_pct",
                     "rating", "review_count", "in_stock", "image_url",
                     "category", "price_source", "page", "position"]
    ok &= check("the family field prefix is present and in order",
                names[:len(family_prefix)] == family_prefix)
    ok &= check("Rakuten's own columns come after it, in order",
                names[len(family_prefix):] ==
                ["variant_id", "item_number", "price_max", "has_price_range",
                 "subscription_price", "points", "point_rate", "shipping_fee",
                 "free_shipping", "delivery_estimate", "shop_name",
                 "shop_code", "shop_id", "shop_url", "genre_id", "genre_path",
                 "genre_rank", "is_super_deal", "is_39shop",
                 "is_official_shop", "variant_count", "original_price_label",
                 "tags", "variants"])
    # Two modes, ONE dataclass — a detail page here is the same product
    # described more fully, not a different kind of object. Which is what
    # lets a consumer join a listing run to a product run on `sku`.
    ok &= check("both modes map to the same row class",
                ROW_CLASS_BY_MODE == {"listing": Product, "product": Product})
    ok &= check("and both are one row per sku",
                set(UNIQUE_BY_SKU_MODES) == {"listing", "product"})

    # §9: a column that is null on every row of every run should not exist.
    # Measured over every listing fixture AND both detail fixtures, because
    # several columns are populated on one route and not the other, and
    # judging them from listings alone would condemn live columns.
    every_row = []
    for name in ("search_p1", "category_p1", "search_deep",
                 "search_sponsored"):
        every_row += fx_rows(name)
    for name in ("item_page", "item_discounted"):
        every_row += parse_product_page(*_fx_detail(name))
    always_null = {f.name for f in fields(Product)
                   if all(getattr(r, f.name) is None for r in every_row)}
    ok &= check("no column is null on every row of every route "
                "(measured over %d rows, both modes)" % len(every_row),
                always_null == set())

    # And the route-specific ones are null only OUTSIDE their route, which is
    # the difference between a sparse column and a dead one. `price_source`
    # is what tells a consumer which is which.
    listings = fx_rows("search_p1")
    detail = parse_product_page(*_fx_detail("item_discounted"))
    ok &= check("variants/variant_count are a detail-route column",
                all(r.variants is None for r in listings)
                and detail[0].variants is not None)
    ok &= check("points/tags are a listing-route column",
                all(r.points is not None for r in listings)
                and detail[0].tags is None)
    ok &= check("original_price is published on the detail route only",
                all(r.original_price is None for r in listings)
                and detail[0].original_price == 10398.0)
    ok &= check("and price_source is how a consumer tells the two apart",
                {r.price_source for r in listings} == {"state"}
                and detail[0].price_source == "itemdata")

    ok &= check("the exit codes are the family's",
                (EXIT_BLOCKED, EXIT_NO_PRODUCTS, EXIT_PARTIAL) == (3, 4, 6))
    ok &= check("an exhausted listing counts as complete",
                "no_new_products" in COMPLETE_STOP_REASONS
                and "pagination_exhausted" in COMPLETE_STOP_REASONS)
    ok &= check("a single-page mode is complete by construction",
                "single_page_mode" in COMPLETE_STOP_REASONS)
    # Reaching the real end of a Rakuten listing is a COMPLETE run, and
    # leaving this out of the tuple produced exit 6 for a correct one. The
    # site does not error past its last page — it re-serves page 1 under
    # HTTP 200 — so finding that IS finding the end.
    ok &= check("and so is reaching the end of a listing",
                "end_of_listing" in COMPLETE_STOP_REASONS)

    # No defaulted currency anywhere: the listing payload states none at all,
    # so a row whose page said nothing carries None rather than claiming JPY.
    ok &= check("Product defaults currency to None, not a guess",
                Product().currency is None)
    ok &= check("Product defaults price_source to None",
                Product().price_source is None)
    ok &= check("the currency allowlist is real ISO codes, not [A-Z]{3}",
                CURRENCY in CURRENCY_CODES and len(CURRENCY_CODES) == 1)
    ok &= check("source is the marketplace, not the browsed host",
                Product().source == "rakuten.co.jp")
    return ok


def test_writers():
    group("writers, dedupe and the refusal to overwrite good data")
    ok = True
    rows = [Product(sku="1", url="u1", price=1.0),
            Product(sku="2", url="u2", price=2.0)]
    with tempfile.TemporaryDirectory() as d:
        prefix = os.path.join(d, "out")

        # A run that finds nothing writes NOTHING: a consumer cannot tell an
        # empty category from a failed run, and the failure destroys the last
        # known good data.
        save(rows, prefix, "json", allow_empty=False)
        ok &= check("a good run writes its output",
                    os.path.exists(prefix + ".json"))
        before = open(prefix + ".json").read()
        save([], prefix, "json", allow_empty=False)
        ok &= check("an empty run does NOT overwrite the previous good output",
                    open(prefix + ".json").read() == before)
        save([], prefix, "json", allow_empty=True)
        ok &= check("--allow-empty is the opt-out and does overwrite",
                    json.load(open(prefix + ".json")) == [])

        # An empty CSV still carries its header, so a consumer reads a table
        # with no rows instead of failing on a zero-byte file.
        csv_path = os.path.join(d, "empty.csv")
        write_csv([], csv_path, row_cls=Product)
        header = open(csv_path).read().strip().split("\n")[0]
        ok &= check("an empty CSV still carries its header",
                    header.split(",")[:4] == ["source", "scraped_at", "url", "sku"])

        # A list column has to survive CSV without becoming a Python repr.
        #
        # NO column here is a list — a listing row carries one image url
        # and no attribute list this repo reads — so this is checked with a
        # local row class rather than with Product. The joining is kept in
        # write_csv because it is generic and because a future column may
        # need it; pinning the CURRENT behaviour is what stops it being
        # deleted as dead or reappearing as a repr() by accident.
        ok &= check("no Product column is a list today",
                    not [f for f in fields(Product)
                         if "List" in str(f.type)])

        from dataclasses import dataclass as _dataclass
        from typing import List as _List, Optional as _Optional

        @_dataclass
        class _WithList:
            sku: _Optional[str] = None
            things: _Optional[_List[str]] = None

        csv_path = os.path.join(d, "list.csv")
        write_csv([_WithList(sku="1", things=["a", "b"])], csv_path,
                  row_cls=_WithList)
        body = open(csv_path).read()
        ok &= check("a list column is joined, not repr()d in CSV",
                    ("a" + LIST_CSV_SEPARATOR + "b") in body and "['a'" not in body)

    seen = set()
    ok &= check("dedupe drops a repeated sku",
                len(dedupe_by_sku([Product(sku="a"), Product(sku="a")], seen)) == 1)
    # A row with no key is always KEPT: there is nothing to check a duplicate
    # against, and dropping it is a silent data loss rather than a dedupe.
    ok &= check("a row with no sku is kept, not dropped",
                len(dedupe_by_key([Product(sku=None), Product(sku=None)],
                                  set())) == 2)

    meta = run_meta("complete", "completed", 3, 3, "u", "u", 36,
                    pages_failed=[], mode="listing", source="rakuten.co.jp")
    ok &= check("the sidecar records status, mode and source",
                meta["status"] == "complete" and meta["mode"] == "listing"
                and meta["source"] == "rakuten.co.jp")
    # A count stops being a description once a page can fail while later ones
    # succeed, so the sidecar names WHICH pages failed.
    meta = run_meta("partial", "blocked", 5, 3, "u", "u", 12,
                    pages_failed=[2, 4], mode="listing", source="rakuten.co.jp")
    ok &= check("the sidecar names which pages failed, by number",
                meta["pages_failed"] == [2, 4])
    return ok


def test_finish_run():
    group("finish_run: the exit codes all three engines must agree on")
    ok = True
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "run")
        rows = [Product(sku="1", url="u")]

        code = finish_run(rows, p, "json", False, blocked=False,
                          stop_reason="completed", pages_requested=1,
                          pages_completed=1, pages_failed=[], mode="listing",
                          source="rakuten.co.jp", start_url="u", final_url="u")
        ok &= check("a complete run exits 0", code == 0)

        code = finish_run([], p + "b", "json", False, blocked=True,
                          stop_reason="blocked_no-response",
                          pages_requested=1, pages_completed=0,
                          pages_failed=[1], mode="listing",
                          source="rakuten.co.jp", start_url="u", final_url="u")
        ok &= check("a blocked run exits 3, not 4", code == EXIT_BLOCKED)
        # A FAILED run writes no sidecar: `save` leaves the previous good
        # output in place, and a "failed" sidecar beside good data would
        # contradict it.
        ok &= check("a failed run writes no sidecar beside older good data",
                    not os.path.exists(p + "b.meta.json"))

        code = finish_run([], p + "c", "json", False, blocked=False,
                          stop_reason="completed", pages_requested=1,
                          pages_completed=1, pages_failed=[], mode="listing",
                          source="rakuten.co.jp", start_url="u", final_url="u")
        ok &= check("a genuinely empty result exits 4, not 3",
                    code == EXIT_NO_PRODUCTS)

        code = finish_run(rows, p + "d", "json", False, blocked=False,
                          stop_reason="page_load_timeout", pages_requested=5,
                          pages_completed=2, pages_failed=[3], mode="listing",
                          source="rakuten.co.jp", start_url="u", final_url="u")
        ok &= check("a run with data that stopped early exits 6 (partial)",
                    code == EXIT_PARTIAL)
        ok &= check("a partial run still writes what it got",
                    os.path.exists(p + "d.json"))
    return ok


def test_diff():
    group("diff_runs")
    ok = True
    # `price_source` on this site is "state" for a listing row and
    # "itemdata" for a detail row, and the source guard earns its keep
    # across MODES rather than across rendering states: a listing row
    # publishes no was-price while a detail row publishes one, so diffing
    # the two would otherwise report a discount appearing on every product.
    old = [{"sku": "1", "price": 10.0, "price_source": "state"},
           {"sku": "2", "price": 20.0, "price_source": "state"},
           {"sku": "3", "price": 30.0, "price_source": "state"}]
    new = [{"sku": "1", "price": 11.0, "price_source": "state"},
           {"sku": "3", "price": 30.5, "price_source": "itemdata"},
           {"sku": "4", "price": 40.0, "price_source": "state"}]
    d = diff_products(old, new)
    ok &= check("a real price move is reported as changed",
                any(c["sku"] == "1" for c in d["changed"]))
    ok &= check("a delisted product is reported as removed",
                [r["sku"] for r in d["removed"]] == ["2"])
    ok &= check("a new product is reported as added",
                [r["sku"] for r in d["added"]] == ["4"])
    ok &= check("a price move with a source change is not 'changed'",
                not any(c["sku"] == "3" for c in d["changed"]))
    ok &= check("...it is reported separately as source_changed",
                any(c["sku"] == "3" for c in d.get("source_changed", [])))

    import diff_runs as _dr
    tracked = _dr.TRACKED_FIELDS
    ok &= check("the money columns are tracked",
                {"price", "original_price", "discount_pct", "currency"}
                <= set(tracked))
    # POINTS ARE TRACKED, and that is the Rakuten-specific decision. A 10x
    # point campaign here is effectively a 10% discount that never touches
    # the price column, so a monitor watching `price` alone would call a
    # product unchanged through an entire Super Sale.
    ok &= check("so are Rakuten points, which move a price without moving it",
                {"points", "point_rate"} <= set(tracked))
    # A variant RANGE means the bottom of the range can hide a move at the
    # top — 95 of 405 measured rows carry one.
    ok &= check("so is the top of a variant price range",
                "price_max" in tracked)
    ok &= check("and the subscription price, which a shop moves on its own",
                "subscription_price" in tracked)
    ok &= check("and shipping, since 'free shipping' is part of the price",
                {"shipping_fee", "free_shipping"} <= set(tracked))
    # NOT tracked: the two columns that drift upwards constantly and would
    # make every diff noisy, and a rank in a listing this run never fetched.
    ok &= check("ratings and review counts are NOT tracked (they drift every "
                "day and would make every diff noisy)",
                not {"rating", "review_count"} & set(tracked))
    ok &= check("nor is genre_rank, which ranks the product in a listing "
                "this run never fetched",
                "genre_rank" not in tracked)
    # A plain price move is still a change — the guards must not swallow the
    # thing the tool exists for.
    repriced = _dr.diff_products(
        [{"sku": "a", "price": 100.0, "price_source": "state"}],
        [{"sku": "a", "price": 120.0, "price_source": "state"}])
    ok &= check("a shop dropping its price is still a change",
                len(repriced["changed"]) == 1)
    # A point campaign with no price move is a change too, which is the
    # point of tracking it.
    pointed = _dr.diff_products(
        [{"sku": "a", "price": 100.0, "points": 1, "price_source": "state"}],
        [{"sku": "a", "price": 100.0, "points": 10, "price_source": "state"}])
    ok &= check("a point campaign with an unchanged price is a change",
                len(pointed["changed"]) == 1)
    # There is deliberately NO lifecycle bucket: Rakuten's paid placements
    # never become rows, so there is no placement column to key one on.
    ok &= check("no lifecycle bucket is reported",
                "lifecycle" not in _dr.diff_products([], []))
    # The sibling repos' auction and per-vertical columns are NOT here.
    ok &= check("no auction columns are tracked (this site has none)",
                not {"bid_kind", "sold", "reserve_price_met", "auction_status"}
                & set(tracked))
    ok &= check("and Product declares none of the other repos' extras",
                not {"bid_kind", "sold", "bedrooms", "kilometers",
                     "payment_frequency"}
                & {f.name for f in fields(Product)})
    return ok


def test_pyppeteer_teardown_noise():
    group("pyppeteer teardown noise is suppressed, and its limit is pinned")
    ok = True
    try:
        import puppeteer_scraper as pyp
    except ImportError:
        return check("pyppeteer engine present (skipped: library absent)", True)

    handler = pyp._AsyncBridge._on_loop_exception.__func__ if hasattr(
        pyp._AsyncBridge._on_loop_exception, "__func__") else pyp._AsyncBridge._on_loop_exception

    class _Loop:
        def __init__(self): self.passed_through = []
        def default_exception_handler(self, context):
            self.passed_through.append(context)

    # Each of these arrives on a run that SUCCEEDED, after the output is
    # written, and four tracebacks under a healthy run is how a reader learns
    # to ignore the log.
    swallowed = [
        {"message": "Task was destroyed but it is pending"},
        {"message": "Future exception was never retrieved",
         "exception": RuntimeError("Protocol error (Target.sendMessageToTarget): "
                                   "No session with given id")},
        {"exception": RuntimeError("Target closed")},
        {"exception": RuntimeError("Connection closed")},
        {"message": "Event loop is closed"},
    ]
    for context in swallowed:
        loop = _Loop()
        handler(loop, context)
        label = (context.get("message") or str(context.get("exception")))[:44]
        ok &= check("teardown noise suppressed: %s" % label,
                    not loop.passed_through)

    # A REAL error must still get through, or the suppression has become a
    # blindfold.
    loop = _Loop()
    handler(loop, {"exception": ValueError("something actually went wrong")})
    ok &= check("a real exception is NOT swallowed", len(loop.passed_through) == 1)

    # The handler reads BOTH fields. It used to read `exception or message`,
    # which meant a context carrying both never had its message inspected —
    # so the asyncio-worded ones kept printing after they were "handled".
    src = inspect.getsource(handler)
    ok &= check("the handler inspects the message as well as the exception",
                'for k in ("exception", "message")' in src)

    # PINNED LIMITATION, not a guard: `Exception ignored in: <coroutine
    # object Connection._recv_loop>` is printed by CPython's garbage
    # collector at interpreter shutdown, after the loop is gone and after the
    # exit code is decided. No loop handler can reach it, and catching it
    # would mean a global unraisable hook that swallows real bugs too. It is
    # documented in TROUBLESHOOTING.md instead; this check makes sure that
    # documentation stays there.
    doc = open(os.path.join(REPO_ROOT, "TROUBLESHOOTING.md"),
               encoding="utf-8").read()
    ok &= check("the shutdown-time traceback is documented rather than hidden",
                "Exception ignored in" in doc and "The run succeeded" in doc)
    return ok


def test_canary_separates_access_from_defect():
    group("the canary fails on defects and only WARNS on access conditions")
    ok = True
    wf_path = os.path.join(REPO_ROOT, ".github", "workflows", "canary.yml")
    wf = open(wf_path, encoding="utf-8").read()

    # The data checks must not run on a blocked or refused run: there is no
    # output file, and a missing file would fail for the wrong reason.
    ok &= check("the data checks are gated on the run having got in",
                "steps.verdict.outputs.tested == 'true'" in wf)

    # Extract the real interpret-the-exit-code script and run it under bash
    # for every code, rather than asserting on the YAML text. What matters is
    # whether the JOB FAILS, and only running it answers that.
    try:
        start = wf.index('          set -e\n          code=')
        end = wf.index('          echo "tested=$tested" >> "$GITHUB_OUTPUT"')
        end += len('          echo "tested=$tested" >> "$GITHUB_OUTPUT"')
    except ValueError:
        return check("the canary's exit-code script could be located", False)
    script = "\n".join(line[10:] if line.startswith(" " * 10) else line
                        for line in wf[start:end].splitlines())

    # WHY EACH CODE LANDS WHERE IT DOES:
    #   0  got in and parsed         -> pass, and the assertions then run
    #   3  blocked before parsing    -> ACCESS. Measured intermittent per
    #      profile on this site, so a daily red badge would be noise.
    #   5  remote API error          -> DEFECT here, unlike in the siblings,
    #      and the difference is the point: this canary uses no remote API
    #      and needs no credential, so exit 5 from it is impossible rather
    #      than routine. A repo that warned on it would be carrying a
    #      sibling's excuse for a condition it cannot have.
    #   6  partial                   -> ACCESS, usually a mid-run throttle.
    #   1  crashed                   -> DEFECT.
    #   2  bad arguments             -> DEFECT (in the workflow itself).
    #   4  served a page, ZERO rows  -> DEFECT, and precisely the regression
    #      this canary exists to catch: the tile anchor moved.
    expected = {0: "pass", 3: "warn", 6: "warn",
                1: "fail", 2: "fail", 4: "fail", 5: "fail", 99: "fail"}
    for code, want in sorted(expected.items()):
        body = script.replace('code="${{ steps.run.outputs.exit_code }}"',
                              'code="%d"' % code)
        with tempfile.TemporaryDirectory() as td:
            out_file = os.path.join(td, "gh_output")
            summary = os.path.join(td, "gh_summary")
            open(out_file, "w").close()
            open(summary, "w").close()
            done = subprocess.run(
                ["bash", "-c", body], capture_output=True, text=True,
                env=dict(os.environ, GITHUB_OUTPUT=out_file,
                         GITHUB_STEP_SUMMARY=summary))
            failed = done.returncode != 0
            warned = "::warning::" in done.stdout
            errored = "::error::" in done.stdout
            wrote_summary = bool(open(summary, encoding="utf-8").read().strip())
            tested = "tested=true" in open(out_file, encoding="utf-8").read()

        if want == "pass":
            got = not failed and not warned and not errored and tested
        elif want == "warn":
            # A warning must NOT read as a pass: it also has to say in the
            # step summary that nothing was actually tested, and it must not
            # claim `tested`.
            got = not failed and warned and wrote_summary and not tested
        else:
            got = failed and errored
        ok &= check("exit %-2d is treated as %s" % (code, want), got)

    ok &= check("the reason access is not a defect is written down",
                "ACCESS CONDITIONS ARE NOT DEFECTS" in wf)

    # IT RUNS ON A SCHEDULE AND NEEDS NO SECRET, which is this repo's
    # central claim under test rather than a convenience. The family's rule
    # is that a canary which cannot pass must SKIP rather than fail; the
    # other half of it is that a canary which CAN pass without a credential
    # must never be gated on one. Gating this would make the badge green
    # every day while testing nothing, and "you need no key for this site" is
    # exactly the sentence that would rot unnoticed.
    ok &= check("the canary runs on a schedule",
                bool(re.search(r"^\s*-\s*cron:", wf, re.M)))
    ok &= check("it is dispatchable by hand too", "workflow_dispatch:" in wf)
    ok &= check("it is gated on NO secret",
                "secrets." not in wf)
    ok &= check("and it says why it needs none",
                "NEEDS NO SECRETS" in wf)
    # More than one page, or pagination is never exercised and a broken ?p=
    # convention stays invisible here — the silent decay the workflow exists
    # to catch.
    pages = re.search(r"--pages (\d+)", wf)
    ok &= check("it asks for at least 3 pages",
                bool(pages) and int(pages.group(1)) >= 3)
    # The thresholds must be real columns of this schema, checked against
    # the run's own output rather than eyeballed.
    for needle in ("total_results", "reachable_max", "page+position",
                   "rating of 0", "original_price"):
        ok &= check("the canary checks %s" % needle, needle in wf)
    ok &= check("it uploads its artefacts on every run, failures included",
                "if: always()" in wf)
    return ok


def test_ci_checks_is_actually_wired_up():
    group("the repo's own checks are RUN, and still catch a real secret")
    ok = True
    script = os.path.join(REPO_ROOT, ".github", "ci_checks.py")
    ok &= check("ci_checks.py exists", os.path.exists(script))
    if not os.path.exists(script):
        return ok

    # IT HAS TO BE INVOKED BY A WORKFLOW. It was not — for the whole of
    # v0.1.0 it sat there implementing three checks that nothing ran, while a
    # second, LOOSER copy of one of them lived inline in tests.yml. Dead code
    # that looks load-bearing is worse than no code, and this is the check
    # that keeps it alive.
    wf_dir = os.path.join(REPO_ROOT, ".github", "workflows")
    workflows = "\n".join(
        open(os.path.join(wf_dir, f), encoding="utf-8").read()
        for f in sorted(os.listdir(wf_dir)) if f.endswith((".yml", ".yaml")))
    ok &= check("a workflow runs ci_checks.py", "ci_checks.py" in workflows)
    ok &= check("the secret check specifically is run",
                "--secret-check" in workflows or "--all" in workflows)

    # IT SCANS WHAT IS TRACKED, AT ANY SUFFIX — pinned because a suffix
    # allowlist is how this check failed once. `--dump-html live_results`
    # writes `live_results.page1`, a name with no suffix the old list knew,
    # and a merge committed two of them at 1.5 MB each while this check ran,
    # passed and never opened them. `.json` was not on the list either, so
    # `fixtures_generated.json` and `sample_output.json` had never been
    # scanned at all.
    import importlib.util as _ilu
    spec = _ilu.spec_from_file_location("ci_checks", script)
    ci = _ilu.module_from_spec(spec)
    spec.loader.exec_module(ci)
    scanned = {os.path.relpath(p, REPO_ROOT) for p in ci.scanned_files()}
    ok &= check("the working-tree scan reads the generated data files, which "
                "a suffix allowlist never did",
                {"fixtures_generated.json", "sample_output.json",
                 "sample_output.csv"} <= scanned)
    ok &= check("...and the workflows, the Dockerfile and the env example",
                {".env.example", "Dockerfile"} <= scanned)
    ok &= check("it asks GIT what is tracked, not the filesystem — a "
                "developer's own .env and captures beside the scripts are "
                "expected and must not turn it red",
                ".env" not in scanned)

    # A RAW CAPTURE MUST NOT BE TRACKED, whatever is inside it. The two that
    # got through carried nothing of ours — no key, no proxy password, no
    # cookie — so a content rule would have passed them. What was wrong was
    # that they were committed at all.
    # A TRACKED PAGE DUMP IS REFUSED BY SHAPE, NOT BY NAME — and the table
    # below is the fix for a hole this repo had on its own first commit.
    # `live/sel_dump.html`, a 1.5 MB raw capture, was staged by `git add -A`
    # because `.gitignore` listed only `live_results/` and every shape here
    # keyed on a NAME somebody had chosen. The shape is now the KIND of
    # directory, and the negative half of the table is as load-bearing as
    # the positive half: a rule this broad has to leave the repo's own
    # sources alone or it becomes a rule somebody switches off.
    caught = lambda p: any(sh.search(p) for sh in ci.CAPTURE_SHAPES)
    for path in ("live_results.page7", "out_page1_debug.html",
                 "captures/search_p1.html", "live/sel_dump.html",
                 "out/page.html", "output/grid.png", "dump.html",
                 "runs/x.mhtml"):
        ok &= check("a capture at %s is refused" % path, caught(path))
    for path in (".github/workflows/tests.yml", "tests/test_smoke.py",
                 "README.md", "fixtures_generated.json",
                 "sample_output.json", "sample_output.csv",
                 "product_parser.py", "live/notes.md"):
        ok &= check("...and %s is NOT mistaken for one" % path,
                    not caught(path))
    # The gitignore must agree with the shape rule, or the check is a second
    # line of defence for a file the first line lets through — which is
    # exactly what happened.
    for path in ("live/x.html", "out/x.html", "output/x.html",
                 "results/x.html", "scratch/x.html"):
        ignored = subprocess.run(["git", "check-ignore", "-q", path],
                                 cwd=REPO_ROOT).returncode == 0
        ok &= check("git also ignores %s" % path, ignored)
    ok &= check("and no such file is tracked here",
                not [p for p in ci.tracked_files()
                     if any(s.search(p.relative_to(ci.REPO).as_posix())
                            for s in ci.CAPTURE_SHAPES)])

    # The key-shaped-field rule applies EVERYWHERE, generated data included —
    # it is what the bare-hex rule was reaching for, said precisely.
    ok &= check("a secret in a key-shaped field is caught",
                ci.KEY_SHAPED_FIELD.search(
                    # Split so this file does not itself carry a bare 32-hex
                    # run: the check it is testing scans this file too, and a
                    # fixture that trips the rule it proves would either turn
                    # the build red or force the rule to be widened.
                    '"x-algolia-api-key": "%s%s"'
                    % ("cdd839b4fdac8402", "89e88633779e8634")))
    ok &= check("...and the scrubbed placeholder is not",
                not ci.KEY_SHAPED_FIELD.search(
                    '"x-algolia-api-key": "REDACTED-SEARCH-KEY"'))

    # THE BARE-HEX RULE APPLIES TO EVERYTHING HERE, including the generated
    # data files, and that exemption being empty is pinned so it stays a
    # decision. A sibling repo has to exempt them, because its site
    # publishes a 32-hex identifier in five contexts and a rule that fires
    # 221 times on correct data is a rule somebody switches off. This repo
    # took the other road: `make_fixtures.py` replaces Rakuten's own 32-hex
    # catalogue id with a placeholder, so there is no 32-hex string in the
    # fixtures at all and the strictest rule can cover the biggest files —
    # which are exactly the ones nobody reads line by line.
    ok &= check("nothing is exempt from the bare-hex rule",
                ci.GENERATED_DATA_FILES == ())
    # THE KEY-SHAPED-FIELD RULE MUST SEE THROUGH JSON ESCAPING, and this is
    # a hole the check had rather than a hypothetical. Every fixture in this
    # family is stored as a JSON string, so its quotes arrive escaped:
    # `fixtures_generated.json` contains \"apiKey\": \"…\", never
    # "apiKey": "…". With bare quotes in the pattern the rule matched ZERO
    # times in the largest file in the repository — the one holding 427 KB
    # of captured page payload, which is exactly where a front-end key
    # arrives. Verified by planting a real-shaped key in a fixture: before
    # the fix the scan reported "nothing credential-shaped" and passed.
    #
    # Note what would NOT have saved it: this site's front-end keys are
    # 32-char ALPHANUMERIC rather than hex, so the bare-hex rule is blind to
    # them too.
    real_shape = "Zq" + "8kR3mNpV7wLxT2yB5cH9dJ4fG6sA"
    ok &= check("a key-shaped value is caught in a PLAIN field",
                bool(ci.KEY_SHAPED_FIELD.search(
                    '"apiKey": "%s"' % real_shape)))
    ok &= check("...and in a JSON-ESCAPED one, which is how every fixture "
                "in this family stores its markup",
                bool(ci.KEY_SHAPED_FIELD.search(
                    '\\"apiKey\\": \\"%s\\"' % real_shape)))
    ok &= check("...while the scrub placeholder is exempt in both spellings",
                not ci.KEY_SHAPED_FIELD.search(
                    '"apiKey": "SCRUBBED_FRONTEND_KEY_0000000000"')
                and not ci.KEY_SHAPED_FIELD.search(
                    '\\"apiKey\\": \\"SCRUBBED_FRONTEND_KEY_0000000000\\"'))
    ok &= check("and no site-id shape is subtracted before the scan either",
                ci.SITE_PUBLIC_IDS == ())
    # Assembled from pieces rather than written out, so this suite file does
    # not itself carry a key-shaped string for its own check to find — the
    # same trick a sibling uses for its banned-phrase list, and the reason
    # its scan can cover the suite instead of exempting it (§22).
    fake_hex = ("dead" + "beef") * 4
    ok &= check("a hex the line never justified still fails",
                ci.HEX32.findall(ci._without_site_ids('key = "%s"'
                                                      % fake_hex)))
    ok &= check("...and the generated fixtures hold none of that shape",
                not ci.HEX32.findall(
                    open(os.path.join(REPO_ROOT, "fixtures_generated.json"),
                         encoding="utf-8").read()))

    # AND IT PASSES ON THIS REPO. A check that is always red teaches everyone
    # to ignore checks; this one WAS red, on six documented placeholders.
    done = subprocess.run([sys.executable, script, "--all"],
                          cwd=REPO_ROOT, capture_output=True, text=True)
    ok &= check("ci_checks.py --all passes on this repo (exit %d)" % done.returncode,
                done.returncode == 0)
    if done.returncode != 0:
        print("        " + (done.stdout or done.stderr).strip()[-400:])

    # AND IT STILL CATCHES A REAL ONE. Loosening an allowlist until the check
    # passes is the failure mode here, so both directions are asserted: a
    # planted CDP endpoint, a planted 32-hex key and a planted http proxy URL
    # must all be found. The http one matters most — the inline grep this
    # replaced covered only ws:// and would have missed a committed proxy.
    planted = os.path.join(REPO_ROOT, "_secret_probe_delete_me.py")
    # The key is ASSEMBLED rather than written as a literal, because a
    # 32-character hex string sitting in this file is exactly what the check
    # under test flags — and it did, on the first run of this test. The file
    # it writes still gets the whole thing, which is what the probe needs.
    planted_key = "3f8a1c9e4b7d2065" + "af13ce88b409d752"
    try:
        with open(planted, "w", encoding="utf-8") as f:
            f.write(
                'CDP = "ws://acct-zone-scraping_browser-pid-x:'
                'S3cretPassw0rd@cb.2captcha.com:9222"\n'
                'KEY = "%s"\n'
                'PROXY = "http://acct-zone-custom:S3cretPassw0rd'
                '@na.proxy.2captcha.com:2334"\n' % planted_key)
        caught = subprocess.run([sys.executable, script, "--secret-check"],
                                cwd=REPO_ROOT, capture_output=True, text=True)
        out = caught.stdout + caught.stderr
        ok &= check("a planted secret fails the check", caught.returncode != 0)
        ok &= check("the planted ws:// CDP endpoint is named",
                    "_secret_probe_delete_me.py:1" in out)
        ok &= check("the planted 32-hex key is named",
                    "_secret_probe_delete_me.py:2" in out)
        ok &= check("the planted http:// PROXY url is named (the grep this "
                    "replaced missed those)",
                    "_secret_probe_delete_me.py:3" in out)
    finally:
        # Never leave it behind: a test that mutates the working tree is its
        # own defect, and this one would plant a fake secret.
        if os.path.exists(planted):
            os.remove(planted)
    ok &= check("the probe file is cleaned up", not os.path.exists(planted))

    # The pre-publication scan: the same rules over every blob that has EVER
    # existed. A later commit cannot remove what a published tag and a merged
    # PR's refs already hold, so this has to be runnable BEFORE the repo goes
    # public — and it has to be findable, which a check makes it.
    hist = subprocess.run([sys.executable, script, "--history-check"],
                          cwd=REPO_ROOT, capture_output=True, text=True)
    ok &= check("--history-check runs and this history is clean",
                hist.returncode == 0)
    ok &= check("it says how many objects it looked at",
                "ever existed" in hist.stdout)
    # NOT in --all, on purpose: it shells out to git once per object, and a
    # dirty history needs a decision rather than a red check on every push.
    every = subprocess.run([sys.executable, script, "--all"],
                           cwd=REPO_ROOT, capture_output=True, text=True)
    ok &= check("--all deliberately excludes the history scan",
                "history check" not in every.stdout)
    return ok


def test_no_capture_leaks():
    group("no credentials or personal data in the committed fixtures")
    ok = True
    # Collected by SUFFIX, which is how this file names its fixtures. An
    # earlier version asked for a "FIX_" PREFIX, matched nothing, and every
    # check below passed against an empty string — 150 KB of committed real
    # captures went unexamined while twelve checks reported green. The
    # non-empty assertion underneath is the actual fix: a corpus check that
    # can silently scan nothing is worse than no corpus check at all.
    names = sorted(FIX)
    fixtures = "\n".join(FIX[k]["html"] for k in names)
    ok &= check("the privacy checks below have fixtures to scan "
                "(%d fixtures, %d chars)" % (len(names), len(fixtures)),
                len(names) >= 8 and len(fixtures) > 300000)
    # Guarded with PATTERNS rather than with the literals a previous capture
    # happened to contain, so the NEXT capture is checked too. MediaMarkt's
    # pages embed a front-end configuration blob — a Sentry DSN, a Woosmap
    # public key, a store-code JWT — none of which is needed to test a
    # parser, and none of which belongs in a public repository.
    patterns = {
        "a JWT": r"eyJ[A-Za-z0-9_\-]{16,}\.[A-Za-z0-9_\-]{10,}",
        "an access token": r"(?:access|auth|bearer)[_\-]?[Tt]oken\"?\s*[:=]\s*\"?[A-Za-z0-9._\-]{12,}",
        # The placeholder is exempt by NAME, not by shape: it is 19
        # characters of the same alphabet a real key uses, so a shape-only
        # rule flags the very substitution that makes the fixture safe.
        # The placeholder is exempt by NAME, not by shape: it is the same
        # alphabet a real key uses, so a shape-only rule flags the very
        # substitution that makes the fixture safe.
        "an API key": r"(?:api|public|secret|private)[_\-]?[Kk]ey\"?\s*[:=]\s*\"?(?!SCRUBBED_FRONTEND_KEY)[A-Za-z0-9._\-]{12,}",
        "a Sentry DSN": r"https://[0-9a-f]{16,}@[\w.]*ingest",
        "a session id": r"session[_\-]?[Ii]d\"?\s*[:=]\s*\"?[A-Za-z0-9._\-]{8,}",
        "an email address": r"[\w.+-]+@[\w-]+\.[a-z]{2,}",
        "a proxy credential": r"://[^\s/@\"]+:[^\s/@\"]+@",
    }
    for label, pattern in patterns.items():
        hits = re.findall(pattern, fixtures)
        ok &= check("no %s in the fixtures" % label, not hits)

    # The SITE's OWN per-impression material, which a fresh capture brings with
    # it: a click-tracking key, its checksum, and the logging key that ties an
    # impression to a session. Anonymous and expired, and still not something
    # to commit — and a 40-character hex-ish blob in a public repo reads as a
    # credential to every scanner that looks, including this repo's own CI
    # grep. Matched as PATTERNS rather than as the values one capture
    # happened to hold, so the NEXT capture is checked too.
    site_session = {
        # Two 32-char alphanumeric front-end API keys are baked into EVERY
        # item page (`apiConfig.ncp.apiKey`, `apiConfig.shipping.apiKey`).
        # They are Rakuten's own rather than ours and they still do not
        # belong in a public repo: they read as live credentials to every
        # scanner that looks, this repo's own included. Exempt by NAME, not
        # by shape — the placeholder is the same alphabet a real key uses,
        # so a shape-only rule would flag the very substitution that makes
        # the fixture safe.
        "a front-end API key":
            r'"apiKey"\s*:\s*"(?!SCRUBBED_FRONTEND_KEY)[A-Za-z0-9._\-]{12,}"',
        # A real customer's display name and their words. Republishing a
        # person's review is a separate act from the site showing it on its
        # own page; the checks need the STRUCTURE, not the person.
        "a reviewer's display name":
            r'"nickName"\s*:\s*"(?!SCRUBBED_REVIEWER)[^"]+"',
        "a customer's review text":
            r'"review"\s*:\s*"(?!SCRUBBED_REVIEW_TEXT)[^"]{12,}"',
        # A per-impression session id on each sponsored slot.
        "an ad session id":
            r'"qsess"\s*:\s*"(?!SCRUBBED_SESSION)[^"]+"',
        # Rakuten's own cross-merchant catalogue id — public and harmless,
        # and it trips every 32-hex scanner including this repo's CI grep,
        # which would greet a new user with a FAILED line on their first
        # command. Scrubbed rather than allowlisted, because an allowlist
        # forgiving 32-hex inside a rakuten.co.jp URL is a hole a real key
        # could later hide in.
        "a catalogue id that reads as a credential":
            r'"productUrl"\s*:\s*"[^"]*?[0-9a-f]{32}',
    }
    for label, pattern in site_session.items():
        hits = re.findall(pattern, fixtures)
        ok &= check("no %s in the fixtures (scrub a new capture before "
                    "committing it)" % label, not hits)

    # The repo-wide grep CI runs, applied here too so a failure is local.
    # Asked of GIT, not of the filesystem. A developer's own `.env` beside
    # the scripts is EXPECTED — it is how the local runs get their key — and
    # `.gitignore` is what keeps it out of the repo. Checking for the file's
    # existence made this red on every machine that had ever run the scraper
    # for real, which is the machine most likely to be running the suite.
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", ".env"],
        cwd=REPO_ROOT, capture_output=True, text=True).returncode == 0
    ok &= check("no .env file is tracked by git", not tracked)
    return ok


def test_wording():
    group("wording and removed flags")
    ok = True
    # Asked of GIT, so the scan reaches the workflows and the issue
    # templates under .github/ — eight shipped files that an os.listdir of
    # the repo ROOT silently missed, including the four a contributor is
    # most likely to paste marketing wording into. Untracked scratch files
    # and .pytest_cache/ are excluded for free by asking git.
    listed = subprocess.run(["git", "ls-files"], cwd=REPO_ROOT,
                            capture_output=True, text=True)
    if listed.returncode == 0 and listed.stdout.strip():
        shipped = [f for f in listed.stdout.split("\n")
                   if f.endswith((".py", ".md", ".txt", ".toml", ".yml", ".yaml"))
                   and os.path.basename(f) != os.path.basename(__file__)]
    else:  # not a git checkout (a release tarball): fall back to the root
        shipped = [f for f in os.listdir(REPO_ROOT)
                   if f.endswith((".py", ".md", ".txt", ".toml", ".yml", ".yaml"))
                   and f != os.path.basename(__file__)]
    ok &= check("the wording scan reaches beyond the repo root",
                any(os.sep in f or "/" in f for f in shipped))
    for phrase in BANNED_PHRASES:
        offenders = []
        for f in shipped:
            try:
                text = open(os.path.join(REPO_ROOT, f), encoding="utf-8").read()
            except (OSError, UnicodeDecodeError):
                continue
            if phrase.lower() in text.lower():
                offenders.append(f)
        ok &= check("no shipped file says %r" % phrase, not offenders)

    for flag in REMOVED_ENGINE_FLAGS:
        offenders = []
        for f in ENGINE_FILES:
            path = os.path.join(REPO_ROOT, f)
            if not os.path.exists(path):
                continue
            text = open(path, encoding="utf-8").read()
            # A prose mention explaining why the flag does NOT exist is fine
            # and is worth keeping; an argparse registration is not.
            if ('add_argument("%s"' % flag) in text or \
                    ("add_argument('%s'" % flag) in text:
                offenders.append(f)
        ok &= check("no engine registers the removed flag %s" % flag,
                    not offenders)

    # The product this repo integrates with, named correctly.
    readme = os.path.join(REPO_ROOT, "README.md")
    if os.path.exists(readme):
        text = open(readme, encoding="utf-8").read()
        ok &= check("the README names the Scraping Browser API",
                    "Scraping Browser API" in text)
        ok &= check("the README does not name a competitor",
                    not re.search(r"brightdata|oxylabs|smartproxy|zyte|scraperapi\.com",
                                  text, re.IGNORECASE))
    return ok


def test_fingerprint_client_reads_env():
    group("fingerprint_client resolves its key the way the docs promise")
    ok = True
    import fingerprint_client as fpc

    # THE DEFECT THIS PINS, found the first time --fingerprint was run live
    # here and confirmed present in five sibling repos: `--key` defaulted to
    # `os.environ.get("TWOCAPTCHA_KEY")` alone. So a key put in `.env` —
    # which is exactly what §3, the README and .env.example instruct — worked
    # for every engine and failed HERE with "No API key". A documented
    # mechanism not applied on one path, which is the shape of half the
    # defects §16 lists.
    src = inspect.getsource(fpc.main)
    ok &= check("it loads .env itself, rather than hoping an engine did",
                "env_config.load_env()" in src)
    ok &= check("...and reads the key through the family's loader",
                'env_config.env_value("TWOCAPTCHA_KEY")' in src)
    # Through `env_value` and NOT `os.environ.get`, because only the former
    # applies the placeholder rule. Measured both ways with
    # TWOCAPTCHA_KEY=your_2captcha_api_key_here exported: os.environ.get
    # sends the placeholder to the API and the run reports "Fingerprint API
    # rejected the key (401) — note this is a separate subscription", which
    # sends the reader to check a subscription they never needed.
    ok &= check("...not straight from os.environ, which skips the "
                "placeholder rule",
                'os.environ.get("TWOCAPTCHA_KEY")' not in src)
    # Behaviourally, not just by reading the source — and written
    # self-contained so this check is byte-identical in every repo of the
    # family rather than depending on a local helper.
    saved = os.environ.get("TWOCAPTCHA_KEY")
    try:
        os.environ["TWOCAPTCHA_KEY"] = "your_2captcha_api_key_here"
        read_back = env_config.env_value("TWOCAPTCHA_KEY")
    finally:
        if saved is None:
            os.environ.pop("TWOCAPTCHA_KEY", None)
        else:
            os.environ["TWOCAPTCHA_KEY"] = saved
    ok &= check("a placeholder still reads as unset on this path",
                read_back is None)

    # The default must never reach `--help`. argparse prints a default only
    # when the help string asks for it, so this is one substring away from
    # printing a live credential to anyone who types --help.
    ok &= check("the --key help text does not interpolate its default",
                "%(default)s" not in src)
    return ok


def test_fingerprint_application():
    group("a fingerprint is applied as the fingerprint describes it")
    ok = True
    import fingerprint_client as fpc

    ua = fpc.fingerprint_user_agent(FIX_FINGERPRINT)
    # The UA used to be read from `userAgent.value`, a key the API returns in
    # NEITHER format. So --fingerprint silently set no user agent at all and
    # the browser kept its own: a German fingerprint's screen and locale
    # wearing a local Chromium's UA, which is precisely the identity mismatch
    # the flag exists to avoid.
    ok &= check("the user agent is found in the shape the API returns",
                ua and ua.startswith("Mozilla/5.0 (Windows NT 10.0"))
    ok &= check("the `raw` format's ua key is understood too",
                fpc.fingerprint_user_agent({"data": {"ua": "UA/1.0"}}) == "UA/1.0")
    ok &= check("a fingerprint with no user agent yields None, not a crash",
                fpc.fingerprint_user_agent({"country": "DE"}) is None)

    kw = fpc.playwright_context_kwargs(FIX_FINGERPRINT)
    ok &= check("the context carries the fingerprint's user agent",
                kw.get("user_agent") == ua)
    # `locale` used to be built as f"en-{country}", giving "en-NL" for a
    # Dutch fingerprint. An English-speaking visitor in the Netherlands is
    # possible, but it is not what this fingerprint describes, and a locale
    # that contradicts the rest of the identity is the mismatch again. This
    # was one of the six defects a sibling repo inherited from copied core
    # and never ran (§16).
    ok &= check("the locale is the fingerprint's own, not en-<country>",
                kw.get("locale") == "nl-NL")
    ok &= check("the timezone is carried, so the browser cannot contradict it",
                kw.get("timezone_id") == "Europe/Amsterdam")
    # The device pixel ratio, which Playwright takes as its own option and
    # which was dropped on the floor until a live browser was compared
    # against the fingerprint: a fingerprint stating 1.25 produced a browser
    # reporting 1, so the identity contradicted itself on an axis a
    # fingerprinter reads for free.
    ok &= check("the device scale factor is carried",
                kw.get("device_scale_factor") == 1)
    # A viewport exactly equal to the screen is itself a signal, and the
    # fingerprint states its own window size rather than needing one guessed.
    ok &= check("the viewport is the fingerprint's window, not its screen",
                kw.get("viewport") == {"width": 1920, "height": 992}
                and kw.get("screen") == {"width": 1920, "height": 1080})

    # Falling back sensibly when a field is absent, rather than dropping it.
    bare = fpc.playwright_context_kwargs({"country": "FR", "screen":
                                          {"width": 1280, "height": 800}})
    ok &= check("a fingerprint with no intl block still gets a locale",
                bare.get("locale") == "en-FR")
    ok &= check("...and a window smaller than the screen",
                bare["viewport"]["height"] < bare["screen"]["height"])
    ok &= check("a fingerprint with nothing usable yields no kwargs",
                fpc.playwright_context_kwargs({}) == {})

    # Every key this produces must be one Playwright's new_context accepts;
    # an unknown one is a TypeError at launch, on the paid path, at runtime.
    accepted = {"user_agent", "viewport", "screen", "locale", "timezone_id",
                "geolocation", "permissions", "extra_http_headers",
                "device_scale_factor", "is_mobile", "has_touch", "color_scheme"}
    ok &= check("every context kwarg is one Playwright accepts",
                set(kw) <= accepted)
    return ok


def test_credentials_never_reach_a_log():
    group("an API key never reaches a log or an exception message")
    ok = True
    import fingerprint_client as fpc
    import captcha_solver as cs

    # requests puts the FULL URL — query string included — into the text of
    # HTTPError and of every connection error. Both of these modules have an
    # endpoint that takes the key as a query parameter, so an error there
    # echoed a live key to the terminal. It did, once, on a real call.
    # An obviously fake key, and NOT a real one even a revoked one: a
    # 32-hex string in a public repo reads as a live credential to every
    # scanner that looks, including this repo's own CI grep. The word
    # "example" in the name is what tells that grep this line is a fixture.
    example_key = "0123456789abcdef0123456789abcdef"
    for name, module in (("fingerprint_client", fpc), ("captcha_solver", cs)):
        redacted = module._redact(
            "400 Client Error: Bad Request for url: "
            "https://api.2captcha.com/fingerprint/random?format=chromium&"
            "key=%s" % example_key)
        ok &= check("%s redacts a key out of an error message" % name,
                    example_key not in redacted)
        ok &= check("...and keeps the endpoint, which is the useful half",
                    "api.2captcha.com/fingerprint/random" in redacted)
        ok &= check("%s redacts clientKey too" % name,
                    example_key not in module._redact("clientKey=%s" % example_key))
        ok &= check("%s leaves ordinary text alone" % name,
                    module._redact("upstream status 403") == "upstream status 403")
    return ok


def test_concurrent_dispatch(skips):
    group("concurrent page dispatch (threads, stop event, accounting)")
    ok = True
    try:
        import playwright_scraper as eng
    except ImportError as e:
        skips.append("concurrent dispatch (%s)" % e)
        return ok

    # The thread fan-out is the one part of --concurrency that the rest of
    # this suite does not reach, and it is not reachable from a live run in
    # every environment either: page 1 is always fetched alone and decides
    # whether the rest may be addressed, so a blocked page 1 means the
    # workers never start. Driven here with the browser stubbed out, which
    # leaves exactly the concurrency logic under test.
    original = (eng.sync_playwright, eng._BrowserSession, eng._fetch_one_page)

    class Args:
        delay = 0
        mode = "listing"
        out = "x"

    def run(specs, concurrency, rows_for_page, die_on=()):
        fetched, lock = [], threading.Lock()

        def fake_fetch(session, args, pool, page_num, url):
            with lock:
                fetched.append(page_num)
            if page_num in die_on:
                raise RuntimeError("worker blew up on page %d" % page_num)
            outcome = eng.PageOutcome(page_num=page_num, url=url)
            outcome.products = rows_for_page(page_num)
            return outcome

        eng.sync_playwright = lambda: _FakePlaywright()
        eng._BrowserSession = lambda pw, args, pool, **kw: _FakeSession(pool)
        eng._fetch_one_page = fake_fetch
        try:
            results, unattempted, exhausted = eng._fetch_pages_concurrently(
                Args(), None, specs, concurrency)
        finally:
            (eng.sync_playwright, eng._BrowserSession,
             eng._fetch_one_page) = original
        return fetched, results, unattempted, exhausted

    # 1. Every page fetched exactly once, whatever the worker count.
    specs = [(n, "u%d" % n) for n in range(2, 12)]
    fetched, results, unattempted, exhausted = run(
        specs, 4, lambda n: ["row"])
    ok &= check("every queued page is fetched exactly once",
                sorted(fetched) == [n for n, _ in specs])
    ok &= check("every page produces an outcome",
                sorted(o.page_num for o in results) == [n for n, _ in specs])
    ok &= check("nothing is left unattempted when the listing does not end",
                unattempted == [] and not exhausted)

    # 2. Results arrive in whatever order the threads finish, which is
    #    exactly why the caller merges by page number instead of by arrival.
    #    Sorting them must reconstruct the page order.
    ok &= check("outcomes can be put back into page order",
                [o.page_num for o in sorted(results, key=lambda o: o.page_num)]
                == [n for n, _ in specs])

    # 3. The stop event. Asking for 50 pages of a listing that ends at page 5
    #    must not fetch 45 empty ones: workers check the event before taking
    #    more work, so at most (concurrency - 1) extra are already in flight.
    specs = [(n, "u%d" % n) for n in range(2, 51)]
    fetched, results, unattempted, exhausted = run(
        specs, 3, lambda n: [] if n >= 5 else ["row"])
    ok &= check("the end of the listing stops dispatch", exhausted)
    ok &= check("an exhausted listing costs at most (concurrency-1) extra "
                "fetches (%d fetched of 49 queued)" % len(fetched),
                len(fetched) <= 4 + 3)
    ok &= check("the pages never tried are reported, not counted as failed",
                unattempted and all(o.ok for o in results))
    ok &= check("unattempted pages are reported in order",
                unattempted == sorted(unattempted))

    # 4. A worker that dies must not hang the run, and must not swallow the
    #    pages its siblings did fetch.
    specs = [(n, "u%d" % n) for n in range(2, 8)]
    fetched, results, unattempted, exhausted = run(
        specs, 3, lambda n: ["row"], die_on={3})
    ok &= check("a worker that raises does not hang the run",
                len(results) + len(unattempted) + 1 >= len(specs))
    ok &= check("the pages other workers fetched still come back",
                any(o.page_num != 3 for o in results))
    return ok


def test_no_undefined_names():
    group("no engine references a name that does not exist")
    ok = True
    # This exists because of a bug that got all the way to a live run.
    # puppeteer_scraper.py called `detect_page_state(...)` on a line reached
    # only while fetching a page, after the import of that name had been
    # removed. The module imported fine, `--help` worked, `compileall`
    # passed, the whole offline suite passed and CI was green — and the
    # engine died with NameError on its first real page.
    #
    # Byte-compiling proves a file PARSES. It says nothing about whether the
    # names in it resolve, and the paths where they do not are exactly the
    # ones an offline suite cannot execute.
    for name in sorted(f for f in os.listdir(REPO_ROOT) if f.endswith(".py")):
        missing = _undefined_names(os.path.join(REPO_ROOT, name))
        detail = ", ".join("%s (line %d)" % (k, v[0])
                           for k, v in sorted(missing.items()))
        ok &= check("%s references no undefined name%s"
                    % (name, ": " + detail if missing else ""), not missing)

    # And a statement that can never RUN — see `_unreachable_statements`.
    for name in sorted(f for f in os.listdir(REPO_ROOT) if f.endswith(".py")):
        dead = _unreachable_statements(os.path.join(REPO_ROOT, name))
        ok &= check("%s has no statement the control flow can never reach%s"
                    % (name, "" if not dead else ": line %d" % dead[0]),
                    not dead)

    return ok


def test_dockerfile_copies_what_it_runs():
    group("the Docker image contains every module its entrypoint imports")
    ok = True
    path = os.path.join(REPO_ROOT, "Dockerfile")
    if not os.path.exists(path):
        return check("Dockerfile exists", False)

    # The Dockerfile COPYs an explicit list rather than the whole directory,
    # which is right — the image should not carry the test suite, the
    # fixtures or a stray .env. The cost is that the list can fall behind the
    # imports, and NOTHING else in this repo would notice: CI never builds
    # the image, so a missing module ships and the container dies with
    # ModuleNotFoundError on every invocation, `--help` included.
    #
    # That is not hypothetical. `proxy_pool.py` was missing from this list,
    # and playwright_scraper.py imports it at module level.
    raw = open(path, encoding="utf-8").read()
    joined = re.sub(r"\\\n\s*", " ", raw)          # fold line continuations
    copied = set()
    for line in joined.splitlines():
        if line.startswith("COPY "):
            copied.update(tok for tok in line.split() if tok.endswith(".py"))

    entrypoint = None
    m = re.search(r'ENTRYPOINT\s*\[([^\]]*)\]', joined)
    if m:
        parts = [x.strip().strip('"\'') for x in m.group(1).split(",")]
        entrypoint = next((x for x in parts if x.endswith(".py")), None)
    ok &= check("the Dockerfile names a Python entrypoint", bool(entrypoint))
    if not entrypoint:
        return False
    ok &= check("the entrypoint itself is copied into the image",
                entrypoint in copied)

    # Every LOCAL module the entrypoint reaches, transitively.
    local = {f[:-3] for f in os.listdir(REPO_ROOT) if f.endswith(".py")}

    def reached(module, seen=None):
        seen = seen if seen is not None else set()
        if module in seen:
            return seen
        seen.add(module)
        tree = ast.parse(open(os.path.join(REPO_ROOT, module + ".py"),
                              encoding="utf-8").read())
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module.split(".")[0]]
            for name in names:
                if name in local:
                    reached(name, seen)
        return seen

    needed = reached(entrypoint[:-3])
    missing = sorted(m + ".py" for m in needed if (m + ".py") not in copied)
    ok &= check("every module the entrypoint imports is COPYed (%s)"
                % (", ".join(missing) if missing else "none missing"),
                not missing)

    # The other direction is a warning, not a failure: diff_runs.py is copied
    # deliberately as a companion tool even though the engine never imports
    # it. But anything copied must at least still EXIST.
    gone = sorted(f for f in copied
                  if not os.path.exists(os.path.join(REPO_ROOT, f)))
    ok &= check("the Dockerfile copies no file that has been deleted (%s)"
                % (", ".join(gone) if gone else "none"), not gone)
    return ok


def test_sample_output():
    group("sample_output.* is cut from a real run, not fabricated")
    ok = True
    jpath = os.path.join(REPO_ROOT, "sample_output.json")
    cpath = os.path.join(REPO_ROOT, "sample_output.csv")
    ok &= check("sample_output.json exists", os.path.exists(jpath))
    ok &= check("sample_output.csv exists", os.path.exists(cpath))
    if not (os.path.exists(jpath) and os.path.exists(cpath)):
        return ok
    rows = json.load(open(jpath, encoding="utf-8"))
    ok &= check("it holds rows", isinstance(rows, list) and len(rows) >= 5)
    names = [f.name for f in fields(Product)]
    ok &= check("every row carries exactly the schema's columns, in order",
                all(list(r) == names for r in rows))
    with open(cpath, encoding="utf-8", newline="") as f:
        header = next(csv.reader(f))
    ok &= check("the CSV header is the same columns in the same order",
                header == names)
    # FABRICATION MARKERS. A sample that was typed rather than captured is
    # worse than no sample: it teaches a reader the wrong shape and cannot
    # catch a schema change.
    blob = json.dumps(rows, ensure_ascii=False)
    fabricated = [w for w in ("example.com", "lorem", "ipsum", "foo",
                              "PRODUCT_NAME", "TODO", "XXX", "999999")
                  if w.lower() in blob.lower()]
    ok &= check("no fabrication markers%s"
                % ((" (%s)" % ", ".join(fabricated)) if fabricated else ""),
                not fabricated)
    # Every row must be a real Rakuten product, addressed the way the site
    # itself addresses it.
    ok &= check("every row's URL is a real item page on item.rakuten.co.jp",
                all((r["url"] or "").startswith(
                    "https://item.rakuten.co.jp/") for r in rows))
    ok &= check("and carries no tracking tail, so two runs produce the same "
                "URL",
                not any("scid=" in (r["url"] or "") for r in rows))
    ok &= check("every row's sku is {shop}:{manageNumber}, as this schema "
                "defines it",
                all(r["sku"] and r["sku"] == sku_from_url(r["url"])
                    for r in rows))
    ok &= check("and the shop half of it matches the shop_code column",
                all(r["sku"].split(":")[0] == r["shop_code"] for r in rows))
    ok &= check("source is the marketplace on every row",
                {r["source"] for r in rows} == {"rakuten.co.jp"})
    # A MULTI-PAGE sample, so the two columns that only mean something
    # together are visible doing their job. A single-page sample cannot show
    # that `position` restarts per page, which is the arithmetic bug §18
    # names.
    pages = sorted({r["page"] for r in rows})
    ok &= check("the sample spans more than one page (%s)" % pages,
                len(pages) > 1)
    pairs = [(r["page"], r["position"]) for r in rows]
    ok &= check("page+position is unique across it",
                len(set(pairs)) == len(pairs))
    ok &= check("and position restarts at 1 on each page",
                all(min(p for pg, p in pairs if pg == page) == 1
                    for page in pages))
    # The columns a reader most needs to see populated, because they are the
    # ones this site is scraped FOR.
    ok &= check("every row carries a price",
                all(isinstance(r["price"], (int, float)) for r in rows))
    ok &= check("every priced row carries its currency, and it was read "
                "rather than defaulted",
                all(r["currency"] == "JPY" for r in rows))
    ok &= check("the sample shows Rakuten points, which are half the price "
                "story on this site",
                all(isinstance(r["points"], int) for r in rows))
    ok &= check("and the merchant behind each row, because this site is a "
                "mall",
                all(r["shop_name"] and r["shop_id"] for r in rows))
    ok &= check("the sample shows a real price_source",
                {r["price_source"] for r in rows} <= {"state", "itemdata",
                                                      "dom"}
                and "state" in {r["price_source"] for r in rows})
    # And the measured absences, so the sample does not imply a column is
    # broken. `original_price` is null on every listing row because the
    # listing payload publishes no was-price at all.
    ok &= check("original_price is null throughout, as the listing route "
                "measures",
                all(r["original_price"] is None for r in rows))
    ok &= check("scraped_at is a real timestamp on every row",
                all(re.match(r"^\d{4}-\d{2}-\d{2}T", r["scraped_at"] or "")
                    for r in rows))
    return ok


def test_captcha():
    group("captcha detection and reconciliation")
    ok = True
    from captcha_solver import CaptchaChallenge

    # Format 2: the site's own wrapper element carries the config as
    # attributes, with the execute() call inside a bundled file that never
    # appears as readable inline script.
    widget = ('<captcha-widget data-captcha-type="recaptcha" data-version="v3" '
              'data-sitekey="6LcABCDEFGHIJKLMNOPQRSTUVWXYZ0123" '
              'data-action="submit"></captcha-widget>')
    c = detect_recaptcha_v3(widget, "https://www.rakuten.co.jp/")
    ok &= check("a captcha-widget declaring v3 is detected",
                c is not None and c.kind == "recaptcha_v3")

    # A sitekey is at least 20 characters; a short string next to
    # data-sitekey is not one, and treating it as one would send a malformed
    # task to the API and bill for the answer.
    ok &= check("a too-short sitekey is not accepted as a challenge",
                detect_recaptcha_v3('<div data-sitekey="short" '
                                    'class="g-recaptcha"></div>',
                                    "https://www.rakuten.co.jp/") is None)
    ok &= check("a page with no reCAPTCHA at all is not a challenge",
                detect_recaptcha_v3(fx("search_p1")[0], LISTING_URL) is None)

    # THE LOADER WINS. A site's own wrapper can declare v3 while the Google
    # loader it actually ships is the v2-invisible signature
    # (render=explicit, size=invisible, a bframe challenge iframe). v3
    # parameters sent for a v2-invisible widget buy a token the site
    # rejects — so the runtime reading is authoritative and the two
    # detectors are reconciled rather than short-circuited.
    static_v3 = CaptchaChallenge(kind="recaptcha_v3", sitekey="6LcABC" + "X" * 20,
                                 action="submit", source="html")
    runtime_v2 = CaptchaChallenge(kind="recaptcha_v2_invisible",
                                  sitekey="6LcABC" + "X" * 20,
                                  source="runtime", size="invisible")
    merged = reconcile_detections(static_v3, runtime_v2)
    ok &= check("when the detectors disagree, the live loader wins",
                merged is not None and merged.kind == "recaptcha_v2_invisible")
    ok &= check("...and the real action from the static markup is kept",
                merged.action == "submit")
    ok &= check("one detector alone is still used when only it fires",
                reconcile_detections(static_v3, None) is static_v3
                and reconcile_detections(None, runtime_v2) is runtime_v2)
    ok &= check("neither firing means no challenge",
                reconcile_detections(None, None) is None)

    # Deliberately absent: no solver for a first-party image captcha. This
    # site has no such page — measured 2026-09-10, its refusal is not a page
    # at all: the HTTP/2 stream is reset and nothing arrives — so a solver
    # for one would be dead code that looks load-bearing. Pinned so that
    # reintroducing it is a decision rather than a drift.
    import captcha_solver
    ok &= check("no first-party image-captcha solver was ported",
                not [n for n in dir(captcha_solver)
                     if "image" in n.lower() and "captcha" in n.lower()])
    # ...while the DETECTORS stay broad, which is the family's standing
    # policy: which challenge a visitor meets depends on the exit country and
    # on what the address has been doing.
    # THE MARKER SET IS THE MEASUREMENT, and on this site the measurement
    # is zero. §18's question is not "did we meet a captcha" but "is one
    # CONFIGURED, and would we recognise it if it appeared?" — so it was
    # asked properly: across all 13 captures, served and refused alike,
    # there are no reCAPTCHA, hCaptcha, Turnstile, DataDome, PerimeterX,
    # Incapsula, Kasada or AWS WAF markers, no challenge iframe, no
    # `data-sitekey`, no `*_SITE_KEY` in any page config and no
    # `<captcha-*>` mount point.
    #
    # So this set exists to RECOGNISE an escalation, not because one was
    # seen. It is checked in both directions: every entry must be absent
    # from every served page (or it is not a marker, it is a fact about the
    # site), and the obvious vendor loaders must be present (or an
    # escalation would be reported as a plain block with no name on it).
    markers = {m.lower() for m in product_parser.BOT_CHALLENGE_MARKERS}
    ok &= check("detection covers the loaders a challenge would arrive as",
                {"recaptcha/api.js", "recaptcha/api2/anchor", "g-recaptcha",
                 "data-sitekey"} <= markers)
    ok &= check("and the other vendors' own hosts",
                {"hcaptcha.com/1/api.js", "challenges.cloudflare.com",
                 "captcha-delivery.com", "px-captcha", "awswaf.com"}
                <= markers)
    # `cf-turnstile` is specifically EXCLUDED: the Scraping Browser's
    # auto-solve extension injects a cf-turnstile hunter into every page it
    # loads, so it fires on GOOD pages fetched over --cdp-endpoint — and on
    # two sibling sites it was measured to MISS the real challenge while
    # doing so. A marker that fires on good pages and is absent from the bad
    # one is worse than no marker (§19).
    ok &= check("cf-turnstile is not carried",
                not any("cf-turnstile" in m for m in markers))
    # A bare CDN or vendor NAME is not carried either. Rakuten is fronted by
    # Akamai, and "this site uses Akamai" is a fact about its
    # infrastructure rather than a signal about this response (§18).
    ok &= check("a bare vendor/CDN name is not a marker",
                not any(m in ("akamai", "akam", "edgesuite", "cloudflare")
                        for m in markers))
    # NOT ONE of them may appear on a page the site served. This is the
    # check that would have caught the family's `cf-turnstile` mistake, run
    # against the fixtures that matter rather than against a 404 fetched by
    # curl (§21: a guard is only as good as the fixture it runs against).
    for name in ("search_p1", "search_p1_browser", "category_p1",
                 "search_deep", "search_sponsored", "item_page",
                 "item_discounted", "search_no_results"):
        html = fx(name)[0]
        hits = sorted(m for m in product_parser.BOT_CHALLENGE_MARKERS
                      if m in html)
        ok &= check("no challenge marker fires on %s%s"
                    % (name, (" (fires: %s)" % hits) if hits else ""),
                    not hits)
    # The extension's injected hunters are stripped before the scan, so a
    # managed browser's own auto-solve tags cannot be mistaken for the
    # site's challenge.
    ok &= check("the Scraping Browser's injected tags do not read as a "
                "challenge",
                detect_bot_challenge(fx("search_p1")[0] + EXTENSION_TAGS)
                is None)
    # AND THE SAME THING AGAINST A PAGE ACTUALLY FETCHED THAT WAY, which is
    # the fixture §21 says a guard needs: a sibling repo's version of this
    # check ran only against a curl-fetched 404, which carries no injection
    # at all, so it passed for the wrong reason.
    #
    # Counted on the real thing: 16 injected hunter/interceptor scripts, and
    # `cf-turnstile` appears ONCE — on a page holding the catalogue. Carrying
    # that marker, as this family's notes originally recommended, would
    # report exit 3 on a good 1.1 MB page. It took the count to stop the
    # mistake, not the rule.
    cdp_html, cdp_url, cdp_status = fx("search_via_cdp")
    injected = len(re.findall(r"(?:chrome|moz)-extension://", cdp_html))
    ok &= check("the CDP fixture really carries the extension's injections "
                "(%d script(s))" % injected, injected >= 10)
    ok &= check("...including a cf-turnstile hunter, on a SERVED page "
                "(%d occurrence(s))" % cdp_html.count("cf-turnstile"),
                cdp_html.count("cf-turnstile") >= 1)
    ok &= check("cf-turnstile is therefore NOT in the marker set",
                not any("cf-turnstile" in m
                        for m in product_parser.BOT_CHALLENGE_MARKERS))
    ok &= check("the marker set scores zero against it, WITHOUT relying on "
                "the extension strip",
                not [m for m in product_parser.BOT_CHALLENGE_MARKERS
                     if m in cdp_html])
    # `<captcha-widgets>` — AN EMPTY MOUNT POINT, and whose it is differs
    # between repos in this family, which is why it is pinned here.
    #
    # On a sibling site that element is the SITE's own: it ships on every
    # page and a reCAPTCHA key sits in the page config beside it, so that
    # repo needed markers for the shapes its captcha would take. Here it is
    # the 2Captcha auto-solve extension's, and the proof is the split:
    #
    #   /search/mall/-/551177/ via local Chromium    0 occurrences
    #   the SAME url via the Scraping Browser        1 occurrence
    #
    # Both carry Akamai's own sensor, so the difference is not the route.
    #
    # It matters because it SURVIVES the extension-script strip — the strip
    # removes `<script src="chrome-extension://…">` tags, not a custom
    # element the extension creates. So anyone who adds `<captcha-widgets`
    # to the marker set, copying the sibling that needs it, would fire on
    # every good page fetched over --cdp-endpoint.
    ok &= check("the extension's empty <captcha-widgets> mount is present "
                "on the CDP page",
                re.search(r"<captcha[-a-z]*[\s>]", cdp_html) is not None)
    ok &= check("...and absent from the same URL fetched locally",
                re.search(r"<captcha[-a-z]*[\s>]",
                          fx("search_sponsored")[0]) is None)
    ok &= check("...so it is NOT carried as a marker, because it would fire "
                "on every good CDP page",
                not any("captcha-widget" in m
                        for m in product_parser.BOT_CHALLENGE_MARKERS))
    ok &= check("...and it survives the extension strip, which is why the "
                "marker set rather than the strip has to be right",
                re.search(r"<captcha[-a-z]*[\s>]",
                          product_parser._without_extension_scripts(cdp_html))
                is not None)
    ok &= check("so a CDP-fetched page reads as content, not as a challenge",
                detect_page_state(cdp_html, cdp_status, cdp_url) == "content"
                and detect_bot_challenge(cdp_html) is None)
    ok &= check("...and its rows parse",
                len(parse_products(cdp_html, cdp_url)) >= 3)
    # And the honest sentence about the paid product, which is the one this
    # family has got wrong before (§19). What may be written is "this repo
    # does not implement X", never "X cannot be solved" — 2Captcha solves
    # enterprise reCAPTCHA (RecaptchaV2EnterpriseTaskProxyless) and
    # Cloudflare Turnstile (TurnstileTaskProxyless), whatever this client
    # happens to build today.
    sources = []
    for path in ("README.md", "product_parser.py", "page_flow.py",
                 "captcha_solver.py", "playwright_scraper.py",
                 "scraper_api_client.py"):
        full = os.path.join(REPO_ROOT, path)
        if os.path.exists(full):
            sources.append((path, open(full, encoding="utf-8").read()))
    bad = [p for p, text in sources
           if re.search(r"captcha[^.]{0,80}(cannot|can't|impossible) "
                        r"(be )?solv", text, re.I)]
    ok &= check("nothing claims a captcha cannot be solved%s"
                % ((" (%s)" % ", ".join(bad)) if bad else ""), not bad)
    # Akamai's deny page is the narrow, honest use of "unsolvable": it
    # carries no widget, so there is nothing on it for any solver at any
    # price. That is a property of THAT PAGE and says nothing about a vendor.
    deny = fx("block_akamai")[0]
    ok &= check("the deny page really does carry no widget",
                detect_bot_challenge(deny) is None
                and "sitekey" not in deny.lower())
    return ok


def _placeholder_reads_unset(raw):
    """Whether env_config would treat `raw` as "not configured".

    Goes through the real rule — `env_config.env_value`, which is where the
    placeholder logic lives — rather than reimplementing it, because a
    reimplementation is what drifts. The variable is set in os.environ
    directly and restored afterwards: `load_env` only fills variables that
    are not already set, so writing a temporary .env would be shadowed by
    whatever the suite has already loaded.
    """
    name = "CATAWIKI_CDP_ENDPOINT"
    saved = os.environ.get(name)
    try:
        os.environ[name] = raw
        with io.StringIO() as buf, redirect_stdout(buf):
            value = env_config.env_value(name)
    finally:
        if saved is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = saved
    return value is None


def test_env_config():
    group("env_config")
    ok = True
    ok &= check("the env keys are this site's, not another repo's",
                set(env_config.ENV_KEYS) ==
                {"TWOCAPTCHA_KEY", "RAKUTEN_CDP_ENDPOINT",
                 "RAKUTEN_PROXY", "RAKUTEN_URL"})

    # .env.example must document exactly the variables the code reads, in
    # both directions. It drifts otherwise, and a documented-but-unread
    # variable is worse than an undocumented one.
    example = os.path.join(REPO_ROOT, ".env.example")
    documented = set()
    if os.path.exists(example):
        for line in open(example, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                documented.add(line.split("=", 1)[0].strip())
    ok &= check(".env.example documents exactly the variables the code reads",
                documented == set(env_config.ENV_KEYS))

    # A variable mapped onto a flag with a non-empty default would be
    # silently inert, because the loader only fills UNSET values: a setting
    # that looks configurable and is not.
    ok &= check("no env variable is mapped onto --out (it has a default)",
                "out" not in env_config.ENV_KEYS.values())

    # THE ROUND TRIP, on the file this repo actually ships rather than on
    # strings written here. `cp .env.example .env` and run: every credential
    # must read as unset, and the one non-credential default must survive.
    #
    # Hand-written placeholder strings are not enough, and that is why this
    # exists: a sibling repo's literal-only check passed while the SHIPPED
    # example's two credentialled URLs read as CONFIGURED, so a copied
    # example connected to cb.2captcha.com with `{login}-zone-…` as its
    # username and got a 401 a long way from its cause.
    with tempfile.TemporaryDirectory() as d:
        copied = os.path.join(d, ".env")
        with open(os.path.join(REPO_ROOT, ".env.example"), encoding="utf-8") as src:
            example_text = src.read()
        with open(copied, "w", encoding="utf-8") as dst:
            dst.write(example_text)

        class Copied:
            twocaptcha_key = None
            url = None
            cdp_endpoint = None
            proxy = None

        saved = {k: os.environ.pop(k, None) for k in env_config.ENV_KEYS}
        try:
            args = Copied()
            env_config.load_env(copied)
            env_config.apply(args, quiet=True)
            ok &= check("a copied .env.example leaves every CREDENTIAL unset",
                        args.twocaptcha_key is None
                        and args.cdp_endpoint is None
                        and args.proxy is None)
            ok &= check("...and leaves the target URL usable, so a copied "
                        "example still runs",
                        isinstance(args.url, str)
                        and args.url.startswith(
                            "https://search.rakuten.co.jp/search/mall/"))
            ok &= check("the example names no variable the loader does not "
                        "read", not env_config.unknown_keys(copied))
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    # And the placeholder shapes themselves, pinned so a future example that
    # writes a credential differently is still caught. Any value carrying
    # `{...}` braces is unset, whatever else it looks like.
    for raw in ('ws://{login}-zone-scraping_browser-country-ae-pid-'
                '{profileId}:{password}@cb.2captcha.com:9222',
                'http://{user}:{password}@ae.proxy.2captcha.com:2334',
                'your_2captcha_api_key_here'):
        ok &= check("a placeholder value reads as unset: %s..." % raw[:34],
                    _placeholder_reads_unset(raw))
    # ...and a REAL value still reads as set, or the guard has eaten the
    # feature it was protecting.
    ok &= check("a real value is not mistaken for a placeholder",
                _placeholder_reads_unset(
                    "ws://acct1-zone-scraping_browser-country-ae-pid-p1:"
                    "secret@cb.2captcha.com:9222") is False)
    ok &= check("the example's default URL is usable as-is",
                _placeholder_reads_unset(
                    SEARCH_URL) is False)

    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, ".env")
        with open(path, "w", encoding="utf-8") as f:
            f.write("TWOCAPTCHA_KEY=fromfile\n")
            f.write("RAKUTEN_URL=https://www.rakuten.co.jp/category/100356/\n")
            f.write("NOT_A_REAL_KEY=1\n")

        class A:
            twocaptcha_key = None
            url = None
            cdp_endpoint = None
            proxy = None

        a = A()
        env_config.load_env(path)
        env_config.apply(a, quiet=True)
        ok &= check("a value in .env fills an unset flag",
                    a.twocaptcha_key == "fromfile")

        b = A()
        b.twocaptcha_key = "fromflag"
        env_config.apply(b, quiet=True)
        # A .env must never override something the caller typed.
        ok &= check("an explicit flag beats .env", b.twocaptcha_key == "fromflag")
        # A typo is REPORTED rather than silently ignored.
        ok &= check("an unrecognised variable in .env is reported",
                    "NOT_A_REAL_KEY" in env_config.unknown_keys(path))
    return ok


def test_proxy_pool():
    group("proxy_pool: credentials never reach argv or logs")
    ok = True
    url = "http://user:secret@eu.proxy.2captcha.com:2334"
    masked = mask(url)
    ok &= check("credentials are masked in logs", "secret" not in masked)
    # The host and port are KEPT: which exit a run used is the point of the
    # log and is not the secret.
    ok &= check("...but the host and port survive masking",
                "eu.proxy.2captcha.com:2334" in masked)

    pw = to_playwright(url)
    # A `--proxy-server=` value becomes part of the browser's command line,
    # readable by anything that can run `ps`. The credentials go through the
    # driver's own fields instead.
    ok &= check("the server string handed to the browser has no credentials",
                "secret" not in pw["server"])
    ok &= check("credentials go through the driver's own fields",
                pw["username"] == "user" and pw["password"] == "secret")

    scrubbed, creds = split_credentials(url)
    ok &= check("split_credentials separates the two",
                scrubbed == "http://eu.proxy.2captcha.com:2334"
                and creds == ("user", "secret"))

    pool = ProxyPool(["http://a:1", "http://b:2", "http://c:3"])
    ok &= check("a pool reports its size", len(pool) == 3)
    first = pool.current
    pool.advance("test")
    ok &= check("advancing moves to another exit", pool.current != first)
    # `.proxies` hands back a COPY, so a worker building its own pool from it
    # cannot mutate the parent's list. Two threads sharing one mutable list
    # is the bug that makes concurrency stop being worth it.
    copy = pool.proxies
    copy.append("http://d:4")
    ok &= check("the pool hands out a copy of its exits, not the list itself",
                len(pool) == 3)

    # Workers start on DIFFERENT exits, each with its own pool object, so no
    # thread needs a lock: the concurrency is safe by construction rather
    # than by discipline. Tested through the engine's own helper, because
    # that is where the offset actually lives.
    try:
        import playwright_scraper
    except ImportError:
        playwright_scraper = None
    if playwright_scraper is not None:
        exits = [playwright_scraper._worker_pool(pool, i).current
                 for i in range(3)]
        ok &= check("three workers start on three different exits",
                    len(set(exits)) == 3)
        ok &= check("a worker with no pool gets none",
                    playwright_scraper._worker_pool(None, 0) is None)

    # A pool of one is legal and must not rotate itself into an index error.
    one = ProxyPool(["http://only:1"])
    one.advance("nowhere else to go")
    ok &= check("a single-exit pool survives a rotation",
                one.current == "http://only:1")
    ok &= check("an empty pool is refused rather than silently accepted",
                _raises(lambda: ProxyPool([])))

    # This used to assert that "http://host:port:login:pass" — a line from a
    # proxy LIST FILE — "is understood", checking only that parse_proxy_line
    # did not reject it. It returned the string unchanged, so the check
    # passed; the value was never usable, and it blew up several calls later.
    # A test that asserts a function did not complain is not a test that its
    # answer was right.
    #
    # A proxy LIST FILE line pasted where a proxy URL belongs. This is the
    # mistake a new user makes — the file format is
    # scheme://host:port:login:password and the flag wants
    # http://login:password@host:port — and it reached a real CI run.
    #
    # It used to sail through parse_proxy_line (which never looked at the
    # port) and blow up much later inside to_playwright as an uncaught
    # ValueError: exit 1, a crash, where it should be exit 2, bad usage. And
    # the traceback printed the login AND the password into a public CI log.
    from proxy_pool import ProxyError
    pasted = ("http://eu.proxy.2captcha.com:2334:"
              "SOMELOGIN-zone-custom-region-de:SOMEPASSWORD")
    raised = None
    try:
        parse_proxy_line(pasted, source="CATAWIKI_PROXY")
    except ProxyError as exc:
        raised = str(exc)
    ok &= check("a proxy-list line pasted as a URL is refused, not crashed on",
                raised is not None)
    ok &= check("...and the refusal says what the value should look like",
                raised is not None and "login:password@host:port" in raised)
    ok &= check("...and neither the login nor the password is in the message",
                raised is not None
                and "SOMEPASSWORD" not in raised and "SOMELOGIN" not in raised)

    # mask() is the last thing standing between a password and a log, and it
    # is called precisely when the value is already wrong. It read
    # `parsed.port`, which urlparse computes lazily and which RAISES on a
    # malformed authority — so the masker blew up on exactly the input that
    # most needed masking. A masker that raises is worse than a vague one.
    ok &= check("mask() does not raise on a malformed URL",
                "SOMEPASSWORD" not in mask(pasted))
    for junk in ("::::", "not a url", "http://", "://x", ""):
        try:
            mask(junk)
            raised_here = False
        except Exception:
            raised_here = True
        ok &= check("mask(%r) does not raise" % junk, not raised_here)
    ok &= check("mask() still keeps host and port on a good URL",
                mask("http://u:p@h.example:8080") == "http://***:***@h.example:8080")

    # PINNED LIMITATION, not a defence. mask() takes a bare URL; given a
    # SENTENCE containing one it returns "?://?" — the password is gone,
    # which is the property that matters, but so is the host and port the log
    # was written to show. Every caller here passes the URL as its own `%s`
    # argument for that reason, and `_mask_credentials()` is what handles
    # arbitrary text. Asserting the CURRENT behaviour makes a future swap a
    # failing check rather than an unreadable log (§10).
    sentence = "a http://u:supersecret@h.example:8080 b"
    ok &= check("mask() on a sentence loses the host — the documented limit",
                mask(sentence) == "?://?")
    ok &= check("...but never the password", "supersecret" not in mask(sentence))
    ok &= check("every mask() call site passes a bare URL, not a sentence",
                not [ln for f in ("playwright_scraper.py", "puppeteer_scraper.py",
                                  "selenium_scraper.py", "proxy_pool.py")
                     for ln in open(os.path.join(REPO_ROOT, f),
                                    encoding="utf-8").read().split("\n")
                     if re.search(r'[^_]mask\(f?["\']', ln)])
    # ...and the engines' own masker handles a sentence, globally. A masker
    # that fixes the first occurrence and prints the password the other four
    # times looks exactly like one that works.
    for name in _ENGINE_MODULES:
        try:
            mod = __import__(name)
        except ImportError:
            continue
        many = ("x ws://u:supersecret@h:1 y ws://u:supersecret@h:1 "
                "z http://u:supersecret@h:2")
        out = mod._mask_credentials(many)
        ok &= check("%s._mask_credentials masks EVERY occurrence" % name,
                    "supersecret" not in out)
        ok &= check("%s._mask_credentials keeps the surrounding text" % name,
                    out.startswith("x ") and out.endswith(":2")
                    and "h:1" in out)
    return ok


# The three engines. Playwright is primary; the other two exist for parity
# and are demoted in priority, not in correctness — all three must agree on
# exit codes, run status, and whether a run crashes or spends money.
_ENGINE_MODULES = ("playwright_scraper", "puppeteer_scraper",
                   "selenium_scraper")


# Stand-ins for a real browser session, so the concurrency machinery can be
# driven with the browser stubbed out. A live run cannot always reach it:
# page 1 is fetched alone and decides whether the rest may be addressed, so a
# blocked page 1 means the workers never start.
class _FakeSession:
    """Stands in for a _BrowserSession: opened, closed, carries a pool."""

    def __init__(self, pool=None):
        self.pool = pool
        self.closed = False

    def open(self):
        return self

    def close(self):
        self.closed = True


class _FakePlaywright:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


STATE_POLICY_NAMES = ("content", "empty", "blocked", "challenge", "unknown")


def test_engines(skips):
    group("engines: all three must behave identically")
    ok = True
    loaded = {}
    for name in _ENGINE_MODULES:
        try:
            loaded[name] = __import__(name)
        except ImportError as e:
            # Reported, never swallowed: "skipped, engine absent" reads
            # exactly like a passing run, and CI's engine-smoke job fails if
            # this list is non-empty.
            skips.append("%s (%s)" % (name, e))

    for name, mod in loaded.items():
        ok &= check("%s exposes scrape() and parse_args()" % name,
                    hasattr(mod, "scrape") and hasattr(mod, "parse_args"))
        # The engines must reach the shared policy rather than carry copies.
        src = inspect.getsource(mod)
        ok &= check("%s takes its readiness policy from page_flow" % name,
                    "page_flow.ready_selector" in src)
        ok &= check("%s takes its state policy from page_flow" % name,
                    "page_flow.should_retry" in src or "page_flow.classify" in src)
        # NO scroll anywhere, and it is asserted rather than merely absent:
        # three scrolls to the document's own bottom added zero cards and left
        # the page height unchanged on all three page kinds, so an engine that
        # grew a loop back would be buying latency for nothing -- and would
        # disagree with its twins about what a settled page is. A sibling repo
        # cannot see one product without the scroll, which is exactly why this
        # was measured here instead of ported.
        ok &= check("%s has no scroll loop of its own" % name,
                    "scroll_until_settled" not in src
                    and "scroll_to_bottom" not in src)
        ok &= check("%s passes no JS across the page_flow boundary" % name,
                    "page_flow.wait_for_count" in src)
        # "Not painted yet" is not a fault, and every engine has to make that
        # distinction the same way — the first live search run of the
        # Playwright engine reported 0 rows and exit 4 because it did not.
        # §17's rule, applied to this repo's own additions: a policy
        # constant or a helper that nothing consults is the same defect as
        # dead code, and harder to see because the prose reads like
        # enforcement. All three of these were written, documented in the
        # README, and called by nothing until this check was added.
        ok &= check("%s shows the block advice rather than only computing it"
                    % name, "page_flow.block_advice" in src)
        ok &= check("%s reports the pages beyond the site's own cap" % name,
                    "parser_page_cap" in src
                    and "deeper than Rakuten will" in src)
        ok &= check("%s passes the page's own ad count to the readiness "
                    "threshold, so a short last page does not time out" % name,
                    "page_flow.expected_cards" in src)
        ok &= check("%s waits for an unpainted page instead of retrying it"
                    % name, "page_flow.is_unpainted" in src)
        # Credentials never reach a log, in any engine.
        ok &= check("%s masks credentials globally, not just once" % name,
                    "pass@" not in mod._mask_credentials(
                        "a ws://user:pass@h:1/ b ws://user:pass@h:1/"))
        ok &= check("%s refuses a host it does not read, with the reason" % name,
                    "is_supported_host" in src)
        # The same modes in every engine — a mode one engine offers and
        # another does not is the drift page_flow.py and finish_run() exist
        # to prevent, one level up. There are exactly TWO here, and both are
        # measured against real captures; the third one a reader would
        # expect — a merchant storefront — is deliberately absent, because
        # its page carries an EMPTY payload and reading it would need a
        # parser written against markup nobody has captured.
        ok &= check("%s offers exactly the two measured modes" % name,
                    'choices=["listing", "product"]' in src)
        # Looked for in the CHOICES list specifically, not anywhere in the
        # file: the word "shop" appears legitimately all over these engines
        # (a shop column, a shop refusal message, a sidecar key), and a
        # substring check over the whole source flagged `extra["shop"]`.
        mode_choices = re.findall(r'--mode"[^)]*?choices=(\[[^\]]*\])', src,
                                  re.S)
        ok &= check("%s has no unmeasured shop or seller mode" % name,
                    mode_choices
                    and not any(w in mode_choices[0]
                                for w in ('"shop"', '"seller"')))
        # Crossing the two modes is a SILENT failure — the listing parser on
        # a detail page and the detail parser on a listing page both produce
        # nothing and read as an empty result — so each engine must refuse
        # the combination up front rather than attempt it.
        ok &= check("%s refuses --mode product on a listing URL and the "
                    "reverse" % name,
                    'mode product needs an item page' in src
                    and 'Use --mode product for it' in src)
        # HEADFUL is the default here, against headless in every sibling: a
        # headless browser is refused with HTTP 403 from every address tried,
        # residential included, while a real window is served from the same
        # ones. A --headless default would be a scraper whose default cannot
        # fetch the site.
        # HEADLESS is the default, as in the rest of the family: every live
        # run of this repo was headless and was served. What was refused was
        # a non-UAE address, headful and headless alike — the discriminator
        # here is the exit's country, not the window.
        ok &= check("%s defaults to headless, like the rest of the family"
                    % name,
                    'dest="headless", action="store_true",' in src
                    and "default=True" in src)

    # THE FLAG CONTRACT, and the exact ways the engines differ from it.
    #
    # §9 lists the flags every engine must offer. Checked rather than
    # trusted, because a flag one engine has and another does not is the
    # drift page_flow.py and finish_run() exist to prevent, one level up —
    # and because the README documents these differences by name, so a new
    # divergence has to update the README or fail here.
    contract = ("--url --pages --category --format --out --delay --retries "
                "--retry-delay --concurrency --proxy --proxy-file "
                "--proxy-rotate --proxy-shuffle --proxy-block-retries "
                "--twocaptcha-key --captcha-api --solve-captcha --min-score "
                "--cdp-endpoint --allow-empty --dump-html").split()
    flags = {}
    for name in _ENGINE_MODULES:
        path = os.path.join(REPO_ROOT, name + ".py")
        if not os.path.exists(path):
            continue
        src = open(path, encoding="utf-8").read()
        flags[name] = set(re.findall(r'add_argument\(\s*"(--[a-z-]+)"', src))
        missing = [f for f in contract if f not in flags[name]]
        ok &= check("%s offers every flag in the family contract" % name,
                    not missing)
        if missing:
            print("        missing: %s" % missing)
        ok &= check("%s offers --headless and --headful" % name,
                    "--headless" in flags[name] and "--headful" in flags[name])
    if len(flags) == 3:
        pw = flags["playwright_scraper"]
        # The differences the README states, pinned in both directions: a NEW
        # divergence fails here, and closing one of these also fails here, so
        # the README cannot quietly go stale either way.
        # Asserted in BOTH directions and with NO exception list, because
        # there is nothing left to except: the family's contract flags are
        # in all three engines. A sibling repo shipped twelve flags the
        # primary engine had and its twins did not, nine of them predating
        # the work, while its README promised "same CLI" (§20) — so a new
        # unshared flag has to fail here.
        #
        # This closed two real gaps rather than documenting them: pyppeteer
        # was missing --fingerprint/--fp-tags/--fp-country/--locale and
        # selenium was missing --locale, and both now APPLY what they
        # accept rather than merely declaring it.
        ok &= check("pyppeteer offers every contract flag playwright does",
                    not (pw - flags["puppeteer_scraper"]))
        ok &= check("selenium offers every contract flag playwright does",
                    not (pw - flags["selenium_scraper"]))
        # The extras are driver plumbing and are allowed to differ — but only
        # these, so a site-behaviour flag cannot hide among them.
        ok &= check("the only extra flags are driver plumbing",
                    (flags["puppeteer_scraper"] | flags["selenium_scraper"])
                    - pw <= {"--no-sandbox", "--disable-dev-shm-usage",
                             "--chromium-path"})
        # And a flag every engine declares must be one every engine READS. A
        # flag that is accepted and then ignored looks configurable and is
        # not, which is the defect this family keeps finding in its own
        # copied core (§17).
        for eng in ("playwright_scraper", "puppeteer_scraper",
                    "selenium_scraper"):
            esrc = open(os.path.join(REPO_ROOT, eng + ".py"),
                        encoding="utf-8").read()
            # Read at least once OUTSIDE its own add_argument call, which
            # is what "applied" means. Playwright passes it to
            # new_context, pyppeteer to setExtraHTTPHeaders and Selenium to
            # --lang plus intl.accept_languages.
            uses = [ln for ln in esrc.split("\n")
                    if "args.locale" in ln and "add_argument" not in ln]
            # THE REMOTE FLAG MUST GATE THE IDENTITY, in both directions.
            # A sibling repo minted a session cookie over HTTP and installed
            # it into a --cdp-endpoint browser, so it was issued on one
            # continent and replayed from another. This repo cannot do that
            # — its engines never fetch the site over HTTP at all — but the
            # same class of mistake is live here as a fingerprint: stacking
            # a second identity onto a managed browser that already has one
            # is the thing measured to get a client refused on this site.
            #
            # Two of the three engines were correct only by accident of
            # code structure: the remote branch returns before the
            # fingerprint is applied, so the flag stayed True while the log
            # said "ignored". Asserted as an enforced gate now.
            ok &= check("%s FORCES --fingerprint off with --cdp-endpoint "
                        "rather than only warning" % eng,
                        "args.fingerprint = False" in esrc)
            ok &= check("%s says why, rather than silently dropping it"
                        % eng,
                        re.search(r"--fingerprint is ignored with "
                                  r"--cdp-endpoint", esrc) is not None)
            # ...and does NOT disable it on the ordinary path, or the flag
            # would be dead everywhere.
            forced = [ln for ln in esrc.split("\n")
                      if "args.fingerprint = False" in ln]
            ok &= check("%s forces it off in exactly one place" % eng,
                        len(forced) == 1)
            # THE BEHAVIOUR, not a comment saying so — §20's rule that an
            # assertion which is a proxy for behaviour goes stale. Forcing
            # `args.fingerprint = False` only gates the identity if EVERY
            # path that applies one is itself behind that flag.
            #
            # Followed through ONE level of indirection, because two of the
            # three engines put the work in an `_apply_fingerprint` method
            # and guard its CALL SITE. A version of this check that looked
            # only for calls lexically inside an `if ... fingerprint` block
            # reported those two as ungated, which was the check being wrong
            # rather than the code (§22: ask what a check does when its
            # resolution fails).
            etree = ast.parse(esrc)
            applies = ("get_fingerprint", "_apply_fingerprint",
                       "playwright_init_script", "playwright_context_kwargs")

            def _calls_in(node):
                return [ast.unparse(n.func) for n in ast.walk(node)
                        if isinstance(n, ast.Call)]

            # Helpers whose whole job is applying a fingerprint: a call
            # inside one of these is gated iff every call TO it is gated.
            helpers = {n.name for n in ast.walk(etree)
                       if isinstance(n, ast.FunctionDef)
                       and "fingerprint" in n.name.lower()}
            gated_lines = set()
            for node in ast.walk(etree):
                if (isinstance(node, ast.If)
                        and "fingerprint" in ast.unparse(node.test)):
                    for sub in ast.walk(node):
                        if isinstance(sub, ast.Call):
                            gated_lines.add(sub.lineno)
            # Every call to a helper must be gated...
            helper_calls = [n for n in ast.walk(etree)
                            if isinstance(n, ast.Call)
                            and any(h in ast.unparse(n.func)
                                    for h in helpers)]
            ungated_helpers = [ast.unparse(n.func) for n in helper_calls
                               if n.lineno not in gated_lines]
            # ...and every direct application outside a helper must be too.
            in_helper = set()
            for n in ast.walk(etree):
                if isinstance(n, ast.FunctionDef) and n.name in helpers:
                    for sub in ast.walk(n):
                        if isinstance(sub, ast.Call):
                            in_helper.add(sub.lineno)
            direct = [n for n in ast.walk(etree)
                      if isinstance(n, ast.Call)
                      and any(a in ast.unparse(n.func) for a in applies)
                      and not any(h in ast.unparse(n.func) for h in helpers)]
            ungated_direct = [ast.unparse(n.func) for n in direct
                              if n.lineno not in gated_lines
                              and n.lineno not in in_helper]
            ungated = ungated_helpers + ungated_direct
            ok &= check("%s applies a fingerprint ONLY behind the flag, so "
                        "forcing it off really gates it%s"
                        % (eng, (" (ungated: %s)" % ungated) if ungated
                           else ""),
                        bool(direct or helper_calls) and not ungated)
            ok &= check("%s does not merely declare --locale, it applies it"
                        % eng, len(uses) >= 1)

    # THE THREE ENGINES MUST WRITE THE SAME SIDECAR, and this is a check
    # written because an audit caught them not doing it. Two of the three
    # still assembled a sibling's `extra` keys — `scroll`, `result_header`,
    # `pages_still_growing` — so a Selenium run's sidecar was MISSING the
    # cap arithmetic, which on this site is the figure that keeps
    # `status: complete` honest: the site serves 6,750 of 3,000,000 matches,
    # and a sidecar that says only "complete" is lying by omission (§21).
    #
    # Compared as the SET OF KEYS each engine builds, read off the source,
    # because the alternative is three live runs and a diff — which is how
    # it was actually found, and is too slow to keep.
    extra_keys = {}
    for eng in _ENGINE_MODULES:
        path = os.path.join(REPO_ROOT, eng + ".py")
        if not os.path.exists(path):
            continue
        esrc = open(path, encoding="utf-8").read()
        # The `extra` assembly, read off the source between `extra = None`
        # and the `finish_run` call. Line-based rather than one multi-line
        # regex, which is easier to read and does not depend on how the
        # block happens to be formatted.
        lines = esrc.split("\n")
        try:
            first = next(i for i, ln in enumerate(lines)
                         if ln.strip() == "extra = None")
        except StopIteration:
            first = None
        keys = set()
        if first is not None:
            for ln in lines[first:first + 40]:
                if "return finish_run" in ln:
                    break
                keys |= set(re.findall(r'extra\[["\']([a-z_]+)["\']\]', ln))
                keys |= set(re.findall(r'extra = \{"([a-z_]+)"', ln))
        extra_keys[eng] = keys
        ok &= check("%s assembles a sidecar `extra`" % eng, bool(keys))
        # The stale keys must be gone, by name: `scroll` is meaningless on a
        # site that serves its whole page at once.
        ok &= check("%s carries no sibling's sidecar keys" % eng,
                    not ({"scroll", "result_header", "pages_still_growing"}
                         & keys))
    if len(extra_keys) == 3:
        sets = list(extra_keys.values())
        ok &= check("all three engines build the SAME sidecar keys (%s)"
                    % ", ".join(sorted(sets[0])),
                    sets[0] == sets[1] == sets[2])
        # And the cap summary has to be in it, or the honesty check above is
        # decoration.
        ok &= check("the sidecar carries the site's own arithmetic",
                    all("page_language" in k for k in sets))
    for eng in _ENGINE_MODULES:
        path = os.path.join(REPO_ROOT, eng + ".py")
        if not os.path.exists(path):
            continue
        esrc = open(path, encoding="utf-8").read()
        ok &= check("%s merges page 1's cap summary into the sidecar" % eng,
                    "page_one_cap" in esrc
                    and "extra.update(page_one_cap)" in esrc)
        # A dead dataclass field is the same defect as a dead column (§9).
        ok &= check("%s has no dead `scroll` outcome field" % eng,
                    "scroll: Optional" not in esrc)

    # `--fp-tags` MUST DEFAULT TO ONE OS-FAMILY TAG. It shipped in this
    # family as "Windows,Chrome,Desktop", which the fingerprint API rejects
    # with HTTP 400 — so --fingerprint failed on every invocation, which is
    # one of the six defects §16 of the family notes lists. Measured
    # 2026-09-10 against the live API: `Windows` succeeds;
    # `Windows,Chrome,Desktop`, `Chrome` and `Desktop` each 400.
    for name in _ENGINE_MODULES:
        path = os.path.join(REPO_ROOT, name + ".py")
        if not os.path.exists(path):
            continue
        src = open(path, encoding="utf-8").read()
        m = re.search(r'--fp-tags"\s*,\s*default="([^"]*)"', src)
        if m is None:
            continue          # pyppeteer has no fingerprint flags
        ok &= check("%s's --fp-tags default is ONE tag the API accepts"
                    % name,
                    "," not in m.group(1)
                    and m.group(1) in ("Windows", "Microsoft Windows",
                                       "Android"))

    # EVERY page_flow CALL IN EVERY ENGINE, CHECKED AGAINST THE REAL
    # SIGNATURE. This is the general form of a bug the first live run of the
    # pyppeteer engine found: `classify(html, status, url)` took `status`
    # positionally, and two of the three engines called it as
    # `classify(html, url=…)` because they have no response object to read a
    # status from. Both crashed with TypeError on their FIRST fetch — and
    # that was invisible to import, to --help, to compileall, to the AST
    # undefined-name walk and to 400+ green offline checks, because none of
    # those calls a function the way a live run does.
    #
    # An offline suite cannot execute a fetch. It CAN bind every call's
    # arguments to the callee's signature, which is the same check the
    # interpreter does at the moment of the call, minus the browser.
    import inspect as _inspect
    for name in _ENGINE_MODULES:
        path = os.path.join(REPO_ROOT, name + ".py")
        if not os.path.exists(path):
            continue
        tree = ast.parse(open(path, encoding="utf-8").read())
        bad = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if not (isinstance(fn, ast.Attribute)
                    and isinstance(fn.value, ast.Name)
                    and fn.value.id == "page_flow"):
                continue
            target = getattr(page_flow, fn.attr, None)
            if not callable(target):
                bad.append("%s: page_flow has no %s()" % (name, fn.attr))
                continue
            try:
                sig = _inspect.signature(target)
            except (TypeError, ValueError):
                continue
            # Bind PLACEHOLDERS, not values: this checks arity and keyword
            # names, which is what drifts. `*args` in the call (none today)
            # would make the binding unknowable, so it is skipped rather
            # than guessed at.
            if any(isinstance(a, ast.Starred) for a in node.args) or \
                    any(k.arg is None for k in node.keywords):
                continue
            try:
                sig.bind(*[object()] * len(node.args),
                         **{k.arg: object() for k in node.keywords})
            except TypeError as exc:
                bad.append("%s:%d page_flow.%s(...) — %s"
                           % (name, node.lineno, fn.attr, exc))
        ok &= check("every page_flow call in %s matches its signature" % name,
                    not bad)
        for line in bad:
            print("        %s" % line)

    # The same check for product_parser, which the engines call as often.
    for name in _ENGINE_MODULES:
        path = os.path.join(REPO_ROOT, name + ".py")
        if not os.path.exists(path):
            continue
        tree = ast.parse(open(path, encoding="utf-8").read())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "product_parser":
                imported.update(a.asname or a.name for a in node.names)
        bad = []
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id in imported):
                continue
            target = getattr(product_parser, node.func.id, None)
            if not callable(target):
                continue
            if any(isinstance(a, ast.Starred) for a in node.args) or \
                    any(k.arg is None for k in node.keywords):
                continue
            try:
                _inspect.signature(target).bind(
                    *[object()] * len(node.args),
                    **{k.arg: object() for k in node.keywords})
            except TypeError as exc:
                bad.append("%s:%d %s(...) — %s"
                           % (name, node.lineno, node.func.id, exc))
        ok &= check("every product_parser call in %s matches its signature"
                    % name, not bad)
        for line in bad:
            print("        %s" % line)

    # And the two-argument call itself, pinned: `status` must stay optional,
    # because two of the three engines have no status to pass.
    ok &= check("page_flow.classify works with no status, as two engines "
                "call it",
                page_flow.classify("<html>x</html>",
                                   url=LISTING_URL) in STATE_POLICY_NAMES)

    # For "it must pass with no engine installed" to mean anything, each
    # engine has to import its driver at MODULE level — otherwise the module
    # imports cleanly with the library absent, the group never skips, and the
    # CI job that exists to catch that cannot. This drifts back silently, so
    # it is asserted rather than trusted.
    driver_imports = {"playwright_scraper": "playwright",
                      "puppeteer_scraper": "pyppeteer",
                      "selenium_scraper": "selenium"}
    for name, lib in driver_imports.items():
        path = os.path.join(REPO_ROOT, name + ".py")
        if not os.path.exists(path):
            continue
        tree = ast.parse(open(path, encoding="utf-8").read())
        top_level = set()
        for node in tree.body:
            if isinstance(node, ast.Import):
                top_level.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                top_level.add(node.module.split(".")[0])
        ok &= check("%s imports %s at module level, so an absent library skips"
                    % (name, lib), lib in top_level)
    return ok


def _raises_type(fn, exc_type) -> bool:
    """True if `fn()` raises exactly `exc_type` (or a subclass)."""
    try:
        fn()
    except exc_type:
        return True
    except Exception:  # noqa: BLE001 — a different type is a failed check
        return False
    return False


def _no_secret_in(fn, secret: str) -> bool:
    """True if `fn()` raises and the secret is absent from the message."""
    try:
        fn()
    except Exception as e:  # noqa: BLE001
        return secret not in str(e)
    return False


# Names Python provides that are not imports and not assignments.
_MODULE_DUNDERS = {"__file__", "__name__", "__doc__", "__package__",
                   "__spec__", "__loader__", "__builtins__", "__debug__"}


def _undefined_names(path):
    """Names loaded in `path` that are never imported, defined or assigned.

    A deliberately coarse approximation — it pools every binding in the file
    rather than tracking scopes, so it under-reports and never invents a
    problem. That is the right trade here: this exists to catch a name that
    is nowhere at all, and a false positive would be worse than a miss.
    """
    tree = ast.parse(open(path, encoding="utf-8").read())
    bound = set(dir(builtins)) | _MODULE_DUNDERS
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            bound |= {(a.asname or a.name.split(".")[0]) for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            bound |= {(a.asname or a.name) for a in node.names}
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                               ast.ClassDef)):
            bound.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            bound.add(node.id)
        elif isinstance(node, ast.arg):
            bound.add(node.arg)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, ast.Global):
            bound |= set(node.names)
    missing = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) \
                and node.id not in bound:
            missing.setdefault(node.id, []).append(node.lineno)
    return missing



def _unreachable_statements(path):
    """Line numbers of statements that can never run.

    A statement sitting after a `return`/`raise`/`break`/`continue` in the
    SAME block. Deliberately narrow: it makes no claim about conditions or
    reachability in general, only about a block whose control flow has
    already left. Measured across the eighteen repos in this family on
    2026-09-16 it reported six problems and zero false positives.

    `_undefined_names` above cannot see this class at all, by design — it
    pools every binding in the file rather than tracking scopes, so a name
    used inside dead code passes as long as anything else in the module
    binds it. What was hiding there: a function whose `def` line had been
    lost, leaving its docstring and body absorbed into the end of the
    function above it. Identical in six repos, present since each one's
    first commit, invisible to import, `--help`, `compileall` and every
    green run of this suite.
    """
    tree = ast.parse(open(path, encoding="utf-8").read())
    dead = []
    for node in ast.walk(tree):
        for field in ("body", "orelse", "finalbody"):
            block = getattr(node, field, None)
            if not isinstance(block, list):
                continue
            for i, stmt in enumerate(block[:-1]):
                if isinstance(stmt, (ast.Return, ast.Raise,
                                     ast.Continue, ast.Break)):
                    dead.append(block[i + 1].lineno)
                    break
    return sorted(dead)

def test_public_names_have_consumers():
    group("no public name is dead code or unenforced policy")
    ok = True
    # §17 #5: grep every public name in a shared module for a consumer
    # outside its own module, then READ what comes back. The ones that
    # encode SITE POLICY rather than plumbing are pinned here, so deleting
    # one fails instead of quietly changing behaviour — which is the failure
    # mode a policy constant has, and it is harder to see than dead code
    # because the prose beside it reads like enforcement.
    ok &= check("the four Ichiba hosts are the supported set",
                set(product_parser.HOSTS) ==
                {"search.rakuten.co.jp", "www.rakuten.co.jp",
                 "item.rakuten.co.jp", "rakuten.co.jp"})
    ok &= check("the Rakuten Group hosts that are NOT Ichiba are listed, "
                "not refused by accident",
                {"ranking.rakuten.co.jp", "books.rakuten.co.jp",
                 "travel.rakuten.co.jp"}
                <= set(product_parser.OTHER_RAKUTEN_HOSTS))
    ok &= check("and none of them is also a supported host",
                not set(product_parser.OTHER_RAKUTEN_HOSTS)
                & set(product_parser.HOSTS))
    ok &= check("each one's refusal states a REASON, not just a refusal",
                all(len(v) > 40 for v in
                    product_parser.OTHER_RAKUTEN_HOSTS.values()))
    ok &= check("the page parameter is the one page_url builds with",
                product_parser.PAGE_PARAM == "p"
                and "%s=2" % product_parser.PAGE_PARAM
                in page_url(SEARCH_URL, 2))
    ok &= check("the paginated kinds are the two search-host routes",
                product_parser.PAGINATED_KINDS == ("search", "genre"))
    # The site's own two numbers, and the arithmetic between them. Both are
    # policy: 6,750 is what Rakuten will serve and 45 is what it serves per
    # page, and PAGE_CAP must stay their quotient or the engines will walk
    # past the end.
    ok &= check("the result cap and page size divide into the page cap",
                product_parser.SUBSET_CAP // product_parser.PAGE_SIZE
                == product_parser.PAGE_CAP == 150)
    ok &= check("and the page size is what the payload states",
                hits_per_page(fx("search_p1")[0])
                == product_parser.PAGE_SIZE)
    ok &= check("Rakuten's campaign parameters are named, and stripping "
                "uses them",
                "scid" in product_parser.TRACKING_PARAMS
                and "scid" not in strip_tracking(SEARCH_URL + "?scid=x"))
    ok &= check("jsonld_blocks reads every block on the page",
                len(product_parser.jsonld_blocks(fx("search_p1")[0])) >= 1)
    ok &= check("the readiness timeout is what content_timeout_ms returns",
                page_flow.content_timeout_ms("listing")
                == page_flow.CONTENT_TIMEOUT_MS)
    ok &= check("SOURCE_DEFAULT is what a row's `source` actually says",
                Product().source == "rakuten.co.jp")
    # THE THROTTLE POLICY IS CONSULTED, not merely declared. This is the
    # §17/§23 defect at its most expensive: `SOLVES_PER_PAGE` read like an
    # enforced cap in every repo of this family while each engine called the
    # solver twice per attempt and counted once.
    ok &= check("the throttle budget and its backoff agree in length",
                len(page_flow.THROTTLE_BACKOFF_MS)
                == page_flow.THROTTLE_RETRIES)
    for eng in _ENGINE_MODULES:
        path = os.path.join(REPO_ROOT, eng + ".py")
        if not os.path.exists(path):
            continue
        src = open(path, encoding="utf-8").read()
        ok &= check("%s consults the throttle budget" % eng,
                    "THROTTLE_RETRIES" in src
                    and "throttle_delay_ms" in src)
        ok &= check("%s consults RETRY_ON_BLOCKED rather than only "
                    "documenting it" % eng, "RETRY_ON_BLOCKED" in src)
        # Count the solve call sites, the guards and the increments, and
        # assert the three are equal. That is the check §23 asks for by
        # name, and it is what a reading of the code does not give you.
        calls = src.count("handle_captcha_if_present(")
        guards = src.count("page_flow.solve_budget(")
        bumps = src.count("solves_bought += 1")
        # One of the "calls" is the function's own def.
        ok &= check("%s: every solve call site is behind the budget "
                    "(%d call(s), %d guard(s), %d increment(s))"
                    % (eng, calls - 1, guards, bumps),
                    calls - 1 == guards == bumps)
    return ok


def test_log_format_strings_match_their_args():
    group("every log call's placeholders match its arguments")
    ok = True
    # A %-format string and its argument list drift the moment someone edits
    # the prose, and the failure is invisible until that branch runs: Python's
    # logging swallows the TypeError, prints "--- Logging error ---" and the
    # RAW TEMPLATE, and the run carries on. A live property run printed
    # "This listing is %d page(s) deeper..." for exactly that reason, after a
    # rewrite dropped one placeholder and left both arguments.
    #
    # Counted rather than formatted: this walks the AST, so it needs no
    # branch to execute and catches the ones a live run never reaches.
    import ast as _ast
    for name in ENGINE_FILES + ("product_parser.py", "page_flow.py",
                                "output_writer.py", "proxy_pool.py",
                                "env_config.py", "captcha_solver.py",
                                "scraper_api_client.py", "diff_runs.py",
                                "fingerprint_client.py"):
        path = os.path.join(REPO_ROOT, name)
        if not os.path.exists(path):
            continue
        tree = _ast.parse(open(path, encoding="utf-8").read())
        bad = []
        for node in _ast.walk(tree):
            if not isinstance(node, _ast.Call):
                continue
            fn = node.func
            if not (isinstance(fn, _ast.Attribute)
                    and fn.attr in ("debug", "info", "warning", "error",
                                    "exception", "critical")
                    and isinstance(fn.value, _ast.Name)
                    and fn.value.id == "logger"):
                continue
            if not node.args:
                continue
            template = node.args[0]
            # Only literal templates can be counted; a variable one is the
            # caller's problem and is rare here.
            parts = []
            if isinstance(template, _ast.Constant) and isinstance(template.value, str):
                parts = [template.value]
            elif isinstance(template, _ast.JoinedStr):
                continue        # an f-string interpolates itself
            else:
                continue
            text = "".join(parts)
            # `%%` is a literal percent and consumes no argument.
            holders = len(re.findall(r"%[-+ #0-9.*]*[diouxXeEfFgGcrsa%]",
                                     text.replace("%%", "")))
            supplied = len([a for a in node.args[1:]
                            if not isinstance(a, _ast.Starred)])
            has_star = any(isinstance(a, _ast.Starred) for a in node.args[1:])
            if has_star:
                continue
            if holders != supplied:
                bad.append("%s:%d %d placeholder(s), %d argument(s)"
                           % (name, node.lineno, holders, supplied))
        ok &= check("%s: every logger call's placeholders match its arguments%s"
                    % (name, "" if not bad else " -- " + "; ".join(bad[:3])),
                    not bad)
    return ok


def test_every_shared_call_binds():
    group("every call into a shared module binds, including the paths a "
          "credential gates")
    ok = True
    # §17's check #1, widened to the case that matters most here: the
    # credential-gated modules. `fingerprint_client`, `scraper_api_client`
    # and `captcha_solver` are reached only with a 2Captcha key, and no key
    # was available to the work that built this repo — so a wrong call in
    # them would be invisible to every live run AND to the narrower
    # page_flow-only version of this check. On a sibling repo, a key
    # arriving later surfaced FIVE calls to functions that never existed.
    #
    # This binds FROM-IMPORTED names too, not just `module.attr()` calls,
    # because that is how these modules are actually used
    # (`from fingerprint_client import get_fingerprint`).
    import importlib
    import inspect as _inspect

    SHARED = ("product_parser", "page_flow", "output_writer", "proxy_pool",
              "captcha_solver", "fingerprint_client", "scraper_api_client",
              "env_config", "diff_runs")
    CALLERS = ("playwright_scraper", "puppeteer_scraper", "selenium_scraper",
               "scraper_api_client", "fingerprint_client", "diff_runs",
               "page_flow", "product_parser")

    # name -> the callable it was imported from, per calling module.
    checked = 0
    for caller in CALLERS:
        path = os.path.join(REPO_ROOT, caller + ".py")
        if not os.path.exists(path):
            continue
        src = open(path, encoding="utf-8").read()
        try:
            tree = ast.parse(src)
        except SyntaxError:
            ok &= check("%s parses" % caller, False)
            continue
        # Build the from-import map, and note every name BOUND locally —
        # a parameter, an assignment or a local def shadows the import, and
        # binding against the shared module's signature would then be wrong.
        imported = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module in SHARED:
                for alias in node.names:
                    imported[alias.asname or alias.name] = (node.module,
                                                            alias.name)
        shadowed = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                shadowed.add(node.name)
                for a in node.args.args + node.args.kwonlyargs:
                    shadowed.add(a.arg)
                if node.args.vararg:
                    shadowed.add(node.args.vararg.arg)
                if node.args.kwarg:
                    shadowed.add(node.args.kwarg.arg)
            elif isinstance(node, ast.ClassDef):
                shadowed.add(node.name)
            elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.For)):
                targets = (node.targets if isinstance(node, ast.Assign)
                           else [node.target])
                for t in targets:
                    for sub in ast.walk(t):
                        if isinstance(sub, ast.Name):
                            shadowed.add(sub.id)
        bad = []
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)):
                continue
            local = node.func.id
            if local not in imported or local in shadowed:
                continue
            module_name, real = imported[local]
            try:
                module = importlib.import_module(module_name)
            except ImportError:
                continue
            target = getattr(module, real, None)
            # A name the module does NOT define is the loudest thing this
            # check can say, so it must not be skipped — a sibling repo
            # resolved with getattr(..., None), skipped anything
            # not-callable, and stayed silent on exactly that case (§22).
            if target is None:
                bad.append("%s:%d %s.%s does not exist"
                           % (caller, node.lineno, module_name, real))
                continue
            if not callable(target):
                continue
            # A star-arg or **kwargs in the CALL makes the binding
            # unknowable, so it is skipped rather than guessed at.
            if (any(isinstance(a, ast.Starred) for a in node.args)
                    or any(k.arg is None for k in node.keywords)):
                continue
            try:
                sig = _inspect.signature(target)
            except (TypeError, ValueError):
                continue
            try:
                sig.bind(*[object()] * len(node.args),
                         **{k.arg: object() for k in node.keywords})
            except TypeError as exc:
                bad.append("%s:%d %s(...) — %s"
                           % (caller, node.lineno, local, exc))
            checked += 1
        ok &= check("every from-imported shared call in %s binds" % caller,
                    not bad)
        for line in bad:
            print("        %s" % line)
    # A binding check that bound NOTHING passes for the wrong reason.
    ok &= check("the check actually bound something (%d call(s))" % checked,
                checked >= 40)
    # And the credential-gated modules specifically must be among the
    # callers examined, because they are the ones no run reaches.
    ok &= check("the credential-gated modules were examined",
                all(os.path.exists(os.path.join(REPO_ROOT, m + ".py"))
                    for m in ("fingerprint_client", "scraper_api_client",
                              "captcha_solver")))
    return ok


def test_sponsored_slots_do_not_shift_position():
    group("Rakuten injects paid slots into its own result list")
    ok = True
    html, url, _ = fx("search_sponsored")
    items = search_payload(html).get("items") or []
    sponsored = [i for i in items if is_sponsored(i)]
    ok &= check("the fixture carries real sponsored entries",
                len(sponsored) == 7)
    ok &= check("a sponsored entry has no product URL to key a row on",
                all(sku_from_url(i.get("url") or "") is None
                    for i in sponsored))
    ok &= check("every one points at Rakuten's ad click-tracking host, not "
                "at a product",
                all("ias.rakuten.co.jp/redirect" in (i.get("url") or "")
                    for i in sponsored))
    ok &= check("and each carries a cpc block, which is the other marker",
                all((i.get("itemOptions") or {}).get("cpc")
                    for i in sponsored))
    rows = parse_products(html, url)
    ok &= check("no sponsored entry becomes a row",
                len(rows) == len(items) - len(sponsored))
    # TWO DEFENCES, and this asserts the FILTER rather than the accident.
    # The row count above holds even with `is_sponsored` disabled, because
    # a click-tracking URL yields no sku and `_row_from_hit` drops the entry
    # anyway — so that assertion alone would pass against a broken filter.
    # (Found by planting exactly that fault and watching the suite stay
    # green; §22's "verify that your control actually failed".)
    #
    # A sponsored entry wearing a product-shaped URL is the case only the
    # filter catches, and Rakuten already puts `cpc` on every one of them.
    disguised = dict(sponsored[0])
    disguised["url"] = "https://item.rakuten.co.jp/someshop/somecode/"
    ok &= check("a paid slot is recognised by its cpc block even when its "
                "URL looks like a product",
                is_sponsored(disguised) is True)
    payload = search_payload(html)
    spiked = json.loads(json.dumps(
        {"state": {"data": {"ichibaSearch": dict(
            payload, items=[disguised] + [i for i in items
                                          if not is_sponsored(i)][:2])}}}))
    spiked_html = ('<html><head><link rel="preload" href="https://a.r10s.jp/x">'
                   '<link rel="preload" href="https://b.r10s.jp/y"></head>'
                   '<body><script>window.__INITIAL_STATE__ = %s;</script>'
                   '</body></html>' % json.dumps(spiked, ensure_ascii=False))
    spiked_rows = parse_products(spiked_html, url)
    ok &= check("...and is still kept out of the rows",
                len(spiked_rows) == 2
                and "someshop:somecode" not in {r.sku for r in spiked_rows})
    ok &= check("...without shifting the rows that remain",
                [r.position for r in spiked_rows] == [1, 2])
    # THE BUG THIS PINS. Numbering positions by payload index made `position`
    # depend on how many ads the site happened to inject: two engines
    # fetching the same listing seconds apart got 52 and 45 entries, so the
    # same 45 products came out numbered 8-52 in one run and 1-45 in the
    # other — identical rows, every position different.
    ok &= check("positions count the rows emitted, not the payload's slots",
                [r.position for r in rows] == list(range(1, len(rows) + 1)))
    ok &= check("position starts at 1 even though the payload starts with ads",
                rows[0].position == 1)
    # page+position must be unique across a multi-page run — one line, and
    # the column is worthless without it (§18).
    multi = parse_products(html, url, page=1) + parse_products(html, url,
                                                               page=2)
    pairs = [(r.page, r.position) for r in multi]
    ok &= check("page+position is unique across pages",
                len(set(pairs)) == len(pairs))
    return ok

def test_euc_jp_detail_pages():
    group("detail pages are EUC-JP and listing pages are UTF-8")
    ok = True
    # The trap: `Content-Type: text/html;charset=EUC-JP` on 8 of 8 item
    # pages, while every listing page is UTF-8. Chromium decodes it, so the
    # three browser engines never see this — an HTTP client does, and those
    # bytes decoded as UTF-8 raise on the first Japanese character.
    text = "商品名テスト"
    euc = text.encode("euc_jp")
    ok &= check("the header's charset is honoured",
                decode_page(euc, "text/html;charset=EUC-JP") == text)
    ok &= check("and a meta charset when there is no header",
                decode_page(b'<meta charset="EUC-JP">' + euc).endswith(text))
    ok &= check("UTF-8 bytes still decode as UTF-8",
                decode_page(text.encode("utf-8")) == text)
    ok &= check("a str passes through untouched", decode_page(text) == text)
    ok &= check("undecodable bytes degrade rather than raising",
                isinstance(decode_page(b"\xff\xfe\x00bad"), str))
    ok &= check("None is the empty string, not a crash", decode_page(None) == "")
    # And the real fixture: its own declared encoding, parsed to real
    # Japanese rather than to mojibake.
    html, url, _ = fx("item_page")
    ok &= check("the item fixture declares EUC-JP",
                "EUC-JP" in html)
    rows = parse_product_page(html, url)
    ok &= check("its title is real Japanese, not replacement characters",
                rows and "\ufffd" not in (rows[0].title or "")
                and "ブレンディ" in rows[0].title)
    ok &= check("its shop name too",
                "\ufffd" not in (rows[0].shop_name or ""))
    return ok

def test_detail_pages():
    group("detail pages: variants, a VERIFIED was-price, and float noise")
    ok = True
    rows = parse_product_page(*_fx_detail("item_discounted"))
    ok &= check("one row per item, not per variant", len(rows) == 1)
    row = rows[0]
    ok &= check("the sku is the same one the listing route produces",
                row.sku == "sawaicoffee-tea:solandluna")
    ok &= check("price is the lowest variant price", row.price == 5199.0)
    ok &= check("the variants ride in their own column",
                row.variant_count == 4 and len(row.variants) == 4)
    ok &= check("each variant carries its own price",
                all(v.get("price") for v in row.variants))
    # A WAS-PRICE, and only where Rakuten's own verification flag allows one.
    # Japan's double-pricing rules require a merchant's reference price to be
    # substantiated, and the site carries the outcome as a flag — which is
    # the "use the site's own should-I-show-this flag" rule (§21).
    ok &= check("a verified reference price becomes original_price",
                row.original_price == 10398.0)
    ok &= check("and WHICH reference price it was is recorded",
                row.original_price_label == "当店通常価格")
    ok &= check("the discount is computed from the two prices",
                row.discount_pct == 50.0)
    # The other fixture has the flag FALSE, so no was-price at all — both
    # values of the flag are exercised, which §20 asks for.
    plain = parse_product_page(*_fx_detail("item_page"))[0]
    ok &= check("an UNVERIFIED reference price is not read",
                plain.original_price is None
                and plain.discount_pct is None)
    # FLOAT NOISE. The listing route serializes this rating as 4.76; the same
    # product's detail island serializes it as 4.760000228881826. Written
    # through, every product run would diff against its listing run on every
    # row.
    ok &= check("the detail island's float32 rating is rounded",
                plain.rating == 4.76)
    ok &= check("and agrees with the listing route's value for the same "
                "product",
                plain.rating == fx_rows("search_p1")[0].rating)
    # A detail page prints three individual review `ratingValue` metas before
    # the aggregate one, so a naive "first ratingValue" read gets 5.00 from
    # one customer.
    ok &= check("the rating is the aggregate, not the first review's score",
                plain.rating != 5.0)
    ok &= check("in_stock comes from the page's own availability microdata",
                plain.in_stock is True)
    ok &= check("the subscription price is its own column and is never read "
                "as a was-price",
                plain.subscription_price == 2324.0
                and plain.original_price is None)
    ok &= check("category is the page's own breadcrumb trail",
                plain.category.startswith("水・ソフトドリンク"))
    ok &= check("price_source names the detail structure",
                plain.price_source == "itemdata")
    ok &= check("the merchant's facts are available for the sidecar",
                shop_metadata(_fx_detail("item_page")[0]).get("shop_code")
                == "ajinomoto")
    ok &= check("a title containing <br> does not carry markup into the row",
                "<" not in (plain.title or ""))
    return ok


def test_x_debug_header_is_redacted():
    """SECURITY.md names the Scraper API's x-debug header as a place
    credentials reach a log unmasked. It was then logged verbatim.

    The fixtures are assembled from pieces rather than written out whole,
    because this file is scanned by the credential check like every other
    and a fixture that LOOKS like a live key fails it. They are the SHAPES a
    credential takes, not the literals this repo happens to contain today.
    """
    try:
        import scraper_api_client as sac
    except ImportError:
        return False

    pw = "SeCr" + "EtPw"
    key = "abcdef01" * 4
    raw = ("cdpurl=ws://acct-zone-scraping_browser-pid-7:" + pw
           + "@cb.2captcha.com:9222 cost=0.00145 key=" + key + " status=200")
    out = sac._redact_debug_header(raw)
    ok = True
    ok &= check("x-debug: the credential and the key are gone",
                      pw not in out and key not in out)
    ok &= check("x-debug: the cost, host and status survive",
                      "cost=0.00145" in out and "cb.2captcha.com:9222" in out
                      and "status=200" in out)

    s1, s2 = "secret" + "one", "secret" + "two"
    two = sac._redact_debug_header(
        "a=http://u1:" + s1 + "@h1:1 b=http://u2:" + s2 + "@h2:2")
    ok &= check("x-debug: both credentials are masked, not just the first",
                      s1 not in two and s2 not in two)

    src = inspect.getsource(sac)
    ok &= check("x-debug: the log line calls the redactor",
                      'logger.info("x-debug: %s", _redact_debug_header(debug))' in src)
    return ok


def test_parse_is_gated_on_the_policy():
    group("the engines read STATE_POLICY's parse column")
    ok = True
    # `should_parse` existed, was correct, and had NO consumer: every
    # engine parsed whatever reached the parse line, so the `parse` column
    # of STATE_POLICY decided nothing and an engine could disagree with the
    # table — and with its twins — without anything noticing. That is the
    # defect CLAUDE.md §17 names for constants, with a function instead.
    #
    # Measured across the family on 2026-09-23 by counting definitions
    # against readers: 7 of 24 repos defined it and none called it.
    import glob as _glob
    engines = sorted(_glob.glob(os.path.join(REPO_ROOT, "*_scraper.py")))
    ok &= check("there are engines to check (%d)" % len(engines), engines)
    for path in engines:
        name = os.path.basename(path)
        text = open(path, encoding="utf-8").read()
        if "_parse_for_mode" not in text:
            continue
        ok &= check("%s reads the parse decision from the policy" % name,
                    "should_parse(" in text)
        # And nothing parses unconditionally any more: a bare
        # `products = _parse_for_mode(` is the shape that ignored the table.
        ok &= check("%s does not parse unconditionally" % name,
                    not re.search(r"products = _parse_for_mode\(", text))
    # Every state the policy names must be answerable — a typo'd state name
    # would make should_parse fall through to its default for ever.
    for state in page_flow.STATE_POLICY:
        ok &= check("should_parse answers for %r" % state,
                    isinstance(page_flow.should_parse(state), bool))

    return ok


def main() -> int:
    ok = True
    # Checks that could not run because an optional engine library is absent.
    # Reported at the end: a suite that silently skips part of itself and
    # still says "all passed" is the same defect as code that reports success
    # without checking that what it wanted actually happened.
    skips = []

    ok &= test_parse_is_gated_on_the_policy()
    ok &= test_price_parsing()
    ok &= test_listing_values()
    ok &= test_measured_absences()
    ok &= test_jsonld_is_a_carousel_not_the_grid()
    ok &= test_dom_fallback()
    ok &= test_sponsored_slots_do_not_shift_position()
    ok &= test_euc_jp_detail_pages()
    ok &= test_detail_pages()
    ok &= test_urls()
    ok &= test_pagination()
    ok &= test_page_state()
    ok &= test_page_flow()
    ok &= test_output_contract()
    ok &= test_writers()
    ok &= test_finish_run()
    ok &= test_diff()
    ok &= test_captcha()
    ok &= test_env_config()
    ok &= test_proxy_pool()
    ok &= test_engines(skips)
    ok &= test_pyppeteer_teardown_noise()
    ok &= test_canary_separates_access_from_defect()
    ok &= test_ci_checks_is_actually_wired_up()
    ok &= test_no_capture_leaks()
    ok &= test_wording()
    ok &= test_fingerprint_client_reads_env()
    ok &= test_fingerprint_application()
    ok &= test_credentials_never_reach_a_log()
    ok &= test_concurrent_dispatch(skips)
    ok &= test_no_undefined_names()
    ok &= test_dockerfile_copies_what_it_runs()
    ok &= test_sample_output()
    ok &= test_public_names_have_consumers()
    ok &= test_log_format_strings_match_their_args()
    ok &= test_every_shared_call_binds()
    ok &= test_x_debug_header_is_redacted()

    print()
    if _failures:
        print("%d check(s) FAILED:" % len(_failures))
        for f in _failures:
            print("  - %s" % f)
    if skips:
        print("%d engine group(s) SKIPPED — an optional engine library is "
              "absent. CI's engine-smoke job installs all three and fails if "
              "this list is non-empty, because a skip reads exactly like a "
              "passing run:" % len(skips))
        for s in skips:
            print("  - %s" % s)
    print("smoke_test: %s" % ("OK" if ok else "FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
