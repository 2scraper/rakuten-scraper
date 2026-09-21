"""
output_writer.py
-----------------
Shared row models + JSON/CSV writers used by all three scrapers.

Two modes, one row shape
------------------------
    --mode listing   a keyword search or a genre listing -> Product
    --mode product   one item.rakuten.co.jp/{shop}/{code}/ page -> Product,
                     with the detail-only fields populated

Both modes yield the SAME class, and on this site that is load-bearing
rather than tidy. Rakuten states a product's identity as
`{shopCode}:{manageNumber}` — the detail page publishes it as
`<meta itemprop="sku">` and it is recoverable from the URL on both routes —
so a listing row and a product row for the same product carry a
byte-identical `sku` and a consumer can JOIN the two files on it. Verified
live: a product run of `sawaicoffee-tea:solandluna` and a genre listing run
that happened to contain it agreed on the id exactly.

What that does NOT buy is a cross-mode DIFF, and `diff_runs.py` still
refuses one (`--force` overrides). The ids line up; the row SETS do not. A
90-row listing run against a 1-row product run would report 89 products
removed, and every line of it would be an artefact of the two runs covering
different things. Same reasoning as the family's refusal to diff a partial
run: the join key being right is necessary and not sufficient.

There is deliberately no `--mode shop`. A merchant's storefront at
`www.rakuten.co.jp/{shopCode}/` looks like it should be a third mode and is
not: its `__INITIAL_STATE__.state.data` is EMPTY and it carries no
`ichibaSearch` payload at all (measured 2026-09-21), so the mode would need
a second parser written against markup nobody has captured. A mode that
ships untested is worse than a mode that is absent — `product_parser`
refuses that URL with that reason instead.

`Product` keeps the family's first eighteen columns in the family's order,
with Rakuten's own ones appended after `position`, so a consumer written
against another repo in this family still reads the prefix unchanged.

Everything below is row-class-agnostic: pass `row_cls` so an empty CSV still
gets the right header for the mode that produced it.
"""

import csv
import json
from dataclasses import dataclass, asdict, field, fields
from datetime import datetime, timezone
from typing import Optional, List, Set, Sequence, Any, Type


# The hostname a row came from. Rakuten Ichiba is ONE marketplace reached
# through several hostnames — `search.rakuten.co.jp` for the result grid,
# `www.rakuten.co.jp` for a genre landing page, `item.rakuten.co.jp` for an
# individual product — and they serve one catalogue in one currency. So this
# column is `rakuten.co.jp` on every row of every run rather than the
# hostname of the moment; which route a row came from is recorded by
# `price_source`, and the merchant behind it by `shop_code`.
#
# It is kept in this position because the family's schema has it here and
# consumers read the columns by name across repos.
SOURCE_DEFAULT = "rakuten.co.jp"


@dataclass
class Product:
    """One row per PRODUCT.

    The first eighteen fields are the family prefix, byte-identical and in
    the same order as every other repo in this family, so one consumer reads
    a rakuten run and a mediamarkt run with the same code. Everything after
    `position` is Rakuten's own.

    Rakuten is a MALL — 74 distinct merchants across 405 measured rows — and
    two of the prefix's assumptions need saying out loud rather than being
    quietly wrong:

      * A "product" here is one merchant's listing, not a catalogue entry.
        The same physical article is sold by many shops at many prices under
        many ids, so two rows with different `sku`s can be the same coffee.
        Rakuten's own cross-merchant catalogue id is `productUrl`, which is
        null far too often to dedupe on; `shop_code` is what makes a row's
        identity legible.
      * Several columns are constant on the listing route because of what
        that route returns rather than because of what the parser reads:
        `in_stock` is True on all 405 measured rows (the search excludes
        sold-out items) and `original_price` is null on all 405 (the listing
        payload publishes no was-price). Both take other values on the
        detail route. `price_source` says which route a row came from, so a
        consumer can tell a measured absence from a missing read.
    """
    source: str = SOURCE_DEFAULT
    scraped_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    # The product's own absolute URL on item.rakuten.co.jp, as the site
    # publishes it, with Rakuten's tracking parameters (`scid`, `iasid`,
    # `rafcid`, `l-id`, …) stripped so two runs of the same listing produce
    # the same URL. Never rebuilt from the browsed host: two sibling repos
    # lost a whole column by rebuilding a URL the page then never matched.
    url: str = ""
    # `{shopCode}:{manageNumber}` — Rakuten's own item code, exactly as the
    # detail page states it in `<meta itemprop="sku">`.
    #
    # Recovered from the URL rather than from the payload, and that is
    # measured rather than stylistic. The payload's `variantId` is the
    # obvious candidate and it names the pre-selected SKU INSIDE the item,
    # not the item: it equals the URL's item code on only 39 of 180 rows.
    # The rest read `ac-sale-1960-80-as-m` for a product that is
    # `ac-sale-1960`, so a `variantId` sku would make two runs of an
    # unchanged listing diff as wholesale replacement every time a shop
    # changed which variant it shows first.
    #
    # `variant_id` below carries that value where it is wanted.
    sku: Optional[str] = None
    title: Optional[str] = None
    # The manufacturer, and only where Rakuten states one as a taxonomy
    # value: the item's tag under the tag group Rakuten itself names
    # ブランド. Populated on 240 of 405 measured rows (59%).
    #
    # `hit["brand"]` — the field that looks like the source — is null on
    # 405 of 405 and would make this a dead column (§9). Deliberately NOT
    # derived by splitting the title: a Rakuten product name opens with
    # campaign text (`【9/24まで P10倍！】`) far more often than with a maker,
    # so a split would be a guess wearing a fact's clothes.
    brand: Optional[str] = None
    # The tax-included price a buyer pays today, in yen, as an integer —
    # JPY has no subunit. For an item whose variants differ in price this is
    # the LOWEST (what the tile prints), and `price_max` carries the
    # highest; 95 of 405 rows have a range.
    #
    # Never the subscription price, which is lower and is a different offer:
    # it has its own column.
    price: Optional[float] = None
    # JPY — and read, never defaulted. The listing payload states no
    # currency at all (zero occurrences of `priceCurrency` or `JPY` across
    # 405 rows), so the value comes from the page's own structured data:
    # `offers.priceCurrency` in the listing page's JSON-LD, or
    # `<meta itemprop="priceCurrency">` on a detail page. Null rather than
    # defaulted when a page states nothing (§4).
    currency: Optional[str] = None
    # The was-price, and ONLY where Rakuten's own
    # `doublePrice.referencePriceVerified` flag is true — Japan's
    # double-pricing rules require a merchant's reference price to be
    # substantiated, and this field is the outcome of that check. Null on
    # every listing row, because the listing payload carries no was-price of
    # any kind; populated on 2 of 8 measured detail pages (¥10,398 against
    # ¥5,199, and ¥3,476 against ¥1,738).
    #
    # `original_price_label` records WHICH reference price it was.
    original_price: Optional[float] = None
    # Computed from the two prices, never read off a printed badge, and
    # None rather than 0 or a negative when `original_price` is not above
    # `price` — two figures that are not what they were taken for should
    # stop the arithmetic rather than produce a plausible wrong number.
    discount_pct: Optional[float] = None
    # Rakuten's review score, rounded to two places, and NULL when nobody
    # has reviewed the product.
    #
    # The rounding and the null are both measured. `review` is never absent
    # on this site — an unreviewed product comes back
    # `{score: 0, numReviews: 0}`, on 28 of 405 rows (6.9%) — and a 0
    # written through as a rating drags every average a consumer computes,
    # so both columns go null together keyed on the COUNT. The rounding is
    # needed for a different reason: the listing route serializes this as
    # `4.76` while the detail route serializes the same product as
    # `4.760000228881826`, so without it every product run would diff
    # against its listing run on every row.
    rating: Optional[float] = None
    review_count: Optional[int] = None
    # True on 405 of 405 measured listing rows, including pages 80, 120, 149
    # and 150 of a 142,000-hit genre, because Rakuten's search does not
    # return sold-out items rather than marking them. So a False on the
    # listing route is UNPROVEN; on the detail route it comes from the
    # page's own `itemprop="availability"`, read as an allowlist of the
    # values that mean available so an unanticipated one reads as "not
    # available" rather than as a sale.
    in_stock: Optional[bool] = None
    image_url: Optional[str] = None
    # The product's genre path as Rakuten names it
    # (`水・ソフトドリンク > コーヒー > インスタントコーヒー`) — a fact about
    # the product, not about the query that found it. Falls back to the
    # URL's own category only when the payload carries no genres.
    category: Optional[str] = None
    # Which of Rakuten's structured sources this row was read from:
    #   state      __INITIAL_STATE__.state.data.ichibaSearch (listing)
    #   itemdata   the item-page-app-data island (detail page)
    #   dom        the fallback, when neither was in the page
    # A `dom` row has every payload-only column null, so this is how a
    # consumer tells a thin row from a missing value.
    price_source: Optional[str] = None
    page: Optional[int] = None
    position: Optional[int] = None
    # ---- Rakuten's own columns ----
    # The pre-selected SKU inside the item — see `sku`. Not an identity.
    variant_id: Optional[str] = None
    # Rakuten's numeric per-shop item number (`10000338`). Unique only
    # within a shop, which is why it is not the sku.
    item_number: Optional[str] = None
    # The top of the variant price range, where the variants differ.
    price_max: Optional[float] = None
    has_price_range: Optional[bool] = None
    # 定期購入 — the price for a recurring order, on 54 of 405 rows. ALWAYS
    # below `price` and deliberately not read as a was-price: it is a
    # different offer on the same product, not a reduction of this one.
    subscription_price: Optional[float] = None
    # Rakuten points granted on purchase, and the multiplier behind them.
    # Worth a column on this site specifically: a 10x point campaign is
    # effectively a 10% discount that never touches the price, so a
    # price-only view of Rakuten misses most of what moves.
    points: Optional[int] = None
    point_rate: Optional[int] = None
    # 0 means free shipping, which is the common case (317 of 405 rows).
    # `free_shipping` is that comparison made once, here, rather than in
    # every consumer.
    shipping_fee: Optional[float] = None
    free_shipping: Optional[bool] = None
    delivery_estimate: Optional[str] = None
    # The merchant. Rakuten is a mall, so these are not run metadata.
    shop_name: Optional[str] = None
    shop_code: Optional[str] = None
    shop_id: Optional[int] = None
    shop_url: Optional[str] = None
    genre_id: Optional[int] = None
    genre_path: Optional[str] = None
    # The product's rank in its GENRE ranking, where Rakuten prints one on
    # the tile — 20 of 405 rows, values 1 to 3.
    #
    # Explicitly NOT a position in this result set, and the distinction
    # matters: a sibling repo uses published ranks to prove cards went
    # missing ("30 rows spanning ranks 1-50"), and that arithmetic does not
    # work here. This rank is the item's standing in a different listing
    # altogether.
    genre_rank: Optional[int] = None
    is_super_deal: Optional[bool] = None
    # 39ショップ — a shop in Rakuten's free-shipping-over-¥3,900 programme.
    is_39shop: Optional[bool] = None
    is_official_shop: Optional[bool] = None
    variant_count: Optional[int] = None
    # WHICH reference price `original_price` was — `当店通常価格` ("this
    # shop's usual price") is the one kind observed. Recorded rather than
    # dropped because a sibling repo found two kinds of struck-through price
    # in near-identical markup, one above the price and one below it, and
    # read the second as the first.
    original_price_label: Optional[str] = None
    # Rakuten's own tag groups for the item, as {group: [values]}.
    tags: Optional[dict] = None
    # Per-variant prices off a detail page: variant_id, label, price,
    # subscription_price, hidden. Null on every listing row — the listing
    # payload states only the range.
    variants: Optional[list] = None


# One kind of thing, one dataclass. A detail page here is not a different
# kind of object from a tile — it is the same product described more fully —
# so both modes map to the same row class and there is no second one to keep
# in step.
ROW_CLASS_BY_MODE = {"listing": Product, "product": Product}

# Modes whose rows are one-per-sku, and therefore safe to dedupe on `sku` and
# to hand to diff_runs.py. Both of this repo's modes qualify: a listing page
# names each product once, and a product page IS one product.
UNIQUE_BY_SKU_MODES = ("listing", "product")


def dedupe_by_key(rows: Sequence[Any], seen: Set[str], key: str = "sku") -> List[Any]:
    """Drop rows whose key already appeared earlier in this same run.

    `seen` is mutated in place, so callers thread the same set across pages —
    a stale or repeating next-page link then re-parses a page without
    duplicating its rows into the final output. On this site this DOES fire
    on healthy runs: page 1 and page 2 of one category listing shared
    exactly 3 products, all three from the "cheaper products" carousel that
    appears on every page of a listing. So a small non-zero drop count here
    is expected and a large one is not.

    A row with no key is always kept: there is nothing to check a duplicate
    against, and dropping it would be a silent data loss rather than a
    duplicate removal.

    Both of this repo's modes are one row per `sku`, so `key` is never
    overridden here — the parameter exists because the rest of the family
    shares this function and one of them needs it.
    """
    fresh = []
    for r in rows:
        val = getattr(r, key, None)
        if val is None or val not in seen:
            if val is not None:
                seen.add(val)
            fresh.append(r)
    return fresh


# Kept under its old name: the engines and smoke tests in this family all
# call it, and a listing run does dedupe by sku.
def dedupe_by_sku(rows: Sequence[Any], seen: Set[str]) -> List[Any]:
    return dedupe_by_key(rows, seen, key="sku")


# CSV cannot hold a list. Joining with " | " keeps the cell readable in a
# spreadsheet and round-trippable by splitting on the same separator; the
# JSON output keeps the real list, so nothing is lost for a consumer that
# wants structure. `repr()` of a Python list (the default if this is not
# handled) is neither readable nor parseable by anything but Python.
LIST_CSV_SEPARATOR = " | "


def _csv_value(v: Any) -> Any:
    if isinstance(v, (list, tuple)):
        return LIST_CSV_SEPARATOR.join(str(x) for x in v)
    # `tags` and each entry of `variants` are mappings, and a Python repr
    # of one is neither readable in a spreadsheet nor parseable by anything
    # but Python.
    # Compact JSON is both, and round-trips through json.loads.
    if isinstance(v, dict):
        return json.dumps(v, ensure_ascii=False, separators=(",", ":"))
    return v


def write_json(rows: Sequence[Any], path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump([asdict(r) for r in rows], f, ensure_ascii=False, indent=2)


def write_csv(rows: Sequence[Any], path: str, row_cls: Type = Product) -> None:
    # An empty result still gets the header row. A zero-byte file makes a
    # consumer fail on read (no columns to parse) instead of reading a valid
    # table with zero rows — and "an empty result is still a well-formed
    # result" is the same principle as `save` refusing to overwrite good data.
    #
    # The header comes from `row_cls`, not from the first row, so an empty
    # run still writes the columns of the mode that produced it.
    fieldnames = [f.name for f in fields(row_cls)]
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            writer.writerow({k: _csv_value(v) for k, v in asdict(r).items()})


# Exit code used when a run completes but produced nothing. Distinct from 1
# (crash) so a caller can tell "ran, found nothing" from "blew up".
EXIT_NO_PRODUCTS = 4

# Exit code for a run blocked by a bot-check/challenge page before parsing
# even started — distinct from EXIT_NO_PRODUCTS so a caller can tell "the
# search genuinely matched nothing" from "something stood between us and the
# content". See product_parser.detect_bot_challenge.
#
# On this site this code specifically does NOT cover the three ways to get a
# real page with no products on it: a `/p/<slug>` discovery hub, which
# answers 200 with banners and carousels and no grid; a search whose query
# matches nothing ("Oops, produk nggak ditemukan"); and one page past the
# end of a category listing. All three are EXIT_NO_PRODUCTS — the request
# was served exactly as asked and simply has no products on it. Reporting
# any of them as blocked would send a user hunting for a proxy problem that
# does not exist.
#
# What EXIT_BLOCKED means here is unusually literal: this site refuses a
# address it has scored NOTHING at all. No status code, no interstitial, no
# vendor marker — the HTTP/2 stream is reset and the run sees a connection
# error rather than a page.
EXIT_BLOCKED = 3

# Exit code for a run that gathered SOME rows and then stopped early — a
# page-load timeout, a 503 throttle, or a challenge on page 3 of 10. The
# output file is still written (throwing away three good pages would be
# worse), but it is not a complete picture, and a consumer that cannot tell
# the difference will read the pages that were never fetched as products that
# disappeared from the catalogue. See write_run_meta.
# A REMOTE service failed — the Scraping Browser refusing the connection
# (`profile_locked` is the common one: a profile allows a single live
# connection), or the Scraper API answering an error. Distinct from 1 (a
# crash in this code) and from 2 (bad usage) because it means "try again, or
# use a different profile", not "there is a bug here". Defined once, here,
# because the browser engines and scraper_api_client.py both return it and
# two definitions of the same code is exactly how a family's exit contract
# drifts.
EXIT_API_ERROR = 5

EXIT_PARTIAL = 6


def write_run_meta(out_prefix: str, meta: dict) -> str:
    """Write a run-metadata sidecar next to the output, return its path.

    Deliberately a separate `<out>.meta.json` rather than columns on every
    row: this describes the RUN, not the product, and repeating it across
    every row would both bloat the output and change the schema every
    consumer of this project already parses.

    diff_runs.py reads it to refuse a comparison between runs that are not
    both complete, and between runs of different `mode`.
    """
    path = f"{out_prefix}.meta.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    print(f"[+] Wrote run metadata -> {path} (status={meta.get('status')})")
    return path


def run_meta(status: str, stop_reason: str, pages_requested: int,
             pages_completed: int, start_url: str, final_url: str,
             products: int, pages_failed: Optional[List[int]] = None,
             mode: str = "listing", source: str = SOURCE_DEFAULT,
             extra: Optional[dict] = None) -> dict:
    """Build the metadata dict for a finished run.

    `status` is the field a consumer branches on:
      complete — every requested page was fetched, or the site's own
                 pagination genuinely ran out (nothing more existed to get)
      partial  — rows were gathered, then the run stopped early
      failed   — nothing was gathered at all

    `mode` and `source` are recorded because `mode` is not implied by the
    repo: the same output prefix can hold a listing run or a product run,
    and those populate different columns — `sold` is a FLOOR on a listing
    row and exact on a product row, so diffing one against the other would
    report every row as changed. diff_runs.py refuses a pair whose modes or
    sources differ. `source` is `rakuten.co.jp` on every row of every run
    here, since the site has one storefront and one currency; it is kept
    because consumers read these columns by name across the family.

    `extra` carries facts about the run that are not about any single row.
    `--mode shop` uses it for the SELLER's own name, location, rating and
    review count: a run covers exactly one shop, so those belong to the run
    rather than repeated down a column, and the shop's review count (16679
    on the captured seller) is a different number from its listings' own
    (827 on one of them) — putting them in one column would make the schema
    lie.

    `pages_failed` lists the pages that did not yield data, by number.
    `pages_completed` alone was enough only while pages were fetched strictly
    in order, where "3 of 10 completed" could only mean 1-2-3: a count is not
    a description once pages can be fetched independently and page 3 can fail
    while 4 and 5 succeed. Recording the numbers keeps the sidecar honest
    about WHICH part of the catalogue is missing, not just how much.
    """
    meta = {
        "source": source,
        "mode": mode,
        "status": status,
        "stop_reason": stop_reason,
        "pages_requested": pages_requested,
        "pages_completed": pages_completed,
        "pages_failed": pages_failed or [],
        "products": products,
        "start_url": start_url,
        "final_url": final_url,
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }
    if extra:
        # Merged rather than nested under a key, so a consumer reads
        # `shop_rating` at the top level beside `products`. Run fields win a
        # name collision: a caller cannot accidentally overwrite `status`.
        meta.update({k: v for k, v in extra.items() if k not in meta})
    return meta


def save(rows: Sequence[Any], out_prefix: str, fmt: str,
         allow_empty: bool = False, row_cls: Type = Product) -> int:
    """Write JSON/CSV and return a process exit code.

    Returns 0 when rows were written, EXIT_NO_PRODUCTS when there were none.
    Callers are expected to exit with it.

    On zero rows, nothing is written at all unless `allow_empty`. Two reasons,
    and a live run demonstrated both. A page-load timeout produced
    `Saved 0 products -> out.json` and exit 0: a two-byte `[]` that a
    consuming pipeline reads as a successful run with no stock. Worse, if the
    file already held a good result from an earlier run, that result is now
    gone — the failure destroyed the last known good data. So an empty result
    leaves the previous file intact and says why.

    `allow_empty=True` is for the legitimate case: a filter that genuinely
    matches nothing, where an empty file is the answer.
    """
    if not rows and not allow_empty:
        print(f"[!] 0 products — refusing to write {out_prefix}.json/.csv, so an "
              f"earlier good result isn't overwritten with an empty one. "
              f"Pass --allow-empty if an empty result is the expected answer.")
        return EXIT_NO_PRODUCTS

    if fmt in ("json", "both"):
        write_json(rows, f"{out_prefix}.json")
        print(f"[+] Saved {len(rows)} products -> {out_prefix}.json")
    if fmt in ("csv", "both"):
        write_csv(rows, f"{out_prefix}.csv", row_cls=row_cls)
        print(f"[+] Saved {len(rows)} products -> {out_prefix}.csv")
    return 0 if rows else EXIT_NO_PRODUCTS


# Stop reasons that mean the run saw everything there was to see. Anything
# else ended the page loop early, so the result is only a partial view.
#
# "no_new_products" belongs here and "pagination_exhausted" is kept for the
# engines that still stop on a missing next-link: the first is a property of
# the DATA (a page contributed nothing not already seen, so the listing is
# over), while the second is a property of a CSS SELECTOR and is therefore
# the weaker signal — a renamed attribute looks identical to a short
# catalogue. On this site that ordering is not a preference, it is the only
# thing that works: the site publishes NO `link[rel=next]` and no numbered
# anchors anywhere, a CATEGORY listing is addressable by `?page=N`, and a
# SEARCH is not addressable at all — `?page=2` there returns an empty result
# set rather than page 2. So "no new products" is the one termination
# condition available on a search. See page_flow.pagination_is_addressable.
#
# "single_page_mode" is complete by construction: --mode product reads one
# page because one page is all there is.
# `end_of_listing` is a COMPLETE result and leaving it out of this tuple is a
# bug worth naming, because it produced exit 6 for a correct run. On this
# site a listing does not end with an error or an empty page: Rakuten answers
# a request past the last page by serving page 1 again under HTTP 200, and
# the engine detects that from the offset the server states rather than from
# its own request. Having found the real end of the results, the run has
# everything the site will give it.
COMPLETE_STOP_REASONS = ("completed", "pagination_exhausted", "no_new_products",
                         "single_page_mode", "end_of_listing")


def finish_run(rows: Sequence[Any], out_prefix: str, fmt: str,
               allow_empty: bool, *, blocked: bool, stop_reason: str,
               pages_requested: int, pages_completed: int,
               start_url: str, final_url: str,
               pages_failed: Optional[List[int]] = None,
               mode: str = "listing", source: str = SOURCE_DEFAULT,
               extra: Optional[dict] = None) -> int:
    """Write output + the run-metadata sidecar; return the exit code.

    Shared by all three browser engines so the status/exit-code mapping
    cannot drift between them.

    The metadata sidecar is written ONLY when the row file was written.
    Otherwise a failed run would leave a "status": "failed" sidecar next to
    the previous run's still-intact good output (which `save` deliberately
    does not overwrite) — the two files would contradict each other, and
    diff_runs.py would refuse to compare data that is in fact fine.
    """
    complete = stop_reason in COMPLETE_STOP_REASONS
    row_cls = ROW_CLASS_BY_MODE.get(mode, Product)
    rc = save(rows, out_prefix, fmt, allow_empty=allow_empty, row_cls=row_cls)
    wrote_output = bool(rows) or allow_empty

    if wrote_output:
        status = "complete" if (rows and complete) else (
            "partial" if rows else "failed")
        write_run_meta(out_prefix, run_meta(
            status=status, stop_reason=stop_reason,
            pages_requested=pages_requested, pages_completed=pages_completed,
            pages_failed=pages_failed, mode=mode, source=source,
            start_url=start_url, final_url=final_url, products=len(rows),
            extra=extra))

    if not rows:
        # Nothing gathered at all: a challenge outranks "empty result",
        # because it says something stood between the run and the content.
        return EXIT_BLOCKED if blocked else rc
    if not complete:
        print(f"[!] Partial run: stopped after {pages_completed} of "
              f"{pages_requested} page(s) ({stop_reason}). The output holds "
              f"what was gathered, but it is NOT a complete view — see "
              f"{out_prefix}.meta.json.")
        return EXIT_PARTIAL
    return rc
