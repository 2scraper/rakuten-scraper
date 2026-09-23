# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project uses [Semantic Versioning](https://semver.org/) as closely
as a command-line toolkit can. **A patch release means fixes** — including a
fix that changes a default, where leaving the old default in place would mean
shipping a known-wrong answer. Anything that changes behaviour for an
existing user leads the release notes, so nobody has to discover it from
their output or their bill.

## [Unreleased]

### Fixed

- Leftovers from the repos this one was bootstrapped from, none of which
  changes behaviour:
  - `.gitignore` and `.dockerignore` ignored another repo's output prefix
    (`dubizzle_listings.*`) instead of this one's; they now ignore
    `rakuten_products.*`, the engines' real `--out` default.
  - The offline suite used another site's environment variable names in its
    placeholder and proxy-line checks; it now uses `RAKUTEN_CDP_ENDPOINT`
    and `RAKUTEN_PROXY`, the names `env_config.py` actually reads.
  - The `run_meta()` docstring described a `--mode shop` and a seller's
    review counts that this repo does not have; it now describes what
    `extra` really carries here.
  - A comment in the pyppeteer and Selenium engines explained single-page
    handling through a shop mode that does not exist here.
  - A privacy-check comment now names mediamarkt-scraper as the sibling it
    describes.

- `SECURITY.md` said this project has no releases or version tags; it has
  both. "Supported versions" now names the latest release and `main`.
- `captcha_solver.py`'s docstring pointed at a "No DataDome solver" section
  that does not exist in this repo (it came with the copied core). Removed.

## [0.2.0] — 2026-09-21

> **Changes behaviour for an existing consumer:** the Selenium and pyppeteer
> engines were writing a *different sidecar* from the Playwright one. They
> carried three keys belonging to a sibling repo (`scroll`, `result_header`,
> `pages_still_growing` — `scroll` is meaningless on a site that serves its
> whole page at once) and were **missing the cap arithmetic**. On this site
> that arithmetic is what keeps `status: complete` honest: Rakuten serves
> 6,750 results of a query that matched 7,460,738, so a sidecar saying only
> "complete" is lying by omission. All three engines now write the same 20
> keys, verified by a live run of each, and the suite compares the key sets.

### Fixed

- **The `--fingerprint` + `--cdp-endpoint` guard was a warning, not a
  gate.** Two of the three engines logged "--fingerprint is ignored with
  --cdp-endpoint" while leaving the flag True; they were correct only
  because each remote branch happens to `return` before the fingerprint is
  applied. That is a claim enforced by where a `return` sits, and the day
  someone moves the fingerprint into shared setup those two engines would
  silently start stacking a second identity onto a browser that already has
  one — which on this site is the one thing measured to get a client
  refused. All three now force the flag off, and the suite asserts the
  BEHAVIOUR (every fingerprint application sits behind the flag, followed
  through the `_apply_fingerprint` indirection) rather than the log line.
- A dead `scroll` field on the per-page outcome, and a scroll measurement
  ported verbatim from a sibling. Replaced with this site's own: the same
  URL fetched by plain HTTP with no JavaScript and by a real browser parses
  to the same 45 rows.
- The documented Scraping Browser endpoint in `env_config.py` said
  `country-id` — a leftover from another repo in this family.


### Changed

- **All four paid paths are now measured, and one README claim is
  corrected.** A key and a Scraping Browser API endpoint arrived after
  v0.1.0, so the paths that had shipped documented as unrun were run:

  | Path | Result |
  |---|---|
  | Scraping Browser API (`--cdp-endpoint`, US exit) | 90 rows over 2 pages, `complete` |
  | Proxy (`--proxy`, EU residential exit) | 90 rows over 2 pages, `complete` |
  | `--fingerprint` | applied and served; 90 rows over 2 pages |
  | 2Captcha Scraper API | HTTP 200, 1.09 MB, 45 products, $0.0005 |

  > **The correction that matters:** v0.1.0 said a fingerprint was
  > "actively counterproductive" on this site. It is not, and the
  > measurement is narrower than the warning was. A fingerprint applied
  > COMPLETELY — user agent, client hints, timezone, languages and platform
  > together — is served. The run *without* it met the same 503 on page 1
  > that the run with it did, so that throttle was request volume rather
  > than the fingerprint. What Akamai refuses is a client that contradicts
  > *itself*, which is why a bare user-agent override is refused and a
  > complete identity is not. Corrected in the README, TROUBLESHOOTING.md,
  > `.env.example`, `page_flow.block_advice` and the engine help.

  `scraper_api_client.py`'s docstring likewise replaces "unverified on this
  site" with the measurement and the price.

### Added

- **`navigator.languages` is now applied from the fingerprint.** The API
  returns `intl.languages: ["en-US", "en"]` while Playwright's `locale=`
  sets only the primary language, so the page reported `["en-US"]` — a
  one-element list beside a two-element `Accept-Language`. Small, and this
  is the site that punishes small contradictions specifically.
- **A fixture fetched over `--cdp-endpoint`**, carrying the Scraping
  Browser API extension's 16 injected captcha hunters, with six checks
  pinning that the marker set scores zero against them WITHOUT relying on
  the extension strip. `cf-turnstile` appears once in that page — on a page
  holding the full catalogue — so carrying that marker, as this family's
  own notes originally recommended, would report exit 3 on a good 1.1 MB
  page. Controlled: adding it now fails three checks.

  This is the fixture the family's notes ask for and a sibling repo lacked:
  its equivalent guard ran only against a curl-fetched 404, which carries no
  injection at all, so it passed for the wrong reason.
- The widened signature-binding check now covers the credential-gated
  modules, which is what stood in for a live run before the key arrived.
  601 offline checks in total.

## [0.1.0] — 2026-09-21

First release. Reads Rakuten Ichiba (`rakuten.co.jp`) listings and product
pages to JSON or CSV, with four interchangeable back ends and the row schema
shared across the [2scraper](https://github.com/2scraper) family.

### Added

- **Two modes, both measured against real captures.**
  `--mode listing` reads a keyword search
  (`search.rakuten.co.jp/search/mall/{keyword}/`), a genre listing
  (`.../search/mall/-/{genreId}/`) or a genre landing page
  (`www.rakuten.co.jp/category/{genreId}/`) — 45 products a page.
  `--mode product` reads one `item.rakuten.co.jp/{shop}/{code}/` page and
  adds per-variant prices, a variant count and a verified was-price.
- **42 columns**, the first 18 byte-identical to the family prefix. Rakuten's
  own additions include `points` and `point_rate` (a point campaign is a
  discount that never touches the price column), `shop_*` (this is a mall —
  74 distinct merchants across 405 measured rows), `shipping_fee` /
  `free_shipping`, `price_max` and `has_price_range`, `subscription_price`,
  `genre_id` / `genre_path` / `genre_rank`, `is_39shop`, and `variants`.
- **Four back ends**: `playwright_scraper.py` (primary),
  `selenium_scraper.py`, `puppeteer_scraper.py` and `scraper_api_client.py`.
  The three browser engines take the same flags and were verified to produce
  the same rows: 90 rows over two pages, identical skus in identical order,
  every column identical.
- **`page_flow.py`** carrying the six-state classification as DATA, so the
  three engines cannot disagree about whether a page is worth retrying or
  worth paying for.
- **`diff_runs.py`** joining two runs on `sku`, tracking the money columns
  plus `points`, and refusing to compare runs it cannot compare.
- **565 offline checks** (`smoke_test.py`), passing with no engine library
  installed, with fixtures cut from 13 real captures by `make_fixtures.py`
  and verified to parse identically to the untrimmed originals.
- **An ungated daily canary.** A real three-page scrape from a bare GitHub
  runner with no secrets, which is this repo's central claim under test
  rather than a convenience.

### Measured, and worth knowing before you use this

Every figure here is from a real run on 2026-09-21 from one Hetzner
datacentre exit in Helsinki.

- **No paid product is needed for the routes this reads, and the gate is the
  CLIENT rather than the address.** From one address, unchanged: `curl` with
  curl's own User-Agent was served 92 KB and 45 products; the same `curl`
  claiming a Chrome User-Agent got a 43-byte Akamai deny; headless Chromium
  was served 855 KB; headful Chromium 969 KB. What is refused is a
  contradiction between a claimed identity and the TLS fingerprint under it.
- **A refusal arrives under HTTP 200.** The status code is not the signal, so
  detection uses the reference-id shape plus the page being built out of
  Rakuten's own `r10s.jp` assets (90–343 references on served pages, 0–1 on
  refusals).
- **HTTP 503 is a throttle, not a block**, and it renders the identical body
  to the 403 refusal — only the status separates them. A 503 costs a wait at
  the same exit and does not count towards exit 3.
- **Every query is capped at 6,750 results (150 pages of 45)** however many
  it matched. One measured query reported `numFound: 3,053,682` against that
  same 6,750, so a complete run of it is a 0.2% sample. The sidecar records
  both numbers.
- **No captcha is configured anywhere on this site** — zero vendor markers
  across 13 captures, served and refused alike, and Akamai's deny page
  carries no widget. The solver is wired and bounded anyway, because a bot
  manager can be switched on between deploys.
- **The 2Captcha Scraper API path is UNVERIFIED on this site.** No key was
  available to the work that built this repo, so `scraper_api_client.py`
  says exactly that rather than guessing in either direction.

### Fixed before the first release

Five defects found by running the code rather than by reading it — three of
them in code inherited from this family's shared core, where they had been
live for months:

- **`SOLVES_PER_PAGE` was not enforced.** Each engine called the captcha
  solver twice per attempt and counted once, so one page could buy two
  solves while the constant read like a cap. Both call sites now route
  through one budget helper, and the suite counts the call sites, the guards
  and the increments and asserts the three are equal.
- **A fully-painted short page reported "the grid never painted".**
  `found <= threshold` where `wait_for_count` returns as soon as it sees
  `threshold` matches. Only visible on a query with fewer results than the
  readiness floor, which is why it survived.
- **`end_of_listing` was missing from the complete-run set**, so a run that
  correctly found the end of a listing reported exit 6 (partial).
- **`position` was numbered by payload index**, so Rakuten's injected
  sponsored slots (7 of 52 entries on one measured page, and the count
  varies between fetches) shifted every row: the same 45 products came out
  1–45 in one engine and 8–52 in another.
- **The pyppeteer engine was refused from its second navigation onward.**
  Setting a user agent through pyppeteer's CDP override gets that engine
  denied on navs 2–4 while nav 1 is served — and a UA that *matched* the real
  platform was refused just as hard, so it is the override rather than the
  platform mismatch. That engine now sets no user agent, which the suite
  pins.

And two closed while building it:

- **The credential scan could not see inside the fixtures.** Every fixture in
  this family is stored as a JSON string, so its quotes arrive escaped; the
  key-shaped-field rule used bare quotes and therefore matched zero times in
  the largest file in the repository. A real-shaped key planted in a fixture
  passed the scan. The pattern now tolerates escaped quotes, verified by
  planting one.
- **The end-of-listing check compared against the wrong page number.** A run
  started on a URL that already carries `?p=2` asks the site for page 2 while
  calling it page 1 of the run, so `--url '...?p=2' --pages 1` threw away 45
  perfectly good rows as "the end of the listing".

[Unreleased]: https://github.com/2scraper/rakuten-scraper/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/2scraper/rakuten-scraper/releases/tag/v0.2.0
[0.1.0]: https://github.com/2scraper/rakuten-scraper/releases/tag/v0.1.0
