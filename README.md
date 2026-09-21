# rakuten-scraper

[![release](https://img.shields.io/github/v/release/2scraper/rakuten-scraper?sort=semver)](https://github.com/2scraper/rakuten-scraper/releases)
[![tests](https://github.com/2scraper/rakuten-scraper/actions/workflows/tests.yml/badge.svg)](https://github.com/2scraper/rakuten-scraper/actions/workflows/tests.yml)
[![canary](https://github.com/2scraper/rakuten-scraper/actions/workflows/canary.yml/badge.svg)](https://github.com/2scraper/rakuten-scraper/actions/workflows/canary.yml)
[![python](https://img.shields.io/badge/python-3.9%20%7C%203.13-blue)](pyproject.toml)
[![licence](https://img.shields.io/badge/licence-MIT-green)](LICENSE)
[![engines](https://img.shields.io/badge/engines-Playwright%20%7C%20Selenium%20%7C%20pyppeteer%20%7C%20CDP-informational)](#engines)
[![runs without an account](https://img.shields.io/badge/runs%20without-an%20account-brightgreen)](#do-you-need-any-of-the-paid-products)

Scrapes [Rakuten Ichiba](https://www.rakuten.co.jp) — Japan's largest online
marketplace — to JSON or CSV: products, prices, Rakuten points, shipping,
shops, review scores and per-variant pricing, with four interchangeable
browser back ends and one row schema shared with the rest of the
[2scraper](https://github.com/2scraper) family.

```bash
pip install -r requirements.txt -r requirements-playwright.txt
playwright install chromium

python3 playwright_scraper.py \
  --url "https://search.rakuten.co.jp/search/mall/コーヒー/" --pages 3
```

That is the whole setup. No key, no proxy, no account — see below.

## The one thing to know first

**You do not need any of the paid products to read this site's listings, and
the reason is not what you would guess.**

Rakuten is fronted by Akamai, and what Akamai refuses here is not a
datacentre address — it is a client whose claimed identity and TLS
fingerprint disagree. Measured 2026-09-21 from one Hetzner datacentre address
in Helsinki, nothing else changed between the rows:

| Client | Result |
|---|---|
| `curl`, curl's own User-Agent | **200**, 92 KB, 45 products |
| `curl`, a Chrome User-Agent | **200**, **43 bytes**, `Reference  #…` |
| headless Chromium (real UA, real TLS) | **200**, 855 KB, 45 products |
| headful Chromium | **200**, 969 KB, 45 products |

Read it by column: the address is identical in all four and the *consistency
of the client* decides everything. curl claiming to be curl is served; curl
claiming to be Chrome is refused. So the practical advice is the opposite of
most sites in this family — **if you are being refused, look for a disguise
you added before you buy an exit**:

* do not pass `--fingerprint` or a custom user agent together with
  `--cdp-endpoint`, because the remote browser already brings its own
  identity and stacking a second one manufactures exactly this mismatch;
* do not put a browser user agent on an HTTP client.

Two more things follow from that table, and both are easy to get wrong:

* **A refusal arrives under HTTP 200.** The status code is not the signal.
  This scraper decides "was this served at all" from the reference-id shape
  and from the page being built out of Rakuten's own `r10s.jp` asset hosts
  (90–343 references on every served page, 0–1 on every refusal).
* **Headless is fine.** It is the default here. The browser even announces
  itself — a tracking beacon from the headless run carried
  `HeadlessChrome/153.0.8010.12` — and Akamai served it anyway.

The canary in this repository is that claim under test: a real three-page
scrape, daily, from a bare GitHub runner, with **no secrets**. If Rakuten ever
puts the listing routes behind something, the badge above goes red the next
morning.

## Install

One engine, not all three. They declare mutually unsatisfiable pins —
playwright and pyppeteer disagree on `pyee`, pyppeteer and selenium on
`urllib3` — so use a virtualenv per engine if you need more than one.

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt -r requirements-playwright.txt
playwright install chromium            # the recommended engine
```

```bash
pip install -r requirements.txt -r requirements-selenium.txt    # or
pip install -r requirements.txt -r requirements-puppeteer.txt   # or
```

`pip install .[playwright]` is equivalent.

## Usage

```bash
# A keyword search, three pages, JSON and CSV
python3 playwright_scraper.py \
  --url "https://search.rakuten.co.jp/search/mall/コーヒー/" --pages 3

# A genre listing, by Rakuten's own genre id
python3 playwright_scraper.py \
  --url "https://www.rakuten.co.jp/category/100356/" --pages 5 --format csv

# One product: per-variant prices, a variant count, and a verified was-price
python3 playwright_scraper.py --mode product \
  --url "https://item.rakuten.co.jp/sawaicoffee-tea/solandluna/"

# The site's own filters are part of the URL and survive pagination
python3 playwright_scraper.py \
  --url "https://search.rakuten.co.jp/search/mall/コーヒー/?min=1000&max=3000" \
  --pages 3

# Spread a long run across exits (and see the note on 503 below)
python3 playwright_scraper.py --url "…" --pages 50 \
  --proxy-file exits.txt --proxy-rotate per-page --delay 4
```

### Pass a listing or an item, not a shop

Three routes carry products and one looks as though it should:

| URL | Reads |
|---|---|
| `search.rakuten.co.jp/search/mall/{keyword}/` | ✅ `--mode listing` |
| `search.rakuten.co.jp/search/mall/-/{genreId}/` | ✅ `--mode listing` |
| `www.rakuten.co.jp/category/{genreId}/` | ✅ `--mode listing` |
| `item.rakuten.co.jp/{shop}/{code}/` | ✅ `--mode product` |
| `www.rakuten.co.jp/{shopCode}/` | ❌ refused, with the reason |
| `ranking.rakuten.co.jp/…` | ❌ not implemented |

A **merchant storefront** is refused rather than attempted because its
`__INITIAL_STATE__.state.data` is *empty* — it carries no product payload at
all, so reading it would need a second parser written against markup nobody
has captured. A mode that ships untested is worse than a mode that is absent.

`ranking.rakuten.co.jp` is a different application: no payload, one JSON-LD
`BreadcrumbList`, 80 product links in hand-rolled markup. It is also the one
route on this site that cares about the window — 403 to plain HTTP and to
headless Chromium (3 of 3 each), **200 to headful Chromium** (3 of 3). Not
unreachable, just not implemented.

## What comes out

42 columns. The first 18 are the family prefix, byte-identical and in the
same order as every other repo in this family, so one consumer reads a
rakuten run and a mediamarkt run with the same code.

```json
{
  "source": "rakuten.co.jp",
  "scraped_at": "2026-09-21T09:22:22+00:00",
  "url": "https://item.rakuten.co.jp/ajinomoto/r4901111371248/",
  "sku": "ajinomoto:r4901111371248",
  "title": "【9/24まで P10倍！】味の素AGF ブレンディ スティックブラック100本…",
  "brand": "味の素AGF",
  "price": 2447.0,
  "currency": "JPY",
  "original_price": null,
  "discount_pct": null,
  "rating": 4.76,
  "review_count": 475,
  "in_stock": true,
  "image_url": "https://thumbnail.image.rakuten.co.jp/@0_mall/ajinomoto/…",
  "category": "水・ソフトドリンク > コーヒー > インスタントコーヒー",
  "price_source": "state",
  "page": 1,
  "position": 1,

  "variant_id": "r4901111371248",
  "item_number": "10000338",
  "price_max": 6975.0,
  "has_price_range": true,
  "subscription_price": 2324.0,
  "points": 220,
  "point_rate": 10,
  "shipping_fee": 0.0,
  "free_shipping": true,
  "delivery_estimate": "最短9/23お届け",
  "shop_name": "味の素グループ公式ショップ",
  "shop_code": "ajinomoto",
  "shop_id": 383220,
  "shop_url": "https://www.rakuten.co.jp/ajinomoto/",
  "genre_id": 303017,
  "genre_path": "/0/100316/100356/303017",
  "genre_rank": 2,
  "is_super_deal": false,
  "is_39shop": true,
  "is_official_shop": true,
  "variant_count": null,
  "original_price_label": null,
  "tags": {"ブランド": ["味の素AGF"], "単品重量": ["～ 99g"]},
  "variants": null
}
```

`sample_output.json` and `sample_output.csv` are cut from a real run, not
written by hand.

Three columns are worth explaining because this site is unusual:

* **`sku` is `{shopCode}:{manageNumber}`** — Rakuten's own item code, exactly
  as a detail page states it in `<meta itemprop="sku">`. It is the same id on
  both routes, so you can join a listing run to a product run on it.
  Deliberately *not* the payload's `variantId`, which names the pre-selected
  SKU inside the item and matches the URL's own code on only 39 of 180
  measured rows.
* **`points` is half the price story.** Rakuten is a points marketplace: a
  10× point campaign is effectively a 10% discount that never touches the
  price column, so a monitor watching `price` alone would call a product
  unchanged through an entire Super Sale. `diff_runs.py` tracks both.
* **`shop_*` is not run metadata.** This is a mall — 74 distinct merchants
  across 405 measured rows — so the same physical article is sold by many
  shops at many prices under many ids.

### Exit codes

| Code | Means |
|---|---|
| 0 | products written |
| 1 | crash |
| 2 | bad usage |
| 3 | blocked before parsing |
| 4 | served a page, zero products |
| 5 | remote API error |
| 6 | partial run |

A run that finds nothing **writes nothing** — `--allow-empty` is the opt-out
— so a failed run cannot replace last night's good output with `[]`.

Every run also writes `<out>.meta.json`, and on this site it carries the
site's own arithmetic as well as the status. See
[complete is not exhaustive](#complete-is-not-exhaustive).

## Where the data actually is

**The JSON-LD on a listing page is a trap.** A search page has exactly one
`application/ld+json` block, it is an `ItemList`, and it holds **ten** items
whose every url carries `?scid=seo-carousel-search` — it is the SEO
recommendation carousel — while the page itself holds **45** products. A
JSON-LD-primary parser returns ten rows of the wrong products and reports
success.

The real sources:

| Route | Source |
|---|---|
| listing | `window.__INITIAL_STATE__.state.data.ichibaSearch` — `items` (45/page) + `pagination` |
| product | `<script type="application/json" id="item-page-app-data">` → `newApi.itemInfoSku` |

The listing JSON-LD is read for exactly one thing: **the currency.** The
listing payload states none at all — zero occurrences of `priceCurrency`,
`currency` or the string `JPY` across 405 rows — and Rakuten publishes the
block that does state it on **page 1 only** (1 block on page 1, 0 on pages 2
and 3 of the same query). So the engines read it once from page 1 and carry
it forward, and a run that never got page 1 leaves `currency` null rather
than defaulting.

A **detail** page publishes a different structure again, and its only JSON-LD
is a `BreadcrumbList` — so the listing parser finds nothing on it, which the
test suite asserts directly.

**Detail pages are EUC-JP.** `Content-Type: text/html;charset=EUC-JP` on 8 of
8, while every listing page is UTF-8. A browser decodes it for you, so the
three engines never meet this; an HTTP client does, and those bytes decoded
as UTF-8 raise on the first Japanese character — or, with
`errors="replace"`, turn every title on the page into replacement characters
while the numbers still parse.

## Pagination

`?p=N`, 45 products a page, and **two routes that lie past the end.**

Rakuten does not error past a listing's last page. `?p=151` of a 150-page
query answers 301 to page 1 and then serves it with HTTP 200 and 45 real
products; and `www.rakuten.co.jp/category/{genreId}/?p=2` answers 200 with
page 1 **without even redirecting**. Both look like success from the client
side, and a run that trusted its own request would re-collect page 1 for as
long as it was asked to and report a complete, entirely duplicate file.

What settles it is that the payload states the offset the server actually
used. Asking for page 151 and being handed `pagination.start: 0` is Rakuten
saying outright that it served page 1, so every fetched page is checked
against it and a mismatch ends the listing (`stop_reason: end_of_listing`,
which counts as a *complete* run).

The site also publishes **no `<link rel="next">` anywhere** — only
`rel="canonical"`, on 3 of 3 captures — so the durable layer the family's
pagination rules lead with simply does not exist here. The `?p=` convention
is verified against the site's own numbered anchors instead, including the
cross-host case: a genre landing page advertises
`search.rakuten.co.jp/search/mall/-/{genreId}/?p=2`, which is exactly what
this scraper builds for it.

### Complete is not exhaustive

Rakuten serves at most **6,750 results per query — 150 pages of 45 —
however many it matched**, and it states both numbers itself. One measured
query reported:

```
numFound: 3,053,682        subset: 6,750        pageSize: 45
```

So a full 150-page run of that query is genuinely `status: complete` — it
fetched everything the site will serve — and it is a **0.2% sample**. A
sidecar that said only "complete" would be lying by omission, so every run
records `total_results`, `reachable_max`, `page_size`, `capped_by_site` and
`pages_beyond_cap` beside the status, and the engine logs the percentage.

It also gives `diff_runs.py` a third meaning for `removed`: not delisted, not
un-fetched, but *outside this run's slice of a capped result set*.

To reach more, narrow the query with the site's own filters — genre, price
band, shop, tag — and run each slice.

## Engines

| Engine | Status |
|---|---|
| `playwright_scraper.py` | **primary**, recommended |
| `selenium_scraper.py` | parity |
| `puppeteer_scraper.py` | parity (pyppeteer is effectively unmaintained) |
| `scraper_api_client.py` | HTTP-only, measured at **$0.0005** a page |

All three browser engines take the same flags, produce the same columns in
the same order, and map the same exit codes. Verified rather than asserted:
on one genre listing, two pages, the three engines produced **90 rows each
with identical skus in identical order and every column identical**.

Known limits, stated here rather than left to be discovered:

* **Selenium cannot use an authenticated remote CDP endpoint.** Playwright's
  `connect_over_cdp` and pyppeteer's `browserWSEndpoint` take a full
  `ws://user:pass@host:port`; chromedriver's `debuggerAddress` takes a bare
  `host:port` with nowhere to put a password. A credentialled
  `--cdp-endpoint` is refused there with that explanation rather than
  silently dropping the credentials.
* **Selenium's `--proxy-server` cannot authenticate at all.** Credentials are
  stripped and a warning printed.
* **The pyppeteer engine deliberately sets no user agent**, where the other
  two set one. That is not a parity gap to close — it is required for that
  engine to work on this site. See the next section.

### The trap that cost the most to find

Setting a user agent through pyppeteer's CDP override gets that engine
refused **from its second navigation onward**. Measured over four consecutive
navigations per variant, same browser, same address:

| pyppeteer variant | Result |
|---|---|
| no override | 4 of 4 served |
| a Windows UA override | nav 1 served, navs 2–4 **denied** |
| a Linux UA override, matching the real platform | nav 1 served, navs 2–4 **denied** |

The third row is what settles the cause: a UA that *agreed* with the platform
was refused just as hard. It is not Windows-on-Linux, it is the override —
most likely because the CDP override changes `navigator.userAgent` and leaves
the User-Agent Client Hints reporting the real browser, so the page claims
two clients at once. (That explanation is an inference and it is not
sufficient on its own: `selenium_scraper.py` issues the same
`Network.setUserAgentOverride` and was served 3 of 3 pages.)

The transferable part is the SHAPE. A fingerprint problem on this site looks
like success on page 1. `--help` worked, `compileall` passed, 565 offline
checks were green, and only a live two-page run found it. **If you add a user
agent or a fingerprint to any engine here, test it over two pages.**

## Do you need any of the paid products?

**For listings and product pages: no.** Measured 2026-09-21 with no key, no
proxy and no account, from a datacentre address:

* a keyword search returned **45 products a page**;
* a three-page run returned **135 rows, 100% with a price**, `status:
  complete`, exit 0;
* the same on the genre route, and with a headful browser.

### All four paid paths, measured

Each was run against this site on 2026-09-21. The point of the table is that
none of them is *needed* — and that each does something specific when you do
want it.

| Path | Result | What it buys |
|---|---|---|
| Scraping Browser API (`--cdp-endpoint`, US exit) | 90 rows over 2 pages, `complete` | no browser infrastructure, a chosen exit country, persistent cookies per profile |
| Proxy (`--proxy`, EU residential exit) | 90 rows over 2 pages, `complete` | volume — spreading a long run so you stop meeting the 503 |
| Fingerprint (`--fingerprint`) | applied and served; 90 rows over 2 pages | a consistent device identity |
| Scraper API | HTTP 200, 1.09 MB, 45 products, **$0.0005** | no browser at all, one request a page |
| Captcha solving | nothing to solve — see below | — |

Four notes, because each cost a measurement:

* **A fingerprint is NOT a hazard here, but half of one is.** This README
  said otherwise before the path had been run, and the measurement is
  narrower than the warning was. A fingerprint applied *completely* — user
  agent, client hints, timezone, languages and platform together — is
  served. The run *without* it hit the same 503 on page 1 that the run with
  it did, so that throttle was request volume rather than the fingerprint.
  What Akamai refuses is a client that contradicts *itself*, which is why a
  bare user-agent override is refused and a complete identity is not.
* **`--concurrency` is refused with `--cdp-endpoint`**, and so is `--proxy`,
  and `--fingerprint` is ignored there. A Scraping Browser API profile allows
  one live connection, it already has an exit, and it already has an
  identity. Each refusal prints its reason rather than quietly doing
  nothing.
* **Proxies buy volume**, which is the thing actually worth buying here.
  Rakuten answers a client going too fast with HTTP 503 and its own
  *アクセスが集中しております* page — met three times during testing, always
  cleared by the wait. A Japanese exit is also what to try if a consistent
  client is somehow still refused.
* **Credentials never reach a log.** A proxied run prints
  `http://***:***@eu.proxy.2captcha.com:2334` — host and port kept, because
  which exit a run used is the useful half and is not the secret.

### There is no captcha on this site

Not "we did not meet one" — **none is configured.** Across 13 captures,
served and refused alike: zero reCAPTCHA, hCaptcha, Turnstile, DataDome,
PerimeterX, Incapsula, Kasada or AWS WAF markers; no challenge iframe; no
`data-sitekey`; no `*_SITE_KEY` in any page config; no `<captcha-*>` mount
point. And Akamai's own refusal here is 43 bytes with **no widget on it** —
which is the narrow, honest use of the word *unsolvable*: a property of that
page, not of any vendor.

The solver is still wired up and bounded to one solve per page, because a bot
manager can be switched on between deploys and a scraper that cannot name
what stopped it is much harder to fix. This repo implements reCAPTCHA v2/v3;
if Rakuten ever renders an enterprise reCAPTCHA or a Turnstile, 2Captcha
solves both and the work would be to add the task type here.

### 503 is a throttle, not a block

Rakuten serves the **identical branded page** under 403 (a route it refuses
outright) and under 503 (a client going too fast). Only the status separates
them, so:

* a 503 costs a wait at the same exit — 6s, then 15s, then 30s — and does
  **not** count towards exit 3;
* a 403 is a refusal of that route.

Measured: a URL that had just 503'd twice under no-delay hammering came back
200 with a full payload on 4 of 4 tries once 6 seconds were put between
requests. Raise `--delay` before you reach for a proxy.

Note that `selenium_scraper.py` cannot give you a status, so it reads a
statusless branded page as the *recoverable* one — calling a throttle a block
spends the block budget on a page that was about to come back, while calling
a block a throttle costs one wait.

### The proxy recipe that works in every engine

```bash
# credentials in .env, never on a command line
echo 'RAKUTEN_PROXY=http://USER:PASS@jp.proxy.2captcha.com:2334' >> .env
python3 playwright_scraper.py --url "…" --pages 20 \
  --proxy-rotate per-page --delay 4
```

A rotation is a fresh browser: cookies issued against one exit and replayed
from another are a stronger signal than either address alone, so the engine
tears the browser down and relaunches rather than swapping the proxy under a
live session.

## Measured on this site

Everything in this section is a figure from a real run, with the date. Where
a column is empty, it says whether that is the site or the parser.

**2026-09-21, one Hetzner datacentre exit in Helsinki, no credentials:**

| Run | Result |
|---|---|
| keyword search, 3 pages | 135 rows, 100% priced, `complete`, exit 0 |
| genre listing, 2 pages | 90 rows, identical across all three engines |
| one item page | 1 row, 4 variants, a verified 50% was-price |
| 11 listing pages (405 rows) | `price` 405/405, `points` 405/405, `shop_code` 405/405 |

**Field coverage across those 405 rows:**

| Column | Populated | Note |
|---|---|---|
| `price` | 405/405 | every listing names a price |
| `points` | 405/405 | 27 of them are 0, which is a real zero |
| `rating` / `review_count` | 377/405 | the other 28 are unreviewed — see below |
| `brand` | 240/405 (59%) | from Rakuten's own ブランド tag |
| `shipping_fee == 0` | 317/405 (78%) | free shipping is the common case |
| `price_max` | 95/405 | items whose variants differ in price |
| `subscription_price` | 54/405 | 定期購入, always below `price` |
| `genre_rank` | 20/405 | the item's rank in its genre ranking |
| `original_price` | **0/405** | the listing payload publishes none — see below |
| `in_stock == true` | **405/405** | the search excludes sold-out items |

## Traps that look like bugs

* **`rating` and `review_count` null on some rows.** Nobody has reviewed that
  product. Rakuten writes that as `{score: 0, numReviews: 0}` rather than as
  a null, on 28 of 405 rows, and a zero written through as a rating drags
  every average a consumer computes — so both columns are nulled together,
  keyed on the count.
* **`original_price` and `discount_pct` null on every listing row.** The
  listing payload publishes no was-price of any kind: zero occurrences of
  `doublePrice`, `listPrice`, `referencePrice` or `strike` across 405 rows.
  The nearest thing it has is `sale`, on 32 of 405, which is a campaign
  *window* with no prices in it — read as a discount it would invent one. A
  verified was-price does exist on the detail page, gated on Rakuten's own
  `doublePrice.referencePriceVerified` flag, which is the outcome of Japan's
  double-pricing substantiation rule. Verified on 2 of 8 item pages
  (¥10,398 → ¥5,199, and ¥3,476 → ¥1,738).
* **`in_stock` true on every row.** The search excludes sold-out products
  rather than marking them — checked specifically on deep pages (80, 120,
  149, 150 of a 142,000-hit genre), where stale rows would live: 0 sold out.
  So a `false` on the listing route is *unproven*; the detail route reads the
  page's own `availability` microdata, which is the source that can say
  otherwise.
* **45 rows from a payload with 52 entries.** Seven were sponsored
  placements. Rakuten injects paid slots into its own result list; they carry
  a click-tracking redirect instead of a product URL, so there is nothing to
  key a row on. The count varies between fetches of the same URL — two
  engines seconds apart saw 45 and 52 — which is why `position` counts the
  rows emitted rather than the payload's slots.
* **`--locale en` does not translate anything you care about.** `?lang=en`
  comes back with `locale: "en"` in the payload and an English navigation
  bar, and product names, shop names and tag values are merchant-authored
  Japanese and come back **byte-identical**. `?lang=zh-tw` and `?lang=ko` are
  accepted with HTTP 200 and silently fall back to `ja`.
* **`genre_rank` is not a position in your results.** It is the item's
  standing in its genre's *ranking* listing, which this run never fetched.
  Sibling repos in this family use published ranks to prove that cards went
  missing; that arithmetic does not work here.
* **A readiness wait that times out costs nothing.** Every column comes out
  of the payload, which is in the first response with no JavaScript run at
  all — the same URL fetched by plain HTTP and by a real browser parses to
  the same 45 rows with the same skus and prices.
* **Never wait on `networkidle`.** Rakuten's ad and tracking beacons keep
  firing indefinitely — 46 fetches and 13 pings still going after load — so
  that wait always runs to its full timeout. All three engines wait for
  `domcontentloaded`.

More in [TROUBLESHOOTING.md](TROUBLESHOOTING.md).

## Diffing two runs

```bash
python3 diff_runs.py --old monday.json --new tuesday.json --fail-on-change
```

Joins on `sku` and reports added / removed / changed. Two things it does that
matter on this site:

* **It tracks `points` and `point_rate`**, because a point campaign is a
  discount that never touches the price column.
* **It refuses to compare runs it cannot compare** — a partial run against a
  complete one, or two different modes — rather than producing a diff whose
  every line is an artefact. A price change that comes with a `price_source`
  change is reported as `source_changed`, not `changed`, and
  `--fail-on-change` ignores it: it says something about our own two
  snapshots, not about the shop.

## Testing

```bash
python3 smoke_test.py      # 565 offline checks, no network, no engine needed
pytest                     # the same suite, as one test
python3 .github/ci_checks.py --all
```

The suite passes with no engine library installed at all, and reports which
groups it skipped — a suite that silently skips part of itself and still says
"all passed" is the same defect as code that reports success without
checking. CI installs each engine in its own virtualenv and fails if any
group reports a skip.

Fixtures are cut from real captures by `make_fixtures.py`, which verifies
that each trim parses **identically** to the untrimmed original — every
column, not a sample — and scrubs credential-shaped and personal material
first: two 32-char front-end API keys baked into every item page, a real
customer's review nickname and text, a per-impression session id on each
sponsored slot, and a 32-hex catalogue id that is public and harmless and
reads as a credential to every scanner including this repo's own.

Every check added while building this repo was **controlled** — the code was
broken deliberately, the suite confirmed red and the expected check confirmed
to be the one that named it — because a control that silently did not apply
prints a green suite and proves nothing.

## Contributing, security, licence

[CONTRIBUTING.md](CONTRIBUTING.md) ·
[SECURITY.md](SECURITY.md) ·
[TROUBLESHOOTING.md](TROUBLESHOOTING.md) ·
[CHANGELOG.md](CHANGELOG.md) · MIT

This repo reads **public pages**. It respects the site's own pagination
limits, defaults to a delay between pages, and treats a rate-limit response
as a signal to wait rather than to push harder. It does not log in, does not
touch a cart or a checkout, and does not attempt to defeat a challenge it was
not given.
