"""Scrape the BazaarPulse competitor price site and load price tables.

    python etl/scrape_bazaarpulse.py

Ground rules (the site is local, but we behave as if it were live):
  * robots.txt is honoured: /internal/ is disallowed and never fetched, and the
    declared Crawl-delay of 1s is respected between every request.
  * FOUR price-markup dialects, one per city: Mumbai <span class="price">₹x,
    Delhi <div class="amt">Rs. x, Chennai <b class="sellingPrice">INR x, and
    Bengaluru data-price-paise="x" (price in paise, "Price on card" as text).
    All four are parsed.
  * Two pagination schemes: /city/{c}/page/{n}.html vs index.html?p=N. The
    query-string pager is broken on a static server; the city directory
    publishes PAGINATION.txt ("Pages after 1 are served at index_p{N}.html"),
    which we fetch and follow.
  * Missing pages (3 product detail pages 404) are skipped, not fatal.
  * Listing titles carry no SKU key. Matching is by normalised title against
    the product master, with an explicit confidence and an unmatched bucket
    that is kept and reported - never silently dropped.

Detail pages (price history) are fetched only for listings that matched one of
our SKUs; unmatched listings do not justify the crawl budget.
Results cache to cache/*.jsonl; load() materialises tables and is also called
by build.py after a rebuild.
"""
import json
import re
import sqlite3
import subprocess
import sys
import time
import urllib.robotparser
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import (ANALYTICS_DB, BAZAARPULSE_SITE_DIR,   # noqa: E402
                    BAZAARPULSE_URL, CACHE_DIR)

LISTINGS = CACHE_DIR / "bazaarpulse_listings.jsonl"
HISTORY = CACHE_DIR / "bazaarpulse_history.jsonl"

CITIES = {"mumbai": "Mumbai", "delhi": "Delhi",
          "bengaluru": "Bengaluru", "chennai": "Chennai"}

CARD = re.compile(r'data-listing-id="(\d+)">\s*'
                  r'<a href="/product/\d+\.html"><strong>(.*?)</strong></a>\s*'
                  r'<div class="muted">(.*?) &middot; (.*?) &middot; (.*?)</div>\s*'
                  r'(?:<span class="price">&#8377;([\d.]+)</span>'                       # Mumbai
                  r'|<span class="pricing-block" data-price-paise="(\d+)"[^>]*>[^<]*</span>'  # Bengaluru
                  r'|<div class="amt"><em>Rs\.</em>\s*([\d.]+)\s*<small>[^<]*</small></div>'  # Delhi
                  r'|<b class="sellingPrice">INR\s*([\d.]+)</b>)\s*'                     # Chennai
                  r'<div class="muted">MRP &#8377;([\d.]+) &middot; (.*?) &middot;.*?</div>\s*'
                  r'<div class="muted">Last seen: (\d{4}-\d{2}-\d{2})</div>', re.S)
PAGE_OF = re.compile(r"page 1 of (\d+)")
HIST_ROW = re.compile(r"<tr><td>(\d{4}-\d{2}-\d{2})</td><td>&#8377;([\d.]+)</td></tr>")


class PoliteSession:
    """requests wrapper that enforces robots.txt permissions and crawl delay."""

    def __init__(self, base: str):
        self.base = base
        self.s = requests.Session()
        self.rp = urllib.robotparser.RobotFileParser(f"{base}/robots.txt")
        self.rp.read()
        self.delay = self.rp.crawl_delay("*") or 1
        self.last = 0.0
        self.fetched = 0

    def get(self, path: str) -> str | None:
        """Returns page text, or None on 404. Refuses robots-disallowed paths."""
        if not self.rp.can_fetch("*", f"{self.base}{path}"):
            raise PermissionError(f"robots.txt disallows {path} - not fetching")
        wait = self.delay - (time.time() - self.last)
        if wait > 0:
            time.sleep(wait)
        self.last = time.time()
        r = self.s.get(f"{self.base}{path}", timeout=10)
        self.fetched += 1
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.text


def site_up() -> bool:
    try:
        return requests.get(f"{BAZAARPULSE_URL}/robots.txt", timeout=3).status_code == 200
    except requests.RequestException:
        return False


def ensure_site() -> subprocess.Popen | None:
    if site_up():
        return None
    if not BAZAARPULSE_SITE_DIR.exists():
        sys.exit(f"BazaarPulse not served at {BAZAARPULSE_URL} and site directory "
                 f"not found at {BAZAARPULSE_SITE_DIR}.")
    port = BAZAARPULSE_URL.rsplit(":", 1)[-1]
    print(f"Serving {BAZAARPULSE_SITE_DIR} on :{port}")
    proc = subprocess.Popen([sys.executable, "-m", "http.server", port,
                             "-d", str(BAZAARPULSE_SITE_DIR)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(20):
        if site_up():
            return proc
        time.sleep(0.5)
    proc.terminate()
    sys.exit("Could not serve the BazaarPulse site.")


def listing_pages(ps: PoliteSession, slug: str):
    """Yield the HTML of every listing page for a city, handling both
    pagination dialects and skipping unreachable pages."""
    first_path = (f"/city/{slug}/page/1.html"
                  if (BAZAARPULSE_SITE_DIR / "city" / slug / "page").exists() or slug in ("mumbai", "delhi")
                  else f"/city/{slug}/index.html")
    first = ps.get(first_path)
    if first is None:
        # dialect probe when the directory layout is unknown
        first_path = f"/city/{slug}/index.html"
        first = ps.get(first_path)
    yield first
    m = PAGE_OF.search(first)
    total = int(m.group(1)) if m else 1
    if "/page/" in first_path:
        pattern = f"/city/{slug}/page/{{n}}.html"
    else:
        # The visible pager links to index.html?p=N, which a static server
        # cannot serve. The city directory documents the real scheme.
        note = ps.get(f"/city/{slug}/PAGINATION.txt")
        if note and "index_p{N}.html" in note:
            pattern = f"/city/{slug}/index_p{{n}}.html"
        else:
            print(f"  [{slug}] no PAGINATION.txt; only page 1 reachable")
            return
    for n in range(2, total + 1):
        page = ps.get(pattern.format(n=n))
        if page is None:
            print(f"  [{slug}] page {n} unreachable (404) - skipped")
            continue
        yield page


def parse_cards(html: str, city: str) -> list[dict]:
    out = []
    for m in CARD.finditer(html):
        (lid, title, retailer, pack, category,
         p_mum, p_paise, p_del, p_che, mrp, stock, seen) = m.groups()
        price = (int(p_paise) / 100.0 if p_paise
                 else float(p_mum or p_del or p_che))
        out.append({
            "listing_id": int(lid), "city": city, "title": title.strip(),
            "retailer": retailer.strip(), "pack": pack.strip(),
            "category": category.strip(), "price_inr": price,
            "site_mrp_inr": float(mrp), "in_stock": int(stock.strip() == "In stock"),
            "last_seen": seen})
    return out


# ---------------------------------------------------------------- matching --

# Retailer-side abbreviations and brand spellings, mapped to the product
# master's vocabulary (each earned its place by appearing in real listings).
REWRITES = [("kestrel sel.", "kestrel select"), ("amritvalley", "amrit valley"),
            ("inst. noodles", "instant noodles"), ("frzn ", "frozen ")]
NOISE_PRE = re.compile(r"^(?:combo|pack of \d+)\s+", re.I)
NOISE_POST = re.compile(r"\s*(?:\(new\)|\|\s*best before[^|]*|-\s*family pack)\s*$", re.I)


def normalise(title: str) -> str:
    t = title.strip().lower()
    for _ in range(2):                       # noise can stack: "Combo X (New)"
        t = NOISE_PRE.sub("", t)
        t = NOISE_POST.sub("", t)
    for a, b in REWRITES:
        t = t.replace(a, b)
    return re.sub(r"\s+", " ", t)


def _fold_kg(s: str) -> str:
    return re.sub(r"(\d)\s*kg\b", r"\1g", s)


def _strip_uom(s: str) -> str:
    return re.sub(r"(\d)\s*(?:ml|kg|g)\b", r"\1", s)


def build_matcher(con):
    """Three indexes over the product master, tried in confidence order:
    exact normalised name (1.0); g/kg-folded (0.8) - both sides of this data
    confuse those units; unit-agnostic (0.6) - same brand+noun+pack number,
    different unit letter (e.g. listing '1000ml' noodles vs master '1000g')."""
    exact, folded, agnostic = {}, {}, {}
    for pid, name in con.execute("select product_id, product_name from dim_product"):
        key = re.sub(r"\s+", " ", name.strip().lower())
        exact[key] = pid
        folded.setdefault(_fold_kg(key), pid)
        agnostic.setdefault(_strip_uom(key), pid)

    def match(title: str) -> tuple[int | None, float, str]:
        t = normalise(title)
        if t in exact:
            return exact[t], 1.0, "exact"
        if _fold_kg(t) in folded:
            return folded[_fold_kg(t)], 0.8, "g_kg_folded"
        if _strip_uom(t) in agnostic:
            return agnostic[_strip_uom(t)], 0.6, "uom_mismatch"
        return None, 0.0, "unmatched"
    return match


def match_all(listings: list[dict]) -> list[dict]:
    """(Re)compute SKU matches for cached listings. Runs at load time so a
    matcher improvement applies without re-crawling the site."""
    con = sqlite3.connect(f"file:{ANALYTICS_DB.as_posix()}?mode=ro", uri=True)
    match = build_matcher(con)
    con.close()
    for r in listings:
        r["product_id"], r["match_confidence"], r["match_method"] = match(r["title"])
    return listings


# ------------------------------------------------------------------- fetch --

def fetch() -> None:
    """Crawl listing pages (once), then detail pages for matched listings.
    Incremental: a matcher improvement or interrupted run only tops up the
    detail pages that are missing from the history cache."""
    CACHE_DIR.mkdir(exist_ok=True)
    proc, ps = None, None
    try:
        if LISTINGS.exists():
            rows = [json.loads(l) for l in
                    LISTINGS.read_text(encoding="utf-8").splitlines() if l]
            print(f"Listings cache present ({len(rows)} rows) - skipping listing crawl.")
        else:
            proc = ensure_site()
            ps = PoliteSession(BAZAARPULSE_URL)
            print(f"robots.txt: crawl-delay {ps.delay}s, /internal/ disallowed -> honoured")
            rows = []
            for slug, city in CITIES.items():
                n0 = len(rows)
                for html in listing_pages(ps, slug):
                    rows.extend(parse_cards(html, city))
                print(f"  [{slug}] {len(rows)-n0} listings")
            LISTINGS.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")

        rows = match_all(rows)
        matched = [r for r in rows if r["product_id"]]
        print(f"Matched {len(matched)}/{len(rows)} listings "
              f"({sum(r['match_method'] == 'exact' for r in rows)} exact, "
              f"{sum(r['match_method'] == 'g_kg_folded' for r in rows)} g/kg-folded, "
              f"{sum(r['match_method'] == 'uom_mismatch' for r in rows)} unit-agnostic); "
              f"{len(rows)-len(matched)} unmatched (kept, reported).")

        have = ({json.loads(l)["listing_id"]
                 for l in HISTORY.read_text(encoding="utf-8").splitlines() if l}
                if HISTORY.exists() else set())
        todo = [r for r in matched if r["listing_id"] not in have]
        if not todo:
            print("Price-history cache is complete - no detail pages to fetch.")
            return
        if ps is None:
            proc = proc or ensure_site()
            ps = PoliteSession(BAZAARPULSE_URL)
        print(f"Fetching {len(todo)} detail pages (of {len(matched)} matched; "
              f"{len(have)} already cached) ...")
        missing, added = 0, 0
        with HISTORY.open("a", encoding="utf-8") as out:
            for i, r in enumerate(todo):
                page = ps.get(f"/product/{r['listing_id']}.html")
                if page is None:
                    missing += 1
                    continue
                for d, p in HIST_ROW.findall(page):
                    out.write(json.dumps({"listing_id": r["listing_id"],
                                          "observed_on": d,
                                          "price_inr": float(p)}) + "\n")
                    added += 1
                if (i + 1) % 100 == 0:
                    out.flush()
                    print(f"  detail pages: {i+1}/{len(todo)}")
        print(f"History: +{added} observations ({missing} detail pages 404 - skipped). "
              f"{ps.fetched} requests this run.")
    finally:
        if proc:
            proc.terminate()


# -------------------------------------------------------------------- load --

def load(db_path: Path = ANALYTICS_DB) -> int:
    """Materialise price_listing (everything, incl. unmatched) and
    price_observation (matched listings x observation dates). No network."""
    if not LISTINGS.exists():
        return 0
    listings = match_all(  # matches recomputed at load time (see match_all)
        [json.loads(l) for l in LISTINGS.read_text(encoding="utf-8").splitlines() if l])
    history = ([json.loads(l) for l in HISTORY.read_text(encoding="utf-8").splitlines() if l]
               if HISTORY.exists() else [])
    history = list({(h["listing_id"], h["observed_on"]): h for h in history}.values())
    con = sqlite3.connect(db_path)
    con.execute("drop table if exists price_listing")
    con.execute("""create table price_listing (
        listing_id int primary key, city text, retailer text, listing_title text,
        pack text, category_site text, price_inr real, site_mrp_inr real,
        in_stock int, last_seen text, product_id int, match_confidence real,
        match_method text)""")
    con.executemany("insert or replace into price_listing values (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    [(r["listing_id"], r["city"], r["retailer"], r["title"], r["pack"],
                      r["category"], r["price_inr"], r["site_mrp_inr"], r["in_stock"],
                      r["last_seen"], r["product_id"], r["match_confidence"],
                      r["match_method"]) for r in listings])

    con.execute("drop table if exists price_observation")
    con.execute("""create table price_observation (
        listing_id int, product_id int, match_confidence real, city text,
        retailer text, listing_title text, observed_price_inr real,
        site_mrp_inr real, our_mrp_inr real, in_stock int, last_seen text,
        observed_on text)""")
    by_id = {r["listing_id"]: r for r in listings if r["product_id"]}
    mrp = dict(con.execute("select product_id, mrp_current_inr from dim_product"))
    rows = []
    for r in by_id.values():                      # the listing's current price
        rows.append((r["listing_id"], r["product_id"], r["match_confidence"],
                     r["city"], r["retailer"], r["title"], r["price_inr"],
                     r["site_mrp_inr"], mrp.get(r["product_id"]), r["in_stock"],
                     r["last_seen"], r["last_seen"]))
    for h in history:                             # plus its observed history
        r = by_id.get(h["listing_id"])
        if r:
            rows.append((r["listing_id"], r["product_id"], r["match_confidence"],
                         r["city"], r["retailer"], r["title"], h["price_inr"],
                         r["site_mrp_inr"], mrp.get(r["product_id"]), r["in_stock"],
                         r["last_seen"], h["observed_on"]))
    con.executemany("insert into price_observation values (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    con.execute("create index if not exists i_po_city on price_observation(city, product_id)")
    con.commit()
    con.close()
    return len(rows)


def main() -> None:
    fetch()
    n = load()
    print(f"price_observation loaded: {n:,} rows")


if __name__ == "__main__":
    main()
