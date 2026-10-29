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

> **Behaviour change: a page that stays throttled is a failed page.** A page
> still answering HTTP 503 after the three waits (6, 15, 30 s) used to count
> as read with no rows: the run reported `status: complete` and exit 0 with
> page 1 missing and `currency` null on every row. It is now recorded as
> failed (`stop_reason: throttled`, named in `pages_failed`): exit 6 when
> other pages produced rows, exit 5 when none did. Under `--concurrency` it
> no longer stops dispatch as if the listing had ended.
>
> **Behaviour change: `--concurrency N` and a typed `--proxy` or
> `--proxy-file` are refused with `--cdp-endpoint`** (exit 2, with the reason)
> in all three engines, as the README already said. They used to be ignored
> with a warning and the run carried on. A `RAKUTEN_PROXY` that only comes
> from `.env` is still ignored with a warning.
>
> **Behaviour change: a repeated key in `.env` — the last one wins.** The
> built-in reader used the first, `python-dotenv` the last, so the same file
> read differently depending on whether that package was installed. A warning
> names the repeated key, never its value.

### Fixed

- **Playwright carried on after page 1 failed.** It planned the next page's
  URL from a page that never loaded and raised out of a dead proxy's browser
  (`Execution context was destroyed`): a traceback and exit 1 where every other
  failure reports exit 5. Pages 2-N were also fetched into a run whose sidecar
  already said it had stopped. A failed page 1 now ends the run, as in the
  other two engines.
- **An exit that accepts the connection and drops it was not replaced.**
  Chromium reports that as `net::ERR_CONNECTION_CLOSED`, not on the proxy-error
  list, so the run retried the same exit three times (in milliseconds, logged
  as "Timeout loading") and ended `partial` with working exits unused. Once the
  retries on an exit are spent and the pool holds another, the engine rotates,
  bounded by `--proxy-block-retries`. Load failures now log what the browser
  said instead of "Timeout".
- **`--out` into a directory that did not exist lost the whole run** (a
  traceback after every page had been fetched). The directory is now created
  before the first request, in all three engines and in the Scraper API
  client, where the request is paid; one that cannot be created is exit 2.
- **Selenium reported a proxy's 407 as a block by the site** ("blocked ...
  Akamai refuses this site", exit 3). Chromium does not raise for it and
  leaves a blank page; the engine now reads the navigation's own status
  (`responseStatus`), reports a failed load naming the 407 (exit 5, or a
  rotation) and says that a credentialled proxy is expected to refuse.
- **pyppeteer's browser that would not start ended in a traceback and exit 1.**
  It is now one sentence and exit 5, with `--chromium-path` named. The Apple
  Silicon case itself was not reproduced (Linux, a browser that exits at once,
  gave the same error).
- **A proxy list named `exits.txt` was not git-ignored**, though the README's
  own example uses that name and the file holds logins. `exits*.txt`,
  `proxies*.txt`, `proxylist*.txt` and `proxy*.list` are ignored.
- **The proxy example named a host that cannot work.** `jp.proxy.2captcha.com`
  resolves to the same Cloudflare addresses as the website and times out; the
  gateways are `ap`, `na` and `eu`, and the country is the `-region-jp`
  segment of the login. `.env.example` and the README now say so.
- **`diff_runs.py` told a reader with two complete runs of different modes to
  "re-run the incomplete side".** The advice now matches the refusal.

### Changed

- README: re-measured claims. The two `curl` rows of the access table
  (919,197 bytes and 42 bytes, 2026-10-29), the `?p=151` redirect target, and
  the item example, whose was-price is gone because the sale ended. The
  "identical output on all three engines" claim is narrowed to what was
  measured: it held on 2026-09-21 and did not on 2026-10-29 (one slot a page
  differed between Playwright and Selenium). The Scraper API price is no
  longer stated as a constant, the offline-check count is gone, and
  `pytest` is documented as living in the `dev` extra.
- CI only: the canary now runs weekly (Mondays 02:17 UTC) instead of daily, and on a manual dispatch.

## [0.3.0] — 2026-10-16

> **Behaviour change: a 403 refusal is now a block, not a throttle.** No
> engine used to pass the HTTP status to the page classifier, and Rakuten
> renders its 403 refusal and its 503 rate-limit page with the same body, so
> every 403 was read as a throttle: waited out three times (about 51 s) at
> the same exit and then reported as a failed page. Playwright and pyppeteer
> now carry the navigation's status into the classifier, so a 403 is
> handled as a block (re-fetch or rotate, then exit 3) and a 503 is still
> waited out. Selenium has no status to give and keeps the recoverable
> reading.
>
> **Behaviour change: output files honour your umask.** `.json`, `.csv` and
> `.meta.json` were written mode 0600 since the atomic writer arrived in
> 0.2.1. A new file now gets `0666` minus the umask (0644 under the usual
> 022), and rewriting an existing file keeps that file's mode.
>
> **Behaviour change: more numeric flags are checked at the command line.**
> `--pages 0`, `--concurrency 0`, a negative `--delay`, `--retry-delay` or
> `--proxy-block-retries` are now usage errors (exit 2) naming the flag, in
> all three engines; `scraper_api_client.py` rejects a negative `--retries`
> or `--retry-delay` and a `--timeout` below 1. `--pages 0` used to fetch
> page 1 anyway and record `pages_requested: 0`.
>
> **Behaviour change: the canary is red whenever it did not test the site.**
> A blocked (exit 3) or partial (exit 6) run used to end the job green with
> a warning. Every non-zero exit now fails it.
>
> **Behaviour change:** `--retries 0` is now a usage error (exit 2). It used
> to be accepted and made the page-load loop unreachable, so nothing was
> fetched and the engine reported the browser's own blank page as a result:
> exit 3 or 4 with a 39-byte "capture", in tens of milliseconds. If a
> script of yours passes 0, pass **1**: the flag counts ATTEMPTS, so 1 means
> "try once and do not retry".

### Fixed

- **`--retries 0` fetched nothing and called it a result.** The load loop is
  `range(1, retries + 1)`, so 0 never runs it and the engine judged
  `about:blank`. Three third-party audits of this family ran exactly that and
  reported a working scraper as broken. `--retries` now goes through
  `page_flow.attempts` in all three browser engines; a check reads the
  source of each one and calls the validator, so a new engine declaring the
  flag with a plain int fails the suite. (`scraper_api_client.py --retries`
  counts EXTRA attempts, so 0 stays legal there.)
- **pyppeteer crashed with `AttributeError` when a page kept failing to load
  and no proxy was configured.** Its load-failure branch called
  `pool.advance()` without checking for a pool. It now rotates only for an
  unusable exit and only when there is another exit to go to, as the
  Playwright and Selenium engines already did.
- **A failing captcha solver could be asked twice for one page.** The first
  of the two solve call sites counted only successful solves against the
  per-page allowance. Both now count every attempted purchase.
- **The static reCAPTCHA detector missed a v2 widget** in a saved page that
  carried only the `g-recaptcha` container or the `api2/anchor` frame,
  without the literal `grecaptcha`, so a `--dump-html` capture of one was
  reported as having no captcha. No captcha has been observed on Rakuten;
  this only affects diagnosis.
- **Help text and comments described another site's refusal** (an HTTP/2
  stream reset, a headless-only "Access Denied" page). The `--solve-captcha`
  help in all three engines and the exit-code comments now describe
  Rakuten's own: a 43-byte Akamai deny under HTTP 200, a branded page under
  HTTP 403, and the same page under HTTP 503 as a rate limit.
- **The canary's exit-5 message** said a remote API error was impossible;
  exit 5 means the content was never obtained, which a transport failure can
  cause on any run.
- `.gitignore` covered `--dump-html` page dumps only up to `.page99`;
  `--pages` goes to 150. `requirements-selenium.txt` mentioned a
  `--chromedriver` flag that does not exist. `.env.example` no longer gives
  a fixed lifetime for Scraping Browser credentials.
- Comments and docstrings no longer cite the numbered sections of internal
  notes the public cannot read; each rule is stated in this repository's own
  words, and the suite fails if such a citation comes back.
- The CI sample-output check runs from one implementation
  (`ci_checks.py --sample-check`) instead of also as an inline copy, and the
  wording scan now covers `smoke_test.py` itself.

## [0.2.1] — 2026-09-30

> **The output files are now replaced atomically.** Before this release an
> interrupted run could leave a shorter file where a complete one had been —
> measured, a 2,084-byte good `out.json` came back 0 bytes and invalid JSON.
> If you have ever seen a truncated output or sidecar, that was this.
>
> **CSV cells that begin `=`, `+`, `-`, `@`, tab, CR or LF now carry a
> leading apostrophe**, so a spreadsheet reads them as text rather than as a
> formula. The JSON output is unchanged and still carries the site's own
> bytes; `csv_cells_escaped` in `<out>.meta.json` says how many cells the
> two files differ in. On this site that number is 0 today — measured across
> 2,662 stored rows — so no existing CSV changes.

### Fixed

- **Output files are now replaced atomically.** `write_json`, `write_csv`
  and `write_run_meta` each wrote with `open(path, "w")`, which truncates
  before a single byte is written — so a crash, a kill or a full disk
  halfway through left a shorter file where a complete one had been.
  Reproduced before the fix: a 2,084-byte good `out.json` came back 0 bytes
  and invalid JSON after an interrupted write. Each file is now written to a
  temporary file in the same directory, `fsync`ed and renamed over the
  target, so an interrupted run leaves the previous output intact. This is
  the same promise `save()` already kept by refusing to overwrite good data
  with an empty result, broken by a different route; the sidecar matters
  most, being the file a consumer branches on.
- **A CSV cell can no longer be read as a formula.** A cell beginning `=`,
  `+`, `-`, `@`, tab, CR or LF is executed by a spreadsheet, and every
  string in a row here is merchant-authored. Such cells are prefixed with an
  apostrophe in the CSV only — the JSON still carries the site's own bytes —
  and the new `csv_cells_escaped` field in `<out>.meta.json` declares how
  many cells the two outputs therefore differ in. Only strings are escaped:
  turning `-5` into text would break every sum a consumer writes over the
  column. Measured on this site on 2026-09-30, 0 of 42,592 string cells
  across 2,662 stored rows are formula-shaped, so this changes no output
  today; a sibling site measured 1 cell in 454.
- **CI now tests Python 3.13.** The README badge has claimed `3.9 | 3.13`
  while the offline matrix ran 3.9 and 3.12 — the ceiling a reader sees
  first was the one version nobody ran. The matrix is now the two ends the
  badge names.

- **Scraper API: `waitFor` is now sent as a JSON object.** It went out as a
  JSON-encoded string, following a docstring that said the API required one.
  Measured 2026-09-23 against `/tasks/sync`: the string form is refused with
  HTTP 422 ("params.waitFor must be an object") and still billed ($0.0005);
  the object form answers HTTP 200. Every `--wait-*` run was therefore
  a paid exit 5.
- **Scraper API: the target's HTTP status is read from `http_code`.** The
  client read `status`, which is the API's own verdict string (`"success"`),
  so the page classifier never saw a target 403/503 — a live rakuten run
  logged "Upstream page status success". `status` is now a fallback only
  when it is an integer.
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

[Unreleased]: https://github.com/2scraper/rakuten-scraper/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/2scraper/rakuten-scraper/releases/tag/v0.3.0
[0.2.1]: https://github.com/2scraper/rakuten-scraper/releases/tag/v0.2.1
[0.2.0]: https://github.com/2scraper/rakuten-scraper/releases/tag/v0.2.0
[0.1.0]: https://github.com/2scraper/rakuten-scraper/releases/tag/v0.1.0
