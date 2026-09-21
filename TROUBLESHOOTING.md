# Troubleshooting

Symptoms first, in the order people actually hit them. Every number here was
measured on 2026-09-21 from a Hetzner datacentre address in Helsinki, with no
key, no proxy and no account. Where something is a property of the site
rather than a bug, it says so — a good half of what looks broken on a
marketplace this size is the site working.

---

## Exit 3, and the dump is 43 or 82 bytes of `Reference  #18.…`

**Akamai refused the request, and it is probably not your address.** This is
the first thing to check and it is not a proxy problem.

The gate on this site is the CONSISTENCY of the client, not where it is
coming from. Measured on one address, unchanged between runs:

| Client | Result |
|---|---|
| `curl`, curl's own User-Agent | **200**, 92 KB, 45 products |
| `curl`, a Chrome User-Agent | **200**, **43 bytes**, `Reference  #…` |
| headless Chromium (real UA, real TLS) | **200**, 855 KB, 45 products |
| headful Chromium | **200**, 969 KB, 45 products |

Three things to take from that:

* **The refusal arrives under HTTP 200.** The status code is not the signal,
  which is why the scraper decides "was this served at all" from the
  reference-id shape and from the page being built out of `r10s.jp` assets.
* **A costume is what gets refused.** curl claiming to be Chrome was denied
  while curl claiming to be curl was served. So look for a disguise you
  added before you buy an exit:
  * do **not** pass `--fingerprint` or a custom user agent together with
    `--cdp-endpoint` — the remote browser brings its own identity and
    stacking a second one manufactures exactly this mismatch;
  * do **not** put a browser user agent on an HTTP client.
* **The window is not the variable** for the routes this scraper reads.
  Headless and headful were both served.

If your client is already consistent and you are still refused, a Japanese
exit is the next thing to try — `--proxy` with a JP exit, or `country-jp` in
a Scraping Browser login.

## The dump is 43 bytes in one engine and 82 in another

Both are the same refusal. The edge sends a bare 43-byte body; a browser
parses it and serialises it back out wrapped in `<html><head></head><body>`,
which is 82. The marker is whitespace-tolerant and matches both — note that
the bytes Akamai actually sends contain **two spaces** (`Reference  #`), so a
literal `"Reference #"` matches none of the three refusal skins.

## Every page after the first comes back refused, but page 1 worked

This was a real bug in the pyppeteer engine and it is worth knowing the
shape, because it is the shape a fingerprint problem takes on this site:
**nav 1 is served and navs 2 onward are denied.** Akamai scores the session
after the first page.

Measured over four consecutive navigations, same browser, same address:

| pyppeteer variant | Result |
|---|---|
| no user-agent override | 4 of 4 served |
| a Windows UA override | nav 1 served, navs 2–4 denied |
| a Linux UA override (matching the real platform) | nav 1 served, navs 2–4 denied |

The third row is what settles the cause: a UA that AGREED with the platform
was refused just as hard. It is not Windows-on-Linux, it is the override —
pyppeteer sets it through CDP without the matching User-Agent Client Hints,
so the page claims two clients at once. The engine therefore sets no user
agent at all, and the offline suite pins that.

If you add a UA or a fingerprint to any engine here, **test it over two
pages**. One page will look fine.

## HTTP 503 and a page saying アクセスが集中しております

**You are going too fast.** That is Rakuten's own rate-limit page, it is not
a block, and the run treats it as a throttle: it waits (6s, then 15s, then
30s) and re-fetches from the same exit rather than spending an exit rotation.

Raise `--delay` (the default is 2.0; 4–6 seconds is comfortable) or lower
`--concurrency`. A burst of no-delay requests met this reliably during
development; the same URL came back 200 with a full payload on 4 of 4 tries
once 6 seconds were put between requests.

Note that the 403 refusal and this 503 throttle render the **identical
body**. Only the status separates them — which is why `selenium_scraper.py`,
which cannot give you a status, reads a statusless branded page as the
recoverable one.

## Exit 4, zero products, on a URL that plainly has products

Check which route you passed:

* `www.rakuten.co.jp/{shopCode}/` is a **merchant storefront**. Its
  `__INITIAL_STATE__.state.data` is empty and it carries no product payload
  at all, so there is no `--mode shop`. The scraper refuses it with that
  reason.
* `www.rakuten.co.jp/` is the mall front page — category tiles and campaign
  rails, no result grid.
* `ranking.rakuten.co.jp` is a different application with different markup
  (no payload, one JSON-LD BreadcrumbList). This repo does not implement it.
  It is not unreachable, though: it answered 403 to plain HTTP and to
  headless Chromium and **200 to headful Chromium**, 3 of 3 each — the only
  route on this site that cares about the window.
* An `item.rakuten.co.jp/...` URL needs `--mode product`. In `--mode listing`
  it parses to nothing, and the scraper refuses the combination up front.

## The run says "complete" but I only got 6,750 products

That is the site, and the run is telling you the truth twice over. Rakuten
serves at most **6,750 results per query — 150 pages of 45 — however many it
matched**, and it states both numbers itself. One measured query reported
`numFound: 3,053,682` against that same 6,750.

So a 150-page run IS complete as far as the site is concerned, and it is a
0.2% sample of what the site says matched. The sidecar records both figures
(`total_results`, `reachable_max`, `capped_by_site`, `pages_beyond_cap`) so a
consumer can tell the two apart. To reach more, narrow the query with the
site's own filters — genre, price band, shop, tag — and run each slice.

## `--pages 10` stopped after 3 and said `end_of_listing`

The site said it served a page other than the one asked for. Rakuten does
**not** error past its last page: `?p=151` of a 150-page query redirects to
page 1 and serves it with HTTP 200, and a `/category/{genreId}/` URL serves
page 1 for any `?p=` at all. Both look like success from the client side.

The run reads the offset the SERVER states (`pagination.start`) and stops
when it does not match, which is why it did not collect page 1 nine more
times. `end_of_listing` counts as a complete run.

## `currency` is null on some rows

The row's page stated no currency. Rakuten's listing payload contains no
currency field at all — the value comes from the page's own JSON-LD, and the
site publishes that block on **page 1 only** (measured: 1 block on page 1, 0
on pages 2 and 3 of the same query). The engines therefore read it once from
page 1 and carry it forward.

A null here means the run never got page 1, or the site stopped publishing
the block. It is deliberately not defaulted to `JPY`: a defaulted currency is
a guess wearing a fact's clothes.

## `rating` and `review_count` are null on some rows

Nobody has reviewed that product. Rakuten writes that as
`{score: 0, numReviews: 0}` rather than as a null — 28 of 405 measured rows,
6.9% — and a zero written through as a rating drags every average a consumer
computes, so both columns are nulled together. It is not a parsing failure.

## `original_price` and `discount_pct` are null on every listing row

That is the site. The listing payload publishes no was-price of any kind:
zero occurrences of `doublePrice`, `listPrice`, `referencePrice` or `strike`
across 405 rows. The nearest thing it has is `sale`, on 32 of 405, which is a
campaign WINDOW with no prices in it.

A verified was-price does exist on the **detail** page, gated on Rakuten's
own `doublePrice.referencePriceVerified` flag — Japan's double-pricing rules
require a merchant's reference price to be substantiated. Use
`--mode product` for it. Measured on 8 item pages: verified on 2 of them
(¥10,398 against ¥5,199, and ¥3,476 against ¥1,738).

## `in_stock` is True on every row

Rakuten's search excludes sold-out products rather than marking them.
Measured True on 405 of 405 rows, including pages 80, 120, 149 and 150 of a
142,000-hit genre, which is where stale rows would live. So a `False` on the
listing route is **unproven**; the detail route reads the page's own
`availability` microdata, which is the source that can say otherwise.

## I got 45 rows but the payload had 52 entries

Seven of them were **sponsored placements**. Rakuten injects paid slots into
its own result list; they carry a click-tracking redirect
(`grp07.ias.rakuten.co.jp/redirect_rpp/...`) instead of a product URL and a
`cpc` block, so there is nothing to key a row on and they are dropped with a
log line saying so.

The count varies between fetches of the same URL — two engines seconds apart
saw 45 and 52 — which is why `position` counts the rows emitted rather than
the payload's slots. Numbering by slot made the same 45 products come out
1–45 in one run and 8–52 in the other.

## Japanese text comes out as `���` or raises UnicodeDecodeError

You are reading an **item page as UTF-8**. Detail pages are `EUC-JP`
(`Content-Type: text/html;charset=EUC-JP`, on 8 of 8) while every listing
page is UTF-8. A browser decodes it for you, so the three engines never meet
this; an HTTP client does. `product_parser.decode_page` is the one place that
knows — pass it the raw bytes and the `Content-Type`, and never
`bytes.decode("utf-8", "replace")`, which turns every title on the page into
replacement characters while the numbers still parse, so the run reports
success with a column of garbage.

## The readiness wait timed out, and the rows are fine anyway

Expected, and it costs nothing. Every column comes out of
`__INITIAL_STATE__`, which is in the first response's HTML with no JavaScript
run at all — the same URL fetched by plain HTTP and by a real browser parses
to the same 45 rows with the same skus and prices. The DOM is decoration over
the payload here, not a second source, so a wait that times out costs the
wait and nothing else.

Relatedly: **never wait on `networkidle`** on this site. Its ad and RAT
beacons keep firing indefinitely — 46 fetches and 13 pings still going after
load — so that wait always runs to its full timeout. All three engines wait
for `domcontentloaded`.

## A run ends with `Exception ignored in: <coroutine object Connection._recv_loop>`

**The run succeeded.** Check the exit code and the output files; they are
fine.

That traceback is printed by CPython's garbage collector at interpreter
shutdown, after the event loop is gone and after the exit code has already
been decided, by pyppeteer's own connection object. No loop handler can reach
it, and catching it would mean installing a global unraisable hook that
swallows real bugs too. It is a known limitation of the pyppeteer engine,
pinned as such in the offline suite, and it is the reason
`playwright_scraper.py` is the primary engine.

## `--fingerprint` says HTTP 400 "Request parameters are invalid"

`--fp-tags` takes **ONE OS-family tag** — `Windows`, `macOS`, `Linux`. A
list is rejected, and so are `Chrome`, `Desktop` and `Mobile` on their own.
Measured against the live API on 2026-09-10: `Windows` succeeds;
`Windows,Chrome,Desktop`, `Chrome` and `Desktop` each 400.

And on this site, think before reaching for it at all: a fingerprint replaces
the browser's own consistent identity with a claimed one, and a claimed
identity that does not match the TLS underneath it is precisely what Akamai
refuses here.

## Which engine should I use?

`playwright_scraper.py`. The other two are parity engines — they must produce
the same rows, and they are verified to (all three agreed on 90 rows across
2 pages, every column identical) — but Playwright is the one that is
maintained upstream and the one with no driver-specific limitation.

Selenium cannot use an authenticated remote CDP endpoint (`debuggerAddress`
takes a bare `host:port` with nowhere to put a password) and its
`--proxy-server` cannot authenticate at all. pyppeteer is effectively
unmaintained and its own README points at Playwright.

## Something else

Re-run with `--dump-html out.html`, which writes the exact bytes the parser
was given **on success as well as on failure**, and open it. A run can return
the right number of rows with a column silently unpopulated, and then those
bytes are the only way to tell a parsing bug from a too-early snapshot.
