"""product_parser.py — Rakuten Ichiba (rakuten.co.jp) extraction.

This module IS the site. Everything that knows what Rakuten publishes, how
it spells a product id, how it paginates and how it refuses lives here; the
engines carry a dozen named constants and nothing else (§1).

Where the data actually is
--------------------------
Measured on 2026-09-21 from a Hetzner datacentre exit in Helsinki, over
eleven listing captures (405 rows), eight item pages and one shop page.

    LISTING (a keyword search or a genre listing)
        window.__INITIAL_STATE__
          -> .state.data.ichibaSearch.items       45 rows per page
          -> .state.data.ichibaSearch.pagination  {numFound, start,
                                                   pageSize, subset}

    ITEM (one /{shop}/{manageNumber}/ detail page)
        <script type="application/json" id="item-page-app-data">
          -> .newApi.itemInfoSku                  (or .api.data.itemInfoSku)

**Do not use the listing page's JSON-LD.** There is exactly one
`application/ld+json` block on a search page, it is an `ItemList`, and it
holds TEN items whose every url carries `?scid=seo-carousel-search` — it is
the SEO recommendation carousel, not the result grid, while the page itself
holds 45 products. A parser anchored on it returns ten rows of the wrong
products and reports success. It is still read for one narrow purpose: it is
the only place on a listing page that states a currency (see `CURRENCY`
below).

A DETAIL page publishes a different `@type` again — its only JSON-LD is a
`BreadcrumbList` (§20, proven here on a second site). The listing parser
finds nothing at all on a detail page, which is asserted in the suite rather
than left to be discovered.

The item detail page is EUC-JP
------------------------------
`Content-Type: text/html;charset=EUC-JP`, on 8 of 8 item pages, while every
listing page is UTF-8. A browser decodes it and hands `page.content()` back
as `str`, so the three engines never see this; an HTTP client does, and
decoding those bytes as UTF-8 raises on the first Japanese character.
`decode_page` is the one place that knows, and the HTTP paths route through
it. This is why the parser accepts `bytes` as well as `str` everywhere.
"""

from __future__ import annotations

import html as _html
import json
import logging
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import (parse_qsl, quote, unquote, urlencode, urljoin,
                          urlsplit, urlunsplit)

from bs4 import BeautifulSoup

from output_writer import Product

logger = logging.getLogger("product_parser")


# ---------------------------------------------------------------------------
# Hosts and routes
# ---------------------------------------------------------------------------
# Rakuten Ichiba is one marketplace spread over four hostnames, and they are
# not interchangeable — each answers a different question and two of them
# behave differently enough that the distinction is load-bearing:
#
#   search.rakuten.co.jp   the result grid. Paginates with ?p=N.
#   www.rakuten.co.jp      /category/{genreId}/ — the genre LANDING page.
#                          Carries the same ichibaSearch payload, and
#                          IGNORES ?p= entirely (see `paginates_by_url`).
#                          Also /{shopCode}/ — a merchant's own storefront.
#   item.rakuten.co.jp     /{shop}/{manageNumber}/ — one product. EUC-JP.
#   ranking.rakuten.co.jp  refused outright from every address tested.
SEARCH_HOST = "search.rakuten.co.jp"
WWW_HOST = "www.rakuten.co.jp"
ITEM_HOST = "item.rakuten.co.jp"
RANKING_HOST = "ranking.rakuten.co.jp"

HOSTS = (SEARCH_HOST, WWW_HOST, ITEM_HOST, "rakuten.co.jp")

# Hosts that belong to Rakuten Group but are NOT Rakuten Ichiba. Refused with
# the reason rather than with a generic "unsupported host" — §5: telling a
# reader their URL "is not a Rakuten site" when it plainly is sends them
# hunting for a typo.
OTHER_RAKUTEN_HOSTS = {
    RANKING_HOST: ("ranking.rakuten.co.jp is Rakuten's ranking site. This "
                   "repo does not implement it, and the reason is the "
                   "markup rather than the gate: its pages carry NO "
                   "__INITIAL_STATE__ and no item-page-app-data island — "
                   "one JSON-LD BreadcrumbList and 80 product links in "
                   "hand-rolled markup — so reading it needs a third parser "
                   "written against captures nobody has taken, not a new "
                   "host in this list. Note it is NOT unreachable: measured "
                   "2026-09-21 from one datacentre address, it answered 403 "
                   "to plain HTTP and to headless Chromium (3 of 3) and "
                   "HTTP 200 with 416 KB to HEADFUL Chromium (3 of 3) — the "
                   "only route on this site that cares"),
    "books.rakuten.co.jp": ("Rakuten Books is a separate storefront with its "
                            "own markup; this scraper reads Ichiba"),
    "travel.rakuten.co.jp": ("Rakuten Travel sells accommodation, not "
                             "Ichiba products"),
    "www.rakuten.com": ("rakuten.com is Rakuten's US cashback service, a "
                        "different application from the Japanese Ichiba "
                        "marketplace this scraper reads"),
}

# `?lang=` picks the UI language. Two values are honoured and the rest fall
# back silently, which is measured rather than assumed: `?lang=en` comes back
# with `locale: "en"` in the payload, while `?lang=zh-tw` and `?lang=ko` are
# accepted with HTTP 200 and come back `locale: "ja"` (2026-09-21).
#
# And the honest half, which belongs in the README as much as here: `en`
# translates the CHROME and not the DATA. Product names, shop names and tag
# values are merchant-authored Japanese and are byte-identical between an
# `en` and a `ja` run. So `--locale en` gets you an English navigation bar
# around Japanese rows.
LOCALES = ("ja", "en")

# JPY, and this needs saying carefully because the honest answer is not
# "JPY" flatly.
#
# The listing payload states NO currency: zero occurrences of `priceCurrency`,
# `currency` or the string `JPY` across 405 rows. So it is not a fact read
# off the row (§4 rung 1), and defaulting it would be exactly the "guess
# presented as a fact" §8 forbids.
#
# What the site does state, on the same page: `"priceCurrency": "JPY"` in the
# listing page's own JSON-LD carousel block, and `<meta
# itemprop="priceCurrency" content="JPY">` on every detail page — a written
# ISO code, §4 rung 2. `currency_from_page` reads those. This constant is the
# allowlist of what a read is allowed to return, not a default, and a row
# whose page states nothing gets `None`.
CURRENCY = "JPY"
CURRENCY_CODES = frozenset(("JPY",))

# The positive structural signal (§8): a page Rakuten actually served is
# built out of Rakuten's own asset hosts, and an interstitial is not.
# Measured across six served pages and three refusals:
#
#   r10s.jp           90 - 343 on every served page   0 or 1 on every refusal
#
# The `1` is why the threshold is 2 and not 1: the branded refusal page
# carries a single logo off that host.
_ASSET_MARKER = re.compile(r"r10s\.jp|thumbnail\.image\.rakuten\.co\.jp")
_ASSET_MIN_MATCHES = 2

_ITEM_PATH_RE = re.compile(r"^/(?P<shop>[A-Za-z0-9_-]+)/(?P<code>[^/?#]+)/?$")
_GENRE_PATH_RE = re.compile(r"^/search/mall/-/(?P<genre>\d+)/?")
_CATEGORY_PATH_RE = re.compile(r"^/category/(?P<genre>\d+)/?$")
_SEARCH_PATH_RE = re.compile(r"^/search/mall/(?P<keyword>[^/]+)/?")

SELECTORS = {
    # Anchored on the URL pattern, never a class (§4). Rakuten's own class
    # names on the search grid are build hashes.
    "item_link": 'a[href*="item.rakuten.co.jp/"]',
    "tile_price": '[class*="price--"]',
    "tile_title": '[class*="title--"]',
    "grid": '[class*="searchresult"]',
}

PAGE_PARAM = "p"

# 45 rows per page, on 11 of 11 captures, and the payload states it as
# `pagination.pageSize` so it is read rather than assumed where available.
PAGE_SIZE = 45

# Rakuten will serve 6,750 results per query however many it matched, and it
# states that itself as `pagination.subset`. 6750 / 45 = 150 pages, and page
# 150 is exactly the last: it comes back `start: 6705`, which is 6705 + 45 =
# 6750. Page 151 does not error — see `served_offset`.
#
# This is §21's "complete and exhaustive are different words" with Rakuten's
# own arithmetic: one measured query reported `numFound: 3,053,682` against a
# `subset` of 6,750, so a full 150-page run is a complete run and a 0.2%
# sample. The sidecar records both numbers rather than only the status.
SUBSET_CAP = 6750
PAGE_CAP = SUBSET_CAP // PAGE_SIZE          # 150

PAGINATED_KINDS = ("search", "genre")

TRACKING_PARAMS = frozenset("""
scid iasid sc_i sc_e sc_o sc_used rafcid trflg bc_uid l-id
utm_source utm_medium utm_campaign utm_term utm_content utm_id
gclid fbclid msclkid yclid
""".split())

# Not a category, when reading one off a path.
_NOT_A_CATEGORY = frozenset(("search", "mall", "category", "-", ""))

_STATE_RE = re.compile(r"window\.__INITIAL_STATE__\s*=\s*")
_ISLAND_RE = re.compile(
    r'<script[^>]*id="item-page-app-data"[^>]*>(.*?)</script>', re.S)
_LD_RE = re.compile(
    r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.S)

_ZERO_WIDTH = "​‌‍⁠﻿"
_GROUP_SPACES = "    "

# Japanese retail writes a price three ways and all three are real on this
# site's rendered markup: `￥1,234` (fullwidth yen), `¥1,234` (ASCII yen) and
# `1,234円`. There are no decimal subunits in JPY, so a comma is always a
# thousands separator here and the "whichever comes last is the decimal
# point" rule §4 describes does not apply — a bare `1,234` is 1234, never
# 1.234. Used only by the DOM fallback; the payload paths read integers.
_AMOUNT = (r"\d{1,3}(?:[,%s]\d{3})+|\d+" % _GROUP_SPACES)
_PRICE_RE = re.compile(
    r"[￥¥]\s*(" + _AMOUNT + r")"
    r"|(" + _AMOUNT + r")\s*(?:円|JPY)")
_PCT_RE = re.compile(r"-?\s*\d{1,3}(?:[.,]\d+)?\s*[%％]"
                     r"|-?\s*[%％]\s*\d{1,3}(?:[.,]\d+)?")

# ---------------------------------------------------------------------------
# How Rakuten refuses
# ---------------------------------------------------------------------------
# Three refusal skins, and no two of them agree on a status code. Counted
# over six served pages (search/item/shop/category, plain HTTP and headless
# Chromium) and three refusals, 2026-09-21:
#
#                                     status  bytes  ref-id  アクセスが集中
#   Akamai deny (UA/TLS mismatch)        200     43      1        0
#   ranking.rakuten.co.jp                403  1,003     1        3
#   rate-limit / throttle                503  1,806     1        3
#   -- every served page --              200  31k-981k  0        0
#
# The reference-id shape is therefore ONE marker that catches all three
# skins and fires on none of the six served pages. `アクセスが集中`
# ("access is concentrated") separates the two branded pages from the bare
# Akamai stub but cannot tell the 403 from the 503 — those two render the
# IDENTICAL body and differ only in status.
#
# Which is worth stating as its own lesson, because this family has had it
# both ways: on one sibling the status was the only signal a block gave,
# here the status is the only thing separating a THROTTLE you should wait out
# from a BLOCK you should rotate away from — while the Akamai refusal that
# needs neither arrives under HTTP 200. Neither signal is sufficient alone
# and each covers the other's blind spot.
#
# Bounded to a prefix (§20): every refusal body is under 2 KB entire, so
# matching in the first 8 KB covers all of them and cannot let a product
# title deep in a 981 KB grid read as a marker.
_MARKER_PREFIX_BYTES = 8192

# Whitespace-tolerant on purpose. The bytes Akamai actually sends are
# `Reference  #18.536a645f.1789976769.70a9bb4f` — with TWO ASCII spaces. A
# literal `"Reference #"` marker matches none of the three refusals, which is
# §20's "a marker must survive both spellings of the same page" arriving in a
# third costume.
_AKAMAI_REF_RE = re.compile(
    r"Reference\s*#\s*\d+\.[0-9a-f]+\.\d+\.[0-9a-f]+", re.I)

BLOCK_MARKERS = (
    "アクセスが集中しております",         # the branded 403/503 page's <title>
    "アクセスが集中",
)

# Deliberately not carried, each for a measured reason:
#
#   cf-turnstile   §8/§19 — the Scraping Browser's auto-solve extension
#                  injects it into every page it loads, so it fires on good
#                  pages. Measured useless on two sibling sites.
#   akamai / akam  §18 — a marker that matches every page is worse than no
#                  marker. Rakuten is fronted by Akamai, and although its
#                  served markup happens to reference no Akamai host (0 of 6
#                  captures), a CDN name is a fact about the site's
#                  infrastructure and not a signal about this response.
#   Reference #    the literal spelling, for the two-space reason above.

# No captcha is configured anywhere on this site, and that is a measurement
# rather than an assumption — §18's "did we meet one" is the wrong question,
# so the right one was asked: *is one configured, and would we recognise it
# if it appeared?*
#
# Counted across all nine captures (six served, three refused): zero
# occurrences of reCAPTCHA, hCaptcha, Turnstile, DataDome, PerimeterX,
# Incapsula, Kasada or AWS WAF; no challenge iframe; no `data-sitekey`; no
# `*_SITE_KEY` in any page config; no `<captcha-*>` mount point. Akamai
# refuses here with a 43-byte deny and renders nothing to solve.
#
# So this set exists to RECOGNISE an escalation, not because one was seen —
# and none of these strings appears on a served page, so none of them can
# fire on a good one. What the narrow, honest word "unsolvable" applies to
# (§19) is that 43-byte deny page: it carries no widget, so there is nothing
# on it for any solver at any price. That is a property of that page and says
# nothing about what 2Captcha can solve.
BOT_CHALLENGE_MARKERS = (
    "recaptcha/api.js",
    "recaptcha/api2/anchor",
    "recaptcha/api2/bframe",
    "grecaptcha.render",
    "g-recaptcha",
    "hcaptcha.com/1/api.js",
    "challenges.cloudflare.com",
    "captcha-delivery.com",          # DataDome
    "px-captcha",                    # PerimeterX
    "awswaf.com",
    "data-sitekey",
)

_EXTENSION_SCRIPT_RE = re.compile(
    r'<script[^>]+src="(?:chrome|moz)-extension://[^"]*"[^>]*>\s*</script>',
    re.I)

# The site's own "nothing matched" sentence, in its own language. Kept as a
# secondary signal only: the payload states `numFound: 0` outright, and a
# number the site computed beats a sentence a translation could change.
NO_RESULTS_MARKERS = (
    "該当する商品",
    "見つかりませんでした",
)


# ---------------------------------------------------------------------------
# Encoding
# ---------------------------------------------------------------------------
_META_CHARSET_RE = re.compile(rb"""charset=["']?([\w-]+)""", re.I)


def decode_page(raw: Any, content_type: Optional[str] = None) -> str:
    """Bytes from this site as text, using the charset the site declares.

    Exists because rakuten.co.jp serves two encodings and the difference is
    not cosmetic:

        search.rakuten.co.jp   UTF-8
        item.rakuten.co.jp     EUC-JP   (8 of 8 item pages, 2026-09-21)

    `Content-Type: text/html;charset=EUC-JP` with `X-Content-Type-Options:
    nosniff`, and those bytes decoded as UTF-8 raise `UnicodeDecodeError` on
    the first Japanese character of the title. Passed `errors="replace"`
    instead they come back as mojibake, which is worse: every title, shop
    name and tag on the page becomes replacement characters while the
    numbers still parse, so the run reports success with a column of garbage.

    The three browser engines never see this — Chromium decodes the page and
    `page.content()` is already `str` — so this is the HTTP paths' problem
    alone, and a `str` in is returned unchanged.
    """
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    enc = None
    if content_type:
        m = re.search(r"charset=([\w-]+)", content_type, re.I)
        if m:
            enc = m.group(1)
    if not enc:
        m = _META_CHARSET_RE.search(raw[:4096])
        if m:
            try:
                enc = m.group(1).decode("ascii")
            except Exception:
                enc = None
    for candidate in (enc, "utf-8", "euc_jp", "shift_jis"):
        if not candidate:
            continue
        try:
            return raw.decode(candidate)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


def _text(html: Any) -> str:
    """Whatever an engine or a test handed us, as text."""
    if html is None:
        return ""
    return decode_page(html)


# ---------------------------------------------------------------------------
# URLs
# ---------------------------------------------------------------------------
def site_host(url: str) -> Optional[str]:
    host = (urlsplit(url).hostname or "").lower()
    if not host:
        return None
    return host


def unsupported_reason(url: str) -> Optional[str]:
    """Why this URL is not an Ichiba URL this scraper reads, or None.

    Refused WITH the reason (§5). Every entry in `OTHER_RAKUTEN_HOSTS` is a
    real Rakuten property, so "not a Rakuten site" would be false and would
    send the reader looking for a typo they did not make.
    """
    host = site_host(url)
    if not host:
        return "%r is not a URL" % (url,)
    if host in OTHER_RAKUTEN_HOSTS:
        return "%s: %s" % (host, OTHER_RAKUTEN_HOSTS[host])
    bare = host[4:] if host.startswith("www.") else host
    if host in HOSTS or bare == "rakuten.co.jp":
        kind = listing_kind(url)
        if kind == "shop":
            return ("%s is a merchant's own storefront. Its page carries an "
                    "EMPTY `__INITIAL_STATE__.state.data` and no "
                    "`ichibaSearch` payload at all (measured 2026-09-21), so "
                    "reading it would need a second, untested parser. Pass a "
                    "search or genre URL, or an item URL with "
                    "--mode product" % host)
        if kind == "unknown":
            return ("%s is a Rakuten Ichiba host but %r is not a route this "
                    "scraper reads. Supported: a keyword search "
                    "(search.rakuten.co.jp/search/mall/{keyword}/), a genre "
                    "listing (.../search/mall/-/{genreId}/ or "
                    "www.rakuten.co.jp/category/{genreId}/), or one product "
                    "(item.rakuten.co.jp/{shop}/{code}/)"
                    % (host, urlsplit(url).path))
        return None
    return ("%s is not a Rakuten Ichiba host. Supported hosts: %s"
            % (host, ", ".join(HOSTS[:3])))


def is_supported_host(url: str) -> bool:
    return unsupported_reason(url) is None


def locale_of(url: str) -> Optional[str]:
    """The `?lang=` this URL asks for, where it asks for a known one."""
    for key, value in parse_qsl(urlsplit(url).query):
        if key == "lang":
            return value if value in LOCALES else None
    return None


def listing_kind(url: str) -> str:
    """Which of Rakuten's routes this URL is.

    Five answers, and the distinctions all cost something to get wrong:

        search    /search/mall/{keyword}/          paginates with ?p=
        genre     /search/mall/-/{genreId}/        paginates with ?p=
        category  www.rakuten.co.jp/category/{id}/ IGNORES ?p=
        item      item.rakuten.co.jp/{shop}/{code}/
        shop      www.rakuten.co.jp/{shopCode}/
    """
    host = site_host(url) or ""
    path = urlsplit(url).path or "/"
    if host in OTHER_RAKUTEN_HOSTS:
        return "other"
    if host == ITEM_HOST:
        return "item" if _ITEM_PATH_RE.match(path) else "unknown"
    if host == SEARCH_HOST:
        if _GENRE_PATH_RE.match(path):
            return "genre"
        m = _SEARCH_PATH_RE.match(path)
        if m and unquote(m.group("keyword")) != "-":
            return "search"
        if path.rstrip("/") in ("/search/mall", "/search"):
            return "home"
        return "unknown"
    if host in (WWW_HOST, "rakuten.co.jp"):
        if _CATEGORY_PATH_RE.match(path):
            return "category"
        if path.rstrip("/") in ("", "/"):
            return "home"
        if re.match(r"^/[A-Za-z0-9_-]+/?$", path):
            return "shop"
        return "unknown"
    return "unknown"


def strip_tracking(url: str) -> str:
    parts = urlsplit(url)
    kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if k not in TRACKING_PARAMS]
    return urlunsplit((parts.scheme, parts.netloc, parts.path,
                       urlencode(kept), ""))


def paginates_by_url(url: str) -> bool:
    """Whether page N of this listing has an address of its own.

    False for `www.rakuten.co.jp/category/{id}/`, and this is the single
    most expensive thing about Rakuten's pagination — measured, not read off
    the markup:

        www.rakuten.co.jp/category/100356/?p=2   HTTP 200, start: 0
        www.rakuten.co.jp/category/100356/?p=4   HTTP 200, start: 0

    The genre LANDING page accepts `?p=` and serves page one anyway, with no
    error and no redirect. A run that trusted its own request would collect
    page 1 as many times as it was asked to and report a complete,
    well-formed, entirely duplicate file.

    The route is not broken, it is just not the addressable one: that page's
    OWN next link points at `search.rakuten.co.jp/search/mall/-/100356/?p=2`,
    which does work and comes back `start: 45`. So `page_url` sends page 2 of
    a `/category/` URL to the search host, exactly where the site's own link
    sends a browser.
    """
    return listing_kind(url) in PAGINATED_KINDS


def genre_of(url: str) -> Optional[str]:
    """The Rakuten genre id this URL names, on either of the two routes."""
    path = urlsplit(url).path or ""
    m = _CATEGORY_PATH_RE.match(path) or _GENRE_PATH_RE.match(path)
    return m.group("genre") if m else None


def page_url(url: str, page_num: int) -> Optional[str]:
    """The address of page `page_num` of this listing, or None.

    `?p=N`, 1-based, and it replaces an existing `p` rather than appending a
    second one. A `/category/{genreId}/` URL is rewritten onto the search
    host for page 2 and beyond, because that is the only address the site
    will honour — see `paginates_by_url`.
    """
    if page_num < 1:
        return None
    kind = listing_kind(url)
    if kind == "category":
        genre = genre_of(url)
        if not genre:
            return None
        if page_num == 1:
            return strip_tracking(url)
        lang = locale_of(url)
        query = [(PAGE_PARAM, str(page_num))]
        if lang:
            query.append(("lang", lang))
        return urlunsplit(("https", SEARCH_HOST,
                           "/search/mall/-/%s/" % genre,
                           urlencode(query), ""))
    if kind not in PAGINATED_KINDS:
        return None
    parts = urlsplit(strip_tracking(url))
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if k != PAGE_PARAM]
    if page_num > 1:
        query.append((PAGE_PARAM, str(page_num)))
    return urlunsplit((parts.scheme or "https", parts.netloc, parts.path,
                       urlencode(query), ""))


def page_number_from_url(url: str) -> int:
    for key, value in parse_qsl(urlsplit(url).query):
        if key == PAGE_PARAM:
            try:
                return max(1, int(value))
            except (TypeError, ValueError):
                return 1
    return 1


def category_from_url(url: str) -> Optional[str]:
    """What this URL asks for — a keyword, or a genre id.

    A fallback for the run metadata and for `--category`. The per-ROW
    category is the product's own genre path out of the payload, which is a
    fact about the product rather than about the query.
    """
    kind = listing_kind(url)
    if kind in ("genre", "category"):
        genre = genre_of(url)
        return "genre:%s" % genre if genre else None
    if kind == "search":
        m = _SEARCH_PATH_RE.match(urlsplit(url).path or "")
        if not m:
            return None
        keyword = unquote(m.group("keyword"))
        return keyword if keyword not in _NOT_A_CATEGORY else None
    if kind == "item":
        m = _ITEM_PATH_RE.match(urlsplit(url).path or "")
        return m.group("shop") if m else None
    return None


def search_url(keyword: str, genre: Optional[str] = None,
               lang: Optional[str] = None) -> str:
    """A keyword search URL, with the keyword percent-encoded.

    Rakuten's own search URLs carry the keyword as a UTF-8 percent-encoded
    PATH segment (`/search/mall/%E3%82%B3%E3%83%BC%E3%83%92%E3%83%BC/`), not
    as a query parameter, so a caller passing Japanese text needs it encoded
    the way the site spells it.
    """
    path = "/search/mall/%s/" % quote(keyword, safe="")
    if genre:
        path += "-/%s/" % genre
    query = urlencode([("lang", lang)]) if lang in LOCALES and lang else ""
    return urlunsplit(("https", SEARCH_HOST, path, query, ""))


def sku_from_url(url: str) -> Optional[str]:
    """This product's id, as Rakuten itself spells it: `{shop}:{manageNumber}`.

    Recovered from the URL, and that is a decision with a measurement behind
    it. The obvious candidate in the listing payload is `variantId`, and it
    is WRONG: it names the pre-selected SKU inside the item, not the item.
    Measured over 405 rows, `variantId` equals the URL's own item code on
    only 39 of 180 — the rest read `ac-sale-1960-80-as-m` where the product
    is `ac-sale-1960`, or `26562725` where it is `solandluna`.

    The URL tail is the item's `manageNumber`, and using it makes one id work
    everywhere: on 8 of 8 item pages `{shop}:{tail}` equals both
    `{shop.shopUrl}:{itemInfoSku.manageNumber}` and the detail page's own
    `<meta itemprop="sku">`. So a listing row and a product row for the same
    product carry a byte-identical `sku`, which is what lets `diff_runs.py`
    compare a listing run against a product run at all.
    """
    parts = urlsplit(url)
    if (parts.hostname or "").lower() != ITEM_HOST:
        return None
    m = _ITEM_PATH_RE.match(parts.path or "")
    if not m:
        return None
    return "%s:%s" % (m.group("shop"), m.group("code"))


# ---------------------------------------------------------------------------
# Payload extraction
# ---------------------------------------------------------------------------
def initial_state(html: Any) -> Optional[dict]:
    """`window.__INITIAL_STATE__` as a dict, or None.

    Decoded with `json.JSONDecoder().raw_decode`, which parses exactly one
    JSON value and reports where it ended. The obvious alternative — count
    `{` and `}` until they balance — is wrong on this site for a reason that
    would surface as a rare, undebuggable failure: the payload is full of
    merchant-authored product names, and a name containing a brace
    (`【1箱{30本}】`) unbalances the count and truncates the parse. A real
    JSON parser is both shorter and correct.
    """
    text = _text(html)
    m = _STATE_RE.search(text)
    if not m:
        return None
    try:
        value, _ = json.JSONDecoder().raw_decode(text, m.end())
    except ValueError as exc:
        logger.debug("__INITIAL_STATE__ did not parse: %s", exc)
        return None
    return value if isinstance(value, dict) else None


def search_payload(html: Any) -> dict:
    """`.state.data.ichibaSearch`, or {}."""
    state = initial_state(html)
    if not isinstance(state, dict):
        return {}
    node = ((state.get("state") or {}).get("data") or {}).get("ichibaSearch")
    return node if isinstance(node, dict) else {}


def item_island(html: Any) -> Optional[dict]:
    """`itemInfoSku` off a detail page, or None.

    `newApi` first and `api.data` as the fallback: both are present and hold
    the same object on 8 of 8 item pages, and reading whichever exists costs
    one line against a page kind that clearly has a migration in flight.
    """
    text = _text(html)
    m = _ISLAND_RE.search(text)
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
    except ValueError as exc:
        logger.debug("item-page-app-data did not parse: %s", exc)
        return None
    if not isinstance(data, dict):
        return None
    for path in (("newApi", "itemInfoSku"), ("api", "data", "itemInfoSku")):
        node: Any = data
        for key in path:
            node = (node or {}).get(key) if isinstance(node, dict) else None
        if isinstance(node, dict) and node:
            node = dict(node)
            node["_shop"] = data.get("shop") or {}
            node["_rat"] = (data.get("rat") or {}).get("genericParameter") or {}
            return node
    return None


def pagination(html: Any) -> dict:
    node = search_payload(html).get("pagination")
    return node if isinstance(node, dict) else {}


def total_results(html: Any) -> Optional[int]:
    """`numFound` — how many products the site says matched.

    A living number and not a stable one: three fetches of one query inside
    a minute reported 3,053,682, 3,053,713 and 3,053,712. Recorded as the
    site's own answer at the moment of the run, never asserted against.
    """
    return _int(pagination(html).get("numFound"))


def reachable_max(html: Any) -> Optional[int]:
    """How many of those the site will actually serve — `subset`.

    Null in the payload whenever the result set is small enough not to be
    capped (measured: `subset: null` on a 0-hit and on a 5-hit query, 6,750
    on every query above the cap), so None here means "not capped" and not
    "unknown".
    """
    return _int(pagination(html).get("subset"))


def hits_per_page(html: Any) -> int:
    return _int(pagination(html).get("pageSize")) or PAGE_SIZE


def served_offset(html: Any) -> Optional[int]:
    """Which offset the SERVER says it served — `pagination.start`.

    The defence against Rakuten's out-of-range behaviour, and it has to be
    this rather than a heuristic, because neither of the two routes fails in
    a way a client can see from its own request:

        search.rakuten.co.jp/.../?p=151     301 -> page 1, then HTTP 200
                                            with `start: 0` and 45 rows
        www.rakuten.co.jp/category/{id}/?p=2   HTTP 200, `start: 0`

    Both answer 200 with a full, well-formed page of real products. Asking
    for page 151 and being handed `start: 0` is the site stating outright
    that it served page 1, so "asked for N, got something else" is an
    unambiguous end of listing (§23, on a second site and in a second
    spelling).
    """
    return _int(pagination(html).get("start"))


def served_page_number(html: Any) -> Optional[int]:
    offset = served_offset(html)
    if offset is None:
        return None
    size = hits_per_page(html) or PAGE_SIZE
    return offset // size + 1 if size else None


def total_pages(html: Any, url: str = "") -> Optional[int]:
    """Pages this listing has, as the site's own arithmetic gives them."""
    found = total_results(html)
    if found is None:
        return None
    size = hits_per_page(html)
    cap = reachable_max(html)
    usable = min(found, cap) if cap else found
    if usable <= 0:
        return 0
    return -(-usable // size)          # ceil


def capped_by_site(html: Any) -> bool:
    found, cap = total_results(html), reachable_max(html)
    return bool(found and cap and cap < found)


def pages_beyond_cap(html: Any) -> int:
    """Pages of matches this query has that Rakuten will not address."""
    found, cap = total_results(html), reachable_max(html)
    if not found or not cap or cap >= found:
        return 0
    size = hits_per_page(html)
    return -(-(found - cap) // size)


def search_header(html: Any) -> Optional[str]:
    state = initial_state(html) or {}
    meta = (state.get("state") or {}).get("metadata") or {}
    lang = meta.get("lang")
    return str(lang) if lang else None


# ---------------------------------------------------------------------------
# Small conversions
# ---------------------------------------------------------------------------
def _clean(text: Any) -> str:
    """Text as a human reads it, with markup removed.

    The tag strip is not defensive tidiness: a detail page's own
    `itemInfoSku.title` contains literal `<br>` (1 of 8 item pages measured),
    so a title written straight through carries HTML into the CSV. The
    listing payload's `name` is clean on 405 of 405, and is put through the
    same function anyway rather than relying on that staying true.
    """
    if text is None:
        return ""
    s = str(text)
    if "<" in s:
        s = re.sub(r"<[^>]+>", " ", s)
    s = _html.unescape(s)
    s = s.translate({ord(c): None for c in _ZERO_WIDTH})
    return re.sub(r"\s+", " ", s).strip()


def _int(value: Any) -> Optional[int]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    try:
        return int(str(value).strip().replace(",", ""))
    except (TypeError, ValueError):
        return None


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip().replace(",", ""))
    except (TypeError, ValueError):
        return None


def _rating(value: Any) -> Optional[float]:
    """A review score, rounded to two places.

    The rounding is load-bearing across modes rather than cosmetic. A
    listing row states `4.76`; the SAME product's detail island states
    `4.760000228881826` — the site serializes that field from a 32-bit float
    on the item route and not on the listing route. Written through, every
    row of a product run differs from its listing row in the `rating`
    column, and `diff_runs.py` reports a rating change on every product in
    the file. Rakuten publishes one decimal place; two is already generous.
    """
    number = _number(value)
    return None if number is None else round(number, 2)


def _normalize_amount(raw: str) -> Optional[float]:
    if not raw:
        return None
    cleaned = raw
    for space in _GROUP_SPACES:
        cleaned = cleaned.replace(space, "")
    cleaned = cleaned.replace(",", "")
    try:
        return float(cleaned)
    except ValueError:
        return None


def prices_in(text: str) -> List[float]:
    """Every JPY amount in a piece of rendered text, in order.

    Percentages are removed BEFORE matching, not rejected afterwards (§4):
    Japanese discount badges write `50%OFF` and `-10％` with both ASCII and
    fullwidth signs, and a rejected match has still consumed its
    neighbouring yen symbol.
    """
    if not text:
        return []
    cleaned = _PCT_RE.sub(" ", _clean(text))
    out: List[float] = []
    for m in _PRICE_RE.finditer(cleaned):
        amount = _normalize_amount(m.group(1) or m.group(2) or "")
        if amount is not None:
            out.append(amount)
    return out


def price_in(text: str) -> Optional[float]:
    found = prices_in(text)
    return found[0] if found else None


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------
def served_by_rakuten(html: Any) -> bool:
    text = _text(html)
    if not text:
        return False
    return len(_ASSET_MARKER.findall(text)) >= _ASSET_MIN_MATCHES


def _without_extension_scripts(text: str) -> str:
    return _EXTENSION_SCRIPT_RE.sub("", text)


def detect_block_marker(html: Any) -> Optional[str]:
    """Which refusal this is, or None.

    Matched over a bounded prefix and with entities unescaped, per §20 — an
    edge can entity-escape the punctuation of its own reference id on the way
    to an HTTP client while a browser parses it back out plain.
    """
    text = _text(html)
    if not text:
        return None
    head = _html.unescape(text[:_MARKER_PREFIX_BYTES])
    if _AKAMAI_REF_RE.search(head):
        return "akamai-reference-id"
    for marker in BLOCK_MARKERS:
        if marker in head:
            return marker
    return None


def detect_bot_challenge(html: Any, url: str = "") -> Optional[str]:
    """Which solvable challenge is on this page, or None.

    Expected to return None on every page of this site, and that is a
    measurement (see `BOT_CHALLENGE_MARKERS`) rather than a shortcut: the
    scan runs, and it runs against the markup with extension-injected
    `<script>` tags removed so a managed browser's own auto-solve hunters
    cannot be mistaken for the site's (§8).
    """
    text = _text(html)
    if not text:
        return None
    scanned = _without_extension_scripts(text)
    for marker in BOT_CHALLENGE_MARKERS:
        if marker in scanned:
            return marker
    return None


def is_no_results(html: Any) -> bool:
    """Whether the site said, itself, that nothing matched.

    `numFound == 0` is the site's own arithmetic and is checked first; the
    Japanese sentence is a fallback for a response whose payload is missing.
    """
    payload = search_payload(html)
    if payload:
        found = _int((payload.get("pagination") or {}).get("numFound"))
        if found is not None:
            return found == 0
    text = _text(html)
    return any(m in text for m in NO_RESULTS_MARKERS)


def detect_page_state(html: Any, status: Optional[int] = None,
                      url: str = "") -> str:
    """Which of six states this response is.

    Ordered by how much each signal PROVES, not by what is cheap to check
    (§17): a payload the site computed outranks a threshold on how many of
    its own images a page mentions. Getting that backwards is how a sibling
    repo reported exit 3 for a correct answer.

        blocked    Akamai's 43-byte deny (HTTP 200!), or the branded page
                   under 403
        throttled  the SAME branded page under 503 — Rakuten's answer to a
                   client going too fast. Retryable at the same exit, which
                   is what separates it from `blocked`
        empty      served, and `numFound: 0`
        content    the listing payload or the item island is here
        shell      served, built from Rakuten's own assets, payload not here
                   yet — wants a WAIT, not a refetch
        challenge  something solvable rendered (never observed on this site)

    The STATUS is consulted before the block marker, and that order is a
    bug this function had first. All three of Rakuten's refusal skins carry
    an Akamai reference id — the branded page prints one in its own
    `.code` div — so a marker-first reading called the 503 throttle
    `blocked`, turning a wait-and-retry into a rotate-and-give-up. The 403
    and the 503 render the IDENTICAL body; the status is the only thing
    that separates them, which is the mirror image of the sibling site
    where the body was the only thing that separated anything.
    """
    if html is None:
        return "blocked"
    text = _text(html)

    # An unambiguous positive: the site's own payload. Checked before the
    # status, because a page carrying its whole grid is content whatever an
    # edge stamped on the response.
    payload = search_payload(text)
    if payload:
        found = _int((payload.get("pagination") or {}).get("numFound"))
        items = payload.get("items")
        if found == 0:
            return "empty"
        if isinstance(items, list) and items:
            return "content"
        # Payload present, hits not in it. Served and still assembling.
        return "shell"
    if item_island(text) is not None:
        return "content"

    marker = detect_block_marker(text)
    if status == 503:
        return "throttled"
    if status in (403, 401, 407, 429):
        return "blocked"
    if status is not None and status >= 500:
        return "throttled"
    if marker:
        # No status to read — Selenium cannot give one. The branded page
        # (which says アクセスが集中, "access is concentrated") is the 403 AND
        # the 503 at once here, so with no status the safe reading is the
        # recoverable one: a throttle costs a wait, while calling a throttle
        # a block spends the block budget and the exit rotation on a page
        # that would have come back on its own.
        branded = any(m in _html.unescape(text[:_MARKER_PREFIX_BYTES])
                      for m in BLOCK_MARKERS)
        return "throttled" if branded else "blocked"

    if detect_bot_challenge(text, url):
        return "challenge"
    if served_by_rakuten(text):
        return "shell"
    return "blocked"


# ---------------------------------------------------------------------------
# JSON-LD — for the currency, and for nothing else
# ---------------------------------------------------------------------------
def jsonld_blocks(html: Any) -> List[Any]:
    out: List[Any] = []
    for raw in _LD_RE.findall(_text(html)):
        try:
            out.append(json.loads(raw))
        except ValueError:
            continue
    return out


def _walk(node: Any):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


def currency_from_page(html: Any) -> Optional[str]:
    """The currency this page states, or None.

    Two places state one and neither is the row itself:

        listing   `offers.priceCurrency` in the page's own JSON-LD
        item      `<meta itemprop="priceCurrency" content="JPY">`

    Checked against an allowlist of real ISO 4217 codes rather than accepted
    as any three letters (§4), and returning None rather than defaulting to
    JPY when a page states nothing — a defaulted currency is a guess wearing
    a fact's clothes, and this marketplace having only ever traded in yen is
    not the same as this page having said so.
    """
    text = _text(html)
    for node in _walk(jsonld_blocks(text)):
        code = node.get("priceCurrency")
        if isinstance(code, str) and code.strip().upper() in CURRENCY_CODES:
            return code.strip().upper()
    m = re.search(
        r'<meta[^>]+itemprop="priceCurrency"[^>]+content="([A-Z]{3})"', text)
    if m and m.group(1) in CURRENCY_CODES:
        return m.group(1)
    return None


def jsonld_currency(html: Any) -> Optional[str]:
    """Family-shared name for `currency_from_page`."""
    return currency_from_page(html)


def carousel_urls(html: Any) -> List[str]:
    """The listing page's JSON-LD item urls.

    Exposed so the suite can PIN what they are — the SEO carousel, ten of
    them, every one carrying `?scid=seo-carousel-search` — rather than
    leaving a future reader to rediscover that they are not the grid.
    """
    out: List[str] = []
    for block in jsonld_blocks(html):
        if not isinstance(block, dict) or block.get("@type") != "ItemList":
            continue
        for entry in block.get("itemListElement") or []:
            item = entry.get("item") if isinstance(entry, dict) else None
            url = item.get("url") if isinstance(item, dict) else None
            if isinstance(url, str):
                out.append(url)
    return out


# ---------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------
_BRAND_TAG_GROUPS = ("ブランド",)


def _brand(hit: dict) -> Optional[str]:
    """The manufacturer, where Rakuten states one as a taxonomy value.

    `hit["brand"]` is null on 405 of 405 rows, so the column would be dead
    if that were the only source (§9). What IS populated is the item's tag
    under the tag group Rakuten itself names ブランド ("brand") — 240 of 405
    rows, 59%.

    That is a value the merchant picked out of Rakuten's own list, which
    makes it a fact about the product. Deliberately NOT derived by splitting
    the title, which on this site would be a guess of the worst kind: a
    Rakuten product name opens with campaign text (`【9/24まで P10倍！】`)
    far more often than with a maker.
    """
    for tag in hit.get("tags") or []:
        if not isinstance(tag, dict):
            continue
        group = (tag.get("tag_group") or {}).get("name")
        if group in _BRAND_TAG_GROUPS:
            name = _clean(tag.get("name"))
            if name:
                return name
    return None


def _tags(hit: dict) -> Optional[dict]:
    """Rakuten's own tag groups for this item, as {group: [values]}."""
    out: Dict[str, List[str]] = {}
    for tag in hit.get("tags") or []:
        if not isinstance(tag, dict):
            continue
        group = _clean((tag.get("tag_group") or {}).get("name")) or "?"
        name = _clean(tag.get("name"))
        if name:
            out.setdefault(group, []).append(name)
    return out or None


def _review(node: Any) -> Tuple[Optional[float], Optional[int]]:
    """A rating and its count, with Rakuten's zeros read as absence.

    `review` is NEVER null on this site — an unreviewed product comes back
    `{score: 0, numReviews: 0}`, on 28 of 405 measured rows (6.9%), and a
    zero written through drags every average a consumer computes (§21).
    Both columns go null together, keyed on the COUNT: a count of zero means
    nobody has rated it, and the score beside it is a sentinel rather than a
    grade of nought.
    """
    if not isinstance(node, dict):
        return None, None
    count = _int(node.get("numReviews"))
    if node.get("numReviews") is None:
        count = _int(node.get("itemReviewCount"))
        score_raw = node.get("itemReviewRating")
    else:
        score_raw = node.get("score")
    if not count:
        return None, None
    return _rating(score_raw), count


def _price_range(hit: dict) -> Tuple[Optional[float], Optional[float]]:
    """(low, high) for an item whose variants are not all one price.

    `skuInfo.priceRange` is `"2447/6975"` when `hasPriceRange` and a bare
    `"1720"` when not — 95 of 405 rows carry a range. `price` is the low end,
    which is what the tile prints, and `price_max` carries the high end so a
    consumer is not told a ¥2,447 product when the variant they want is
    ¥6,975.
    """
    raw = (hit.get("skuInfo") or {}).get("priceRange")
    low = _number(hit.get("price"))
    if not isinstance(raw, str) or "/" not in raw:
        return low, None
    left, _, right = raw.partition("/")
    high = _number(right)
    low = _number(left) if _number(left) is not None else low
    if high is not None and low is not None and high <= low:
        return low, None
    return low, high


def _discount(price: Optional[float],
              original: Optional[float]) -> Optional[float]:
    """The discount the two prices imply, or None.

    None rather than 0 or a negative when `original` is not above `price`:
    two figures that are not what they were taken for should stop the
    arithmetic rather than produce a plausible wrong number (§4).
    """
    if price is None or original is None or original <= price or price < 0:
        return None
    return round((original - price) / original * 100, 1)


def _image(hit: dict) -> Optional[str]:
    for image in hit.get("images") or []:
        if isinstance(image, dict):
            url = image.get("url") or image.get("location")
            if isinstance(url, str) and url:
                return url
    return None


def _genre_path(hit: dict) -> Optional[str]:
    names = [_clean(g.get("name")) for g in hit.get("genres") or []
             if isinstance(g, dict) and _clean(g.get("name"))]
    return " > ".join(names) if names else None


def is_sponsored(hit: dict) -> bool:
    """Whether this payload entry is a paid placement rather than a product.

    Rakuten injects sponsored entries into `ichibaSearch.items` alongside the
    real results, and they are not products in any usable sense: all of them
    on one measured page shared a single `url` pointing at
    `grp07.ias.rakuten.co.jp/redirect_rpp/?s=…`, a click-tracking redirect,
    with `itemOptions.cpc` populated and no item code anywhere. There is
    nothing to key a row on.

    Measured 2026-09-21 on one genre listing: 52 payload entries, of which
    **7 were sponsored** and 45 were products — and the count VARIES between
    fetches of the same URL, which is what makes this worth its own
    function. Two engines fetching the same listing seconds apart saw 45 and
    52 entries.
    """
    if (hit.get("itemOptions") or {}).get("cpc"):
        return True
    url = hit.get("url") or ""
    return isinstance(url, str) and "ias.rakuten.co.jp/redirect" in url


def _row_from_hit(hit: dict, page: int, position: int, url: str,
                  category: Optional[str], currency: Optional[str]
                  ) -> Optional[Product]:
    item_url = hit.get("url") or hit.get("originalItemUrl")
    if not isinstance(item_url, str) or not item_url:
        return None
    sku = sku_from_url(item_url)
    if not sku:
        return None
    shop = hit.get("shop") or {}
    shipping = hit.get("shipping") or {}
    point = hit.get("point") or {}
    options = hit.get("itemOptions") or {}
    ranking = hit.get("ranking") or {}
    rating, review_count = _review(hit.get("review"))
    price, price_max = _price_range(hit)
    shipping_fee = _number(shipping.get("price"))
    return Product(
        url=strip_tracking(item_url),
        sku=sku,
        title=_clean(hit.get("name")) or None,
        brand=_brand(hit),
        price=price,
        currency=currency if price is not None else None,
        # No was-price exists in the listing payload — see `parse_products`.
        original_price=None,
        discount_pct=None,
        rating=rating,
        review_count=review_count,
        # `isSoldOut` is False on 405 of 405 rows: this search excludes
        # sold-out items rather than marking them. See `parse_products`.
        in_stock=(not hit.get("isSoldOut")) if "isSoldOut" in hit else None,
        image_url=_image(hit),
        category=_genre_path(hit) or category,
        price_source="state",
        page=page,
        position=position,
        # ---- Rakuten's own columns ----
        variant_id=_clean(hit.get("variantId")) or None,
        item_number=_clean(hit.get("code")) or None,
        price_max=price_max,
        has_price_range=bool(hit.get("hasPriceRange")),
        subscription_price=_number((hit.get("subscription") or {}).get("price")),
        points=_int(point.get("count")),
        point_rate=_int(point.get("itemMultiplier")),
        shipping_fee=shipping_fee,
        free_shipping=(shipping_fee == 0) if shipping_fee is not None else None,
        delivery_estimate=_clean(shipping.get("estimateDeliveryDayShort")) or None,
        shop_name=_clean(shop.get("name")) or None,
        shop_code=_clean(shop.get("urlCode")) or None,
        shop_id=_int(shop.get("id")),
        shop_url=shop.get("url") or None,
        genre_id=_int((hit.get("genres") or [{}])[-1].get("id")) or None,
        genre_path=(hit.get("genreIdList") or None),
        genre_rank=_int(ranking.get("rank")),
        is_super_deal=bool(options.get("superDeal")),
        is_39shop=bool(options.get("shop39")),
        is_official_shop=bool(shop.get("officialBrandShopLabel")),
        variant_count=None,
        original_price_label=None,
        tags=_tags(hit),
    )


def parse_products(html: Any, url: str, page: int = 1,
                   position_offset: int = 0,
                   category: Optional[str] = None,
                   currency: Optional[str] = None) -> List[Product]:
    """Rows from a listing page.

    `page` is threaded in rather than inferred, and the reason is a sibling
    repo's arithmetic bug worth not repeating: `position` restarts at 1 on
    every page, so a two-page run whose rows all claim `page: 1` has 45 rows
    silently sharing a position with another row. The suite asserts
    `page`+`position` unique across a multi-page run.

    `currency` is an override, and it exists because of where this site
    states one. A listing page declares `priceCurrency: "JPY"` in its own
    JSON-LD — and Rakuten publishes that block on **page 1 only**: measured
    1 block on page 1 and 0 on pages 2 and 3 of the same query. So a run
    that read each page independently came back with a currency on the first
    45 rows and a null on the other 90, which reads as "unknown" for rows
    whose currency the site had already stated.

    The engine therefore reads it once from page 1 and passes it forward.
    That keeps §4's rule intact rather than bending it: the value is still
    something the site said, on this run, about this listing — not a
    constant compiled in. A run that never got page 1 passes nothing and the
    rows carry null, which is the honest answer for a run with no statement
    to carry.

    Two family columns come back null on every row of this path, and both
    are measured rather than unimplemented:

      * `original_price` / `discount_pct` — the listing payload publishes no
        was-price at all. Zero occurrences of `doublePrice`, `listPrice`,
        `referencePrice` or `strike` across 405 rows. The nearest thing it
        has is `sale`, on 32 of 405, which is a campaign WINDOW
        (`{status: "ongoing", start, end}`) with no prices in it — read as a
        discount it would invent one. A verified was-price does exist, on the
        DETAIL page, and `parse_product_page` reads it.
      * `in_stock` is True on all 405, because this search does not return
        sold-out items. Deep pages were checked specifically for stale rows
        (pages 80, 120, 149 and 150 of a 142,000-hit genre): 0 sold out. So
        a False here is unproven — §20 — and the detail route's own
        `availability` microdata is the source that can say otherwise.
    """
    text = _text(html)
    payload = search_payload(text)
    hits = payload.get("items")
    currency = currency or currency_from_page(text)
    kind = listing_kind(url)
    rows: List[Product] = []
    if isinstance(hits, list) and hits:
        skipped = 0
        for hit in hits:
            if not isinstance(hit, dict):
                skipped += 1
                continue
            if is_sponsored(hit):
                skipped += 1
                continue
            # `position` counts the ROWS THIS FUNCTION EMITS, not the
            # payload's own slots, and that distinction is a bug this had
            # first. Numbering by payload index made `position` depend on how
            # many sponsored entries the site happened to inject: two engines
            # fetching the same genre listing seconds apart got 52 and 45
            # entries, so the same 45 products came out numbered 8-52 in one
            # run and 1-45 in the other. Identical rows, every position
            # different — which breaks both the family rule that all three
            # engines produce identical rows and any consumer comparing two
            # runs.
            row = _row_from_hit(hit, page, position_offset + len(rows) + 1,
                                url, category, currency)
            if row is None:
                skipped += 1
                continue
            rows.append(row)
        if skipped:
            # Said out loud rather than dropped silently: a consumer counting
            # 45 rows against the site's own 52 entries deserves to know
            # which 7 went and why (§8).
            logger.info(
                "Skipped %d of %d payload entries on page %d: sponsored "
                "placements and entries with no item code. Rakuten injects "
                "paid slots into its own result list and they carry a click-"
                "tracking redirect instead of a product URL, so there is "
                "nothing to key a row on.",
                skipped, len(hits), page)
        if rows:
            return rows
        logger.warning(
            "ichibaSearch held %d hits and none parsed into a row - this is "
            "a parser failure on a page the site served, not an empty "
            "listing", len(hits))
    if kind not in PAGINATED_KINDS + ("category",):
        # The DOM fallback is for LISTING routes, and gating it is not
        # tidiness — it is the §20 failure, caught by running this function
        # against a detail fixture. A detail page links to plenty of other
        # products (breadcrumb rails, "similar items", the shop's other
        # lines), so the fallback happily returned two well-formed phantom
        # rows for a page that holds exactly one product and belongs to the
        # other parser. The suite pins that this returns zero.
        logger.debug("no listing payload on a %s route (%s) - the listing "
                     "parser has nothing to read here", kind, url)
        return []
    return _dom_only_rows(text, url, page, position_offset, currency, category)


# ---------------------------------------------------------------------------
# The DOM fallback
# ---------------------------------------------------------------------------
# Runs only when the payload yields nothing. It is deliberately thin: every
# column worth having is in `ichibaSearch`, and a fallback that reconstructed
# all of them from build-hashed class names would rot faster than it helped.
# What it does buy is the difference between "the site changed its payload
# key" and "there are no products", which is a distinction a reader needs
# (§20).
_TILE_MAX_LEVELS = 8


def _tile_of(anchor: Any) -> Any:
    """The outermost ancestor still covering exactly ONE product (§4).

    Counted by DISTINCT item URL, not by link count: a Rakuten tile links to
    its own product several times (image, title, shop), so a walk that
    stopped at "more than one product link" would never leave the anchor,
    and one that ignored the count would walk into the next tile and report
    its neighbour's price.
    """
    node = anchor
    best = anchor
    for _ in range(_TILE_MAX_LEVELS):
        parent = getattr(node, "parent", None)
        if parent is None or getattr(parent, "name", None) in (None, "body",
                                                               "html",
                                                               "[document]"):
            break
        skus = set()
        for link in parent.select(SELECTORS["item_link"]):
            sku = sku_from_url(_absolutise(link.get("href") or "", ""))
            if sku:
                skus.add(sku)
        if len(skus) > 1:
            break
        best = parent
        node = parent
    return best


def _absolutise(href: str, url: str) -> str:
    if not href:
        return ""
    if href.startswith("//"):
        return "https:" + href
    if href.startswith("http"):
        return href
    return urljoin(url or "https://%s/" % SEARCH_HOST, href)


def _dom_only_rows(text: str, url: str, page: int, position_offset: int,
                   currency: Optional[str],
                   category: Optional[str]) -> List[Product]:
    if not text:
        return []
    soup = BeautifulSoup(text, "html.parser")
    seen: Dict[str, Product] = {}
    for anchor in soup.select(SELECTORS["item_link"]):
        href = _absolutise(anchor.get("href") or "", url)
        sku = sku_from_url(href)
        if not sku or sku in seen:
            continue
        tile = _tile_of(anchor)
        price = price_in(tile.get_text(" ", strip=True)) if tile else None
        title = _clean(anchor.get("title") or anchor.get_text(" ", strip=True))
        seen[sku] = Product(
            url=strip_tracking(href),
            sku=sku,
            title=title or None,
            price=price,
            currency=currency if price is not None else None,
            category=category,
            price_source="dom",
            page=page,
            position=position_offset + len(seen) + 1,
            shop_code=sku.split(":")[0],
        )
    if seen:
        logger.warning(
            "read %d rows from the DOM because ichibaSearch was not in the "
            "page - every payload-only column is null on these rows",
            len(seen))
    return list(seen.values())


# ---------------------------------------------------------------------------
# Detail pages
# ---------------------------------------------------------------------------
def _variants(island: dict) -> List[dict]:
    out: List[dict] = []
    by_id = {}
    for sku in island.get("sku") or []:
        if isinstance(sku, dict) and sku.get("variantId"):
            by_id[sku["variantId"]] = sku
    for variant_id, sku in by_id.items():
        labels = [_clean(v) for v in sku.get("selectorValues") or []]
        out.append({
            "variant_id": variant_id,
            "label": " / ".join(l for l in labels if l) or None,
            "price": _number(sku.get("taxIncludedPrice")),
            "subscription_price": _number(sku.get("taxIncludedSubscriptionPrice")),
            "hidden": bool(sku.get("hidden")),
        })
    return out


def _reference_price(island: dict) -> Tuple[Optional[float], Optional[str]]:
    """A was-price, only where Rakuten says it may be shown.

    Japan's double-pricing rules require a merchant's reference price to be
    substantiated, and Rakuten carries the outcome as its own flag:

        purchaseInfo.sku[].doublePrice = {
            "displayType": "REFERENCE_PRICE",
            "displayText": "当店通常価格",
            "value": "10398",
            "taxIncludedVerifiedReferencePrice": 10398.0,
            "referencePriceVerified": true }

    Read the value only when that flag is true. Measured on 8 item pages:
    verified on 2 of them (¥10,398 against ¥5,199, and ¥3,476 against
    ¥1,738 — real 50% reductions), and `{"referencePriceVerified": false}`
    with no value at all on the rest. Where a site publishes a "should I
    show this" flag, use it rather than inferring from the value (§21).

    `displayText` is kept as `original_price_label` rather than dropped,
    because WHICH reference price it is matters and a sibling repo learned
    that the expensive way: one kind meant a manufacturer's RRP above the
    price and another meant a 30-day low below it. One kind has been
    observed here (`REFERENCE_PRICE` / 当店通常価格, "this shop's usual
    price"); recording the label means a second kind shows up in the data
    instead of silently becoming a negative discount.
    """
    for sku in ((island.get("purchaseInfo") or {}).get("sku") or []):
        if not isinstance(sku, dict):
            continue
        double = sku.get("doublePrice") or {}
        if not double.get("referencePriceVerified"):
            continue
        value = _number(double.get("taxIncludedVerifiedReferencePrice"))
        if value is None:
            value = _number(double.get("value"))
        if value is not None:
            return value, _clean(double.get("displayText")) or None
    return None, None


_AVAILABLE = ("http://schema.org/InStock", "https://schema.org/InStock",
              "InStock")


def _availability(text: str) -> Optional[bool]:
    """In stock, from the detail page's microdata, as an ALLOWLIST.

    `availability` is matched against the values that mean available rather
    than tested for `!= OutOfStock`, so a value nobody anticipated reads as
    "not available" instead of as a sale (§20).
    """
    m = re.search(r'<meta[^>]+itemprop="availability"[^>]+content="([^"]+)"',
                  text)
    if not m:
        return None
    return m.group(1).strip() in _AVAILABLE


def _microdata(text: str, prop: str) -> Optional[str]:
    m = re.search(
        r'<meta[^>]+itemprop="%s"[^>]+content="([^"]*)"' % re.escape(prop),
        text)
    return m.group(1) if m else None


def parse_product_page(html: Any, url: str,
                       page: int = 1, position: int = 1,
                       category: Optional[str] = None) -> List[Product]:
    """One row for one detail page.

    ONE row per ITEM and not per variant, which is a deliberate divergence
    from a sibling repo that emits a row per size — and the reason is the
    family's output contract rather than taste. A per-variant row needs a
    per-variant id in `sku`, and that id cannot equal the listing row's, so
    the two modes' files would stop being comparable and `diff_runs.py`
    would have nothing to join on. Here `sku` is the same
    `{shop}:{manageNumber}` in both modes, verified on 8 of 8 item pages,
    and the variants ride in `variants` with `variant_count`, `price` (the
    lowest) and `price_max` (the highest) beside them.
    """
    text = _text(html)
    island = item_island(text)
    if island is None:
        logger.warning("no item-page-app-data on %s - the detail parser has "
                       "nothing to read", url)
        return []
    sku = sku_from_url(url) or "%s:%s" % (
        (island.get("_shop") or {}).get("shopUrl"),
        island.get("manageNumber"))
    variants = _variants(island)
    prices = [v["price"] for v in variants if v["price"] is not None]
    purchase = ((island.get("purchaseInfo") or {})
                .get("purchaseBySellType") or {}).get("normalPurchase") or {}
    stated = purchase.get("price") or {}
    price = _number(stated.get("minPrice"))
    price_max = _number(stated.get("maxPrice"))
    if price is None and prices:
        price = min(prices)
    if price_max is None and prices:
        price_max = max(prices) if max(prices) > (price or 0) else None
    if price is None:
        price = _number(_microdata(text, "price"))
    original, label = _reference_price(island)
    rating, review_count = _review(
        (island.get("itemReviewInfo") or {}).get("summary"))
    shop = island.get("_shop") or {}
    rat = island.get("_rat") or {}
    subs = [v["subscription_price"] for v in variants
            if v["subscription_price"] is not None]
    crumbs = [(_clean(c.get("name")))
              for c in ((island.get("breadcrumbs") or {})
                        .get("genreBreadcrumbs") or [])
              if isinstance(c, dict)]
    return [Product(
        url=strip_tracking(url),
        sku=sku,
        title=_clean(island.get("title")) or None,
        brand=None,
        price=price,
        currency=currency_from_page(text) if price is not None else None,
        original_price=original,
        discount_pct=_discount(price, original),
        rating=rating,
        review_count=review_count,
        in_stock=_availability(text),
        image_url=(island.get("oldImage") or None),
        category=" > ".join(c for c in crumbs if c) or category,
        price_source="itemdata",
        page=page,
        position=position,
        variant_id=None,
        item_number=_clean(island.get("itemId")) or None,
        price_max=price_max,
        has_price_range=bool(price_max and price and price_max > price),
        subscription_price=min(subs) if subs else None,
        points=_int(rat.get("ratPoint")),
        point_rate=None,
        shipping_fee=None,
        free_shipping=None,
        delivery_estimate=None,
        shop_name=_clean(shop.get("shopName")) or None,
        shop_code=_clean(shop.get("shopUrl")) or None,
        shop_id=_int(island.get("shopId")),
        shop_url=("https://%s/%s/" % (WWW_HOST, shop.get("shopUrl"))
                  if shop.get("shopUrl") else None),
        genre_id=_int(island.get("rCategoryId")),
        genre_path=_clean(rat.get("ratItemGenrePath")) or None,
        genre_rank=None,
        is_super_deal=bool(island.get("superDeal")),
        is_39shop=bool(island.get("is39Shop")),
        is_official_shop=None,
        variant_count=len(variants) or None,
        original_price_label=label,
        tags=None,
        variants=variants or None,
    )]


def shop_metadata(html: Any) -> dict:
    """What a detail page says about the merchant selling the item."""
    island = item_island(html)
    if island is None:
        return {}
    shop = island.get("_shop") or {}
    return {k: v for k, v in {
        "shop_name": _clean(shop.get("shopName")) or None,
        "shop_code": shop.get("shopUrl"),
        "shop_id": _int(island.get("shopId")),
        "tax_rate": _number(shop.get("taxRate")),
    }.items() if v is not None}
