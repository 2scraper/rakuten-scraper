"""page_flow.py — what to do with the page Rakuten just gave us.

Rakuten answers a request six ways and five of them want a different
response, which is why this module exists rather than the same triage being
written three times inside three engines and drifting apart (§1):

    content    the listing payload or the item island is in the response
    empty      served exactly as asked and `numFound: 0` — the site's own
               arithmetic, not an inference from what we received
    shell      served, built from Rakuten's own assets, payload not there
               yet. Wants a WAIT, not a refetch
    throttled  HTTP 503 and the site's branded "アクセスが集中しております"
               page. Rakuten's answer to a client going too fast — a WAIT
               at the SAME exit, which is what separates it from `blocked`
    challenge  something solvable rendered (never observed here — see
               `product_parser.BOT_CHALLENGE_MARKERS`)
    blocked    Akamai refused. Either the 43-byte `Reference  #…` deny,
               which arrives under **HTTP 200**, or the branded page under
               403 on `ranking.rakuten.co.jp`

The three refusal skins do not agree on a status code and two of them share
one body byte for byte, so `product_parser.detect_page_state` needs both
signals and each covers the other's blind spot. That is worth knowing here
because it decides the budget: a `throttled` page comes back on its own and
a `blocked` one does not.

The policy lives in `STATE_POLICY` as DATA, so an engine cannot quietly
disagree with its twins about whether a page is worth retrying or worth
paying for.

Everything here is pure or driven through small callables, so each engine
passes its own driver's primitives and keeps its browser plumbing to itself:

    count(selector) -> int          how many elements match
    content() -> Optional[str]      current HTML, None if unavailable
    sleep(ms) -> None               wait

No JavaScript crosses that boundary in either direction (§1): Selenium's
`execute_script` takes a function BODY with an explicit `return` where
Playwright and pyppeteer take `() => expr`, so the shared module names the
OPERATION and the engine spells it in its own driver's dialect.

This module has NO scroll loop, and that is measured rather than assumed
-----------------------------------------------------------------------
A listing page carries all 45 of its products in the FIRST response, inside
`window.__INITIAL_STATE__`, with no JavaScript run at all. Proof rather than
belief: the same URL fetched by plain HTTP (no JS, no scroll, no browser) and
by headless Chromium parsed to 45 rows each, with byte-identical skus and
prices. So a scroll here would be latency bought for nothing — the opposite
of a sibling repo where the scroll is the only way any product is ever seen,
which is exactly why this was checked instead of ported.

And one thing NOT to do, which cost a wasted probe: never wait on
`networkidle`. Rakuten's ad and RAT tracking beacons keep firing
indefinitely — 46 fetches and 13 pings still going after load — so a
`networkidle` wait runs to its timeout on a page that was complete in two
seconds. Every engine here waits for `domcontentloaded`.
"""

from __future__ import annotations

import logging
from typing import Callable, Dict, Iterable, List, Optional, Union
from urllib.parse import urljoin, urlsplit

from product_parser import (PAGE_CAP, PAGE_SIZE, PAGINATED_KINDS, SUBSET_CAP,
                            capped_by_site, detect_block_marker,
                            detect_bot_challenge, detect_page_state,
                            genre_of, hits_per_page, listing_kind,
                            page_number_from_url,
                            page_url, pages_beyond_cap, reachable_max,
                            search_payload, served_by_rakuten, served_offset,
                            served_page_number, strip_tracking, total_pages,
                            total_results, unsupported_reason)

logger = logging.getLogger("page_flow")


# ---------------------------------------------------------------------------
# Readiness
# ---------------------------------------------------------------------------
# A price node WITH SOMETHING IN IT rather than a card count, for the reason
# §8 gives: a selector matching the wrong element is worse than one matching
# nothing, and a tile's shell can exist while its amount is still blank.
#
# What a timed-out wait costs here is small and worth stating plainly,
# because it changes how hard this has to work: every column in the output
# comes out of `__INITIAL_STATE__`, which is in the first response's HTML.
# A wait that times out costs NOTHING but the time it waited — not a column,
# not a row. That is why there is no DOM cross-check on this site and no
# `--dump-html`-and-compare ritual around the readiness wait: the DOM is not
# a second source here, it is decoration over the payload.
READY_SELECTOR_LISTING = '[class*="price--"]'
READY_SELECTOR_PRODUCT = '[class*="price"], #priceCalculationConfig'

# Above 1, per §5: waiting for a single match resolves on an unrelated node
# long before a grid paints. Below the smallest real page — a Rakuten
# listing page is 45 products on 11 of 11 captures, and the LAST page of a
# capped query is 45 too (page 150 came back `start: 6705`, exactly
# 6750 - 45), so the only short pages are queries with fewer than 45 total
# hits. 8 leaves room for those without ever being satisfied by one stray
# node.
MIN_CARD_MATCHES = 8

CONTENT_TIMEOUT_MS = 20_000

_READY = {"listing": READY_SELECTOR_LISTING, "product": READY_SELECTOR_PRODUCT}
_MIN = {"listing": MIN_CARD_MATCHES, "product": 1}


def ready_selector(mode: str = "listing") -> str:
    return _READY.get(mode, READY_SELECTOR_LISTING)


def min_matches(mode: str = "listing", expected: Optional[int] = None) -> int:
    """How many matches mean "this page painted".

    `expected` is the page's own hit count where the caller knows it,
    because this site states it: a query with 5 total hits renders 5 tiles,
    and waiting for 8 of them would time out on a page that is fully
    painted. Never above the floor and never below 2 (§5).
    """
    floor = _MIN.get(mode, MIN_CARD_MATCHES)
    if expected is None:
        return floor
    if mode == "product":
        return 1
    return max(2, min(floor, expected))


def content_timeout_ms(mode: str = "listing") -> int:
    return CONTENT_TIMEOUT_MS


def expected_cards(html: Optional[str]) -> Optional[int]:
    """How many products this page's own payload says it holds.

    Used to size the readiness wait, never to build rows — the rows come
    from the payload itself.
    """
    payload = search_payload(html)
    items = payload.get("items")
    if isinstance(items, list) and items:
        return len(items)
    found = total_results(html)
    if found is None:
        return None
    return min(found, hits_per_page(html))


def wait_for_count(count: Callable[[str], int], sleep: Callable[[int], None],
                   selector: str, minimum: int, timeout_ms: int,
                   poll_ms: int = 250) -> int:
    """Poll a selector's match count until it reaches `minimum`.

    A COUNT through the driver's own query, never a string handed to the
    page to evaluate. A sibling repo's readiness wait died with
    `EvalError: Evaluating a string as JavaScript violates the following
    Content Security Policy directive` on a site whose CSP has no
    `unsafe-eval`, and took the run down with exit 1 on that site's most
    obvious URL (§18). Counting elements goes through the protocol, works
    under any CSP, and spells the same in all three drivers.
    """
    waited = 0
    seen = 0
    while waited <= timeout_ms:
        try:
            seen = count(selector)
        except Exception as exc:               # a driver-specific failure
            logger.debug("readiness count failed: %s", exc)
            seen = 0
        if seen >= minimum:
            return seen
        sleep(poll_ms)
        waited += poll_ms
    return seen


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------
def classify(html: Optional[str], status: Optional[int] = None,
             url: str = "") -> str:
    """Which of the six states this response is.

    `status` is positional and comes SECOND, matching `detect_page_state`.
    Getting that wrong is not a style question: a sibling repo shipped two
    of three engines calling this as `classify(html, url=...)`, both crashed
    on their first fetch, and nothing short of a live run or a
    signature-binding check saw it (§17).

    On this site the status is more load-bearing than on any sibling: it is
    the only thing separating a 503 throttle from a 403 block, because
    Rakuten renders both with the same body.
    """
    if html is None:
        return "blocked"
    return detect_page_state(html, status, url)


# The retry/solve/blocked decision as DATA rather than as three copies of an
# if-chain in three engines (§1).
#
#   parse    is there anything on this page worth writing down?
#   retry    would fetching it again plausibly help?
#   solve    is there something to pay a solver for?
#   blocked  does this count towards exit 3?
STATE_POLICY: Dict[str, Dict[str, bool]] = {
    "content":   {"parse": True,  "retry": False, "solve": False, "blocked": False},
    # NOT parsed, and on this site that is load-bearing rather than tidy. A
    # no-results search page still renders the SEO recommendation carousel —
    # ten real products with real prices and real urls, in the page's own
    # JSON-LD — so a parser that treated `empty` as "parse what is there"
    # would write ten plausible phantom rows for a query that matched
    # nothing, and nothing downstream could tell them from results.
    "empty":     {"parse": False, "retry": False, "solve": False, "blocked": False},
    # Served, and still painting. Wants the readiness wait, not another
    # fetch: refetching a shell just buys another shell (§18).
    "shell":     {"parse": True,  "retry": False, "solve": False, "blocked": False},
    # Retryable, NOT blocked, and not paid for. Rakuten's 503 is a rate
    # limit: it arrived while a probe was fetching with no delay between
    # requests, and the SAME url that 503'd twice came back HTTP 200 with a
    # full payload on four consecutive tries once a 6-second delay was put
    # between them. So this costs a wait, it does not cost the exit, and it
    # must not count towards exit 3 — reporting "blocked" for a page that
    # would have come back on its own sends a reader to buy a proxy they do
    # not need.
    "throttled": {"parse": False, "retry": True,  "solve": False, "blocked": False},
    "challenge": {"parse": False, "retry": True,  "solve": True,  "blocked": False},
    "blocked":   {"parse": False, "retry": True,  "solve": False, "blocked": True},
}


def should_parse(state: str) -> bool:
    return STATE_POLICY.get(state, STATE_POLICY["blocked"])["parse"]


def should_retry(state: str) -> bool:
    return STATE_POLICY.get(state, STATE_POLICY["blocked"])["retry"]


def should_solve(state: str) -> bool:
    return STATE_POLICY.get(state, STATE_POLICY["blocked"])["solve"]


def counts_as_blocked(state: str) -> bool:
    return STATE_POLICY.get(state, STATE_POLICY["blocked"])["blocked"]


def is_unpainted(state: str, html: Optional[str]) -> bool:
    """Whether this page is served but has not filled its grid in yet."""
    if state != "shell":
        return False
    return served_by_rakuten(html or "")


# Rotating the exit does NOT help on this site, and saying so is the point
# of having measured it — the opposite of a sibling where the address was
# the whole story. Measured 2026-09-21 from one Hetzner datacentre address
# in Helsinki:
#
#   plain HTTP, curl's own User-Agent          HTTP 200, 92 KB, 45 products
#   plain HTTP, a Chrome User-Agent            HTTP 200, 43 bytes, Akamai deny
#   headless Chromium (real Chrome UA + TLS)   HTTP 200, 855 KB, 45 products
#
# The address was identical in all three. What Akamai refuses here is the
# MISMATCH between a claimed User-Agent and the TLS and HTTP/2 fingerprint
# underneath it — a curl handshake claiming to be Chrome. A consistent
# client is served from a datacentre IP without a proxy, a key or a solve,
# which is why `BLOCK_RETRIES_WITHOUT_POOL` is 1 rather than 3: if a
# consistent client is being refused, another address is unlikely to be the
# answer and the advice below says what is.
#
# The engines read these constants rather than computing their own budget —
# a policy constant nothing consults is the same defect as dead code (§17),
# and the suite asserts each one has a consumer.
RETRY_ON_BLOCKED = True
BLOCK_RETRIES_WITHOUT_POOL = 1
BLOCK_RETRIES_WITH_POOL = 3

# A throttle's own budget, separate from the block budget because the right
# response is opposite: wait longer at the SAME exit rather than move.
# Measured: 6 seconds cleared it on 4 of 4 tries at a url that had just
# 503'd twice under no-delay hammering.
THROTTLE_RETRIES = 3
THROTTLE_BACKOFF_MS = (6_000, 15_000, 30_000)

# One solve per page, and on this site the honest number is zero: no captcha
# of any kind is configured anywhere on rakuten.co.jp (measured across nine
# captures — see `product_parser.BOT_CHALLENGE_MARKERS`), and Akamai's deny
# page is 43 bytes with no widget on it. The cap is kept, and kept ENFORCED
# through `solve_budget`, because a sibling repo discovered that this
# constant had read like a limit in every repo in the family while every
# engine called the solver twice per attempt and counted once — one page
# bought three Turnstile solves (§23).
SOLVES_PER_PAGE = 1


def solve_budget(spent: int) -> bool:
    """Whether another solve on this page is within the cap.

    One helper both of an engine's call sites route through, so the cap is
    enforced rather than merely declared. Counting the increments and the
    guards and asserting they are equal is a check in the suite.
    """
    return spent < SOLVES_PER_PAGE


def throttle_delay_ms(attempt: int) -> int:
    """How long to wait before retrying a throttled page."""
    if attempt < 1:
        attempt = 1
    index = min(attempt, len(THROTTLE_BACKOFF_MS)) - 1
    return THROTTLE_BACKOFF_MS[index]


def block_advice(html: Optional[str], headless: bool,
                 has_pool: bool) -> str:
    """What a reader should actually DO about this block.

    Exists because the honest answer differs per site and a generic "try a
    proxy" wastes an afternoon where it is wrong. Here it IS wrong, and
    specifically so: the thing that gets refused is an inconsistent client,
    not an address.
    """
    marker = detect_block_marker(html or "") or "no marker"
    if marker == "akamai-reference-id":
        return (
            "blocked (%s). Akamai refused this request. On this site that is "
            "usually NOT the address: one datacentre IP was served the full "
            "catalogue by plain curl and by headless Chromium, and refused "
            "with a 43-byte deny only when a curl handshake claimed a Chrome "
            "User-Agent. So look for a CONTRADICTION in the client before "
            "buying anything — and note the distinction, because it was "
            "measured: a COMPLETE identity is fine (a --fingerprint run "
            "carrying the API's user agent, client hints, timezone and "
            "languages together was served 90 rows over two pages), while "
            "HALF of one is not. So do not set a bare UA on an HTTP client, "
            "and do not stack --fingerprint or a custom UA on top of "
            "--cdp-endpoint, where the remote browser already has an "
            "identity of its own. If the client is already consistent, %s"
            % (marker,
               "rotate with --proxy-rotate." if has_pool
               else "then --proxy with a Japanese exit is the next thing to "
                    "try, and `country-jp` in a Scraping Browser login is "
                    "the same thing one word shorter."))
    return (
        "blocked (%s). This is Rakuten's own branded refusal page, which it "
        "also serves under HTTP 503 as a rate limit — if the status was 503 "
        "the run treats it as a throttle and waits instead. Under 403 it is "
        "a refusal of this route: ranking.rakuten.co.jp answered 403 to "
        "every client tried from this address, while the search and item "
        "routes answered 200. Gating here is per-ROUTE, so check the URL "
        "before the proxy." % marker)


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------
# Three layers, weakest last (§7). On this site layer 1 is MISSING, which is
# worth stating because the family's template leads with it:
#
#   1. A standards-based `<link rel="next">` — ABSENT. Rakuten publishes
#      `<link rel="canonical">` on every listing page and no next/prev link
#      at all, on 3 of 3 captures. So the selector below reaches for the
#      site's own in-page `?p=` anchors instead, which is a build artefact
#      and is therefore the weaker signal the template warns about. Nothing
#      to be done: you cannot order by durability a signal the site does not
#      publish.
#   2. `?p=N` rebuilt by `product_parser.page_url` — and VERIFIED against
#      the site's own links rather than assumed. The genre landing page's
#      own next anchor is
#      `search.rakuten.co.jp/search/mall/-/100356/?p=2`, which is exactly
#      what `page_url` builds for it, host rewrite included.
#   3. A data terminator, and this is the one to trust: the response states
#      which offset it SERVED, in `pagination.start`. See
#      `served_the_page_asked_for`.
NEXT_PAGE_SELECTOR = (
    'link[rel="next"], '
    'a[rel="next"][href*="?p="], '
    'nav a[href*="?p="], '
    'a[href*="?p="]'
)


def next_page_selector(page_num: int = 1) -> str:
    return NEXT_PAGE_SELECTOR


def requested_page_number(url: str, loop_index: int = 1) -> int:
    """Which page the FETCHED URL actually asks for.

    Exists because `served_the_page_asked_for` was first given the loop's
    own page counter, and those two numbers are not the same thing. A run
    started on a URL that already carries `?p=2` calls it page 1 of the run
    while asking the site for page 2 — and the site, correctly, serves page
    2. The comparison then read "asked for 1, got 2", concluded the listing
    had ended, and threw away 45 perfectly good rows while reporting a
    complete run with nothing in it.

    Caught by running `--url '...?p=2' --pages 1`, which is an obvious thing
    for a user to type and which no test and no ordinary run had tried
    (§15). Inside the loop the two numbers agree, because the URL is built
    by `page_url` from that same counter — which is exactly why the bug was
    invisible.
    """
    from_url = page_number_from_url(url)
    return from_url if from_url > 1 else max(1, loop_index)


def served_the_page_asked_for(html: Optional[str],
                              requested_page: int) -> bool:
    """Whether the page we got is the page we asked for.

    The defence against Rakuten's out-of-range behaviour, and it has to be
    the site's own statement rather than a heuristic, because neither route
    fails in a way the client can see from its own request:

        search.rakuten.co.jp/.../?p=151      301 to page 1, then HTTP 200
                                             with 45 real products
        www.rakuten.co.jp/category/{id}/?p=2 HTTP 200 with 45 real products

    Both are full, well-formed pages of genuine rows. A run that trusted its
    own request would re-collect page 1 for as long as it was asked to and
    report a complete, entirely duplicate file. What settles it is that the
    payload states `pagination.start`: asked for page 151 and handed
    `start: 0` is the server saying outright that it served page 1.

    That is §23's trap on a second site and in a second spelling — there the
    server's own page number hid in an Apollo cache key, here it is a plain
    integer — and the general rule is the transferable part: before trusting
    `?p=N`, fetch one page PAST the site's own last and read what comes
    back.

    Unknown (no payload) is not a mismatch: that is what the `shell` and
    `blocked` states are for, and answering False here would end a listing
    on a page that was merely slow.
    """
    served = served_page_number(html)
    if served is None:
        return True
    return served == max(1, requested_page)


def pagination_is_addressable(page1_url: str,
                              next_href: Optional[str] = None) -> bool:
    """Whether page N can be fetched without walking pages 2..N-1.

    True for all three listing routes, including `/category/{genreId}/` —
    which does NOT honour `?p=` on its own host, but whose page 2 has a
    perfectly good address on the search host, and that is where both
    `page_url` and the site's own link send it.
    """
    kind = listing_kind(page1_url)
    if kind not in PAGINATED_KINDS + ("category",):
        return False
    if next_href and "p=" not in next_href:
        # The site's own next link does not spell what the convention
        # builds. Chain link-to-link rather than guessing.
        return False
    return True


def _resolve(base_url: str, href: str) -> str:
    """A possibly-relative href as an absolute URL."""
    return urljoin(base_url, href) if href else href


def _as_hrefs(next_href: Union[str, Iterable[str], None]) -> List[str]:
    """One href or many, always as a list.

    Both spellings are real: an engine hands over EVERY advertised link so
    the filtering rule lives here instead of in three engines, while a test
    or a caller holding one link passes the string. Accepting only the
    string is what took a sibling repo's FIRST live run down —
    `TypeError: Cannot mix str and non-str arguments`, from `urljoin` being
    handed a list — and nothing short of running it saw that, because the
    offline checks called it the way its author was thinking (§17).
    """
    if next_href is None:
        return []
    if isinstance(next_href, str):
        return [next_href] if next_href else []
    return [h for h in next_href if isinstance(h, str) and h]


def comparable(url: str) -> str:
    """A URL reduced to what makes two spellings the same page."""
    parts = urlsplit(strip_tracking(url))
    host = (parts.hostname or "").lower()
    host = host[4:] if host.startswith("www.") else host
    return "%s%s?%s" % (host, parts.path.rstrip("/"), parts.query)


def pagination_agrees(current_url: str, page_num: int,
                      next_href: Union[str, Iterable[str], None]) -> bool:
    """Whether the site's own next link matches what the convention builds.

    True when ANY advertised link agrees. A listing page carries links to
    several of its own pages at once, and the question is whether the
    CONVENTION is the site's, not whether every link happens to be page N+1.
    """
    hrefs = _as_hrefs(next_href)
    if not hrefs:
        return True
    want = page_url(current_url, page_num + 1)
    if not want:
        return False
    target = comparable(want)
    return any(comparable(_resolve(current_url, h)) == target for h in hrefs)


def _same_listing(current_url: str, candidate: str) -> bool:
    """Whether a candidate is another page of the SAME listing.

    Host-tolerant on purpose, and only on purpose: page 2 of a
    `www.rakuten.co.jp/category/100356/` listing legitimately lives on
    `search.rakuten.co.jp/search/mall/-/100356/`, so a strict host+path
    comparison would reject the site's own next link and silently cost the
    run its `--concurrency`. The genre id is what has to match.
    """
    a, b = urlsplit(current_url), urlsplit(candidate)
    genre_a, genre_b = genre_of(current_url), genre_of(candidate)
    if genre_a and genre_b:
        return genre_a == genre_b
    if b.netloc and a.netloc and a.netloc != b.netloc:
        return False
    return a.path.rstrip("/") == b.path.rstrip("/")


def next_page_candidates(current_url: str,
                         next_href: Union[str, Iterable[str], None] = None
                         ) -> List[str]:
    """Addresses worth trying for the page after `current_url`, best first.

    Takes one href or the whole advertised set, and FILTERS it: a Rakuten
    listing page links to plenty of OTHER listings — tag chips, sibling
    genres, the related-keyword rail — and chaining onto one of those
    returns rows from the wrong query while reporting success.
    """
    page_num = page_number_from_url(current_url)
    out: List[str] = []
    built = page_url(current_url, page_num + 1)
    if built:
        out.append(built)
    for href in _as_hrefs(next_href):
        resolved = _resolve(current_url, href)
        known = {comparable(u) for u in out}
        # The number has to be the NEXT one, not merely a legal one: a
        # listing page advertises links to several of its own pages at once
        # (page 1 of a 150-page query links to pages 2 through 9), so
        # accepting any of them would let page 9 be fetched as page 2 the
        # moment the built URL failed.
        if (comparable(resolved) not in known
                and _same_listing(current_url, resolved)
                and page_number_from_url(resolved) == page_num + 1
                and page_num + 1 <= PAGE_CAP):
            out.append(resolved)
    return out


def pages_at_cap(html: Optional[str]) -> bool:
    """Whether this query is deeper than Rakuten will address.

    Reported rather than swallowed, because on this site the gap between
    "complete" and "exhaustive" is enormous and the site states both
    numbers itself: one measured query reported `numFound: 3,053,682`
    against a `subset` of 6,750. A 150-page run of that query is complete —
    it fetched everything Rakuten will serve — and it is a 0.2% sample.
    Only saying so lets a consumer tell the two apart (§21), and it gives
    `diff_runs.py` the third meaning of `removed`: not delisted, not
    un-fetched, but outside this run's slice of a capped result set.
    """
    return capped_by_site(html)


def cap_summary(html: Optional[str]) -> Dict[str, Optional[int]]:
    """Rakuten's own arithmetic about this query, for the sidecar."""
    return {
        "total_results": total_results(html),
        "pages_available": total_pages(html),
        "reachable_max": reachable_max(html) or total_results(html),
        "page_size": hits_per_page(html),
        "capped_by_site": capped_by_site(html),
        "pages_beyond_cap": pages_beyond_cap(html),
        "site_page_cap": PAGE_CAP,
        "site_result_cap": SUBSET_CAP,
    }


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------
def concurrency_limit(url: str) -> Optional[int]:
    """The highest `--concurrency` this URL can honestly support."""
    return None if pagination_is_addressable(url) else 1


def concurrency_refusal(url: str) -> Optional[str]:
    """Why concurrency above 1 is refused for this URL, or None.

    Refused WITH the reason (§18): a single product page and the mall's
    front page have no page 2 at all, so there is nothing for a second
    worker to fetch, and silently running one worker would look like the
    flag did something.
    """
    kind = listing_kind(url)
    if kind == "item":
        return ("that URL is one product, not a listing; --concurrency above "
                "1 has nothing to fetch. Pass a search or genre URL, or use "
                "--mode product for the single page.")
    if kind == "shop":
        return ("a merchant storefront carries no ichibaSearch payload at "
                "all, so there is no result grid to split between workers")
    if kind == "home":
        return ("the mall front page carries no result grid, only category "
                "tiles and promo rails; --concurrency above 1 has nothing "
                "to fetch")
    if kind == "other":
        # A Rakuten Group host that is not Ichiba. `unsupported_reason` has
        # the specific answer; repeating "not paginated" here would send the
        # reader after the wrong problem.
        return unsupported_reason(url)
    if not pagination_is_addressable(url):
        return "%r is not a paginated Rakuten Ichiba listing" % (url,)
    return None
