"""Ask-anything core: natural language -> SQL over the semantic layer.

Design constraints, in order of importance:
  1. Answers must carry their numbers: every reply returns the generated SQL
     and the rows behind it, so Divya gets "an answer with the numbers".
  2. The model only ever sees the CLEANED semantic layer (same tables as the
     dashboard), so chat and charts cannot disagree.
  3. Safety: the connection is opened read-only at the SQLite level, and the
     generated statement must be a single SELECT/WITH. Defence in depth - the
     read-only mode is the real guarantee.
  4. No API key -> the caller gets a clean "no_key" signal and falls back to
     pre-wired questions.

Pure logic - no UI imports. server.py exposes it over HTTP.
"""
import re

import pandas as pd

import metrics as M
from config import CHAT_MODEL, IN_FULL_THRESHOLD, ON_TIME_GRACE_MIN

MAX_ROWS = 200

# The schema contract given to the model. Kept in one string so the prompt is
# stable (cacheable) and reviewable.
SCHEMA_DOC = f"""
You write SQLite SELECT queries over Kestrel Provisions' analytics database
(a cleaned semantic layer - the raw ops data has already been normalised).

TABLES
  dim_region(region_id, region_code, region_name, hq_city)
  dim_warehouse(warehouse_id, warehouse_code, warehouse_name, city, region_id)
  dim_route(route_id, route_code, route_name, warehouse_id, region_id, is_reefer,
            vehicle_type, planned_stops, status)
  dim_product(product_id, sku_code, product_name, brand, category, subcategory,
              pack_size_value, pack_size_uom, case_pack, is_chilled,
              storage_temp_band, shelf_life_days, mrp_current_inr,
              list_price_current_inr, status, discontinued_date)
  dim_outlet(outlet_id, outlet_code, outlet_name, channel, outlet_format, city,
             state, region_id, route_id, chiller_available, credit_limit_inr,
             status, closed_date, is_deleted, exclude_reason, canonical_outlet_id)
  dim_date(date, year, month, month_label, fy, fy_quarter)
  fct_order(order_id, order_number, order_date, month_label, fy_quarter, outlet_id,
            channel, region_id, route_id, warehouse_id, salesperson_id, order_status,
            source_system, promo_code, gross_inr, discount_inr, tax_inr, net_inr,
            net_as_reported_inr, created_at_ist, service_measurable)
  fct_order_line(order_line_id, order_id, order_date, month_label, fy_quarter,
                 outlet_id, region_id, route_id, warehouse_id, channel, order_status,
                 source_system, service_measurable, product_id, uom_as_booked,
                 case_pack_at_order, ordered_each, ordered_case, delivered_each,
                 delivered_case, unit_price_inr, line_discount_pct, line_value_inr,
                 delivered_value_inr, short_reason_code, substitution_flag)
  fct_delivery(delivery_id, order_id, outlet_id, region_id, route_id, warehouse_id,
               channel, source_system, delivery_date, month_label, fy_quarter,
               telematics_vendor, delivery_status, delay_minutes, on_time,
               is_reefer_route, temperature_excursion_flag, max_temp_celsius,
               returned_cases, distance_km, pod_captured, failure_reason_code,
               fuel_cost_driver_entered_inr, order_fill_each, in_full, otif,
               carries_chilled)
  fct_return(return_id, credit_note_number, return_date, month_label, fy_quarter,
             order_id, outlet_id, region_id, channel, product_id, return_reason_code,
             return_reason, disposition, status, qty_sign_as_reported, return_each,
             return_case, credit_note_value_inr)
  fct_inventory(snapshot_id, snapshot_date, month_label, fy_quarter, warehouse_id,
                product_id, batch_id, on_hand_cases, on_hand_eaches, available_cases,
                damaged_cases, blocked_cases, expiry_date, ageing_bucket,
                days_to_expiry, is_chilled, category, on_hand_value_inr)
Optional (present only if external fetches have run):
  fct_freight(invoice_id, carrier_id, carrier_name, warehouse_code, route_code,
              invoice_date, service_date, month_label, fy_quarter, amount_inr,
              fuel_surcharge_pct, detention_charge_inr, distance_km, weight_kg,
              temperature_controlled, status)
  price_observation(listing_id, product_id, match_confidence, city, retailer,
                    listing_title, observed_price_inr, site_mrp_inr, our_mrp_inr,
                    in_stock, last_seen, observed_on)

HOUSE RULES (non-negotiable - they encode the client's metric definitions)
  * Fill rate = SUM(delivered_X)/SUM(ordered_X) on fct_order_line WHERE
    service_measurable = 1. Default to eaches (delivered_each/ordered_each);
    use cases only if the user asks for cases.
  * OTIF: use the precomputed on_time / in_full / otif flags on fct_delivery
    (on-time = within {ON_TIME_GRACE_MIN} min; in-full = order fill >=
    {IN_FULL_THRESHOLD:.0%} of ordered eaches - nothing in this data is ever
    100% filled).
  * Chilled/cold-chain deliveries: use carries_chilled = 1, and count excursions
    only where carries_chilled = 1.
  * Rankings and "worst/best outlet" questions: exclude outlets where
    dim_outlet.exclude_reason IS NOT NULL (test/closed/deleted). Company-wide
    historical totals keep them.
  * Money: use net_inr / gross_inr on fct_order, delivered_value_inr for
    dispatched value, credit_note_value_inr for returns. Never use
    net_as_reported_inr (one source system inflates it 8.5%).
  * Fiscal year runs April-March; fy_quarter looks like 'FY27 Q1' (= Apr-Jun
    2026). month_label looks like '2026-06'. Data covers 2025-01 .. 2026-06.
    "Last month" = '2026-06'; "last complete quarter" = 'FY27 Q1'.
  * Freight cost per case is only honest at warehouse x month grain (invoices
    carry no delivery key).

OUTPUT FORMAT
  Reply with a single SQLite SELECT (or WITH...SELECT) statement and nothing
  else - no prose, no code fences. Round percentages to 1 decimal. Limit to
  {MAX_ROWS} rows or fewer. Prefer readable column aliases.
"""

CANNED = {
    "Five worst outlets by fill rate, last month":
        lambda con: M.worst_outlets(con, quarter="FY27 Q1"),
    "Fill rate & OTIF by region, last complete quarter":
        lambda con: M.fill_by_region(con, quarter="FY27 Q1"),
    "Returns by category with top reason":
        lambda con: M.returns_by_category(con),
    "Temperature excursions per 100 chilled deliveries, by month":
        lambda con: M.excursion_trend(con),
    "Stock expiring within 30 days":
        lambda con: M.near_expiry_stock(con),
    "Discontinued SKUs still being ordered":
        lambda con: M.discontinued_still_ordered(con),
}


def get_client():
    """Anthropic client, or None when no credential resolves (the UI then
    offers pre-wired questions and explains how to set the key)."""
    try:
        import anthropic
        client = anthropic.Anthropic()   # resolves key from environment/profile
        client._has_key = bool(client.api_key or client.auth_token)
        return client if client._has_key else None
    except Exception:
        return None


def _extract_sql(text: str) -> str:
    """Model is told not to fence, but strip fences defensively."""
    text = re.sub(r"^```(sql)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    return text.rstrip(";").strip()


def _guard(sql: str) -> str | None:
    """Single read-only statement. The ro connection is the hard guarantee;
    this just produces friendlier errors."""
    if not re.match(r"^\s*(select|with)\b", sql, re.IGNORECASE):
        return "Only SELECT queries are allowed."
    if ";" in sql:
        return "Multiple statements are not allowed."
    return None


def run_sql(con, sql: str) -> pd.DataFrame:
    cur = con.execute(sql)
    cols = [d[0] for d in cur.description]
    return pd.DataFrame(cur.fetchmany(MAX_ROWS), columns=cols)


def ask(client, con, question: str) -> tuple[str, pd.DataFrame | None, str]:
    """Returns (sql, dataframe, answer_text). Retries once on SQL error."""
    msgs = [{"role": "user", "content": question}]
    sql, frame, err = "", None, ""
    for _attempt in range(2):
        resp = client.messages.create(
            model=CHAT_MODEL, max_tokens=2048,
            cache_control={"type": "ephemeral"},     # schema prompt is stable
            system=SCHEMA_DOC, messages=msgs)
        sql = _extract_sql(next(b.text for b in resp.content if b.type == "text"))
        err = _guard(sql)
        if err is None:
            try:
                frame = run_sql(con, sql)
                break
            except Exception as e:                    # bad SQL -> one retry with the error
                err = str(e)
        msgs += [{"role": "assistant", "content": sql},
                 {"role": "user", "content":
                  f"That query failed with: {err}. Return a corrected single "
                  "SELECT statement, nothing else."}]
    if frame is None:
        return sql, None, f"Could not produce a working query ({err})."

    # Second call: turn rows into a short grounded answer.
    resp = client.messages.create(
        model=CHAT_MODEL, max_tokens=1024,
        system="You are a supply-chain analyst. Answer the user's question in "
               "2-4 sentences using ONLY the query result provided. Quote the "
               "key numbers. Currency is INR. If the result is empty, say so.",
        messages=[{"role": "user", "content":
                   f"Question: {question}\n\nSQL used:\n{sql}\n\n"
                   f"Result (first {min(len(frame), 50)} rows):\n"
                   f"{frame.head(50).to_csv(index=False)}"}])
    answer = next((b.text for b in resp.content if b.type == "text"), "")
    return sql, frame, answer
