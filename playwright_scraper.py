"""rakuten-scraper — Playwright edition (primary engine)

Scrapes Rakuten Ichiba (rakuten.co.jp), Japan's largest online marketplace.

    --mode listing   a keyword search or a genre listing —
                     search.rakuten.co.jp/search/mall/{keyword}/ or
                     .../search/mall/-/{genreId}/ or
                     www.rakuten.co.jp/category/{genreId}/
    --mode product   one product page — item.rakuten.co.jp/{shop}/{code}/,
                     which adds per-variant prices and a verified was-price

What this engine has to know about this site, all of it measured on
2026-09-21 from a Hetzner datacentre exit in Helsinki:

* **No paid product is needed for the routes this reads, and the reason is
  not the address.** Akamai fronts Rakuten and what it refuses here is a
  CONTRADICTION between the client's claimed User-Agent and its TLS and
  HTTP/2 fingerprint — not a datacentre IP. From one address, unchanged:

      plain HTTP with curl's own User-Agent   HTTP 200,  92 KB, 45 products
      plain HTTP with a Chrome User-Agent     HTTP 200,  43 bytes, refused
      headless Chromium (real UA + real TLS)  HTTP 200, 855 KB, 45 products

  So a consistent client is served, a costume is not, and 3 of 3 repeats
  agreed. The practical consequence is in `page_flow.block_advice`: if this
  run is being refused, look for a fingerprint you added before you buy an
  exit. Do not set a UA or `--fingerprint` over `--cdp-endpoint` — the
  remote browser brings its own, and stacking a second is how you
  manufacture exactly the mismatch Akamai is looking for.

* **The rows come from the site's own SSR payload, and its JSON-LD is a
  TRAP.** A search page has exactly one `application/ld+json` block, it is
  an `ItemList`, and it holds TEN items whose every url carries
  `?scid=seo-carousel-search` — the SEO recommendation carousel — while the
  page itself holds 45 products in
  `window.__INITIAL_STATE__.state.data.ichibaSearch.items`. A JSON-LD-primary
  parser returns ten rows of the wrong products and reports success. The
  block is read for one thing only: it is the only place a listing page
  states a currency.

* **A detail page publishes a different structure again** — an
  `item-page-app-data` JSON island, and a JSON-LD `BreadcrumbList` as its
  only ld+json. The listing parser finds nothing on it, which the suite pins
  as an assertion rather than leaving to be discovered.

* **Detail pages are EUC-JP.** `Content-Type: text/html;charset=EUC-JP` on
  8 of 8, while every listing page is UTF-8. Chromium decodes it, so this
  engine never sees it; `product_parser.decode_page` is there for the HTTP
  paths, which do.

* **There is no scroll, and that is measured.** The same URL fetched by
  plain HTTP with no JavaScript at all and by headless Chromium parsed to 45
  rows each, with identical skus and prices. A sibling repo cannot see one
  product without its scroll loop, which is exactly why this was checked
  here rather than ported.

* **Never wait on `networkidle`.** Rakuten's ad and RAT beacons keep firing
  indefinitely — a probe counted 46 fetches and 13 pings still going after
  load — so a `networkidle` wait runs to its full timeout on a page that
  was complete in two seconds. This engine waits for `domcontentloaded`.

* **Pagination is `?p=N`, and an out-of-range page LIES.** 45 rows a page,
  and the site caps every query at `subset` = 6,750 results = 150 pages
  however many it matched (one measured query: `numFound` 3,053,682). Page
  151 answers 301 to page 1 and then HTTP 200 with 45 real products; and
  `www.rakuten.co.jp/category/{id}/?p=2` answers 200 with page 1 without
  even redirecting. Both look like success. What settles it is that the
  payload states the offset the server USED — see
  `page_flow.served_the_page_asked_for`.

* **HTTP 503 is a throttle, not a block.** Rakuten answers a client going
  too fast with its own branded "アクセスが集中しております" page under 503 —
  and serves the IDENTICAL page under 403 on the routes it refuses
  outright. So the status is the only thing separating "wait" from "rotate",
  and a 503 costs a wait at the same exit rather than the block budget.

* **No captcha is configured anywhere on this site.** Zero vendor markers
  across nine captures, served and refused alike; Akamai's deny page is 43
  bytes with no widget on it. The solver is still wired up and bounded,
  because a bot manager can be switched on between deploys and a scraper
  that cannot name what stopped it is much harder to fix.

Examples
--------
    python3 playwright_scraper.py \\
        --url "https://search.rakuten.co.jp/search/mall/コーヒー/" --pages 3

    # A genre listing, by Rakuten's own genre id, to CSV.
    python3 playwright_scraper.py \\
        --url "https://www.rakuten.co.jp/category/100356/" --pages 2 --format csv

    # One product, with its per-variant prices and its verified was-price.
    python3 playwright_scraper.py --mode product \\
        --url "https://item.rakuten.co.jp/sawaicoffee-tea/solandluna/"

The site does not geo-redirect: `?lang=` picks the UI language and the price
is JPY for every visitor. `--locale en` translates the chrome and NOT the
data — product and shop names are merchant-authored Japanese and come back
byte-identical either way.
"""

import argparse
import logging
import queue
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import List, Optional
from urllib.parse import urlparse, urljoin, parse_qsl

from playwright.sync_api import (sync_playwright, Error as PWError,
                                 TimeoutError as PWTimeout)

from captcha_solver import (detect_recaptcha_v3, detect_recaptcha_in_page,
                            reconcile_detections, solve_recaptcha,
                            CaptchaUnsolvable, INJECT_TOKEN_JS,
                            RECAPTCHA_DISCOVERY_JS)
from product_parser import (parse_products, parse_product_page,
                            currency_from_page,
                            pages_beyond_cap as parser_pages_beyond_cap,
                            PAGE_CAP as parser_page_cap,
                            SELECTORS, LOCALES,
                            detect_bot_challenge, detect_block_marker,
                            page_url, paginates_by_url, listing_kind,
                            capped_by_site, reachable_max,
                            site_host, is_supported_host, total_results,
                            total_pages, search_header, unsupported_reason,
                            shop_metadata, CURRENCY, served_by_rakuten)
from output_writer import (dedupe_by_key, finish_run, EXIT_API_ERROR,
                           SOURCE_DEFAULT)
import page_flow
from page_flow import MIN_CARD_MATCHES
from proxy_pool import (from_args as proxy_pool_from_args, to_playwright, mask,
                        ROTATE_MODES, ProxyError, ProxyPool)
import env_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("playwright_scraper")


def _chrome_ua(chromium_version: str) -> str:
    """Build a desktop-Chrome UA naming the browser's OWN real version.

    Not a hardcoded version number: that drifts the moment a newer Chromium
    ships, and a UA claiming an older Chrome than what the JS engine, WebGL
    strings and TLS ClientHello all actually report is itself a mismatch a
    fingerprinter can key on.
    """
    return (f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{chromium_version} Safari/537.36")


@dataclass
class PageOutcome:
    """What one page produced.

    Collected per page and merged afterwards rather than folded into shared
    state as the loop goes. Two reasons, and the second is the point:
    dedupe that mutates a running set inside the loop makes the OUTPUT depend
    on the order pages happen to arrive in — fine while that order is fixed,
    wrong the moment pages are fetched concurrently, because which page
    "claims" a duplicate sku (and so which `scraped_at` the row carries)
    would vary between runs of the same command. Merging afterwards in page
    order is deterministic regardless of arrival order.
    """
    page_num: int
    url: str
    final_url: Optional[str] = None
    products: List = field(default_factory=list)
    blocked_by: Optional[str] = None
    load_failed: bool = False
    # The page_flow state this page came back as ("content", "empty",
    # "shell", "throttled", "challenge", "blocked"). Carried so the caller
    # can tell an EMPTY page — a query that matched nothing — from a page
    # that failed. Both produce zero rows and they mean opposite things.
    state: Optional[str] = None
    # What the query itself says it matched — `pagination.numFound`. A
    # LIVING number: three fetches of one query inside a minute gave
    # 3,053,682, 3,053,713 and 3,053,712. Recorded as the site's answer at
    # this moment, never asserted against.
    total_available: Optional[int] = None
    # The page's own declared language (`state.metadata.lang`), verbatim,
    # for the sidecar. `?lang=en` changes the payload's `locale` to "en"
    # while `lang` stays "ja" — so this is the honest record of what the
    # site actually served, and it is why the README says the locale flag
    # translates the chrome and not the data.
    header: Optional[str] = None
    # Rakuten's own arithmetic about the query: total_results,
    # pages_available, reachable_max, capped_by_site, pages_beyond_cap. In
    # the sidecar because on this site "complete" and "exhaustive" are wildly
    # different words and a status alone would be lying by omission (§21).
    cap: Optional[dict] = None
    # Whether the SERVER served the page we asked for, read off
    # `pagination.start` rather than trusted from the request. False means
    # the end of the listing: page 151 of a 150-page query answers 301 to
    # page 1 and then HTTP 200 with 45 real products, and a `/category/` URL
    # answers page 1 for any `?p=` at all. Both look like success.
    served_requested_page: bool = True
    # In --mode product, the merchant's own facts read off the item page.
    # Stored as the small dict rather than by keeping the page's HTML around:
    # a detail page is 244 KB in the browser.
    shop_facts: Optional[dict] = None

    @property
    def ok(self) -> bool:
        return not self.load_failed and self.blocked_by is None


ITEM_LINK_SELECTOR = page_flow.READY_SELECTOR_LISTING

# A price-coverage floor. One number, not a per-section map, because on
# this site there is one answer: Rakuten is a shop and every listing names a
# price. Measured across 405 rows on 11 listing pages — keyword searches and
# genre listings, pages 1 through 150 — `price` was non-null on 405 of 405.
#
# So the floor is high on purpose. A run that comes back with 80% priced has
# not met an unusual page; it has a broken read, and the warning should say
# so rather than shrug.
#
# `product` mode is not in the map: one page is one row, and a share of one
# row is not a measurement.
PRICE_FLOOR = {"listing": 95}

# A page holding less than this share of the fullest page in the same run is
# reported as thin. This site's page size is steady — 45 products on 11 of
# 11 captures, and the LAST page of a capped query is 45 too (page 150 came
# back `start: 6705`, exactly 6750 - 45) — so the bar can sit closer than it
# does on some sibling repos. Not tight, though: the last page of a query
# with fewer than 6,750 total hits is legitimately short.
THIN_PAGE_SHARE = 0.6


# ---------------------------------------------------------------------------
# page_flow, bound to Playwright
# ---------------------------------------------------------------------------
# Every decision about WHAT to do with a page — how long to wait, when to
# scroll, when a fresh session is the only fix — lives in page_flow.py so all
# three engines make it identically. What lives here is only HOW to ask this
# particular driver. See page_flow's docstring for why that split exists.
def _driver(page):
    # The scroll primitives are NAMED OPERATIONS rather than JavaScript, and
    # that is the point of the split. Selenium's execute_script takes a
    # function BODY with an explicit `return` while Playwright and pyppeteer
    # take `() => expr`, so a shared module handing JS across this boundary
    # would quietly acquire one driver's dialect.
    return {
        "count": lambda selector: len(page.query_selector_all(selector)),
        "sleep": page.wait_for_timeout,
        "content": lambda: _content_when_settled(page),
        "current_url": lambda: page.url,
    }


# No scroll primitives and no `page_height` here, and that is measured
# rather than omitted: the same URL fetched by plain HTTP with no JavaScript
# at all and by headless Chromium parsed to 45 rows each, with identical skus
# and prices. Every listing page holds its whole page of products in the
# first response and paginates by URL. A sibling repo cannot see a single
# product without the scroll, which is exactly why this was checked here
# instead of ported (see page_flow's docstring).


def _ready_selector(args) -> str:
    return page_flow.ready_selector(args.mode)


def _min_matches(args, html: str = "") -> int:
    """The readiness threshold, lowered to what THIS page actually holds.

    Passing the payload's own ad count is what keeps a short last page from
    spending the whole timeout and then reporting itself unpainted: a jobs
    category of 148 ads has a last page of 23, and one of 3 could never reach
    the default 8. `page_flow.expected_cards` reads it out of the first
    response, which is available before anything has hydrated.
    """
    return page_flow.min_matches(args.mode, page_flow.expected_cards(html))


def _classify(page, html: str, status=None) -> str:
    return page_flow.classify(html, status=status, url=page.url)

# Every readiness constant, every pagination selector and every state policy
# lives in page_flow.py, with its measurement beside it. Nothing about WHAT
# to do with a page is duplicated here — this file only knows HOW to ask
# Playwright.


def _advertised_next_hrefs(page, page_num: int) -> List[str]:
    """Every href on the page that could be the link to page `page_num` + 1.

    ALL of them, not the first, and the full set is handed to
    `page_flow.next_page_candidates` to filter — so the filtering rule lives
    in one place for all three engines.

    Rakuten DOES advertise its own page links — nine of them on page 1 of a
    150-page query, absolute, spelling `?p=2` through `?p=9` — and it
    publishes **no `<link rel="next">` at all**, on 3 of 3 captures. Only
    `rel="canonical"`. So §7's most-durable layer simply does not exist on
    this site and the selector reaches for a build artefact because there is
    nothing better to reach for; you cannot order signals by durability when
    the site declines to publish the durable one.

    Worth resolving anyway, and worth filtering: a genre landing page's own
    next link points at a DIFFERENT HOST
    (`www.rakuten.co.jp/category/100356/` advertises
    `search.rakuten.co.jp/search/mall/-/100356/?p=2`), which is exactly what
    `page_url` builds for it — so a comparison that insisted on the same
    host would reject the site's own link and cost the run its
    `--concurrency` while looking like a safety decision.
    """
    selector = page_flow.next_page_selector(page_num)
    return [el.get_attribute("href") for el in page.query_selector_all(selector)]


def _plan_page_urls(page, args, page_one_url: str) -> Optional[List[str]]:
    """URLs for pages 2..N, decided once from page 1, or None to chain.

    Following the site's own next-link one page at a time is correct but
    strictly sequential: the address of page 5 is not knowable until page 4
    has been fetched. Constructing the page parameter up front removes that
    chain — which is what makes fetching pages independently (and later,
    concurrently) possible at all.

    It is only safe when the site's own link AGREES with the convention, so
    that is checked rather than assumed: if page 1's next-link is not what
    `page_url()` would build for page 2, pagination is carrying something the
    convention cannot reproduce (a cursor, a token, a filter id) and the
    caller must keep chaining link to link. Returns None in that case.

    On Rakuten all three listing routes are addressable and the site
    agrees with the convention: page 1 of a keyword search advertises
    `?p=2` on its own host and a genre landing page advertises
    `search.rakuten.co.jp/search/mall/-/{genreId}/?p=2`, which is what
    `page_url()` builds for each. The fetched page 2 comes back
    `pagination.start: 45`, confirming it.

    The site also states its own arithmetic — `numFound`, `pageSize` and
    `subset` — so the page count is computed rather than walked. What it
    does NOT do is fail past the end: page 151 of a 150-page query
    redirects to page 1 and serves it with HTTP 200, and a `/category/`
    URL serves page 1 for any `?p=` at all. Neither is visible from the
    request, so every fetched page is checked against
    `page_flow.served_the_page_asked_for` before its rows are kept.
    """
    if args.pages < 2:
        return None

    hrefs = _advertised_next_hrefs(page, 1)
    candidates = page_flow.next_page_candidates(page_one_url, hrefs)
    dropped = len([h for h in hrefs if h]) - len(candidates)
    if dropped > 0:
        logger.info("Ignored %d advertised page-2 link(s) that paginate "
                    "something other than this listing (the SEO chip "
                    "review pages are the known case).", dropped)

    constructed = page_url(page_one_url, 2)
    if candidates:
        if page_flow.pagination_agrees(page_one_url, 1, hrefs):
            logger.info("Pagination follows the ?p=N convention (a page 2 "
                        "link matches the constructed URL) — planning pages "
                        "2-%d up front.", args.pages)
        else:
            logger.info("The site's own next-page link (%s) is not what the "
                        "page convention would build (%s) — following its "
                        "links one page at a time instead. Pages cannot be "
                        "fetched independently for this listing, so "
                        "--concurrency will not help here.",
                        candidates[0], constructed)
            return None
    else:
        logger.info(
            "No pagination link for page 2 was found on page 1 — using the "
            "URL convention. That is not alarming on this site: Rakuten "
            "publishes no rel=next anywhere (measured: 0 matches on 3 of 3 "
            "captures, only rel=canonical), so its numbered `?p=` links are "
            "all there is. The convention is backed by the payload's own "
            "arithmetic — numFound, pageSize and subset — and every fetched "
            "page is checked against the offset the server says it served, "
            "so a page the site quietly re-serves as page 1 ends the listing "
            "instead of being collected twice.")

    return [page_url(page_one_url, n) for n in range(2, args.pages + 1)]


def _next_url_from_page(page, args, page_num: int) -> str:
    """Next page's URL from the site's own link, falling back to ?p=N.

    Only used when pagination could not be planned up front. A missing link
    must not end the run: pagination resting entirely on DOM selectors is a
    silent-success failure waiting to happen, so the convention backs it up
    and the DATA decides when to stop.

    The candidate filter is not optional here either — chaining onto a shop
    front's review pagination would return rows from the wrong listing while
    reporting success.
    """
    candidates = page_flow.next_page_candidates(
        page.url, _advertised_next_hrefs(page, page_num))
    if candidates:
        return candidates[0]
    return page_url(page.url, page_num + 1)


def _same_url(a: str, b: str) -> bool:
    """Whether two URLs address the same page.

    Delegates to page_flow rather than reimplementing the comparison, so all
    three engines cannot drift on it. An engine that carried its own copy of
    this in a sibling repo went stale and silently fell back to sequential
    fetching — the exact divergence page_flow.py exists to prevent,
    reproduced inside one engine.

    On this site the comparison has to strip a long tracking tail: a listing
    anchor arrives with `?extParam=…keyword=kopi&search_id=…&src=search` and
    a detail page's own canonical arrives with a UTM triple, so two views of
    one page never match unless both sides are cleaned.
    """
    return page_flow.comparable(a) == page_flow.comparable(b)


# Chromium's own names for "the proxy is the problem, not the site". Matched
# on the error text because Playwright surfaces them as a generic Error.
_PROXY_ERROR_MARKERS = (
    "ERR_PROXY_CONNECTION_FAILED",     # nothing listening / refused
    "ERR_TUNNEL_CONNECTION_FAILED",    # CONNECT rejected by the proxy
    "ERR_PROXY_AUTH_UNSUPPORTED",      # auth scheme we cannot satisfy
    "ERR_PROXY_AUTH_REQUESTED",        # credentials missing or wrong
    "ERR_UNEXPECTED_PROXY_AUTH",
    "ERR_PROXY_CERTIFICATE_INVALID",
)


def _proxy_failure(exc) -> str:
    """The Chromium proxy-error name in `exc`, or "" if it is not one.

    Distinguishing this from an ordinary timeout matters because the two want
    opposite responses: a timeout deserves a retry from the same exit, while
    an unusable exit deserves a different exit — retrying it unchanged just
    spends the retry budget on a proxy that is not going to answer.
    """
    text = str(exc)
    for marker in _PROXY_ERROR_MARKERS:
        if marker in text:
            return marker
    return ""


def _launch_local(pw, args, pool):
    """Launch our own Chromium on `pool`'s current exit; return (browser, context, page).

    Factored out of scrape() so a proxy rotation can tear the whole browser
    down and call this again. Swapping the proxy under a live session would
    be cheaper and wrong: cookies a bot manager issued against one exit,
    replayed from another, are a stronger signal than either address alone.
    A rotation therefore means a genuinely fresh browser — new cookie jar,
    new storage — which is what an ordinary user on a different network
    looks like.
    """
    launch_kwargs = {"headless": args.headless}
    proxy = to_playwright(pool.current) if pool else None
    if proxy:
        launch_kwargs["proxy"] = proxy
        logger.info("Using proxy exit %s", mask(pool.current))

    browser = pw.chromium.launch(**launch_kwargs)
    # Only override the UA when we launched our own bundled Chromium.
    # Forcing a UA on a page reached via --cdp-endpoint mismatches the remote
    # browser's real TLS/JS fingerprint on purpose-matched values.
    ctx_kwargs = {"user_agent": _chrome_ua(browser.version), "locale": args.locale}
    init_script = None
    if args.fingerprint:
        # Only meaningful on this branch. Over --cdp-endpoint the Scraping
        # Browser already has its own fingerprint, and layering a second one
        # on top produces a mismatch rather than better cover.
        from fingerprint_client import (get_fingerprint,
                                        playwright_context_kwargs,
                                        playwright_init_script)
        fp = get_fingerprint(args.twocaptcha_key,
                             tags=args.fp_tags, country=args.fp_country)
        ctx_kwargs.update(playwright_context_kwargs(fp))
        init_script = playwright_init_script(fp)
        logger.info("Using 2captcha fingerprint %s (%s)", fp.get("id"), fp.get("country"))

    context = browser.new_context(**ctx_kwargs)
    if init_script:
        # Must be installed on the context, before any page script runs.
        context.add_init_script(init_script)
    return browser, context, context.new_page()


class _BrowserSession:
    """One browser + context + page, relaunchable onto a different exit.

    Exists because a rotation replaces all three handles at once, and passing
    three mutable locals through every helper is how one of them ends up
    stale. It also gives a worker thread a single object to own: with
    Playwright's sync API, a browser and everything reachable from it belong
    to the thread that created them, so each worker builds its own.
    """

    def __init__(self, pw, args, pool, remote: bool = False):
        self.pw, self.args, self.pool, self.remote = pw, args, pool, remote
        self.browser = self.context = self.page = None

    def open(self):
        if self.remote:
            self.browser, self.context, self.page = _connect_remote(self.pw, self.args)
        else:
            self.browser, self.context, self.page = _launch_local(
                self.pw, self.args, self.pool)
        return self

    def relaunch(self):
        """Tear the browser down and come back on the pool's current exit.

        On a remote browser this is a no-op — its exit is not ours to change.
        """
        if self.remote:
            return
        try:
            self.browser.close()
        except Exception as e:  # noqa: BLE001 — teardown must not mask the reason we're here
            logger.debug("Ignoring error while closing browser for rotation: %s", e)
        self.open()

    def close(self):
        try:
            if self.remote:
                self.page.close()  # leave the remote browser app running
            else:
                self.browser.close()
        except Exception as e:  # noqa: BLE001
            logger.debug("Ignoring error during browser teardown: %s", e)


def _connect_remote(pw, args):
    """Attach to an already-running browser over CDP; return (browser, context, page)."""
    logger.info("Connecting to existing browser over CDP: %s",
                _mask_credentials(args.cdp_endpoint))
    # Explicit timeout. Playwright defaults to 30s here, but stating it makes
    # the contract visible next to the pyppeteer twin, which has no connect
    # timeout at all. A Scraping Browser session that is still held answers
    # with HTTP 500 rather than stalling, so this mostly guards against the
    # endpoint going quiet.
    try:
        browser = pw.chromium.connect_over_cdp(args.cdp_endpoint, timeout=30000)
    except (PWError, PWTimeout) as e:
        # Playwright puts the endpoint it tried into the exception text, and
        # the endpoint is a URL with the password in it. Unmasked, that
        # password lands in the terminal, in CI output and in any log the run
        # is piped to — which is the one thing this project promises does not
        # happen ("credentials never reach argv or logs"). The message is
        # rewritten with the credentials masked and the host and port kept,
        # because WHICH endpoint failed is the useful half and is not the
        # secret.
        raise PWError(
            f"could not connect to --cdp-endpoint "
            f"{_mask_credentials(args.cdp_endpoint)}: "
            f"{_mask_credentials(str(e))}\n"
            f"A Scraping Browser profile allows ONE live connection at a "
            f"time, so a 500 here usually means another run still holds this "
            f"`pid`. Wait for it to finish, or use a different pid."
        ) from None
    # Reuse the remote browser's existing context so its
    # fingerprint/session/proxy settings stay intact.
    context = browser.contexts[0] if browser.contexts else browser.new_context()
    page = context.new_page()

    # The Scraping Browser API exposes a documented CDP domain
    # (`Captcha.setAutoSolve` / `Captcha.solve`) that clears supported
    # challenges inside the browser: https://2captcha.com/scraper/browser-api/api
    # Tried first when --cdp-endpoint is set; this script's own detect+solve
    # logic still runs as a fallback if the endpoint does not support it.
    # Note it does NOT cover this site's refusal, which is not a challenge:
    # Akamai answers a headless browser with a 394-byte "Access Denied" that
    # has nothing to solve on it, and a real window rather than a better
    # address is the answer. No challenge has ever been observed here; this is
    # wired
    # up because one can appear between deploys.
    try:
        cdp_session = context.new_cdp_session(page)
        cdp_session.send("Captcha.setAutoSolve", {"autoSolve": True, "options": [{"type": "*"}]})
        cdp_session.on("Captcha.detected", lambda *_: logger.info("[Scraping Browser] CAPTCHA detected on page."))
        cdp_session.on("Captcha.waitForSolve", lambda *_: logger.info("[Scraping Browser] CAPTCHA sent to 2captcha for solving."))
        cdp_session.on("Captcha.solveFinished", lambda *_: logger.info("[Scraping Browser] CAPTCHA solved automatically."))
        cdp_session.on("Captcha.solveFailed", lambda *_: logger.warning("[Scraping Browser] CAPTCHA auto-solve failed."))
        logger.info("Scraping Browser API Captcha.setAutoSolve enabled — supported "
                    "challenge types will be solved automatically if this "
                    "--cdp-endpoint is a Scraping Browser API session.")
    except Exception as e:
        logger.info("Captcha.setAutoSolve not available on this --cdp-endpoint (%s) — "
                    "relying on this script's own detect+solve logic instead.", e)
    return browser, context, page


def _resolve_pagination_url(base_url: str, href: str) -> str:
    """Resolve a pagination link's raw href against the page it came from.

    Playwright's get_attribute("href") returns the raw HTML attribute,
    unresolved — unlike the DOM .href property Puppeteer/Selenium read for
    the same purpose in this project, which the browser resolves for you.
    urljoin handles every shape correctly — absolute, protocol-relative,
    absolute-path, and page-relative hrefs alike.
    """
    return urljoin(base_url, href)


# Every `scheme://user:pass@` in a string, however many times it occurs.
# Matching globally rather than once is the point: a Playwright connection
# error repeats the endpoint five times (the message plus a four-line call
# log), so a masker that handled only the first occurrence would print the
# password four times and look like it was working.
_CREDENTIALS_IN_URL_RE = re.compile(r"([a-z][a-z0-9+.\-]*://)[^\s/@]+:[^\s/@]+@",
                                    re.IGNORECASE)


def _mask_credentials(text: str) -> str:
    """`text` with any username:password in an embedded URL replaced.

    Takes arbitrary text, not just a URL, because the strings that most need
    this are exception messages with a URL inside them. The host and port are
    KEPT — which endpoint or exit a run used is the useful half of the line
    and is not the secret.
    """
    return _CREDENTIALS_IN_URL_RE.sub(r"\1***:***@", text or "")


def _content_when_settled(page, attempts: int = 4, pause_ms: int = 700):
    """page.content() that tolerates a page mid-navigation.

    Playwright raises `Page.content: Unable to retrieve content because the
    page is navigating and changing the content` if the document swaps under
    it. Rakuten does not geo-redirect — `?lang=` is a query parameter and
    the price is JPY for every visitor — but it DOES redirect on its own in
    one measured case that this engine will meet routinely: `?p=151` of a
    150-page query answers 301 to page 1. A snapshot taken right after
    goto() can land exactly on that swap, so it is retried rather than
    raised.

    Retries briefly and returns None if the page won't hold still, so the
    caller can skip a check instead of failing the run.
    """
    for attempt in range(1, attempts + 1):
        try:
            return page.content()
        except PWError as e:
            if "navigating" not in str(e).lower():
                raise
            if attempt == attempts:
                logger.warning("Page kept navigating through %d attempts — "
                               "continuing without a snapshot.", attempts)
                return None
            logger.info("Page is navigating (a geo-redirect or the consent "
                        "layer?) — retrying content() in %dms (%d/%d).",
                        pause_ms, attempt, attempts)
            page.wait_for_timeout(pause_ms)
    return None


def handle_captcha_if_present(page, args) -> bool:
    """Detect and solve a challenge. True if something was solved.

    Runs after EVERY navigation, for ANY page — not scoped to one URL. The
    static-HTML and runtime reCAPTCHA detectors are run and reconciled
    against each other rather than short-circuited, because they can disagree
    about the variant and the parameters for one are rejected for the other.

    NOTE what this cannot help with, and note the difference between the two
    honest sentences about it. **This repo implements reCAPTCHA v2/v3;** if
    Rakuten ever renders an enterprise reCAPTCHA or a Turnstile, 2Captcha
    solves both and this client would need the matching task type added —
    that is a TODO here, not a limit of the product (§19).

    What is measured is narrower: no captcha of any kind is configured
    anywhere on rakuten.co.jp. Zero reCAPTCHA, hCaptcha, Turnstile,
    DataDome, PerimeterX, Incapsula, Kasada or AWS WAF markers across nine
    captures, served and refused alike; no challenge iframe; no
    `data-sitekey`; no `*_SITE_KEY` in any page config. And Akamai's own
    refusal here is 43 bytes long with **no widget on it at all** — which is
    the narrow, honest use of the word unsolvable: a property of that PAGE,
    not of any vendor. `detect_page_state` calls it "blocked" rather than
    "challenge" precisely so no solve is attempted or billed for it.

    This path exists because a bot manager can be switched on between
    deploys, and because the family's rule is that detection stays broad:
    different geos and scenarios surface different challenges.
    """
    html = _content_when_settled(page)
    if html is None:
        # Couldn't get a stable snapshot — skip detection for this navigation
        # rather than taking the whole run down. The next navigation gets
        # another chance, and the parse below reads its own copy of the DOM.
        return False

    # Detected is not the same as blocking. A challenge on a page whose
    # products are already rendered guards nothing, and counting the anchors
    # is instant — no wait_for_function, no 20s — which is why this check
    # sits here rather than after the readiness wait. Doing it the other way
    # round would cost 20 wasted seconds on a page the captcha genuinely
    # gates, where solving FIRST is what makes the content appear.
    already_rendered = len(page.query_selector_all(_ready_selector(args)))
    when_blocked = getattr(args, "solve_captcha", "when-blocked") == "when-blocked"

    html_challenge = detect_recaptcha_v3(html, page.url)
    runtime_challenge = detect_recaptcha_in_page(
        lambda js: page.evaluate(js), page_url=page.url)
    challenge = reconcile_detections(html_challenge, runtime_challenge)
    if not challenge:
        return False

    if when_blocked and already_rendered > MIN_CARD_MATCHES:
        logger.info("%s detected via %s, but %d anchors are already on the "
                    "page — not solving it. Pass --solve-captcha always to "
                    "solve it anyway.", challenge.kind, challenge.source,
                    already_rendered)
        return False

    logger.warning("%s detected via %s (sitekey=%s, action=%s) — attempting to solve.",
                   challenge.kind, challenge.source, challenge.sitekey, challenge.action)
    if not args.twocaptcha_key:
        logger.warning("No 2captcha API key, so this challenge cannot be "
                       "solved — continuing with whatever the page already "
                       "holds.")
        return False
    try:
        token = solve_recaptcha(challenge, args.twocaptcha_key,
                               api_version=args.captcha_api,
                               min_score=args.min_score)
    except Exception as e:  # noqa: BLE001 — a solver failure is not a crash
        logger.error("Solving the challenge failed (%s) — continuing with "
                     "whatever the page holds.", e)
        return False

    page.evaluate(INJECT_TOKEN_JS, token)
    logger.info("Token injected. Reloading page to continue.")
    page.wait_for_timeout(1500)
    page.reload(wait_until="domcontentloaded", timeout=60000)
    return True


def _parse_for_mode(html: str, url: str, args, page_num: int = 1) -> List:
    """Rows for this page, always as a list.

    The two modes read two different structures out of two different page
    kinds, and routing them through one helper is what keeps everything
    downstream — dedupe, merge, coverage logging, the writers — working on
    one shape.

    Crossing them is a silent failure rather than a loud one, which is why
    `parse_args` refuses the combination up front and why the suite pins it:
    the LISTING parser on a detail page found two well-formed phantom rows
    (a detail page links to plenty of other products) until its DOM fallback
    was gated, and the DETAIL parser on a listing page finds nothing at all.

    `page_num` is threaded through rather than defaulted, because `position`
    restarts at 1 on every page: without the page number beside it, a row
    from page 2 claims the same position as one from page 1 and the two are
    indistinguishable in the output. A sibling repo's first live run wrote
    120 rows all labelled page 1 for exactly that reason (§18).
    """
    if args.mode == "product":
        return parse_product_page(html, url, page=page_num,
                                  category=args.category)
    return parse_products(html, url, page=page_num, category=args.category,
                          currency=getattr(args, "_currency", None))


def _fetch_one_page(session, args, pool, page_num: int, url: str) -> PageOutcome:
    """Fetch and parse one page. Retries, rotations and debug dumps live here.

    Returns a PageOutcome and never raises for an EXPECTED failure — a
    timeout, a 403 refusal, a captcha page, a dead exit are all recorded on the
    outcome instead. What the run should do about them differs between the
    sequential and concurrent paths, so that decision belongs to the caller
    rather than to a raised exception unwinding through it.

    Always goes through `session.page`, never a captured local: a rotation
    replaces the browser, context and page together, and a stale handle is
    exactly the bug _BrowserSession exists to prevent.
    """
    outcome = PageOutcome(page_num=page_num, url=url)

    # How many times a blocked page may be retried.
    #
    # With a pool, each retry moves to a DIFFERENT exit and the budget is the
    # user's `--proxy-block-retries`. WITHOUT one — the ordinary case here,
    # because this site needs no proxy — the retry re-fetches through the
    # same access path once. Only once, and that is measured: what Akamai
    # refuses on this site is an inconsistent client rather than an address
    # (one datacentre IP was served by both curl and headless Chromium and
    # refused only by curl wearing a Chrome UA), so a second and third
    # identical attempt would confirm the same answer rather than change it.
    # `page_flow.block_advice` says what to look at instead.
    has_pool = bool(pool and len(pool) > 1)
    # `RETRY_ON_BLOCKED` is CONSULTED, not just documented. It was a
    # constant with a paragraph of justification that no engine read — a
    # policy statement nothing enforced, which is the same defect as dead
    # code that looks load-bearing. Setting it False now really does stop
    # the retry loop.
    block_retries = 0 if not page_flow.RETRY_ON_BLOCKED else (
        args.proxy_block_retries if has_pool
        else page_flow.BLOCK_RETRIES_WITHOUT_POOL)
    # Counted across the whole block-retry loop, not per attempt: a page that
    # keeps coming back as a challenge would otherwise buy one solve per
    # rotation, which is how a run quietly turns into a bill.
    solves_bought = 0
    # A THROTTLE's own budget, kept apart from the block budget because the
    # right response is the opposite one. Rakuten answers a client going too
    # fast with HTTP 503 and its own branded page — and serves the IDENTICAL
    # body under 403 on a route it refuses outright, so the status is the
    # only thing separating them. A 503 wants a longer wait at the SAME
    # exit; spending a rotation on it would move away from an address that
    # was working, and counting it as blocked would report exit 3 for a page
    # that was about to come back. Measured: a URL that had just 503'd twice
    # under no-delay hammering came back HTTP 200 with a full payload on 4
    # of 4 tries once 6 seconds were put between requests.
    throttle_spent = 0
    html, state, load_failed = None, "ok", False

    # The block loop may be re-entered for a throttle without consuming a
    # block attempt, so the budget is a while rather than a for.
    block_attempt = -1
    while block_attempt < block_retries:
        block_attempt += 1
        logger.info("Fetching page %d/%d: %s", page_num, args.pages, url)
        # Retry a navigation timeout rather than ending the run on it. One
        # network flap on page 12 of 50 should not break the loop.
        load_failed, exit_failed = False, None
        for attempt in range(1, args.retries + 1):
            try:
                session.page.goto(url, wait_until="domcontentloaded", timeout=60000)
                load_failed = False
                break
            except (PWTimeout, PWError) as e:
                # A dead or misconfigured proxy raises PWError
                # (net::ERR_PROXY_CONNECTION_FAILED), not PWTimeout —
                # catching only the latter lets it escape as a traceback,
                # which is the likeliest failure the first time anyone points
                # --proxy-file at a real list.
                reason = _proxy_failure(e)
                if reason:
                    exit_failed = reason
                    load_failed = True
                    break  # a different exit is the only thing that helps
                load_failed = True
                if attempt < args.retries:
                    pause = args.retry_delay * (2 ** (attempt - 1))
                    logger.warning("Timeout loading %s (attempt %d/%d) — "
                                   "retrying in %.1fs.", url, attempt,
                                   args.retries, pause)
                    time.sleep(pause)

        if exit_failed and has_pool and block_attempt < block_retries:
            logger.warning("Exit %s is unusable (%s) — rotating to another "
                           "one (%d/%d).", mask(pool.current), exit_failed,
                           block_attempt + 1, block_retries)
            pool.advance(f"unusable exit: {exit_failed}")
            session.relaunch()
            continue
        if load_failed:
            break

        # Counted against the SAME budget as the post-classification call
        # below, through page_flow.solve_budget. That is not tidiness: this
        # engine's ancestor called the solver here without counting and
        # counted only at the second call site, so `SOLVES_PER_PAGE = 1`
        # read like an enforced cap in every repo of this family while one
        # page could buy two solves — measured at three on a site where a
        # challenge rendered on every fetch (§23). Invisible wherever a
        # challenge is rare, which is everywhere until it is not.
        if page_flow.solve_budget(solves_bought):
            if handle_captcha_if_present(session.page, args):
                solves_bought += 1
                # A solve navigated the page. Give the destination a moment
                # before judging what came back.
                session.page.wait_for_timeout(1000)

        html = _content_when_settled(session.page) or ""
        state = _classify(session.page, html)

        # "Not painted yet" is not a fault, and telling it apart from one is
        # what the first live search run of this engine got wrong. A CATEGORY
        # listing server-renders its grid container, so it classifies as
        # content at domcontentloaded; a SEARCH grid arrives with the
        # client-side GraphQL response, so at that moment the page is a
        # 608 KB shell with no grid in it. Classified naively that is
        # "unknown", "unknown" retries, and the run fetched the page twice,
        # scrolled not at all and reported 0 rows with exit 4.
        #
        # So wait for the anchor and re-classify BEFORE the retry decision.
        # See page_flow.is_unpainted.
        if page_flow.is_unpainted(state, html):
            wait_timeout = page_flow.content_timeout_ms(args.mode)
            logger.info("Page %d is a shell the site served but has not "
                        "filled in (%d bytes, empty cards) — waiting up to "
                        "%.0fs for the prices rather than spending a retry.",
                        page_num, len(html), wait_timeout / 1000)
            found = page_flow.wait_for_count(
                lambda sel: len(session.page.query_selector_all(sel)),
                session.page.wait_for_timeout,
                _ready_selector(args), _min_matches(args, html), wait_timeout)
            # `<`, not `<=`. `wait_for_count` returns as soon as it sees
            # `minimum` matches, so found == threshold is the SUCCESS case,
            # and `<=` reported "the grid still had not painted" for a page
            # that had painted completely. It only shows up on a page whose
            # own hit count is at or below the floor — a query with fewer
            # than 8 results — which is why it survived in this family: on a
            # full 45-product page the threshold is 8 and nothing looks
            # wrong. Found on a live run of a 5-result query.
            if found < _min_matches(args, html):
                logger.info("The grid still had not painted after %.0fs "
                            "(%d match(es)).", wait_timeout / 1000, found)
            html = _content_when_settled(session.page) or html
            state = _classify(session.page, html)

        # No interstitial-settling step here, and its absence is measured
        # rather than an omission. This site has no interstitial to settle: a
        # refused request gets a 394-byte "Access Denied" with no markup at
        # all, so there is nothing to wait out and nothing to reclassify. See
        # page_flow's "There is no block page".
        #
        # The paid path is reached only for state "challenge", which NO
        # capture of this site has ever produced. It is wired up because a
        # bot manager can be switched on between deploys and a scraper that
        # cannot name what stopped it is much harder to fix — and bounded by
        # SOLVES_PER_PAGE so a speculative path cannot become a bill.
        if (page_flow.should_solve(state)
                and page_flow.solve_budget(solves_bought)):
            solves_bought += 1
            if handle_captcha_if_present(session.page, args):
                session.page.wait_for_timeout(1000)
                html = _content_when_settled(session.page) or html
                state = _classify(session.page, html)
                # The VERIFIED outcome, and the only one worth reporting: a
                # "ready" task result is not evidence the token works. This
                # line is what says whether the money bought anything.
                if state == "content":
                    logger.info("The solve was accepted — page %d is content "
                                "now.", page_num)
                else:
                    logger.warning(
                        "The solve was NOT accepted: page %d is still %s. The "
                        "purchase is spent.", page_num, state)

        if not page_flow.should_retry(state):
            # "content" and "empty" are both final answers. An empty page is
            # a CORRECT one — a query that matched nothing has no products —
            # so retrying it would spend the user's budget re-confirming the
            # same right answer, and rotating the exit would blame an address
            # for the query it was given.
            break

        if state == "throttled":
            # Wait longer at the same exit, on the throttle's own budget.
            # Deliberately does NOT consume a block attempt and does NOT
            # rotate: see `throttle_spent` above.
            if throttle_spent < page_flow.THROTTLE_RETRIES:
                throttle_spent += 1
                pause_ms = page_flow.throttle_delay_ms(throttle_spent)
                logger.warning(
                    "Page %d came back throttled — Rakuten's own \"access is "
                    "concentrated\" page under HTTP 503. That is a rate "
                    "limit rather than a refusal, so waiting %.0fs and "
                    "re-fetching from the SAME exit (%d/%d). Raise --delay "
                    "if this keeps happening.",
                    page_num, pause_ms / 1000, throttle_spent,
                    page_flow.THROTTLE_RETRIES)
                time.sleep(pause_ms / 1000)
                block_attempt -= 1        # not a block attempt
                continue
            logger.error(
                "Page %d is still throttled after %d wait(s) totalling "
                "%.0fs. Rakuten is rate-limiting this client rather than "
                "refusing it, so the fix is fewer requests (raise --delay, "
                "lower --concurrency) rather than another exit.",
                page_num, throttle_spent,
                sum(page_flow.throttle_delay_ms(n)
                    for n in range(1, throttle_spent + 1)) / 1000)
            break

        # Blocked or challenged. A different exit is the one thing that
        # plausibly changes the outcome where the address was what was
        # scored — though on THIS site that is usually not the case, which is
        # what `page_flow.block_advice` explains and why the no-pool budget
        # is 1.
        if block_attempt < block_retries:
            if has_pool:
                logger.warning("Page %d came back as %s from %s — retrying "
                               "from another exit (%d/%d).", page_num, state,
                               mask(pool.current), block_attempt + 1,
                               block_retries)
                pool.advance(f"{state} on page {page_num}")
                session.relaunch()
            else:
                # No pool, so nowhere else to go — but a plain re-fetch is
                # what clears this on a Scraping Browser profile. The browser
                # is NOT relaunched: over `--cdp-endpoint` a profile allows
                # one live connection, so tearing the session down and
                # reconnecting risks `profile_locked` and would lose the very
                # cookies the retry is meant to build on.
                pause = args.retry_delay * (block_attempt + 1)
                logger.warning("Page %d came back as %s — re-fetching through "
                               "the same access path in %.1fs (%d/%d). On this "
                               "site that is often what clears it.",
                               page_num, state, pause, block_attempt + 1,
                               block_retries)
                time.sleep(pause)

    if load_failed:
        logger.error("Gave up loading %s after %d attempt(s).", url, args.retries)
        outcome.load_failed = True
        return outcome

    outcome.state = state

    if state == "blocked":
        # What a caller needs to know here is that there is nothing to
        # solve, and that a proxy is probably not the answer either. Akamai
        # refuses this site with a 43-byte body whose whole content is
        # `Reference  #18.…` — no widget, nothing for any solver at any
        # price — and it arrives under **HTTP 200**, so the status code is
        # not the signal.
        #
        # The dump is written even when it is empty, because "43 bytes" is
        # itself the diagnosis here and a reader who finds no file at all
        # cannot tell that from a run that never got this far.
        debug_html = f"{args.out}_page{page_num}_debug.html"
        with open(debug_html, "w", encoding="utf-8") as f:
            f.write(html or "")
        served = served_by_rakuten(html or "")
        logger.error(
            "The site did not serve this request — %d bytes, %s the site's "
            "own asset host, saved to %s. What clears it on this site is "
            "usually NOT a different address, and that is measured rather "
            "than assumed: on 2026-09-21 one datacentre IP was served the "
            "full 45-product page both by plain curl and by headless "
            "Chromium, and refused with this same 43-byte deny only when a "
            "curl handshake claimed a Chrome User-Agent. What Akamai "
            "refuses here is the CONTRADICTION between a claimed client and "
            "the TLS fingerprint underneath it. So check for a fingerprint "
            "you added before buying an exit: no --fingerprint and no "
            "custom UA over --cdp-endpoint, and no browser UA on an HTTP "
            "client. If the client is already consistent, --proxy with a "
            "Japanese exit (or `country-jp` in a Scraping Browser login) is "
            "the next thing to try. This is exit 3, distinct from a "
            "genuinely empty result (exit 4).%s",
            len(html or ""), "which references" if served else "with no "
            "reference to", debug_html,
            (f" Tried {block_retries + 1} exit(s)." if has_pool
             else f" Re-fetched {block_retries + 1} time(s)."))
        outcome.blocked_by = "no-response" if not html else "not-served"
        outcome.final_url = session.page.url
        return outcome

    if state == "content":
        # Never wait on `networkidle`, and this is not caution — it is
        # measured. Rakuten's ad and RAT beacons keep firing indefinitely: a
        # probe counted 46 fetches and 13 pings still going after load, and a
        # `networkidle` wait ran to its full 60s timeout and then raised, on
        # a page that was complete in two seconds.
        #
        # NO scroll follows either. The payload holds all 45 products in the
        # first response, so a scroll here would be latency bought for
        # nothing — and a wait that times out costs nothing at all, because
        # every column in the output comes out of that payload rather than
        # out of the DOM.
        selector, threshold = _ready_selector(args), _min_matches(args, html)
        content_timeout = page_flow.content_timeout_ms(args.mode)
        # A POLL, not wait_for_function. This site's CSP does allow
        # `unsafe-eval` today, so an evaluated string would work — but
        # wait_for_function hands the browser a STRING, and on a site whose
        # CSP forbids it that raises EvalError and takes the run down with exit
        # 1. See page_flow.wait_for_count.
        found = page_flow.wait_for_count(
            lambda sel: len(session.page.query_selector_all(sel)),
            session.page.wait_for_timeout, selector, threshold, content_timeout)
        session.page.wait_for_timeout(500)
        if found < threshold:
            # Not an error on its own, and what it MEANS depends on the
            # mode — which is why the message does too. A listing page with
            # no grid is a correct answer (a taxonomy hub, or one page past
            # the end); a detail page whose buy box never painted is a
            # different thing entirely, and on this site it is usually just
            # slow rather than absent, because the row is parsed out of the
            # page's JSON-LD and not out of the buy box.
            logger.info("No listing tiles appeared within %.0fs. If this "
                        "URL is a hub page or one page "
                        "past the end of a listing, that is the expected "
                        "answer and the run will report 0 rows (exit 4).",
                        content_timeout / 1000)

        html = _content_when_settled(session.page) or html

    # Dumping on success, not only on failure: a run can return the right
    # NUMBER of rows with a field silently unpopulated, and then the only way
    # to tell a parsing bug from a too-early snapshot is to inspect the exact
    # bytes the parser was given.
    if args.dump_html:
        dump_path = (args.dump_html if args.pages == 1
                     else f"{args.dump_html}.page{page_num}")
        with open(dump_path, "w", encoding="utf-8") as f:
            f.write(html)
        logger.info("Saved the snapshot the parser sees to %s (%d bytes).",
                    dump_path, len(html))

    # Only for a state page_flow already counts as BLOCKED, and that
    # narrowing was earned twice.
    #
    # A marker on a page whose ads have rendered guards nothing — that is the
    # "detected is not blocking" rule the captcha default follows, applied to
    # the blocking decision instead of the spending one. And `state !=
    # "content"` would still be too wide: an EMPTY page is a correct answer,
    # so this runs only for a state the policy has already given up on and
    # only REFINES the reason.
    #
    # The marker set itself is chosen the same way (§18): every candidate
    # was counted on pages Rakuten plainly served before any of it was
    # trusted. `cf-turnstile` is excluded because the Scraping Browser's own
    # auto-solve extension injects it into every page it loads, and a bare
    # `akamai` is excluded because a CDN name is a fact about the site's
    # infrastructure rather than a signal about this response. The set that
    # remains scored 0 on all six served captures — which is also why it has
    # never fired.
    vendor = (detect_bot_challenge(html, url=session.page.url)
              if page_flow.counts_as_blocked(state) else None)
    if vendor:
        debug_html = f"{args.out}_page{page_num}_debug.html"
        with open(debug_html, "w", encoding="utf-8") as f:
            f.write(html)
        try:
            session.page.screenshot(path=f"{args.out}_page{page_num}_debug.png",
                                    full_page=True)
        except Exception as e:
            logger.warning("Could not capture screenshot: %s", e)
        logger.error("Blocked by %s before parsing (%d bytes) — saved to %s%s. "
                     "This is exit 3, distinct from a genuinely empty result "
                     "(exit 4).", vendor, len(html), debug_html,
                     (f" (tried {block_retries + 1} exit(s))" if has_pool
                      else f" (re-fetched {block_retries + 1} time(s))"))
        outcome.blocked_by = vendor
        logger.error("%s", page_flow.block_advice(
            html, headless=bool(getattr(args, "headless", False)),
            has_pool=has_pool))
        return outcome

    # Did the SERVER serve the page we asked for? Read off
    # `pagination.start`, never trusted from the request, because on this
    # site neither out-of-range route fails in a way the client can see:
    # `?p=151` of a 150-page query answers 301 to page 1 and then HTTP 200
    # with 45 real products, and `/category/{id}/?p=2` answers 200 with page
    # 1 without even redirecting. A run that trusted its own request would
    # re-collect page 1 for as long as it was asked to and report a
    # complete, entirely duplicate file (§23).
    if args.mode == "listing":
        # Compared against the page the fetched URL actually ASKS for, not
        # against the loop's counter — a run started on a URL that already
        # carries `?p=2` asks the site for page 2 while calling it page 1 of
        # the run. See page_flow.requested_page_number.
        asked = page_flow.requested_page_number(url, page_num)
        outcome.served_requested_page = page_flow.served_the_page_asked_for(
            html, asked)
        if not outcome.served_requested_page:
            served = page_flow.served_page_number(html)
            logger.info(
                "Asked Rakuten for page %d and it served page %s — its own "
                "`pagination.start` says so. That is the end of this "
                "listing. Rakuten does not error past the last page, it "
                "re-serves page 1 with HTTP 200, and there are two ways to "
                "get here: the query has fewer pages than you asked for, or "
                "you passed page %d of the site's %d-page cap. Dropping "
                "this page's rows, which are page %s over again.",
                asked, served, parser_page_cap + 1, parser_page_cap,
                served)
            outcome.final_url = session.page.url
            outcome.state = "empty"
            return outcome

    products = _parse_for_mode(html, session.page.url, args, page_num)
    logger.info("Parsed %d row(s) from page %d.", len(products), page_num)

    # Rakuten publishes its own arithmetic in the payload — `numFound`,
    # `pageSize` and `subset` — so page count and completeness are computed
    # rather than guessed. Both numbers are recorded, because on this site
    # they are wildly different and only having both makes the run honest:
    # one measured query reported `numFound` 3,053,682 against a `subset` of
    # 6,750, so a full run of it is complete AND a 0.2% sample (§21).
    #
    # `numFound` is also a LIVING number and is recorded as the site's
    # answer at this moment rather than asserted against — three fetches of
    # one query inside a minute reported 3,053,682, 3,053,713 and 3,053,712.
    if args.mode == "listing" and page_num == 1:
        outcome.total_available = total_results(html)
        outcome.header = search_header(html)
        outcome.cap = page_flow.cap_summary(html)
        # The currency the site stated, carried to the rest of the run.
        # Rakuten publishes the JSON-LD block that names it on PAGE 1 ONLY
        # (measured: 1 block on page 1, 0 on pages 2 and 3 of one query), so
        # without this the first 45 rows carried "JPY" and the next 90
        # carried null for products whose currency the site had already
        # stated. Read once, never defaulted: a run that never got page 1
        # leaves it null.
        stated = currency_from_page(html)
        if stated:
            args._currency = stated
            logger.info("Page 1's structured data states the currency as %s "
                        "— carrying it to the rest of the run, since Rakuten "
                        "publishes that block on page 1 only.", stated)
        else:
            logger.warning(
                "Page 1 stated no currency. Rakuten normally declares "
                "`priceCurrency` in this page's own JSON-LD; with no "
                "statement to read, every row's `currency` will be null "
                "rather than a guess.")
        if capped_by_site(html):
            logger.info(
                "Rakuten says this query matches %s products and will serve "
                "%s of them (%d pages of %d). A run that reaches page %d is "
                "COMPLETE as far as the site is concerned and is %.2f%% of "
                "what it says it matched — narrow the query with the site's "
                "own filters to reach the rest.",
                f"{outcome.total_available:,}", f"{reachable_max(html):,}",
                total_pages(html) or 0, page_flow.PAGE_SIZE,
                total_pages(html) or 0,
                100.0 * (reachable_max(html) or 0) / outcome.total_available)

    if products and args.mode == "listing":
        priced = sum(1 for p in products if p.price is not None)
        share = 100.0 * priced / len(products)
        floor = PRICE_FLOOR.get(args.mode, 0)
        # Reported every time, not only when it looks wrong, so a consumer
        # gets the number rather than a threshold someone guessed.
        logger.info("Price coverage on page %d: %d/%d (%.0f%%); the measured "
                    "floor is %d%%.",
                    page_num, priced, len(products), share, floor)
        if share < floor:
            logger.warning(
                "Only %.0f%% of page %d carries a price, against a measured "
                "floor of %d%%. Every one of 405 rows across 11 captured "
                "pages had one, so this is the read breaking rather than the "
                "page being unusual — re-run with --dump-html.",
                share, page_num, floor)

        # There is deliberately NO structured-vs-displayed price
        # confirmation share here, and its absence is measured rather than an
        # omission. This site has no second view to reconcile against: the
        # payload carries the price and the rendered tile prints that same
        # number formatted, so `price_source` is "state" on every listing row
        # and a confirmation threshold would describe nothing. Porting a
        # sibling repo's overlay would be dead code that looks load-bearing
        # (§4).
        #
        # What IS worth reporting is the share of rows Rakuten publishes a
        # was-price for, because on the listing route the honest answer is
        # ZERO and a reader needs to know that is the site rather than the
        # parser. The payload has no was-price field at all; the detail
        # route does, gated on the site's own
        # `doublePrice.referencePriceVerified` flag.
        rated = sum(1 for p in products if p.rating is not None)
        logger.info(
            "Rating coverage on page %d: %d/%d (%.0f%%). A null here means "
            "nobody has reviewed the product — Rakuten writes that as "
            "`{score: 0, numReviews: 0}` and both columns are nulled "
            "together, on 28 of 405 measured rows.",
            page_num, rated, len(products), 100.0 * rated / len(products))
        if any(p.original_price is not None for p in products):
            logger.info(
                "This page carries a verified was-price, which the listing "
                "payload was measured never to publish — worth a look, the "
                "site may have added the field.")

    if not products:
        debug_html = f"{args.out}_page{page_num}_debug.html"
        debug_png = f"{args.out}_page{page_num}_debug.png"
        with open(debug_html, "w", encoding="utf-8") as f:
            f.write(html)
        try:
            session.page.screenshot(path=debug_png, full_page=True)
        except Exception as e:
            logger.warning("Could not capture screenshot: %s", e)
        logger.warning("0 rows parsed — saved what the browser actually saw to "
                       "%s and %s. Open the .png to see it.", debug_html, debug_png)

    outcome.products = products
    outcome.final_url = session.page.url
    return outcome


def _worker_pool(pool, worker_index: int):
    """A private ProxyPool for one worker, starting at a different exit.

    Each worker gets its OWN pool object holding the same exits rotated to a
    different offset. Two things fall out of that, both wanted:

      * Workers start on distinct exits, which is the point of running
        several — N workers all leaving from one address is just a faster way
        to burn that address.
      * No shared mutable state between threads, so rotation needs no lock.
        A worker that gets blocked can still walk the rest of the pool on its
        own.

    Its exit stays put for the worker's lifetime otherwise: a SESSION must
    not change address mid-flight, and a worker is one session.
    """
    if not pool:
        return None
    proxies = pool.proxies
    offset = worker_index % len(proxies)
    return ProxyPool(proxies[offset:] + proxies[:offset], rotate="per-run")


def _fetch_pages_concurrently(args, pool, specs, concurrency: int):
    """Fetch `specs` [(page_num, url), ...] across `concurrency` workers.

    Each worker owns its own Playwright instance, browser and exit: with the
    sync API a browser belongs to the thread that made it, so sharing one
    across threads is not an option even if it were desirable.
    """
    work = queue.Queue()
    for spec in specs:
        work.put(spec)

    results = []
    results_lock = threading.Lock()
    # Set when a page comes back with no rows at all — the end of the
    # listing. Without it, asking for 50 pages of a 5-page result would fetch
    # 45 empty ones. Workers check it before taking more work, so at most
    # (concurrency - 1) extra pages are in flight when it trips.
    exhausted = threading.Event()

    def worker(index: int):
        name = f"worker-{index + 1}"
        try:
            with sync_playwright() as pw:
                session = _BrowserSession(pw, args, _worker_pool(pool, index)).open()
                try:
                    first = True
                    while not exhausted.is_set():
                        try:
                            page_num, url = work.get_nowait()
                        except queue.Empty:
                            break
                        if not first:
                            time.sleep(args.delay)
                        first = False
                        outcome = _fetch_one_page(session, args, session.pool,
                                                  page_num, url)
                        with results_lock:
                            results.append(outcome)
                        if outcome.ok and not outcome.products:
                            logger.info("[%s] page %d returned no rows — "
                                        "treating that as the end of the listing "
                                        "and stopping dispatch.", name, page_num)
                            exhausted.set()
                finally:
                    session.close()
        except Exception:  # noqa: BLE001 — a dead worker must not hang the run
            logger.exception("[%s] died; its pages will be reported as failed.", name)

    threads = [threading.Thread(target=worker, args=(i,), name=f"page-worker-{i + 1}")
               for i in range(concurrency)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Anything still queued was never attempted (a worker died, or dispatch
    # stopped at the end of the listing). Not reported as failed pages: they
    # were not tried, and claiming otherwise would overstate the damage.
    unattempted = []
    while True:
        try:
            unattempted.append(work.get_nowait()[0])
        except queue.Empty:
            break
    return results, sorted(unattempted), exhausted.is_set()


def scrape(args) -> int:
    # One entry per page attempted, merged after the loop rather than folded
    # into shared state during it — see PageOutcome for why that ordering
    # matters more than it looks.
    outcomes: List[PageOutcome] = []
    seen_keys = set()
    blocked = False
    # Both modes are one row per product, so `sku` is the key for both.
    dedupe_key = "sku"
    # Why the loop ended. "completed" means every requested page was fetched;
    # "no_new_products" means the listing itself ran out (also a complete
    # result). "single_page_mode" is complete by construction — a detail page
    # has no page 2. Anything else is an early stop, and the run is only a
    # partial view.
    # Only --mode product is single-page, and "end_of_listing" is complete
    # too: the site said it served a page other than the one asked for, which
    # on Rakuten means the query ran out and the site answered with page 1
    # anyway rather than erroring.
    stop_reason = "completed"

    pool = proxy_pool_from_args(args)
    if pool and args.cdp_endpoint:
        logger.warning("Ignoring --proxy/--proxy-file: with --cdp-endpoint the "
                       "remote browser has its own exit, and layering a second "
                       "proxy on top would contradict it.")
        pool = None

    concurrency = max(1, args.concurrency)
    if concurrency > 1:
        if args.cdp_endpoint:
            logger.warning("--concurrency is ignored with --cdp-endpoint: the "
                           "Scraping Browser API allows one live connection per "
                           "profile, and several workers would collide on it "
                           "(profile_locked). Use several pids instead, one run "
                           "each.")
            concurrency = 1
        elif not pool:
            logger.warning("--concurrency %d with no proxy pool: every worker "
                           "leaves from the SAME address, which is a faster way "
                           "to get that address scored than to gather data. "
                           "Rakuten already answers a client going too fast "
                           "with HTTP 503 and its own \"access is "
                           "concentrated\" page, so N workers from one "
                           "address is the quickest way to meet it. Pass "
                           "--proxy-file to spread the load, or raise "
                           "--delay.", concurrency)
        if pool and pool.rotates_per_page():
            logger.info("--proxy-rotate per-page is redundant under "
                        "--concurrency: each worker already holds its own exit "
                        "for its lifetime, which is the same spread without a "
                        "browser relaunch per page.")
        if concurrency > 8:
            logger.warning("--concurrency %d means %d browsers at once "
                           "(~150-300MB each). Make sure the machine has the "
                           "memory for it.", concurrency, concurrency)

    with sync_playwright() as pw:
        session = _BrowserSession(pw, args, pool,
                                  remote=bool(args.cdp_endpoint)).open()
        try:
            # Page 1 is always fetched on its own: its content is what decides
            # whether pages 2..N can be addressed independently at all.
            first = _fetch_one_page(session, args, pool, 1, args.url)
            outcomes.append(first)

            if not first.ok:
                stop_reason = ("page_load_timeout" if first.load_failed
                               else f"blocked_{first.blocked_by}")
                blocked = first.blocked_by is not None
            seen_keys.update(p.sku for p in first.products if p.sku is not None)
            planned = _plan_page_urls(session.page, args, first.final_url)

            if args.pages > 1 and concurrency > 1 and planned is None:
                logger.warning("--concurrency %d requested, but this "
                               "listing's pagination cannot be addressed "
                               "independently (see above) — falling back to "
                               "one page at a time.", concurrency)
                concurrency = 1

            if args.pages > 1 and concurrency > 1:
                # Close the page-1 browser before starting workers: it has
                # done its job, and holding it open would cost one more
                # browser than asked for.
                session.close()
                specs = [(n, planned[n - 2]) for n in range(2, args.pages + 1)]
                logger.info("Fetching pages 2-%d across %d workers%s.",
                            args.pages, concurrency,
                            f" over {len(pool)} exit(s)" if pool else "")
                rest, unattempted, exhausted = _fetch_pages_concurrently(
                    args, pool, specs, concurrency)
                outcomes.extend(rest)

                failed = [o for o in rest if not o.ok]
                if failed:
                    worst = min(failed, key=lambda o: o.page_num)
                    stop_reason = ("page_load_timeout" if worst.load_failed
                                   else f"blocked_{worst.blocked_by}")
                    blocked = any(o.blocked_by for o in rest)
                elif exhausted:
                    stop_reason = "no_new_products"
                elif unattempted:
                    # Should not happen without a failure or exhaustion,
                    # but say so rather than reporting a complete run.
                    stop_reason = "pages_unattempted"
                session = None  # already closed
            else:
                url = (planned[0] if planned else
                       _next_url_from_page(session.page, args, 1))
                for page_num in range(2, args.pages + 1):
                    # A new exit per page is what actually spreads a run's
                    # volume, and it costs a browser relaunch: carrying the
                    # session across exits would defeat the point.
                    if pool and pool.rotates_per_page():
                        pool.advance(f"per-page rotation, page {page_num}")
                        session.relaunch()

                    outcome = _fetch_one_page(session, args, pool, page_num, url)
                    outcomes.append(outcome)
                    if not outcome.ok:
                        stop_reason = ("page_load_timeout" if outcome.load_failed
                                       else f"blocked_{outcome.blocked_by}")
                        blocked = outcome.blocked_by is not None
                        break

                    # Whether this page contributed anything not already
                    # seen. Kept as a running check because the condition is
                    # inherently sequential — "new" only means anything
                    # relative to the pages before it. The authoritative
                    # dedupe happens once, after the loop, in page order.
                    fresh_count = sum(1 for p in outcome.products
                                      if p.sku is None or p.sku not in seen_keys)
                    seen_keys.update(p.sku for p in outcome.products
                                     if p.sku is not None)

                    # The site said it served a different page than the one
                    # asked for. A distinct stop_reason from
                    # `no_new_products`, because they mean different things
                    # and a reader needs to tell them apart: this one is
                    # "the listing ended and Rakuten answered with page 1
                    # anyway", while `no_new_products` is "the catalogue
                    # repeated itself". Checked BEFORE the dedupe-based
                    # terminator, since a re-served page 1 would trip that
                    # one too and report the vaguer reason.
                    if not outcome.served_requested_page:
                        stop_reason = "end_of_listing"
                        break

                    # A page past the first that contributes nothing new
                    # means the end of the results — or that pagination is
                    # looping back on itself. Either way there is nothing
                    # further to fetch, and this is the honest terminating
                    # condition: a property of the DATA, not of a CSS
                    # selector that may have been renamed.
                    if not fresh_count:
                        logger.info("Page %d added no rows not already seen "
                                    "— treating that as the end of the "
                                    "listing.", page_num)
                        stop_reason = "no_new_products"
                        break

                    if page_num < args.pages:
                        url = (planned[page_num - 1] if planned else
                               _next_url_from_page(session.page, args, page_num))
                        time.sleep(args.delay)
        finally:
            if session is not None:
                session.close()

    # Merge once, in PAGE order — not in the order pages happened to finish.
    # At one page at a time the two are identical, which is the point: this is
    # what keeps the output byte-for-byte the same while removing the
    # dependency on arrival order that concurrency would otherwise introduce.
    all_rows = []
    merged_seen = set()
    for oc in sorted(outcomes, key=lambda o: o.page_num):
        fresh = dedupe_by_key(oc.products, merged_seen, key=dedupe_key)
        if len(fresh) < len(oc.products):
            # Not necessarily "on an earlier page" — a duplicate can be on
            # this page. This site's pagination was measured NOT repeating:
            # 52 rows across two live pages, 52 distinct sku, including two
            # DIFFERENT Cars of the Week because the site rotates that slot.
            # But a classifieds listing reorders as sellers bump their ads to
            # the top, so a small non-zero count here is expected and a large
            # one is not.
            logger.info("Page %d: dropped %d duplicate row(s).",
                        oc.page_num, len(oc.products) - len(fresh))
        all_rows.extend(fresh)

    # Completeness, checked over the MERGED result rather than per page — a
    # per-page check cannot see a gap BETWEEN two pages, which is exactly
    # where a short page hides.
    #
    # NOT "pages x rows-per-page". Rakuten's page size is steady at 45 on 11
    # of 11 captures — including page 150, the last page of a capped query —
    # but the last page of a query with fewer than 6,750 total hits is
    # legitimately short, so multiplying the fullest page by the page count
    # would warn on healthy runs, and a threshold that fires on every
    # healthy run teaches the reader to ignore it.
    #
    # What is worth warning about is a page that came back materially THIN
    # against its siblings — that is what a truncated response or a
    # half-painted grid looks like. A page holding less than 60% of the
    # fullest page is well outside the +-3% spread that the varying page size
    # accounts for.
    total_available = next((o.total_available for o in outcomes
                            if o.total_available is not None), None)
    if args.mode == "listing" and all_rows:
        counts = [(o.page_num, len(o.products)) for o in outcomes if o.ok]
        fullest = max((n for _, n in counts), default=0)
        thin = [(p, n) for p, n in counts
                if fullest and n < THIN_PAGE_SHARE * fullest]
        # The LAST page of a listing is legitimately short — the catalogue
        # simply ran out — so it is excluded unless there are pages after it.
        last_page = max((p for p, _ in counts), default=0)
        thin = [(p, n) for p, n in thin if p != last_page]
        if thin:
            logger.warning(
                "Page(s) %s came back much thinner than the fullest page "
                "(%d rows): %s. A truncated response or a half-painted grid "
                "looks like this — re-run with --dump-html to check the "
                "snapshot for those pages.",
                ", ".join(str(p) for p, _ in thin), fullest,
                ", ".join("page %d: %d" % (p, n) for p, n in thin))
        if total_available:
            logger.info("This listing holds %d product(s) in total; this run "
                        "took %d (%.1f%%).", total_available, len(all_rows),
                        100.0 * len(all_rows) / total_available)
            # The pages the CATALOGUE has that the site will NOT address. A
            # run that stops at the cap is complete as far as the site is
            # concerned and truncated as far as the catalogue is, and only
            # saying so lets a consumer tell the two apart. Computed from the
            # total the payload already stated rather than from another fetch.
            # `fullest` is this run's own observed page size, which is what
            # the site actually served rather than a number hardcoded here.
            beyond = (max(0, -(-total_available // fullest) - parser_page_cap)
                      if fullest else 0)
            if beyond:
                logger.warning(
                    "This query is %d page(s) deeper than Rakuten will "
                    "address. It caps every query at 6,750 results — 150 "
                    "pages of 45 — however many it matched, and it states "
                    "that itself as `pagination.subset`. One measured query "
                    "reported 3,053,682 matches against that same 6,750, so "
                    "99.8%% of it cannot be reached through pagination at "
                    "all, and a request past page 150 does not fail: it "
                    "redirects to page 1 and serves it with HTTP 200. "
                    "Narrow the query with the site's own filters — genre, "
                    "price band, shop, tag — and run each slice.",
                    beyond)

    ok_pages = [o for o in outcomes if o.ok]
    failed_pages = [o.page_num for o in outcomes if not o.ok]
    final_url = (max(ok_pages, key=lambda o: o.page_num).final_url
                 if ok_pages else args.url)

    # One-per-run context, in the sidecar rather than repeated down a column.
    #
    # In --mode product that is the merchant's own id, name and tax rate off
    # the item page. In --mode listing it is Rakuten's own arithmetic about
    # the query, and on this site that is not optional decoration: a
    # `status: complete` run of a query the site caps at 6,750 of 3,053,682
    # matches is complete and is a 0.2% sample, and a sidecar that said only
    # "complete" would be lying by omission (§21). It also gives
    # `diff_runs.py` the third meaning of `removed` — not delisted, not
    # un-fetched, but outside this run's slice of a capped result set.
    extra = None
    caps = {o.page_num: o.cap for o in outcomes if o.cap}
    headers = {o.page_num: o.header for o in outcomes if o.header}
    facts = {o.page_num: o.shop_facts for o in outcomes if o.shop_facts}
    page_one_cap = next((o.cap for o in outcomes if o.cap), None)
    if caps or headers or facts:
        extra = {"page_language": headers}
        if page_one_cap:
            extra.update(page_one_cap)
        if facts:
            extra["shop"] = facts
    if page_one_cap and page_one_cap.get("capped_by_site"):
        logger.warning(
            "This query is capped by the site: %s matches, %s reachable, %d "
            "page(s) of matches that no `?p=` can address. A complete run "
            "here is a sample, and the sidecar records both numbers.",
            f"{page_one_cap.get('total_results'):,}",
            f"{page_one_cap.get('reachable_max'):,}",
            page_one_cap.get("pages_beyond_cap") or 0)

    return finish_run(all_rows, args.out, args.format, args.allow_empty,
                      blocked=blocked, stop_reason=stop_reason,
                      pages_requested=args.pages, pages_completed=len(ok_pages),
                      pages_failed=failed_pages, mode=args.mode,
                      # `rakuten.co.jp`, matching every ROW's `source` —
                      # not the hostname of the moment. A run can
                      # legitimately start on www.rakuten.co.jp and finish on
                      # search.rakuten.co.jp, because `page_url` rewrites a
                      # genre landing page onto the search host for page 2.
                      # `site_host(final_url)` made the sidecar disagree with
                      # the file beside it about which site the data came
                      # from.
                      source=SOURCE_DEFAULT,
                      start_url=args.url, final_url=final_url,
                      extra=extra)


def parse_args():
    p = argparse.ArgumentParser(
        description="Rakuten Ichiba scraper (Playwright edition)")
    p.add_argument("--url", default=None,
                   help="A Rakuten Ichiba URL. In --mode listing: a keyword "
                        "search (search.rakuten.co.jp/search/mall/KEYWORD/), "
                        "a genre listing "
                        "(search.rakuten.co.jp/search/mall/-/GENREID/) or a "
                        "genre landing page "
                        "(www.rakuten.co.jp/category/GENREID/), with or "
                        "without the site's own filters. In --mode product: "
                        "one item page (item.rakuten.co.jp/SHOP/CODE/). "
                        "Required, unless RAKUTEN_URL is set in the "
                        "environment or in .env.")
    p.add_argument("--mode", choices=["listing", "product"],
                   default="listing",
                   help="listing (default): a search or genre grid, 45 "
                        "products a page. product: one item.rakuten.co.jp "
                        "page, which adds per-variant prices, a variant "
                        "count and a verified was-price where Rakuten's own "
                        "`referencePriceVerified` flag allows one. There is "
                        "deliberately no `shop` mode: a merchant storefront "
                        "carries an EMPTY payload and no product grid at "
                        "all, so the mode would need a parser written "
                        "against markup nobody has captured, and a mode that "
                        "ships untested is worse than one that is absent.")
    p.add_argument("--category", default=None,
                   help="Label to tag output rows with. Defaults to each "
                        "product's OWN Rakuten genre path "
                        "(水・ソフトドリンク > コーヒー > インスタントコーヒー), "
                        "which is a fact about the product rather than about "
                        "the request — the browsed URL is in the run's "
                        "sidecar. Pass this to override it with a label of "
                        "your own.")
    p.add_argument("--pages", type=int, default=1,
                   help="Number of listing pages to crawl (45 products "
                        "each). Applies to --mode listing; a product page is "
                        "one page. Rakuten caps EVERY query at 6,750 results "
                        "— 150 pages — however many it matched, and asking "
                        "for page 151 gets page 1 again with HTTP 200, so a "
                        "run stops at the cap and the sidecar records both "
                        "numbers.")
    p.add_argument("--delay", type=float, default=2.0, help="Delay between pages, seconds")
    p.add_argument("--concurrency", type=int, default=1, metavar="N",
                   help="Fetch pages through N parallel workers (default 1 — "
                        "unchanged sequential behaviour). Each worker runs its "
                        "own browser and holds its own proxy exit, so N>1 "
                        "without --proxy-file just sends N times the traffic "
                        "from one address. Ignored with --cdp-endpoint.")
    p.add_argument("--retries", type=int, default=3,
                   help="Attempts per page load before giving up (default 3). "
                        "The pause between attempts doubles each time. A page "
                        "that comes back EMPTY is not retried — see "
                        "page_flow.STATE_POLICY — because an empty hub "
                        "category is a correct answer, not a fault.")
    p.add_argument("--retry-delay", type=float, default=2.0,
                   help="Seconds before the first page-load retry, doubling "
                        "thereafter (default 2.0)")
    p.add_argument("--format", choices=["json", "csv", "both"], default="both")
    p.add_argument("--out", default="rakuten_products", help="Output file prefix")
    p.add_argument("--locale", default="ja-JP",
                   help="Browser locale (default ja-JP). It does NOT decide "
                        "the page language or the currency: Rakuten takes "
                        "the UI language from a `?lang=` QUERY PARAMETER and "
                        "prices everything in JPY for every visitor. And "
                        "even `?lang=en` only translates the CHROME — "
                        "product names, shop names and tag values are "
                        "merchant-authored Japanese and come back "
                        "byte-identical either way, which is measured rather "
                        "than assumed. So this changes what the browser "
                        "claims about itself and nothing else.")
    p.add_argument("--proxy", default=None,
                   help="Proxy URL, e.g. http://ACCOUNT:PASSWORD@HOST:9999 "
                        "(2captcha.com/proxy)")
    p.add_argument("--proxy-file", default=None,
                   help="File with one proxy URL per line (# comments and blank "
                        "lines skipped) to rotate across. Wins over --proxy.")
    p.add_argument("--proxy-rotate", choices=list(ROTATE_MODES), default="per-run",
                   help="per-run (default): one exit for the whole run. per-page: "
                        "a new exit for every page — this is what spreads volume, "
                        "and it relaunches the browser each time so the session "
                        "does not follow the IP around.")
    p.add_argument("--proxy-shuffle", action="store_true",
                   help="Shuffle the pool at startup, so concurrent runs do not "
                        "all begin on the first exit in the file.")
    p.add_argument("--proxy-block-retries", type=int,
                   default=page_flow.BLOCK_RETRIES_WITH_POOL,
                   help="When a page comes back refused, retry it from this "
                        "many OTHER exits before giving up (default %d, from "
                        "page_flow.BLOCK_RETRIES_WITH_POOL — the site's "
                        "measured policy lives there rather than in three "
                        "copies of a literal). Needs a pool of more than one; "
                        "ignored otherwise. Worth less on this site than on "
                        "most in this family, and that is measured: one "
                        "datacentre address was served the full catalogue by "
                        "plain curl AND by headless Chromium, and refused "
                        "only when a curl handshake claimed a Chrome "
                        "User-Agent. What gets refused here is an "
                        "inconsistent CLIENT, not an address, so check for a "
                        "fingerprint you added before spending exits."
                        % page_flow.BLOCK_RETRIES_WITH_POOL)
    p.add_argument("--twocaptcha-key", default=None, help="2captcha.com API key")
    p.add_argument("--allow-empty", action="store_true",
                   help="Write output files even when 0 rows were found. Off by "
                        "default so a failed run can't overwrite a good result "
                        "with an empty one; exit code is 4 either way.")
    p.add_argument("--fingerprint", action="store_true",
                   help="Fetch a browser fingerprint from 2captcha's Fingerprint "
                        "API and apply it to the launched browser. Needs "
                        "--twocaptcha-key. Ignored with --cdp-endpoint, where the "
                        "Scraping Browser supplies its own.")
    # ONE OS-family tag, not a list — and the default is what makes
    # --fingerprint work at all. It shipped as "Windows,Chrome,Desktop" in
    # this family, which the API rejects with HTTP 400 ("Request parameters
    # are invalid"), so --fingerprint failed on every invocation. Measured
    # 2026-09-10: `Windows` succeeds, and `Windows,Chrome,Desktop`, `Chrome`
    # and `Desktop` each 400. fingerprint_client.py's own --tags help has
    # said so all along; the engines' default contradicted it.
    p.add_argument("--fp-tags", default="Windows",
                   help="ONE OS-family tag for the fingerprint filter: "
                        "Windows, Microsoft Windows or Android. NOT a list — "
                        "Chrome, Desktop and Mobile are each rejected by the "
                        "API with 400, and no combination is accepted. Use "
                        "--fp-country to narrow further. (default: Windows)")
    p.add_argument("--fp-country", default=None,
                   help="Fingerprint country, ISO 3166-1 alpha-2. Match it to "
                        "your proxy's exit country — a US fingerprint on a "
                        "German IP is a contradiction.")
    p.add_argument("--captcha-api", choices=["v2", "v1"], default="v2",
                   help="Which 2captcha solver API to use. v2 is the current "
                        "JSON API (api.2captcha.com/createTask); v1 is the "
                        "legacy in.php/res.php pair. Applies to both the image "
                        "captcha and reCAPTCHA.")
    p.add_argument("--solve-captcha", choices=["when-blocked", "always"],
                   default="when-blocked",
                   help="when-blocked (default): only pay to solve a "
                        "reCAPTCHA if the content is not already readable. "
                        "always: solve whenever one is detected. Neither "
                        "setting touches this site's refusal, which is not a "
                        "page at all — the HTTP/2 stream is reset and nothing "
                        "arrives — so no solve helps there and none is "
                        "attempted or billed. In fact NO challenge has ever "
                        "been observed on this site; the path is wired up "
                        "because a bot manager can be switched on between "
                        "deploys.")
    p.add_argument("--min-score", type=float, default=0.7,
                   help="reCAPTCHA v3 minimum score to request (0.3, 0.7 or 0.9 "
                        "— the API only accepts these three). Ignored for v2 "
                        "widgets.")
    p.add_argument("--cdp-endpoint", default=None,
                   help="Connect to an already-running browser over CDP instead "
                        "of launching Playwright's bundled Chromium, e.g. "
                        "ws://user:pass@host:port — the Scraping Browser API "
                        "endpoint, or any browser that exposes a CDP URL. "
                        "--proxy and --headless/--headful are ignored when this "
                        "is set.")
    p.add_argument("--dump-html", default=None, metavar="PATH",
                   help="Save the exact HTML the parser is given, on success as "
                        "well as failure. Useful when the row count is right but "
                        "a column comes back empty — see TROUBLESHOOTING.md.")
    # HEADLESS is the default, and here that is measured in BOTH directions
    # rather than inherited — §19's rule, because a sibling site answered 0
    # of 4 headless against 4 of 4 headful and the family default had been
    # headless simply because headless is what a scraper does.
    #
    # Measured 2026-09-21 from one datacentre address, and the answer is
    # PER-ROUTE, which is the part worth carrying forward:
    #
    #   search / item / category   headless 200 (855 KB, payload present)
    #                              headful  200 (969 KB, payload present)
    #   ranking.rakuten.co.jp      headless 403, 3 of 3 (one timeout first)
    #                              headful  200, 3 of 3, 416 KB
    #
    # So the routes this scraper reads do not care, and headless is right as
    # the default. The browser even announces itself on those routes — a
    # tracking beacon from the headless run carried
    # `HeadlessChrome;153.0.8010.12` — and Akamai served it anyway.
    #
    # But one route on this site does care, and it is the one this repo does
    # not read. "Does Rakuten block headless browsers?" therefore has no
    # single answer, which is why the measurement is written as a table
    # rather than as a sentence.
    p.add_argument("--headful", dest="headless", action="store_false",
                   help="Run with a real browser window. Not needed for the "
                        "routes this scraper reads — search, genre and item "
                        "pages were all served headless in testing. Worth "
                        "knowing that ranking.rakuten.co.jp, which this repo "
                        "does NOT read, answered 403 headless and 200 "
                        "headful from the same address, so if Rakuten ever "
                        "extends that to the listing routes this is the "
                        "flag to reach for.")
    p.add_argument("--headless", dest="headless", action="store_true",
                   default=True,
                   help="Run headless. THE DEFAULT. Ignored with "
                        "--cdp-endpoint, where the remote browser decides.")
    args = p.parse_args()
    # Fill --twocaptcha-key / --cdp-endpoint / --proxy / --url from the
    # environment or .env when the flag was not given. An explicit flag wins.
    env_config.apply(args)
    if not args.url:
        p.error("no --url given, and RAKUTEN_URL is not set in the "
                "environment or in .env.")
    if not is_supported_host(args.url):
        # Refused rather than attempted. The parser's grid containers, its
        # product-path pattern and its pagination convention are all
        # this site's, so pointing this at another marketplace would not fail
        # loudly — it would return zero rows and look like an empty category.
        # `unsupported_reason` names the host itself and says WHY — a real
        # Rakuten Group property like ranking.rakuten.co.jp or Rakuten Books
        # gets a different sentence from a typo, because "is not a Rakuten
        # site" would be FALSE for those and would send the reader hunting
        # for a misspelling that is not there (§5).
        p.error(unsupported_reason(args.url)
                or f"{args.url!r} is not a Rakuten Ichiba URL this scraper "
                   f"reads.")
    kind = listing_kind(args.url)
    if args.mode == "listing" and kind == "home":
        # Said as a warning rather than an error: it IS a Rakuten URL and
        # the run will honestly report zero rows (exit 4). But a reader who
        # tries the obvious front page first would otherwise conclude the
        # tool is broken, so name what happened.
        logger.warning(
            "%s is the mall FRONT PAGE, not a listing. It carries category "
            "tiles and campaign rails and no result grid, so this run will "
            "return 0 rows and exit 4. Pass a keyword search "
            "(https://search.rakuten.co.jp/search/mall/コーヒー/) or a genre "
            "(https://www.rakuten.co.jp/category/100356/) instead.",
            args.url)
    # The two modes take different ROUTES, and crossing them is the
    # likeliest first mistake: --mode listing on an item URL and --mode
    # product on a search URL both parse to zero rows and look like an
    # empty result. Named rather than attempted.
    if args.mode == "product" and kind != "item":
        p.error("--mode product needs an item page "
                "(item.rakuten.co.jp/SHOP/CODE/); %r is a %s URL. Use "
                "--mode listing for that." % (args.url, kind))
    if args.mode == "listing" and kind == "item":
        p.error("%r is one product page, and --mode listing reads result "
                "grids. Use --mode product for it." % (args.url,))
    return args


if __name__ == "__main__":
    args = parse_args()
    if args.fingerprint and not args.twocaptcha_key:
        logger.error("--fingerprint needs --twocaptcha-key (the Fingerprint API "
                     "uses the same key, though it's a separate subscription "
                     "from solving).")
        sys.exit(2)
    if args.fingerprint and args.cdp_endpoint:
        logger.warning("--fingerprint is ignored with --cdp-endpoint: the "
                       "Scraping Browser supplies its own fingerprint, and "
                       "stacking a second one on top creates a mismatch rather "
                       "than better cover.")
    try:
        sys.exit(scrape(args))
    except ProxyError as e:
        # Bad usage, not a crash: a typo in a proxy list would otherwise
        # surface as a connection failure on page 1 with nothing naming it.
        logger.error("%s", e)
        sys.exit(2)
    except PWError as e:
        # A remote browser that will not accept the connection is a REMOTE
        # API failure (exit 5), not a crash in this code (exit 1) and not bad
        # usage (exit 2). The distinction earns its keep on the commonest one:
        # `profile_locked` means another run still holds this `pid`, and a
        # harness that sees exit 1 goes looking for a bug in the scraper
        # instead of waiting or passing a different pid.
        text = _mask_credentials(str(e))
        if "profile_locked" in text or "connect to --cdp-endpoint" in text:
            logger.error("%s", text)
            sys.exit(EXIT_API_ERROR)
        raise
