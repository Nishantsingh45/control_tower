"""Kestrel Control Tower - FastAPI backend + static web UI.

    python server.py            # http://localhost:8500

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
from config import IN_FULL_THRESHOLD, ON_TIME_GRACE_MIN, REPO_ROOT

WEB = Path(__file__).parent / "web"
app = FastAPI(title="Kestrel Control Tower")

LAST_FULL_MONTH = "2026-06"


def j(frame: pd.DataFrame) -> list[dict]:
    """DataFrame -> JSON-safe records (NaN becomes null)."""
    return json.loads(frame.to_json(orient="records"))


def parse_region(region_id: str | None) -> int | None:
    """The UI sends region_id as a string, empty for 'All regions'."""
    return int(region_id) if region_id else None


@app.get("/")
def index():
    return FileResponse(WEB / "index.html")


@app.get("/api/meta")
def meta():
    con = M.connect()
    return {
        "quarters": M.quarters(con),
        "regions": j(M.regions(con)),
        "default_quarter": "FY27 Q1",
        "has_freight": M.has_table(con, "fct_freight"),
        "has_price": M.has_table(con, "price_observation"),
        "has_chat": asksql.get_client() is not None,
        "in_full_threshold": IN_FULL_THRESHOLD,
        "on_time_grace_min": ON_TIME_GRACE_MIN,
        "build": j(M.df(con, "select * from meta_build")),
    }


@app.get("/api/kpis")
def kpis(quarter: str | None = None, region_id: str | None = None, uom: str = "each"):
    con = M.connect()
    k = M.kpi_summary(con, quarter, parse_region(region_id), uom)
    other = M.kpi_summary(con, quarter, parse_region(region_id),
                          "case" if uom == "each" else "each")
    k["fill_rate_other_basis_pct"] = other["fill_rate_pct"]
    return JSONResponse(json.loads(json.dumps(k, default=float).replace("NaN", "null")))


@app.get("/api/service")
def service(quarter: str | None = None, region_id: str | None = None, uom: str = "each"):
    con = M.connect()
    r = parse_region(region_id)
    trend = M.monthly_service_trend(con, r, uom)
    return {
        "worst_outlets": j(M.worst_outlets(con, quarter, r, uom)),
        "worst_routes": j(M.worst_routes(con, quarter, r)),
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
    out = {
        "returns_by_category": j(M.returns_by_category(con, quarter, r)),
        "returns_by_reason": j(M.returns_by_reason(con, quarter, r)),
        "discontinued": j(M.discontinued_still_ordered(con, quarter).head(10)),
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
    unmatched = M.df(con, """select count(*) as n from price_listing
                             where product_id is null""").iloc[0, 0]
    return {
        "available": True,
        "cities": cities,
        "city": city,
        "gap": j(gap),
        "top_skus": j(M.mrp_vs_lowest_competitor(con, city=city)),
        "matched": int(M.df(con, "select count(*) n from price_listing "
                                 "where product_id is not null").iloc[0, 0]),
        "unmatched": int(unmatched),
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
    return {"questions": list(asksql.CANNED)}


@app.post("/api/canned")
def canned_run(payload: dict = Body(...)):
    name = payload.get("name")
    if name not in asksql.CANNED:
        return JSONResponse({"error": "unknown question"}, status_code=400)
    con = M.connect()
    return {"rows": j(asksql.CANNED[name](con))}


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
        sql, frame, answer = asksql.ask(client, con, question)
        return {"sql": sql, "rows": j(frame) if frame is not None else None,
                "answer": answer}
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
    print("Kestrel Control Tower -> http://localhost:8500")
    uvicorn.run(app, host="127.0.0.1", port=8500, log_level="warning")
