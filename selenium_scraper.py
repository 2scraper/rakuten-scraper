#!/usr/bin/env python3
"""rakuten-scraper — Selenium edition

A parity engine, kept for stacks that already have Selenium. It must behave
identically to `playwright_scraper.py`: the same flags, the same rows in the
same column order, the same exit codes and the same run status. Verified on a
live run — see the README's engine-parity table for the numbers and the date.

Two hard limits of this driver, stated here rather than left to be
discovered:

* **It cannot use an authenticated remote CDP endpoint.** Playwright's
  `connect_over_cdp` and pyppeteer's `browserWSEndpoint` take a full
  `ws://user:pass@host:port` and authenticate on the WebSocket upgrade;
  chromedriver's `debuggerAddress` takes a bare `host:port` with nowhere to
  put a password. A credentialled `--cdp-endpoint` is refused here with that
  explanation rather than silently dropping the credentials.
* **`--proxy-server` cannot authenticate at all.** Credentials are stripped
  and a warning printed. The way round it is 2Captcha's IP-whitelist mode,
  which hands out `host:port` connections with no credentials in them.

And one thing about this driver on THIS site that is worth knowing before
reading a run's exit code: **Selenium cannot give you an HTTP status.**
Rakuten's 403 refusal and its 503 throttle render the IDENTICAL body and
differ only in status, so this engine cannot tell them apart from the page
alone. `product_parser.detect_page_state` therefore reads a statusless
branded refusal as `throttled` — the recoverable answer — because calling a
throttle a block spends the block budget on a page that was about to come
back, while calling a block a throttle costs one wait. The other two engines
read the status and get the exact answer.

Three things about the site itself shape every engine here: the rows come
from `window.__INITIAL_STATE__` rather than the DOM (so there is no scroll),
`networkidle` never settles because the ad beacons never stop, and HTTP 503
is a throttle rather than a refusal.

    python3 selenium_scraper.py \\
        --url "https://search.rakuten.co.jp/search/mall/コーヒー/" --pages 3
"""

import argparse
import logging
import re
import sys
import time
from dataclasses import dataclass, field
from typing import List, Optional
from urllib.parse import urlparse, urlsplit, parse_qsl

from selenium import webdriver
from selenium.common.exceptions import (TimeoutException, WebDriverException,
                                        JavascriptException)
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

from captcha_solver import (detect_recaptcha_v3, detect_recaptcha_in_page,
                            reconcile_detections, solve_recaptcha,
                            CaptchaUnsolvable, INJECT_TOKEN_JS)
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
from proxy_pool import (from_args as proxy_pool_from_args, mask, ROTATE_MODES,
                        ProxyError, split_credentials)
import env_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("selenium_scraper")

ITEM_LINK_SELECTOR = page_flow.READY_SELECTOR_LISTING

# A price-coverage floor. One number, not a per-section map, because on
# this site there is one answer: Rakuten is a shop and every listing names a
# price. Measured across 405 rows on 11 listing pages — keyword searches and
# genre listings, pages 1 through 150 — `price` was non-null on 405 of 405.
#
# So the floor is high on purpose. A run that comes back with 80% priced has
# not met an unusual page; it has a broken read, and the warning should say
# so rather than shrug.
PRICE_FLOOR = {"listing": 95}

# A page holding less than this share of the fullest page in the same run is
# reported as thin. This site's page size is steady (25 ads a page on
# captured and every live run), but the LAST page of a listing is
# legitimately short, so the bar stays loose.
THIN_PAGE_SHARE = 0.6

PAGE_LOAD_TIMEOUT = 60
SCRIPT_TIMEOUT = 30

# Chromium's own names for "the proxy is the problem, not the site". A dead
# proxy and a slow page want opposite responses — a different exit versus
# another try at the same one — so they are told apart by the error text.
_PROXY_ERROR_MARKERS = (
    "ERR_PROXY_CONNECTION_FAILED", "ERR_TUNNEL_CONNECTION_FAILED",
    "ERR_PROXY_AUTH_UNSUPPORTED", "ERR_PROXY_AUTH_REQUESTED",
    "ERR_UNEXPECTED_PROXY_AUTH", "ERR_PROXY_CERTIFICATE_INVALID",
)


@dataclass
class PageOutcome:
    """What one page produced. Mirrors playwright_scraper.PageOutcome."""
    page_num: int
    url: str
    final_url: Optional[str] = None
    products: List = field(default_factory=list)
    blocked_by: Optional[str] = None
    load_failed: bool = False
    state: Optional[str] = None
    # Populated here: the SSR payload states `total` and `lotsPerPage` on
    # page 1, which is what makes the page count arithmetic.
    total_available: Optional[int] = None
    # The raw result-count header, verbatim, for the sidecar.
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
    # In --mode product, the seller's own id/name/slug from the page's Apollo
    # cache.
    shop_facts: Optional[dict] = None

    @property
    def ok(self) -> bool:
        return not self.load_failed and self.blocked_by is None


# Every `scheme://user:pass@` in a string, however many times it occurs.
# Matching globally rather than once is the point: a driver's connection
# error can repeat the endpoint several times (the message plus a call log),
# so a masker that handled only the first occurrence would print the password
# the other times and look like it was working.
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


def _chrome_ua(version: str) -> str:
    """A desktop-Chrome UA naming the browser's OWN real version.

    `driver.capabilities["browserVersion"]` is the installed Chrome's version,
    so the claim matches what the JS engine and the TLS handshake report. A
    hardcoded number drifts the moment Chrome updates, and claiming an older
    Chrome than everything else reports is itself a signal.
    """
    return (f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{version} Safari/537.36")


def _cdp_host_port(endpoint: str) -> str:
    """`host:port` for chromedriver's debuggerAddress, or exit 2 with a reason.

    chromedriver takes a bare address here and cannot send credentials, so an
    endpoint that carries them cannot work through this engine. Refused up
    front: connecting anyway would fail somewhere further in with an error
    that names none of this.
    """
    parts = urlsplit(endpoint if "//" in endpoint else f"//{endpoint}")
    if parts.username or parts.password:
        logger.error(
            "This --cdp-endpoint carries credentials (%s), and Selenium cannot "
            "send them: chromedriver's debuggerAddress is a bare host:port. "
            "Use playwright_scraper.py or puppeteer_scraper.py for a "
            "credentialed endpoint such as the Scraping Browser API — both "
            "authenticate on the WebSocket upgrade.",
            _mask_credentials(endpoint))
        sys.exit(2)
    host = parts.hostname or endpoint
    port = f":{parts.port}" if parts.port else ""
    return f"{host}{port}"


class _Session:
    """One Chrome driver, relaunchable onto a different exit.

    Same contract as the Playwright engine's _BrowserSession, including the
    rule that a rotation means a genuinely FRESH browser — and a
    fresh browser is also the only thing that re-rolls the served page
    fresh cookie jar is what an ordinary user on another network looks like.
    """

    def __init__(self, args, pool):
        self.args, self.pool = args, pool
        self.remote = bool(args.cdp_endpoint)
        self.driver = None

    def open(self):
        options = Options()
        if self.remote:
            options.debugger_address = _cdp_host_port(self.args.cdp_endpoint)
            logger.info("Attaching to an existing browser at %s.",
                        options.debugger_address)
            # No UA, no proxy, no fingerprint on this path: the remote browser
            # brings its own, and stacking a second creates a contradiction
            # rather than better cover.
            self.driver = webdriver.Chrome(options=options)
            self._apply_timeouts()
            return self

        if self.args.headless:
            options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--window-size=1600,1000")
        if self.args.locale:
            # APPLIED, not merely accepted. A flag that is declared and then
            # never read is worse than a flag that is absent: it looks
            # configurable and is not, which is the defect this family keeps
            # finding in its own copied core (§17).
            options.add_argument(f"--lang={self.args.locale}")
            options.add_experimental_option(
                "prefs", {"intl.accept_languages": self.args.locale})
        # Not a fingerprint measure, a correctness one: without it Chrome
        # advertises "HeadlessChrome", which is a giveaway on any site with
        # a bot manager in front of it.
        options.add_argument("--disable-blink-features=AutomationControlled")

        if self.pool:
            scrubbed, credentials = split_credentials(self.pool.current)
            options.add_argument(f"--proxy-server={scrubbed}")
            logger.info("Using proxy exit %s", mask(self.pool.current))
            if credentials:
                logger.warning(
                    "This proxy has credentials and SELENIUM CANNOT SEND "
                    "THEM: --proxy-server accepts an address only, and there "
                    "is no Selenium equivalent of pyppeteer's "
                    "page.authenticate. They have been stripped, so requests "
                    "will go out unauthenticated and the exit will most "
                    "likely refuse them. Use playwright_scraper.py or "
                    "puppeteer_scraper.py for an authenticated proxy.")

        self.driver = webdriver.Chrome(options=options)
        self._apply_timeouts()

        version = self.driver.capabilities.get("browserVersion", "")
        if version:
            # Set over CDP rather than as a launch switch, so it can use the
            # version the driver actually reports.
            try:
                self.driver.execute_cdp_cmd(
                    "Network.setUserAgentOverride",
                    {"userAgent": _chrome_ua(version)})
            except WebDriverException as e:
                logger.debug("Could not override the user agent: %s", e)

        if self.args.fingerprint:
            self._apply_fingerprint()
        return self

    def _apply_timeouts(self):
        # Explicit, because a driver that stops answering otherwise hangs the
        # run: "every remote call is bounded" applies to this engine too.
        self.driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT)
        self.driver.set_script_timeout(SCRIPT_TIMEOUT)

    def _apply_fingerprint(self):
        from fingerprint_client import get_fingerprint, playwright_init_script
        fp = get_fingerprint(self.args.twocaptcha_key, tags=self.args.fp_tags,
                             country=self.args.fp_country)
        ua = (fp.get("userAgent") or {}).get("value")
        script = playwright_init_script(fp)
        try:
            if ua:
                self.driver.execute_cdp_cmd("Network.setUserAgentOverride",
                                            {"userAgent": ua})
            # The same patch script the Playwright engine installs on its
            # context. Shared deliberately: two engines applying different
            # halves of one fingerprint would be a contradiction of exactly
            # the kind a fingerprint is meant to avoid.
            self.driver.execute_cdp_cmd(
                "Page.addScriptToEvaluateOnNewDocument", {"source": script})
            logger.info("Using 2captcha fingerprint %s (%s)", fp.get("id"),
                        fp.get("country"))
        except WebDriverException as e:
            logger.warning("Could not apply the fingerprint over CDP (%s) — "
                           "continuing without it.", e)

    def relaunch(self):
        if self.remote:
            return
        self.close()
        self.open()

    def close(self):
        try:
            if self.driver is not None:
                # quit(), not close(): close() ends one window and leaves the
                # driver process running, which on a per-page rotation would
                # leak a chromedriver per page.
                self.driver.quit()
        except Exception as e:  # noqa: BLE001 — teardown must not mask the reason we're here
            logger.debug("Ignoring error during driver teardown: %s", e)


# ---------------------------------------------------------------------------
# page_flow, bound to Selenium
# ---------------------------------------------------------------------------
# Only "how to ask this driver" lives here. Note the JS dialect: Selenium's
# execute_script runs a function BODY and needs an explicit `return`, unlike
# the `() => expr` both other engines take — which is why page_flow names
# operations instead of passing JavaScript.
def _driver(session):
    driver = session.driver

    def count(selector):
        try:
            return len(driver.find_elements(By.CSS_SELECTOR, selector))
        except WebDriverException as e:
            logger.debug("count(%s) failed: %s", selector, e)
            return 0

    def sleep(ms):
        time.sleep(ms / 1000.0)

    def content():
        try:
            return driver.page_source
        except WebDriverException as e:
            # A geo-redirect or the consent layer can navigate, so a
            # snapshot can land on the document swap. None tells the caller to
            # skip a check rather than fail the run.
            logger.debug("page_source unavailable (page navigating?): %s", e)
            return None

    def current_url():
        try:
            return driver.current_url
        except WebDriverException:
            return ""

    # No scroll primitives and no `page_height`, and that is measured on
    # THIS site rather than inherited: the same listing URL fetched by plain
    # HTTP with no JavaScript at all and by a real browser parsed to the
    # same 45 rows, with identical skus and prices. Every column comes out
    # of `window.__INITIAL_STATE__`, which is in the first response, so a
    # scroll here would be latency bought for nothing.
    return {"count": count, "sleep": sleep, "content": content,
            "current_url": current_url}


def _parse_for_mode(html: str, url: str, args, page_num: int = 1) -> List:
    """Rows for this page, always as a list.

    The two modes read two different structures out of two different page
    kinds, and routing them through one helper keeps everything downstream —
    dedupe, merge, coverage logging, the writers — working on one shape.

    `page_num` is threaded through rather than defaulted, because `position`
    restarts at 1 on every page: without the page number beside it, a row
    from page 2 claims the same position as one from page 1 and the two are
    indistinguishable in the output (§18).

    `currency` comes from page 1 and is carried forward, because Rakuten
    publishes the JSON-LD block that states it on PAGE 1 ONLY — 1 block on
    page 1, 0 on pages 2 and 3 of the same query. Read once, never
    defaulted.
    """
    if args.mode == "product":
        return parse_product_page(html, url, page=page_num,
                                  category=args.category)
    return parse_products(html, url, page=page_num, category=args.category,
                          currency=getattr(args, "_currency", None))


def _same_url(a: str, b: str) -> bool:
    """Whether two URLs address the same page.

    Delegates to page_flow rather than reimplementing the comparison, so all
    three engines cannot drift on it. In particular this site writes some of its
    next-links percent-DECODED (".../kühlen-gefrieren-32.html") while a
    pasted URL is encoded (".../k%C3%BChlen-gefrieren-32.html"); an engine
    with its own copy of this got that wrong and silently fell back to
    sequential fetching on every accented category.
    """
    return page_flow.comparable(a) == page_flow.comparable(b)



def _next_page_candidates(session, page_num: int) -> List[str]:
    """The site's own next-page link, resolved by the browser, or None.

    Returns EVERY candidate, filtered by page_flow to the ones that really do
    paginate this listing: the SEO chip rail advertises other listings
    alongside its items, and following that one returns rows from the wrong
    listing while reporting success.

    Reads the DOM's `.href` property, which is already absolute — the
    opposite of Playwright's get_attribute("href"), which returns the raw
    attribute. Kept explicit because the engines differ here.

    Note the JS is a function BODY with an explicit `return`, not the arrow
    expression the other two engines pass. That difference is exactly why no
    JavaScript crosses the page_flow boundary.
    """
    try:
        hrefs = session.driver.execute_script(
            "return Array.from(document.querySelectorAll(arguments[0]))"
            ".map(a => a.href || a.getAttribute('href')).filter(Boolean);",
            page_flow.next_page_selector(page_num))
    except WebDriverException:
        return []
    return page_flow.next_page_candidates(session.driver.current_url,
                                          hrefs or [])


def handle_captcha_if_present(session, args) -> bool:
    """Detect and solve a challenge. True if something was solved.

    Same detectors, same reconciliation and the same "detected is not
    blocking" rule as the Playwright engine — the three must agree about
    when a run spends money.

    NOTE what this cannot help with: this site's refusal is an HTTP 403 with
    403 carrying its own error page with no challenge on it, so no solve
    applies there and none is attempted. See product_parser.detect_page_state.
    """
    driver = session.driver
    d = _driver(session)
    html = d["content"]()
    if html is None:
        return False

    selector = page_flow.ready_selector(args.mode)
    already_rendered = d["count"](selector)
    when_blocked = getattr(args, "solve_captcha", "when-blocked") == "when-blocked"

    html_challenge = detect_recaptcha_v3(html, d["current_url"]())
    runtime_challenge = detect_recaptcha_in_page(
        lambda js: driver.execute_script(f"return ({js})();"),
        page_url=d["current_url"]())
    challenge = reconcile_detections(html_challenge, runtime_challenge)
    if not challenge:
        return False
    if when_blocked and already_rendered > MIN_CARD_MATCHES:
        logger.info("%s detected via %s, but %d anchors are already on the "
                    "page — not solving it.", challenge.kind, challenge.source,
                    already_rendered)
        return False
    logger.warning("%s detected via %s (sitekey=%s) — attempting to solve.",
                   challenge.kind, challenge.source, challenge.sitekey)
    if not args.twocaptcha_key:
        logger.warning("No 2captcha API key, so this challenge cannot be solved.")
        return False
    try:
        token = solve_recaptcha(challenge, args.twocaptcha_key,
                                api_version=args.captcha_api,
                                min_score=args.min_score)
    except Exception as e:  # noqa: BLE001
        logger.error("Solving the challenge failed (%s).", e)
        return False
    try:
        driver.execute_script(f"return ({INJECT_TOKEN_JS})(arguments[0]);", token)
    except WebDriverException as e:
        logger.error("Could not inject the token (%s).", e)
        return False
    logger.info("Token injected. Reloading page to continue.")
    time.sleep(1.5)
    driver.refresh()
    return True


def _fetch_one_page(session, args, pool, page_num: int, url: str) -> PageOutcome:
    """Fetch and parse one page. Mirrors playwright_scraper._fetch_one_page.

    Kept structurally parallel to its twins on purpose — "all three engines
    agree" is checked by reading them side by side as well as by the smoke
    suite.
    """
    outcome = PageOutcome(page_num=page_num, url=url)
    d = _driver(session)
    html, state, load_failed = None, "ok", False

    # See the Playwright engine for the measurement: without a pool there is
    # no exit to rotate to, but a plain re-fetch is what clears a block on a
    # Scraping Browser profile, so the budget is not zero.
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
    # A THROTTLE's own budget, kept apart from the block budget
    # because the right response is the opposite one — see the
    # `throttled` branch below.
    throttle_spent = 0

    # The loop may be re-entered for a throttle without consuming a
    # block attempt, so the budget is a while rather than a for.
    block_attempt = -1
    while block_attempt < block_retries:
        block_attempt += 1
        logger.info("Fetching page %d/%d: %s", page_num, args.pages, url)
        load_failed, exit_failed = False, None
        for attempt in range(1, args.retries + 1):
            try:
                session.driver.get(url)
                load_failed = False
                break
            except (TimeoutException, WebDriverException) as e:
                text = str(e)
                reason = next((m for m in _PROXY_ERROR_MARKERS if m in text), "")
                load_failed = True
                if reason:
                    exit_failed = reason
                    break  # a different exit is the only thing that helps
                if attempt < args.retries:
                    pause = args.retry_delay * (2 ** (attempt - 1))
                    logger.warning("Failed to load %s (attempt %d/%d: %s) — "
                                   "retrying in %.1fs.", url, attempt,
                                   args.retries, text[:120], pause)
                    time.sleep(pause)

        if exit_failed and has_pool and block_attempt < block_retries:
            logger.warning("Exit %s is unusable (%s) — rotating to another "
                           "one (%d/%d).", mask(pool.current), exit_failed,
                           block_attempt + 1, block_retries)
            pool.advance(f"unusable exit: {exit_failed}")
            session.relaunch()
            d = _driver(session)
            continue
        if load_failed:
            break


        # Counted against the SAME budget as the post-classification call
        # below, through page_flow.solve_budget. The ancestor of this engine
        # called the solver here WITHOUT counting and counted only at the
        # second call site, so `SOLVES_PER_PAGE = 1` read like an enforced
        # cap in every repo of this family while one page could buy two
        # solves — measured at three on a site where a challenge rendered on
        # every fetch (§23). Invisible wherever a challenge is rare, which
        # is everywhere until it is not.
        if page_flow.solve_budget(solves_bought):
            if handle_captcha_if_present(session, args):
                solves_bought += 1
                time.sleep(1)

        html = d["content"]() or ""
        state = page_flow.classify(html, url=d["current_url"]())

        # "Not painted yet" is not a fault, and on this site it is also
        # not the usual case: a listing page carries all 45 of its products
        # in the first response, so `shell` means the payload was genuinely
        # absent rather than late. Waiting is still the right answer for it
        # — refetching a shell just buys another shell — and re-classifying
        # BEFORE the retry decision is what stops a slow page spending a
        # block retry. See page_flow.is_unpainted.
        if page_flow.is_unpainted(state, html):
            wait_s = page_flow.content_timeout_ms(args.mode) / 1000.0
            sel = page_flow.ready_selector(args.mode)
            need = page_flow.min_matches(args.mode, page_flow.expected_cards(html))
            logger.info("Page %d is a shell the site served but has not "
                        "filled in (%d bytes, empty cards) — waiting up to "
                        "%.0fs for the prices rather than spending a retry.",
                        page_num, len(html), wait_s)
            found = page_flow.wait_for_count(d["count"], d["sleep"], sel,
                                             need, int(wait_s * 1000))
            if found < need:
                logger.info("The grid still had not painted after %.0fs "
                            "(%d match(es)).", wait_s, found)
            html = d["content"]() or html
            state = page_flow.classify(html, url=d["current_url"]())

        # No interstitial-settling step, and its absence is measured rather
        # than an omission: this site has no interstitial. A refused request
        # gets a 394-byte "Access Denied" with nothing on it to settle, and
        # what triggers it is the browser being headless, not the address. See page_flow's "There is no block page".
        #
        # The paid path is reached only for state "challenge", which no
        # capture of this site has ever produced. Wired up because a bot
        # manager can be switched on between deploys, and bounded by
        # SOLVES_PER_PAGE so a speculative path cannot become a bill.
        if (page_flow.should_solve(state)
                and page_flow.solve_budget(solves_bought)):
            solves_bought += 1
            if handle_captcha_if_present(session, args):
                time.sleep(1)
                html = d["content"]() or html
                state = page_flow.classify(html, url=d["current_url"]())
                if state == "content":
                    logger.info("The solve was accepted — page %d is content "
                                "now.", page_num)
                else:
                    logger.warning("The solve was NOT accepted: page %d is "
                                   "still %s. The purchase is spent.",
                                   page_num, state)

        if not page_flow.should_retry(state):
            # "content" and "empty" are both final answers. An empty page is
            # a CORRECT one — a hub category has no grid — so retrying it
            # would re-confirm the same right answer, and rotating the exit
            # would blame an address for the URL it was given.
            break

        if state == "throttled":
            # Wait longer at the SAME exit, on the throttle's own budget.
            # Rakuten answers a client going too fast with HTTP 503 and its
            # own branded page — and serves the IDENTICAL body under 403 on a
            # route it refuses outright, so the status is the only thing
            # separating them. Deliberately does not consume a block attempt
            # and does not rotate: moving exit would abandon an address that
            # was working, and counting it as blocked would report exit 3 for
            # a page that was about to come back. Measured: a URL that had
            # just 503'd twice came back 200 with a full payload on 4 of 4
            # tries once 6 seconds were put between requests.
            if throttle_spent < page_flow.THROTTLE_RETRIES:
                throttle_spent += 1
                pause = page_flow.throttle_delay_ms(throttle_spent) / 1000
                logger.warning(
                    "Page %d came back throttled (HTTP 503, Rakuten's own "
                    "\"access is concentrated\" page). That is a rate limit "
                    "rather than a refusal, so waiting %.0fs and re-fetching "
                    "from the same exit (%d/%d). Raise --delay if it keeps "
                    "happening.", page_num, pause, throttle_spent,
                    page_flow.THROTTLE_RETRIES)
                time.sleep(pause)
                block_attempt -= 1
                continue
            logger.error(
                "Page %d is still throttled after %d wait(s). Rakuten is "
                "rate-limiting this client rather than refusing it, so the "
                "fix is fewer requests (raise --delay, lower --concurrency) "
                "rather than another exit.", page_num, throttle_spent)
            break

        # Blocked or challenged. The ADDRESS is what was scored, not the URL,
        # so a different exit is the only thing that plausibly changes the
        # outcome.
        if block_attempt < block_retries:
            if pool is not None:
                logger.warning("Page %d came back as %s from %s — retrying "
                               "from another exit (%d/%d).", page_num, state,
                               mask(pool.current), block_attempt + 1,
                               block_retries)
                pool.advance(f"{state} on page {page_num}")
                session.relaunch()
                d = _driver(session)
            else:
                # No pool, so nowhere else to go — but a plain re-fetch does
                # clear this sometimes, and this branch is REACHABLE here:
                # `page_flow.BLOCK_RETRIES_WITHOUT_POOL` is 1 on this site,
                # against 0 in the sibling repo this engine was ported from,
                # and `mask(pool.current)` on a None pool took a live run
                # down with an AttributeError the moment it was. Mirrors
                # playwright_scraper exactly.
                pause = args.retry_delay * (block_attempt + 1)
                logger.warning("Page %d came back as %s — re-fetching through "
                               "the same access path in %.1fs (%d/%d).",
                               page_num, state, pause, block_attempt + 1,
                               block_retries)
                time.sleep(pause)

    if load_failed:
        logger.error("Gave up loading %s after %d attempt(s).", url, args.retries)
        outcome.load_failed = True
        return outcome

    outcome.state = state

    if state == "blocked":
        # There is no challenge on this site to solve. Akamai answers a
        # refused request with HTTP 403 and a 394-byte "Access Denied" that
        # carries a reference id and nothing else, so a 2Captcha key does not
        # help and a REAL WINDOW does — not a better address. The dump is written even when empty: "0 bytes" is itself
        # the diagnosis here.
        debug_html = f"{args.out}_page{page_num}_debug.html"
        with open(debug_html, "w", encoding="utf-8") as f:
            f.write(html or "")
        logger.error(
            "The site did not serve this request — %d bytes, %s the site's "
            "own asset host, saved to %s. Akamai refuses this site with a "
            "43-byte body whose whole content is `Reference  #18.\u2026`, and "
            "it arrives under HTTP **200** — so the status code is not the "
            "signal and there is no widget on the page for any solver. What "
            "clears it is usually NOT a different address, measured "
            "2026-09-21: one datacentre IP was served the full 45-product "
            "page both by plain curl and by headless Chromium, and refused "
            "only when a curl handshake claimed a Chrome User-Agent. What "
            "Akamai refuses here is the CONTRADICTION between a claimed "
            "client and the TLS fingerprint underneath it, so check for a "
            "fingerprint you added before buying an exit. This is exit 3, "
            "distinct from a genuinely empty result (exit 4).",
            len(html or ""),
            "which references" if served_by_rakuten(html or "")
            else "with no reference to", debug_html)
        outcome.blocked_by = "no-response" if not html else "not-served"
        outcome.final_url = d["current_url"]()
        return outcome


    if state == "content":
        # Wait for paint, and NO SCROLL — which is measured rather than
        # omitted. A Rakuten listing page carries all 45 of its products in
        # the first response, inside `window.__INITIAL_STATE__`: the same
        # URL fetched by plain HTTP with no JavaScript at all and by a real
        # browser parsed to 45 rows each, with identical skus and prices. A
        # scroll here would be latency bought for nothing.
        selector = page_flow.ready_selector(args.mode)
        threshold = page_flow.min_matches(args.mode,
                                          page_flow.expected_cards(html))
        timeout_s = page_flow.content_timeout_ms(args.mode) / 1000.0
        # A POLL through the shared helper, so all three engines wait the
        # same way. This engine could use WebDriverWait — it never evaluates
        # a string — but a shared wait is one fewer thing to drift on, and
        # the other two cannot: a CSP without `unsafe-eval` refuses it.
        found = page_flow.wait_for_count(d["count"], d["sleep"], selector,
                                         threshold,
                                         int(timeout_s * 1000))
        time.sleep(0.5)
        if found < threshold:
            # Not an error on its own, and what it MEANS depends on the
            # mode — which is why the message does too. A listing page with
            # no grid is a correct answer (a taxonomy hub, or one page past
            # the end); a detail page whose buy box never painted is a
            # different thing entirely, and on this site it is usually just
            # slow rather than absent, because the row is parsed out of the
            # page's JSON-LD and not out of the buy box.
            logger.info("No listing tiles appeared within %.0fs. Not "
                        "necessarily a problem: every column comes out of "
                        "the payload rather than the DOM, so a wait that "
                        "times out costs the wait and nothing else. If this "
                        "URL is the mall front page, that is the expected "
                        "answer and the run will report 0 rows (exit 4).",
                        timeout_s)

        html = d["content"]() or html

    if args.dump_html:
        dump_path = (args.dump_html if args.pages == 1
                     else f"{args.dump_html}.page{page_num}")
        with open(dump_path, "w", encoding="utf-8") as f:
            f.write(html)
        logger.info("Saved the snapshot the parser sees to %s (%d bytes).",
                    dump_path, len(html))

    # Only when the page is NOT already content. A challenge marker on a page
    # whose ads have rendered guards nothing — and over --cdp-endpoint the
    # Scraping Browser's own auto-solve extension injects such markers into
    # every page it loads, which is why the scan strips extension <script>
    # tags first. Only for a state page_flow already counts as BLOCKED; an
    # EMPTY page is a correct answer. Mirrors playwright_scraper exactly.
    vendor = (detect_bot_challenge(html, url=d["current_url"]())
              if page_flow.counts_as_blocked(state) else None)
    if vendor:
        debug_html = f"{args.out}_page{page_num}_debug.html"
        with open(debug_html, "w", encoding="utf-8") as f:
            f.write(html)
        try:
            session.driver.save_screenshot(f"{args.out}_page{page_num}_debug.png")
        except WebDriverException as e:
            logger.warning("Could not capture screenshot: %s", e)
        logger.error("Blocked by %s before parsing (%d bytes) — saved to %s. "
                     "This is exit 3, distinct from a genuinely empty result "
                     "(exit 4).", vendor, len(html), debug_html)
        outcome.blocked_by = vendor
        logger.error("%s", page_flow.block_advice(
            html, headless=bool(getattr(args, "headless", False)),
            has_pool=has_pool))
        return outcome

    final_url = d["current_url"]() or url
    # Did the SERVER serve the page we asked for? Read off
    # `pagination.start`, never trusted from the request, because neither
    # out-of-range route on this site fails in a way the client can see:
    # `?p=151` of a 150-page query answers 301 to page 1 and then HTTP 200
    # with 45 real products, and `/category/{id}/?p=2` answers 200 with page
    # 1 without even redirecting (§23).
    if args.mode == "listing":
        # Compared against the page the fetched URL actually ASKS for, not
        # against the loop's counter — a run started on a URL that already
        # carries `?p=2` asks the site for page 2 while calling it page 1 of
        # the run. See page_flow.requested_page_number.
        asked = page_flow.requested_page_number(url, page_num)
        outcome.served_requested_page = page_flow.served_the_page_asked_for(
            html, asked)
        if not outcome.served_requested_page:
            logger.info(
                "Asked Rakuten for page %d and it served page %s — its own "
                "`pagination.start` says so. That is the end of this "
                "listing; Rakuten re-serves page 1 with HTTP 200 rather than "
                "erroring. Dropping this page's rows, which are duplicates.",
                asked, page_flow.served_page_number(html))
            outcome.state = "empty"
            return outcome

    # Through the POLICY rather than unconditionally. `STATE_POLICY` is the
    # one place that says which states are worth reading, and until now
    # nothing consulted its `parse` column: the engines parsed whatever
    # reached this line, so every state without an earlier `return` was
    # read regardless of what the table said.
    #
    # # Latent here rather than live — measured on this repo's own fixtures on
    # 2026-09-23: no shipped fixture of a parse:False state yields a row,
    # because the parser is independently defensive. The gate is wired
    # anyway, because "the parser happens to return nothing" is not the
    # same guarantee as "the policy says do not read this", and two
    # siblings had exactly this shape turn into phantom rows.
    #
    # Measured across the family on 2026-09-23 by counting definitions
    # against readers: 7 of 24 repos defined `should_parse` and none of
    # them called it.
    products = (_parse_for_mode(html, final_url, args, page_num)
                if page_flow.should_parse(state) else [])
    logger.info("Parsed %d row(s) from page %d.", len(products), page_num)

    if args.mode == "listing" and page_num == 1:
        outcome.total_available = total_results(html)
        outcome.header = search_header(html)
        outcome.cap = page_flow.cap_summary(html)
        # Read once from page 1 and carried forward: Rakuten publishes the
        # JSON-LD block that names the currency on page 1 only, so without
        # this the first 45 rows carried "JPY" and the rest carried null for
        # products whose currency the site had already stated. Never
        # defaulted — a run that never got page 1 leaves it null.
        stated = currency_from_page(html)
        if stated:
            args._currency = stated
            logger.info("Page 1's structured data states the currency as %s "
                        "— carrying it to the rest of the run.", stated)
        if capped_by_site(html):
            logger.info(
                "Rakuten says this query matches %s products and will serve "
                "%s of them. A run that reaches the last page is COMPLETE as "
                "far as the site is concerned and is a sample of what it "
                "matched — the sidecar records both numbers.",
                f"{outcome.total_available:,}", f"{reachable_max(html):,}")

    if products and args.mode == "listing":
        priced = sum(1 for p in products if p.price is not None)
        share = 100.0 * priced / len(products)
        floor = PRICE_FLOOR.get(args.mode, 0)
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
        rated = sum(1 for p in products if p.rating is not None)
        logger.info(
            "Rating coverage on page %d: %d/%d (%.0f%%). A null here means "
            "nobody has reviewed the product — Rakuten writes that as "
            "`{score: 0, numReviews: 0}` and both columns are nulled "
            "together, on 28 of 405 measured rows.",
            page_num, rated, len(products), 100.0 * rated / len(products))

    if not products:
        debug_html = f"{args.out}_page{page_num}_debug.html"
        with open(debug_html, "w", encoding="utf-8") as f:
            f.write(html)
        try:
            session.driver.save_screenshot(f"{args.out}_page{page_num}_debug.png")
        except WebDriverException as e:
            logger.warning("Could not capture screenshot: %s", e)
        logger.warning("0 rows parsed — saved what the browser actually saw to "
                       "%s.", debug_html)

    outcome.products = products
    outcome.final_url = final_url
    return outcome


def scrape(args) -> int:
    outcomes: List[PageOutcome] = []
    seen_keys = set()
    blocked = False
    # Both modes are one row per product, so `sku` is the key for both.
    dedupe_key = "sku"
    # Only --mode product is single-page: one item page. A listing
    # paginates, so it must never be treated as single-page — that would
    # fetch one page and report "complete", the silent-success failure this
    # family exists to avoid.
    stop_reason = "completed"

    pool = proxy_pool_from_args(args)
    if pool and args.cdp_endpoint:
        logger.warning("Ignoring --proxy/--proxy-file: with --cdp-endpoint the "
                       "remote browser has its own exit, and layering a second "
                       "proxy on top would contradict it.")
        pool = None
    if args.concurrency > 1:
        logger.warning("--concurrency is ignored in this engine: parallel page "
                       "fetching is implemented in playwright_scraper.py, "
                       "which is the primary engine. Running one page at a "
                       "time.")

    session = None
    try:
        session = _Session(args, pool).open()
        first = _fetch_one_page(session, args, pool, 1, args.url)
        outcomes.append(first)

        if not first.ok:
            stop_reason = ("page_load_timeout" if first.load_failed
                           else f"blocked_{first.blocked_by}")
            blocked = first.blocked_by is not None
        elif args.mode == "listing":
            seen_keys.update(p.sku for p in first.products if p.sku is not None)

            planned = None
            if args.pages > 1:
                page_one = first.final_url or args.url
                constructed = page_url(page_one, 2)
                candidates = _next_page_candidates(session, 1)
                if candidates and not page_flow.pagination_agrees(
                        page_one, 1, candidates):
                    logger.info("The site's own next-page link (%s) is not "
                                "what the page convention would build (%s) — "
                                "following its links one page at a time.",
                                candidates[0], constructed)
                else:
                    if not candidates:
                        logger.info(
                            "No pagination link for page 2 was found on page "
                            "1 — using the URL convention. That is EXPECTED "
                            "on this site: it publishes no rel=next "
                            "anywhere, and ?page=N addresses every page "
                            "correctly.")
                    planned = [page_url(page_one, n)
                               for n in range(2, args.pages + 1)]

            first_candidates = _next_page_candidates(session, 1)
            url = (planned[0] if planned else
                   (first_candidates[0] if first_candidates
                    else page_url(session.driver.current_url, 2)))
            for page_num in range(2, args.pages + 1):
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

                fresh_count = sum(1 for p in outcome.products
                                  if p.sku is None or p.sku not in seen_keys)
                seen_keys.update(p.sku for p in outcome.products
                                 if p.sku is not None)
                # The site said it served a different page than the one
                # asked for. A distinct stop_reason from `no_new_products`:
                # this one is "the listing ended and Rakuten answered with
                # page 1 anyway". Checked FIRST, since a re-served page 1
                # would trip the dedupe-based terminator too and report the
                # vaguer reason.
                if not outcome.served_requested_page:
                    stop_reason = "end_of_listing"
                    break
                if not fresh_count:
                    logger.info("Page %d added no rows not already seen — "
                                "treating that as the end of the listing.",
                                page_num)
                    stop_reason = "no_new_products"
                    break

                if page_num < args.pages:
                    nxt = _next_page_candidates(session, page_num)
                    url = (planned[page_num - 1] if planned else
                           (nxt[0] if nxt else
                            page_url(session.driver.current_url, page_num + 1)))
                    time.sleep(args.delay)
    finally:
        if session is not None:
            session.close()

    all_rows = []
    merged_seen = set()
    for oc in sorted(outcomes, key=lambda o: o.page_num):
        fresh = dedupe_by_key(oc.products, merged_seen, key=dedupe_key)
        if len(fresh) < len(oc.products):
            logger.info("Page %d: dropped %d duplicate row(s).",
                        oc.page_num, len(oc.products) - len(fresh))
        all_rows.extend(fresh)

    # Completeness, checked over the MERGED result rather than per page — a
    # per-page check cannot see a gap BETWEEN two pages, which is exactly
    # where a short page hides.
    #
    # NOT "pages x rows-per-page", which is what the sibling repo does and
    # what fires on healthy runs here. This site's page size is steady:
    # a three-page run returned 62, 60 and 62 rows, and multiplying the
    # largest page by the page count then declared the run short by 8. A
    # threshold that warns on every healthy run teaches the reader to ignore
    # it.
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

            # The pages the CATALOGUE has that the site will NOT address.
            # A run that stops at the cap is complete as far as the site is
            # concerned and truncated as far as the catalogue is, and only
            # saying so lets a consumer tell the two apart.
            beyond = (max(0, -(-total_available // fullest) - parser_page_cap)
                      if fullest else 0)
            if beyond:
                logger.warning(
                    "This query is %d page(s) deeper than Rakuten will "
                    "address. It caps every query at 6,750 results — 150 "
                    "pages of 45 — however many it matched, and states that "
                    "itself as `pagination.subset`. A request past page 150 "
                    "does not fail: it redirects to page 1 and serves it "
                    "with HTTP 200. Narrow the query with the site's own "
                    "filters — genre, price band, shop, tag — and run each "
                    "slice.", beyond)


    ok_pages = [o for o in outcomes if o.ok]
    failed_pages = [o.page_num for o in outcomes if not o.ok]
    final_url = (max(ok_pages, key=lambda o: o.page_num).final_url
                 if ok_pages else args.url)

    # One-per-run context, in the sidecar rather than repeated down a column,
    # and IDENTICAL to the other two engines — a sidecar that differs between
    # engines is the drift `finish_run()` exists to prevent, one level up.
    #
    # This carried a sibling's keys (`scroll`, `result_header`,
    # `pages_still_growing`) until an audit diffed the three engines' actual
    # sidecars: `scroll` is meaningless here (this site serves its whole page
    # at once) and the cap arithmetic was MISSING, which is the figure that
    # keeps `status: complete` honest on a site that serves 6,750 of
    # 3,000,000 matches.
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
                      # `rakuten.co.jp`, matching every ROW's `source` — a run
                      # can start on www.rakuten.co.jp and
                      # finish on search.rakuten.co.jp,
                      # because page_url rewrites a genre
                      # landing page onto the search host.
                      source=SOURCE_DEFAULT,
                      start_url=args.url, final_url=final_url,
                      extra=extra)


def parse_args():
    p = argparse.ArgumentParser(
        description="Rakuten Ichiba scraper (Selenium edition). Cannot authenticate a "
                    "proxy or a remote CDP endpoint — see the module "
                    "docstring; playwright_scraper.py is the primary engine.")
    p.add_argument("--url", default=None,
                   help="A Rakuten Ichiba URL. In --mode listing: a "
                        "keyword search "
                        "(search.rakuten.co.jp/search/mall/KEYWORD/), a "
                        "genre listing (.../search/mall/-/GENREID/) or a "
                        "genre landing page "
                        "(www.rakuten.co.jp/category/GENREID/). In --mode "
                        "product: one item page "
                        "(item.rakuten.co.jp/SHOP/CODE/). Required, unless "
                        "RAKUTEN_URL is set in the environment or in .env.")
    p.add_argument("--mode", choices=["listing", "product"],
                   default="listing",
                   help="listing (default): a search or genre grid, 45 "
                        "products a page. product: one item.rakuten.co.jp "
                        "page, which adds per-variant prices, a variant "
                        "count and a verified was-price. There is "
                        "deliberately no `shop` mode — a merchant "
                        "storefront carries an EMPTY payload and no product "
                        "grid, so it would need a parser written against "
                        "markup nobody has captured.")
    p.add_argument("--category", default=None, help="Label to tag output rows with.")
    p.add_argument("--pages", type=int, default=1, help="Listing pages to crawl")
    p.add_argument("--delay", type=float, default=2.0, help="Delay between pages, seconds")
    p.add_argument("--concurrency", type=int, default=1, metavar="N",
                   help="Accepted for flag parity and IGNORED here: parallel "
                        "page fetching lives in playwright_scraper.py.")
    p.add_argument("--retries", type=int, default=3,
                   help="Attempts per page load before giving up (default 3). "
                        "A page that comes back EMPTY is not retried: an empty "
                        "hub category is a correct answer, not a fault.")
    p.add_argument("--retry-delay", type=float, default=2.0,
                   help="Seconds before the first retry, doubling thereafter")
    p.add_argument("--format", choices=["json", "csv", "both"], default="both")
    p.add_argument("--out", default="rakuten_products", help="Output file prefix")
    p.add_argument("--proxy", default=None,
                   help="Proxy URL. NOTE: Selenium cannot authenticate a "
                        "proxy; credentials are stripped and a warning says "
                        "so. Use the Playwright or pyppeteer engine for an "
                        "authenticated exit.")
    p.add_argument("--proxy-file", default=None,
                   help="File with one proxy URL per line to rotate across. "
                        "Wins over --proxy.")
    p.add_argument("--proxy-rotate", choices=list(ROTATE_MODES), default="per-run")
    p.add_argument("--proxy-shuffle", action="store_true")
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
                        "inconsistent CLIENT, not an address."
                        % page_flow.BLOCK_RETRIES_WITH_POOL)
    p.add_argument("--locale", default="ja-JP",
                   help="Browser locale (default ja-JP). It does NOT decide "
                        "the page language or the currency: Rakuten takes "
                        "the UI language from a `?lang=` query parameter and "
                        "prices everything in JPY for every visitor.")
    p.add_argument("--twocaptcha-key", default=None, help="2captcha.com API key")
    p.add_argument("--allow-empty", action="store_true",
                   help="Write output files even when 0 rows were found.")
    p.add_argument("--fingerprint", action="store_true",
                   help="Fetch a fingerprint from 2captcha's Fingerprint API "
                        "and apply it over CDP. Needs --twocaptcha-key. "
                        "Ignored with --cdp-endpoint.")
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
                        "your proxy's exit country.")
    p.add_argument("--captcha-api", choices=["v2", "v1"], default="v2")
    p.add_argument("--solve-captcha", choices=["when-blocked", "always"],
                   default="when-blocked",
                   help="when-blocked (default): only pay to solve a "
                        "reCAPTCHA if the content is not already readable. "
                        "always: solve whenever one is detected. Note that "
                        "NO challenge has ever been observed on this site — a "
                        "refused request gets no page at all — so neither "
                        "setting has anything to act on today, and neither "
                        "helps with a refusal.")
    p.add_argument("--min-score", type=float, default=0.7)
    p.add_argument("--cdp-endpoint", default=None,
                   help="Attach to a running browser at host:port. Must NOT "
                        "carry credentials — chromedriver's debuggerAddress "
                        "cannot send them, so a credentialed endpoint is "
                        "refused with exit 2 rather than silently failing.")
    p.add_argument("--dump-html", default=None, metavar="PATH",
                   help="Save the exact HTML the parser is given, on success "
                        "as well as failure.")
    # HEADFUL by default, a departure from every sibling repo and a measured
    # one: Akamai refuses a headless browser here whatever the exit -- HTTP
    # 403 and a 394-byte "Access Denied" from four residential exits and one
    # datacentre address, against 200 and the full catalogue from those same
    # addresses with a real window.
    # HEADLESS is the default, and here that is measured in BOTH
    # directions rather than inherited (§19). On 2026-09-21 from one
    # datacentre address the answer was PER-ROUTE:
    #
    #   search / item / category   headless 200 with the payload
    #                              headful  200 with the payload
    #   ranking.rakuten.co.jp      headless 403, 3 of 3
    #                              headful  200, 3 of 3
    #
    # So the routes this scraper reads do not care, and headless is right as
    # the default. One route on this site does care, and it is the one this
    # repo does not read.
    p.add_argument("--headful", dest="headless", action="store_false",
                   help="Run with a real browser window. Not needed for the "
                        "routes this scraper reads, which were served "
                        "headless in testing.")
    p.add_argument("--headless", dest="headless", action="store_true",
                   default=True,
                   help="Run headless. THE DEFAULT. Ignored with "
                        "--cdp-endpoint, where the remote browser decides.")
    args = p.parse_args()
    env_config.apply(args)
    if not args.url:
        p.error("no --url given, and RAKUTEN_URL is not set in the environment "
                "or in .env.")
    if not is_supported_host(args.url):
        # Refused rather than attempted: the selectors, the sku pattern and
        # the pagination convention are all this site's, so another marketplace
        # would not fail loudly — it would return zero rows and read as an
        # empty category.
        why = unsupported_reason(args.url)
        if why:
            p.error(f"{site_host(args.url)} {why}.")
        p.error(f"{site_host(args.url) or args.url!r} is not a Rakuten "
                f"Ichiba URL this scraper reads.")
    kind = listing_kind(args.url)
    if args.mode == "listing" and kind == "home":
        logger.warning(
            "%s is the mall FRONT PAGE, not a listing. It carries category "
            "tiles and campaign rails and no result grid, so this run will "
            "return 0 rows and exit 4. Pass a keyword search or a genre "
            "instead.", args.url)
    # The two modes take different ROUTES, and crossing them is the likeliest
    # first mistake: both directions parse to zero rows and look like an
    # empty result. Named rather than attempted.
    if args.mode == "product" and kind != "item":
        p.error("--mode product needs an item page "
                "(item.rakuten.co.jp/SHOP/CODE/); %r is a %s URL."
                % (args.url, kind))
    if args.mode == "listing" and kind == "item":
        p.error("%r is one product page, and --mode listing reads result "
                "grids. Use --mode product for it." % (args.url,))
    return args


if __name__ == "__main__":
    args = parse_args()
    if args.fingerprint and not args.twocaptcha_key:
        logger.error("--fingerprint needs --twocaptcha-key.")
        sys.exit(2)
    if args.fingerprint and args.cdp_endpoint:
        logger.warning("--fingerprint is ignored with --cdp-endpoint: the "
                       "remote browser supplies its own.")
        # SET, not merely warned about. Both of these engines were correct
        # here only by accident of code structure: the remote branch returns
        # before the fingerprint is applied, so the flag stayed True while
        # the log said "ignored". That is a claim enforced by where the
        # `return` happens rather than by the guard, and the day someone
        # moves the fingerprint application into shared setup, these two
        # engines would silently start stacking a second identity onto a
        # browser that already has one — which on THIS site is the one thing
        # measured to get a client refused.
        #
        # puppeteer_scraper.py already did this; the suite now pins all
        # three, in both directions.
        args.fingerprint = False
    try:
        sys.exit(scrape(args))
    except ProxyError as e:
        logger.error("%s", e)
        sys.exit(2)
    except Exception as e:
        # A remote browser that will not accept the connection is a REMOTE
        # API failure (exit 5), not a crash in this code (exit 1) and not bad
        # usage (exit 2). The distinction earns its keep on the commonest
        # one: `profile_locked` means another run still holds this `pid`, and
        # a harness that sees exit 1 goes looking for a bug in the scraper
        # instead of waiting or passing a different pid.
        text = _mask_credentials(str(e))
        if "profile_locked" in text or "connect to --cdp-endpoint" in text:
            logger.error("%s", text)
            sys.exit(EXIT_API_ERROR)
        raise
