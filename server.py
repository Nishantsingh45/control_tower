"""Kestrel Control Tower - FastAPI backend + static web UI.

    python server.py            # http://localhost:8500  (dashboard)
                                # http://localhost:8500/ask  (Ask AI)

Plain JSON endpoints over metrics.py (the single source of KPI truth) and one
POST endpoint for the ask-anything chat. The frontend is dependency-free
HTML/CSS/JS in web/ - no build step, no frontend framework, nothing to install
beyond requirements.txt.
"""
import json
from pathlib import Path

import pandas as pd
import uvicorn
from fastapi import Body, FastAPI
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse

import asksql
import metrics as M
from config import CHAT_MODEL, IN_FULL_THRESHOLD, KPI_TARGETS, ON_TIME_GRACE_MIN, REPO_ROOT

WEB = Path(__file__).parent / "web"
app = FastAPI(title="Kestrel Control Tower")

LAST_FULL_MONTH = asksql.LAST_MONTH


def j(frame: pd.DataFrame) -> list[dict]:
    """DataFrame -> JSON-safe records (NaN becomes null)."""
    return json.loads(frame.to_json(orient="records"))


def jnum(obj) -> JSONResponse:
    """dict with numpy floats / NaN -> JSON with nulls."""
    return JSONResponse(json.loads(json.dumps(obj, default=float).replace("NaN", "null")))


def parse_region(region_id: str | None) -> int | None:
    """The UI sends region_id as a string, empty for 'All regions'."""
    return int(region_id) if region_id else None


@app.get("/")
def index():
    return FileResponse(WEB / "index.html")


@app.get("/ask")
def ask_page():
    return FileResponse(WEB / "ask.html")


@app.get("/api/meta")
def meta():
    con = M.connect()
    return {
        "quarters": M.quarters(con),
        "regions": j(M.regions(con)),
        "default_quarter": asksql.LAST_QUARTER,
        "last_month": LAST_FULL_MONTH,
        "has_freight": M.has_table(con, "fct_freight"),
        "has_price": M.has_table(con, "price_observation"),
        "has_chat": asksql.get_client() is not None,
        "chat_model": CHAT_MODEL,
        "in_full_threshold": IN_FULL_THRESHOLD,
        "on_time_grace_min": ON_TIME_GRACE_MIN,
        "kpi_targets": KPI_TARGETS,
        "build": j(M.df(con, "select * from meta_build")),
    }


@app.get("/api/kpis")
def kpis(quarter: str | None = None, region_id: str | None = None, uom: str = "each"):
    con = M.connect()
    r = parse_region(region_id)
    k = M.kpi_summary(con, quarter, r, uom)
    other = M.kpi_summary(con, quarter, r, "case" if uom == "each" else "each")
    k["fill_rate_other_basis_pct"] = other["fill_rate_pct"]
    prev_q = M.previous_quarter(con, quarter)
    k["prev_quarter"] = prev_q
    k["prev"] = M.kpi_summary(con, prev_q, r, uom) if prev_q else None
    return jnum(k)


@app.get("/api/service")
def service(quarter: str | None = None, region_id: str | None = None, uom: str = "each"):
    con = M.connect()
    r = parse_region(region_id)
    trend = M.monthly_service_trend(con, r, uom)
    return {
        "worst_outlets": j(M.worst_outlets(con, quarter, r, uom)),
        "worst_outlets_10": j(M.worst_outlets(con, quarter, r, uom, n=10)),
        "worst_routes": j(M.worst_routes(con, quarter, r)),
        "late_routes": j(M.late_routes(con, quarter, r)),
        "warehouses": j(M.worst_warehouses(con, quarter, r, uom)),
        "regions": j(M.fill_by_region(con, quarter, uom)),
        "trend": j(trend[trend.month_label <= LAST_FULL_MONTH]),
    }


@app.get("/api/coldchain")
def coldchain(quarter: str | None = None, region_id: str | None = None):
    con = M.connect()
    r = parse_region(region_id)
    exc = M.excursion_trend(con, r)
    exc = exc[exc.month_label <= LAST_FULL_MONTH]
    rr = M.returns_by_reason(con, quarter, r)
    return {
        "excursion_trend": j(exc),
        "ambient_share": (float(exc.chilled_on_ambient_vehicle.sum())
                          / max(int(exc.chilled_deliveries.sum()), 1)),
        "near_expiry": j(M.near_expiry_stock(con)),
        "cold_returns": j(rr[rr.return_reason.isin(["Cold chain breach", "Near expiry"])]),
    }


@app.get("/api/money")
def money(quarter: str | None = None, region_id: str | None = None):
    con = M.connect()
    r = parse_region(region_id)
    disc = M.discontinued_still_ordered(con, quarter)
    out = {
        "returns_by_category": j(M.returns_by_category(con, quarter, r)),
        "returns_by_reason": j(M.returns_by_reason(con, quarter, r)),
        "discontinued": j(disc.head(10)),
        "discontinued_total_lakh": float(disc.value_lakh.sum()) if len(disc) else 0.0,
        "discontinued_lines": int(disc.lines_after_discontinuation.sum()) if len(disc) else 0,
        "discontinued_skus": int(len(disc)),
        "freight_by_warehouse": [], "freight_by_carrier": [],
    }
    if M.has_table(con, "fct_freight"):
        out["freight_by_warehouse"] = j(M.freight_per_case_by_warehouse(con, quarter))
        out["freight_by_carrier"] = j(M.freight_by_carrier(con, quarter))
    return out


@app.get("/api/price")
def price(city: str | None = None):
    con = M.connect()
    if not M.has_table(con, "price_observation"):
        return {"available": False}
    gap = M.price_position_by_city_category(con)
    cities = sorted(gap.city.unique().tolist())
    city = city or (cities[0] if cities else None)
    counts = M.df(con, """select sum(product_id is not null) as matched,
                                 sum(product_id is null) as unmatched,
                                 sum(match_confidence < 1) as fuzzy
                          from price_listing""").iloc[0]
    return {
        "available": True,
        "cities": cities,
        "city": city,
        "gap": j(gap),
        "top_skus": j(M.mrp_vs_lowest_competitor(con, city=city)),
        "matched": int(counts.matched or 0),
        "unmatched": int(counts.unmatched or 0),
        "fuzzy": int(counts.fuzzy or 0),
    }


@app.get("/api/health-data")
def health_data():
    con = M.connect()
    out = {
        "exclusions": j(M.df(con, """select exclude_reason, count(*) as outlets
                                     from dim_outlet where exclude_reason is not null
                                     group by 1 order by 2 desc""")),
        "build": j(M.df(con, "select * from meta_build")),
        "unmatched_listings": [],
    }
    if M.has_table(con, "price_listing"):
        out["unmatched_listings"] = j(M.df(con, """
            select listing_id, city, retailer, listing_title, pack, category_site,
                   price_inr from price_listing where product_id is null
            order by city, listing_title"""))
    return out


@app.get("/api/findings", response_class=PlainTextResponse)
def findings():
    f = REPO_ROOT / "FINDINGS.md"
    return f.read_text(encoding="utf-8") if f.exists() else "FINDINGS.md not generated yet."


@app.get("/api/canned")
def canned_list():
    return {"questions": [{"name": k, "measures": v[0]} for k, v in asksql.CANNED.items()]}


@app.post("/api/canned")
def canned_run(payload: dict = Body(...)):
    name = payload.get("name")
    if name not in asksql.CANNED:
        return JSONResponse({"error": "unknown question"}, status_code=400)
    measures, fn = asksql.CANNED[name]
    con = M.connect()
    frame = fn(con)
    return {"status": "ok", "question": name, "measures": measures,
            "rows": j(frame), "sql": None, "truncated": False,
            "answer": f"Pre-wired answer ({len(frame)} row{'s' if len(frame) != 1 else ''}) - "
                      "the table below is the same query the dashboard runs."}


@app.post("/api/ask")
def api_ask(payload: dict = Body(...)):
    question = (payload.get("question") or "").strip()
    if not question:
        return JSONResponse({"error": "empty question"}, status_code=400)
    client = asksql.get_client()
    if client is None:
        return {"error": "no_key",
                "message": "No Anthropic API key found. Put ANTHROPIC_API_KEY=... "
                           "in control_tower/.env (or export it) and restart the "
                           "server. Pre-wired questions work without a key."}
    try:
        import anthropic
        con = M.connect()
        res = asksql.ask(client, con, question)
        rows = res.pop("rows")
        res["rows"] = j(rows) if rows is not None else None
        res["question"] = question
        return res
    except anthropic.AuthenticationError:
        return {"error": "no_key", "message": "The API key was rejected - check "
                                              "ANTHROPIC_API_KEY in .env."}
    except anthropic.RateLimitError:
        return {"error": "rate_limited", "message": "Rate limited - try again shortly."}
    except anthropic.APIStatusError as e:
        return {"error": "api", "message": f"API error {e.status_code}: {e.message}"}
    except anthropic.APIConnectionError:
        return {"error": "network", "message": "Network error reaching the Anthropic API."}


@app.get("/{path:path}")
def static_files(path: str):
    """Static frontend files (kept last so /api/* wins)."""
    target = (WEB / path).resolve()
    if target.is_file() and target.is_relative_to(WEB.resolve()):
        return FileResponse(target)
    return JSONResponse({"error": "not found"}, status_code=404)


if __name__ == "__main__":
    print("Kestrel Control Tower -> http://localhost:8500   (Ask AI: /ask)")
    uvicorn.run(app, host="127.0.0.1", port=8500, log_level="warning")
