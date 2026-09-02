"""Fetch carrier freight invoices from the partner API and load fct_freight.

    python etl/fetch_freight.py

Behaviour the API is documented to throw at us (and how we answer it):
  * 429 with Retry-After            -> sleep exactly what it asks, retry
  * 503                             -> exponential backoff with jitter
  * slow first page                 -> generous timeout
  * cursor pagination, 208 pages    -> every page appended to a local JSONL
                                       cache with the cursor checkpointed, so a
                                       crash resumes instead of restarting
  * amount in paise                 -> converted to INR on load
  * timestamps UTC                  -> service/invoice dates are dates (no shift)

The network walk runs ONCE; afterwards the cache is the source. `load()` is
side-effect-free w.r.t. the network and is also called by build.py so a
rebuilt analytics.sqlite re-materialises fct_freight from cache.

The date filter (`from`/`to`) is deliberately not used: the server applies it
after slicing pages, so a filtered walk takes as long as a full one (verified
in the server source). We take everything once and slice locally forever.
"""
import datetime as dt
import json
import random
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from build import fy_parts                                          # noqa: E402
from config import (ANALYTICS_DB, CACHE_DIR, PARTNER_API_KEY,       # noqa: E402
                    PARTNER_API_SCRIPT, PARTNER_API_URL)

RAW = CACHE_DIR / "freight_invoices.jsonl"
STATE = CACHE_DIR / "freight_cursor.json"
SURCHARGE = CACHE_DIR / "fuel_surcharge.json"
HEADERS = {"X-API-Key": PARTNER_API_KEY}
MAX_TRIES = 10


def api_up() -> bool:
    try:
        return requests.get(f"{PARTNER_API_URL}/v1/health", timeout=3).status_code == 200
    except requests.RequestException:
        return False


def ensure_api() -> subprocess.Popen | None:
    """Start the pack's mock server if nothing is listening; return the process
    if we started it (so we can stop it afterwards)."""
    if api_up():
        return None
    if not PARTNER_API_SCRIPT.exists():
        sys.exit(f"Partner API not running at {PARTNER_API_URL} and server script "
                 f"not found at {PARTNER_API_SCRIPT}. Start it manually.")
    print(f"Starting partner API: {PARTNER_API_SCRIPT}")
    proc = subprocess.Popen([sys.executable, str(PARTNER_API_SCRIPT)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(40):
        if api_up():
            return proc
        time.sleep(0.5)
    proc.terminate()
    sys.exit("Partner API failed to come up on port 8088.")


def get_with_retries(url: str, params: dict | None = None) -> dict:
    """GET that honours Retry-After on 429 and backs off with jitter on 503 /
    network faults. Gives up only after MAX_TRIES."""
    for attempt in range(MAX_TRIES):
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=20)
        except requests.RequestException:
            time.sleep(min(0.5 * 2 ** attempt, 8) + random.uniform(0, 0.3))
            continue
        if r.status_code == 200:
            return r.json()
        if r.status_code == 429:
            time.sleep(int(r.headers.get("Retry-After", "2")))
        elif r.status_code == 503:
            time.sleep(min(0.5 * 2 ** attempt, 8) + random.uniform(0, 0.3))
        else:
            r.raise_for_status()
    raise RuntimeError(f"{url}: still failing after {MAX_TRIES} attempts")


def fetch() -> None:
    """Cursor walk with resume. Appends pages to RAW, checkpoints STATE."""
    CACHE_DIR.mkdir(exist_ok=True)
    state = json.loads(STATE.read_text()) if STATE.exists() else {"cursor": None, "done": False}
    if state["done"]:
        print(f"Cache complete ({RAW}), skipping fetch. Delete {STATE} to re-fetch.")
        return
    cursor, pages = state["cursor"], 0
    mode = "a" if cursor else "w"
    t0 = time.time()
    with RAW.open(mode, encoding="utf-8") as out:
        while True:
            params = {"limit": 200} | ({"cursor": cursor} if cursor else {})
            page = get_with_retries(f"{PARTNER_API_URL}/v1/freight_invoices", params)
            for row in page["data"]:
                out.write(json.dumps(row) + "\n")
            cursor, pages = page["next_cursor"], pages + 1
            out.flush()
            STATE.write_text(json.dumps({"cursor": cursor, "done": cursor is None}))
            if pages % 25 == 0:
                print(f"  {pages} pages, cursor={cursor}, {time.time()-t0:.0f}s")
            if cursor is None:
                break
    print(f"Fetched {pages} pages in {time.time()-t0:.0f}s -> {RAW}")

    # Small extras: carriers + monthly fuel surcharge index.
    months = [f"{y}-{m:02d}" for y in (2025, 2026) for m in range(1, 13)
              if f"{y}-{m:02d}" <= "2026-06"]
    SURCHARGE.write_text(json.dumps({
        "carriers": get_with_retries(f"{PARTNER_API_URL}/v1/carriers")["data"],
        "fuel_surcharge": [get_with_retries(f"{PARTNER_API_URL}/v1/fuel_surcharge",
                                            {"month": m}) for m in months]}))
    print(f"Carriers + fuel surcharge -> {SURCHARGE}")


def load(db_path: Path = ANALYTICS_DB) -> int:
    """Materialise fct_freight from the JSONL cache (no network). Returns rows.
    Called here and from build.py after a rebuild."""
    if not RAW.exists():
        return 0
    con = sqlite3.connect(db_path)
    con.execute("drop table if exists fct_freight")
    con.execute("""create table fct_freight (
        invoice_id text primary key, carrier_id text, carrier_name text,
        warehouse_code text, route_code text, invoice_date text, service_date text,
        month_label text, fy_quarter text, amount_inr real, fuel_surcharge_pct real,
        detention_charge_inr real, distance_km real, weight_kg real,
        temperature_controlled int, status text)""")
    rows = []
    with RAW.open(encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            d = dt.date.fromisoformat(r["service_date"])
            fy, fq = fy_parts(d)
            rows.append((r["invoice_id"], r["carrier_id"], r["carrier_name"],
                         r["warehouse_code"], r["route_code"], r["invoice_date"],
                         r["service_date"], d.strftime("%Y-%m"), fq,
                         r["amount"] / 100.0,                    # paise -> INR
                         r["fuel_surcharge_pct"],
                         r["detention_charge"] / 100.0,          # paise -> INR
                         r["distance_km"], r["weight_kg"],
                         int(r["temperature_controlled"]), r["status"]))
    con.executemany(  # OR REPLACE: a resumed fetch may have re-written one page
        "insert or replace into fct_freight values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    con.execute("create index if not exists i_ff_q on fct_freight(fy_quarter, warehouse_code)")
    con.commit()
    n = con.execute("select count(*) from fct_freight").fetchone()[0]
    con.close()
    return n


def main() -> None:
    proc = ensure_api()
    try:
        fetch()
    finally:
        if proc:
            proc.terminate()
    n = load()
    print(f"fct_freight loaded: {n:,} invoices "
          f"({'paise converted to INR' if n else 'no cache found'})")


if __name__ == "__main__":
    main()
