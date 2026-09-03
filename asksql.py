"""Ask-anything core: natural language -> SQL over the semantic layer.

Design constraints, in order of importance:
  1. Answers carry their numbers: every reply returns the generated SQL, a
     one-line statement of WHAT WAS MEASURED, and the rows behind it.
  2. The model only ever sees the CLEANED semantic layer (same tables as the
     dashboard) and is handed the dashboard's own metric definitions as
     reference queries, so chat and charts cannot disagree.
  3. Safety: the connection is opened read-only at the SQLite level, and the
     generated statement must be a single SELECT/WITH. Defence in depth - the
     read-only mode is the real guarantee.
  4. Honesty: the model may reply "cannot" when the tables do not hold the
     answer (forecasts, NPS, weather, staff...). We never fall back to prose
     that is not backed by rows.
  5. No API key -> the caller gets a clean "no_key" signal and falls back to
     pre-wired questions.

Why the reference queries exist: an early version answered "how much have we
lost to orders on discontinued SKUs" with the lifetime order value of every SKU
that is discontinued *today* (dropping the order_date > discontinued_date
filter) and then subtracted delivered value - real rows, wrong question, 8x
too big. Grounding in real SQL prevents invented numbers; it does not prevent
a plausible-but-wrong query. The definitions below, and eval_chat.py, do.

Pure logic - no UI imports. server.py exposes it over HTTP.
"""
import re

import pandas as pd

import metrics as M
from config import CHAT_MODEL, IN_FULL_THRESHOLD, ON_TIME_GRACE_MIN

MAX_ROWS = 200
LAST_MONTH = "2026-06"
LAST_QUARTER = "FY27 Q1"

# The schema contract given to the model. Kept in one string so the prompt is
# stable (cacheable) and reviewable. Volatile content (the question) is in the
# user turn, after the cached prefix.
SCHEMA_DOC = f"""
You write SQLite SELECT queries over Kestrel Provisions' analytics database
(a cleaned semantic layer - the raw ops data has already been normalised).
Kestrel is a food & grocery distributor: 8 warehouses, ~700 retail outlets,
5 regions, channels GT / MT / HORECA / ECOM_DARKSTORE.

TABLES
  dim_region(region_id, region_code, region_name, hq_city)
      region_name values: 'West','South','North','East','Central'
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
      return_reason values: 'Near expiry','Transit damage','Wrong SKU','Quality',
      'Oversupply','Cold chain breach'
  fct_inventory(snapshot_id, snapshot_date, month_label, fy_quarter, warehouse_id,
                product_id, batch_id, on_hand_cases, on_hand_eaches, available_cases,
                damaged_cases, blocked_cases, expiry_date, ageing_bucket,
                days_to_expiry, is_chilled, category, on_hand_value_inr)
Present only if the external fetches have run (assume they have):
  fct_freight(invoice_id, carrier_id, carrier_name, warehouse_code, route_code,
              invoice_date, service_date, month_label, fy_quarter, amount_inr,
              fuel_surcharge_pct, detention_charge_inr, distance_km, weight_kg,
              temperature_controlled, status)
  price_observation(listing_id, product_id, match_confidence, city, retailer,
                    listing_title, observed_price_inr, site_mrp_inr, our_mrp_inr,
                    in_stock, last_seen, observed_on)
      city values: 'Mumbai','Delhi','Bengaluru','Chennai'

HOUSE RULES (non-negotiable - they encode the client's metric definitions and
match the dashboard exactly)
  * Fill rate = SUM(delivered_X)*100.0/SUM(ordered_X) on fct_order_line WHERE
    service_measurable = 1 (DELIVERED + PARTIAL orders only). Default to eaches
    (delivered_each/ordered_each); use cases only if the user says cases.
  * OTIF: use the precomputed on_time / in_full / otif flags on fct_delivery
    (on-time = delay_minutes <= {ON_TIME_GRACE_MIN}; in-full = order fill >=
    {IN_FULL_THRESHOLD:.0%} of ordered eaches - nothing in this data is ever 100% filled).
    "Late by more than two hours" = delay_minutes > 120. "More than one delivery
    in ten" = more than 10% of that route's deliveries.
  * Chilled / cold-chain deliveries: carries_chilled = 1. Count excursions only
    where carries_chilled = 1. "Per hundred chilled deliveries" = excursions*100.0
    / chilled deliveries.
  * Rankings and worst/best-outlet questions: exclude outlets where
    dim_outlet.exclude_reason IS NOT NULL (test/closed/deleted) and require
    SUM(ordered_each) >= {M.RANKING_MIN_ORDERED_EACH} in the period, exactly like the dashboard.
    Company-wide historical totals keep every outlet.
  * DISCONTINUED SKUs: "orders on discontinued SKUs", "leakage from discontinued
    SKUs", "lost to discontinued SKUs" ALL mean order lines whose order_date is
    AFTER dim_product.discontinued_date (discontinued_date IS NOT NULL). Value =
    SUM(line_value_inr). NEVER count orders placed before the discontinuation
    date - ordering the SKU was legitimate then. Do NOT subtract delivered value.
  * CLOSED OUTLETS still place orders (F21): "orders from closed outlets" =
    fct_order joined to dim_outlet WHERE status = 'CLOSED' AND order_date >
    closed_date. Count and value them; do not exclude them from company totals.
  * Returns figures come from credit notes only (fct_return); the driver-app
    column fct_delivery.returned_cases disagrees with them (F20) - never use it
    for a returns number, and never add the two together.
  * The fill gap (ordered minus delivered) exists on essentially every line in
    this data. Never present it as a loss caused by something else
    (discontinuation, a promotion, a carrier). Only compute it when the user asks
    about fill rate, shortfall or units short.
  * "Losing money" / "leakage" questions map to one of: returns
    (credit_note_value_inr on fct_return), value ordered on discontinued SKUs,
    freight per case (fct_freight), or near-expiry stock (fct_inventory
    on_hand_value_inr with days_to_expiry BETWEEN 0 AND 30 on the LATEST
    snapshot_date). Use the one the question names. If it names none, use
    returns and say so in the measures line.
  * Money: net_inr / gross_inr on fct_order; delivered_value_inr for dispatched
    value; credit_note_value_inr for returns. Never use net_as_reported_inr (one
    source system inflates it 8.5%). Express big values in crore (/1e7) or
    lakh (/1e5) and name the unit in the column alias (value_cr, returns_lakh).
  * Freight cost per case is only honest at warehouse x month grain (invoices
    carry no delivery key). Freight cost ALWAYS excludes DISPUTED invoices
    (WHERE status != 'DISPUTED') - about 1 in 5 invoices is under formal
    dispute and is not a confirmed cost. Only include disputed amounts if the
    question explicitly asks about disputes, contested charges, or risk
    exposure - and then label them as disputed, never add them to "cost". Never join fct_freight to deliveries or orders.
  * Fiscal year runs April-March; fy_quarter looks like 'FY27 Q1' (= Apr-Jun
    2026). month_label looks like '2026-06'. Data covers 2025-01 .. 2026-06.
    "Last month" = '{LAST_MONTH}'; "last complete quarter" / "last quarter" =
    '{LAST_QUARTER}'; "this year" = fy 'FY27' (Apr 2026 onwards).
  * "How much" questions want ONE total first: return a TOTAL row (label it
    'TOTAL') and then, optionally, the top contributors via UNION ALL.
  * "Why" questions: return a breakdown that locates the driver (by month, region,
    warehouse, category, reason or channel) over the last 3-4 months. Never invent
    a cause; the columns are the evidence.

REFERENCE QUERIES (the dashboard's own definitions - copy their logic)

Q: Which five outlets had the lowest fill rate last month, excluding closed and test outlets?
-- measures: fill rate in eaches (delivered/ordered on delivered+partial orders) for June 2026; outlets with >=500 units ordered; test/closed/deleted outlets excluded
select ou.outlet_code, ou.outlet_name, ou.city, ou.channel,
       round(sum(l.delivered_each)*100.0/sum(l.ordered_each),1) as fill_rate_pct,
       round(sum(l.ordered_each)) as units_ordered
from fct_order_line l join dim_outlet ou using(outlet_id)
where l.service_measurable = 1 and ou.exclude_reason is null and l.month_label = '{LAST_MONTH}'
group by 1,2,3,4 having sum(l.ordered_each) >= {M.RANKING_MIN_ORDERED_EACH}
order by fill_rate_pct limit 5

Q: What was OTIF by region for the last complete quarter?
-- measures: OTIF = on-time (delay <= {ON_TIME_GRACE_MIN} min) AND in-full (>= {IN_FULL_THRESHOLD:.0%} of ordered units), share of deliveries, FY27 Q1 (Apr-Jun 2026)
select rg.region_name, round(avg(d.otif)*100,1) as otif_pct,
       round(avg(d.on_time)*100,1) as on_time_pct, round(avg(d.in_full)*100,1) as in_full_pct,
       count(*) as deliveries
from fct_delivery d join dim_region rg using(region_id)
where d.fy_quarter = '{LAST_QUARTER}' group by 1 order by otif_pct

Q: Which categories drive the largest value of returns, and what is the leading reason?
-- measures: credit-note value by product category over all history, with the reason carrying the most value in each category
select p.category, round(sum(r.credit_note_value_inr)/1e5,1) as returns_lakh,
       (select return_reason from fct_return r2 join dim_product p2 using(product_id)
        where p2.category = p.category group by 1
        order by sum(credit_note_value_inr) desc limit 1) as top_reason
from fct_return r join dim_product p using(product_id)
group by 1 order by 2 desc

Q: Temperature excursions per hundred chilled deliveries, by month
-- measures: excursions on deliveries that carry at least one chilled SKU, per 100 such deliveries, by month
select month_label, sum(carries_chilled) as chilled_deliveries,
       sum(case when carries_chilled then temperature_excursion_flag else 0 end) as excursions,
       round(sum(case when carries_chilled then temperature_excursion_flag else 0 end)*100.0
             /nullif(sum(carries_chilled),0),2) as per_100_chilled
from fct_delivery group by 1 order by 1

Q: Which routes are more than two hours late on more than one delivery in ten?
-- measures: share of each route's deliveries with delay over 120 minutes, all history; routes above 10%
select r.route_code, w.warehouse_name, count(*) as deliveries,
       sum(d.delay_minutes > 120) as late_over_2h,
       round(sum(d.delay_minutes > 120)*100.0/count(*),1) as pct_late_over_2h
from fct_delivery d join dim_route r using(route_id)
join dim_warehouse w on w.warehouse_id = d.warehouse_id
group by 1,2 having pct_late_over_2h > 10 order by pct_late_over_2h desc

Q: Freight cost per delivered case, by warehouse, for the last quarter
-- measures: confirmed carrier invoices (excludes disputed) divided by cases delivered, per warehouse, FY27 Q1 (Apr-Jun 2026)
with freight as (select warehouse_code, sum(amount_inr) as freight_inr
                 from fct_freight where fy_quarter = '{LAST_QUARTER}' and status != 'DISPUTED' group by 1),
cases as (select wh.warehouse_code, sum(l.delivered_case) as delivered_cases
          from fct_order_line l join dim_warehouse wh using(warehouse_id)
          where l.service_measurable = 1 and l.fy_quarter = '{LAST_QUARTER}' group by 1)
select c.warehouse_code, round(f.freight_inr/1e7,2) as freight_confirmed_cr,
       round(c.delivered_cases) as delivered_cases,
       round(f.freight_inr/c.delivered_cases,1) as freight_per_case_inr
from cases c join freight f using(warehouse_code) order by 4 desc

Q: How much have we lost to orders on discontinued SKUs?
-- measures: value of order lines placed AFTER the SKU's discontinuation date (line value at order time), all history; total first, then the top 10 SKUs
select 'TOTAL' as sku_code, 'All discontinued SKUs' as product_name,
       count(*) as lines_after_discontinuation, round(sum(l.line_value_inr)/1e7,2) as value_cr
from fct_order_line l join dim_product p using(product_id)
where p.discontinued_date is not null and l.order_date > p.discontinued_date
union all
select * from (
  select p.sku_code, p.product_name, count(*) as lines_after_discontinuation,
         round(sum(l.line_value_inr)/1e7,2) as value_cr
  from fct_order_line l join dim_product p using(product_id)
  where p.discontinued_date is not null and l.order_date > p.discontinued_date
  group by 1,2 order by 4 desc limit 10)

Q: For our top twenty SKUs by value, how does our MRP compare with the lowest observed competitor price in Mumbai?
-- measures: current MRP vs the lowest competitor shelf price observed in Mumbai, for the 20 SKUs with the highest delivered value
with top_skus as (select product_id, sum(delivered_value_inr) as value_inr
                  from fct_order_line where service_measurable = 1
                  group by 1 order by 2 desc limit 20)
select p.sku_code, p.product_name, round(t.value_inr/1e7,2) as sales_cr,
       p.mrp_current_inr as our_mrp, round(min(m.observed_price_inr),2) as lowest_competitor_price,
       round((min(m.observed_price_inr)-p.mrp_current_inr)*100.0/p.mrp_current_inr,1) as gap_pct
from top_skus t join dim_product p using(product_id)
left join price_observation m on m.product_id = t.product_id and m.city = 'Mumbai'
group by 1,2,3,4 order by t.value_inr desc

Q: Why did fill rate drop in the West last month?
-- measures: fill rate in eaches for the West region by month (Mar-Jun 2026) and warehouse, so the drop can be located
select l.month_label, wh.warehouse_name,
       round(sum(l.delivered_each)*100.0/sum(l.ordered_each),1) as fill_rate_pct,
       round(sum(l.ordered_each)) as units_ordered
from fct_order_line l join dim_region rg using(region_id) join dim_warehouse wh using(warehouse_id)
where l.service_measurable = 1 and rg.region_name = 'West'
  and l.month_label between '2026-03' and '{LAST_MONTH}'
group by 1,2 order by 1,2

OUTPUT FORMAT (exactly this, nothing else - no prose, no code fences)
  line 1:  -- measures: <one plain-English sentence: what is computed, the
           definition used, and the period>
  then:    one SQLite SELECT or WITH...SELECT statement. No trailing semicolon.
           Round percentages to 1 decimal. Limit to {MAX_ROWS} rows or fewer.
           Readable column aliases (fill_rate_pct, returns_lakh, value_cr).
If the question cannot be answered from these tables - forecasts or predictions,
data that does not exist here (NPS, customer satisfaction, weather, staff,
budgets), or any request to change data - reply with exactly one line:
  -- cannot: <one sentence saying what is missing or why>
"""

NARRATE_SYSTEM = """You are a supply-chain analyst writing for a non-technical Head of
Operations. Answer the question in 2-4 short sentences using ONLY the rows
provided.
Rules:
  * Open by saying what was measured, in plain words, following the MEASURES
    line (e.g. "Counting only order lines placed after each SKU's
    discontinuation date, ...").
  * Quote the key numbers with units. Currency is INR: use lakh / crore as the
    column names indicate (a column ending _cr is crore, _lakh is lakh,
    _inr is rupees). Percentages get one decimal.
  * Never introduce a concept or number that is not in the rows: no "loss",
    "shortfall", "gap", "cause" or "because" unless a column carries it.
    Describe what the rows show; do not speculate about why.
  * If a TOTAL row exists, lead with it. If the result is empty, say that
    nothing matched.
  * If the result is marked TRUNCATED, say the figures cover only the rows
    returned.
  * Light Markdown is rendered, so use it to help a skim-reader: **bold** the
    headline number(s), and a short bullet list when comparing three or more
    items (categories, warehouses, months). Do not use headings, and do not
    bullet a two- or three-sentence answer that reads fine as prose.
"""

# Pre-wired questions (no API key needed). Each entry: (measures, function).
CANNED = {
    "Five worst outlets by fill rate, last quarter (FY27 Q1)": (
        "Fill rate in eaches on delivered/partial orders, Apr-Jun 2026; outlets with "
        f">= {M.RANKING_MIN_ORDERED_EACH} units ordered; test/closed/deleted outlets excluded",
        lambda con: M.worst_outlets(con, quarter=LAST_QUARTER)),
    "Fill rate and OTIF by region, last quarter (FY27 Q1)": (
        "Fill rate in eaches and OTIF (on-time within 30 min AND >= 90% of units) by region, Apr-Jun 2026",
        lambda con: M.fill_by_region(con, quarter=LAST_QUARTER)),
    "Returns by category, with the leading reason": (
        "Credit-note value by product category over all history, with the reason carrying the most value",
        lambda con: M.returns_by_category(con)),
    "Temperature excursions per 100 chilled deliveries, by month": (
        "Excursions on deliveries carrying at least one chilled SKU, per 100 such deliveries",
        lambda con: M.excursion_trend(con)),
    "Routes more than 2 hours late on over 10% of deliveries": (
        "Share of each route's deliveries with delay over 120 minutes, all history; routes above 10%",
        lambda con: M.late_routes(con)),
    "Stock expiring within 30 days": (
        "Saleable stock on the latest weekly snapshot with 0-30 days to expiry, valued at list price",
        lambda con: M.near_expiry_stock(con)),
    "Value ordered on discontinued SKUs after their discontinuation date": (
        "Order lines placed after the SKU's discontinuation date, valued at order time; all history",
        lambda con: M.discontinued_still_ordered(con)),
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


def parse_reply(text: str) -> dict:
    """Split the model reply into (measures, cannot, sql). Leading `--` comment
    lines carry the measures / cannot markers; everything after is SQL."""
    text = re.sub(r"^```(?:sql)?\s*|\s*```$", "", text.strip(), flags=re.MULTILINE).strip()
    measures, cannot, body = "", "", []
    for line in text.splitlines():
        s = line.strip()
        if not body and s.startswith("--"):
            c = s[2:].strip()
            low = c.lower()
            if low.startswith("measures:"):
                measures = c[len("measures:"):].strip()
            elif low.startswith("cannot:"):
                cannot = c[len("cannot:"):].strip()
            continue
        if s or body:
            body.append(line)
    sql = "\n".join(body).strip().rstrip(";").strip()
    return {"measures": measures, "cannot": cannot, "sql": sql}


def _guard(sql: str) -> str | None:
    """Single read-only statement. The ro connection is the hard guarantee;
    this just produces friendlier errors."""
    if not sql:
        return "The reply contained no SQL."
    if not re.match(r"^\s*(select|with)\b", sql, re.IGNORECASE):
        return "Only SELECT queries are allowed."
    if ";" in sql:
        return "Multiple statements are not allowed."
    return None


def run_sql(con, sql: str) -> tuple[pd.DataFrame, bool]:
    """Execute and return (rows capped at MAX_ROWS, truncated?)."""
    cur = con.execute(sql)
    cols = [d[0] for d in cur.description]
    rows = cur.fetchmany(MAX_ROWS + 1)
    truncated = len(rows) > MAX_ROWS
    return pd.DataFrame(rows[:MAX_ROWS], columns=cols), truncated


def _text(resp) -> str:
    return next((b.text for b in resp.content if b.type == "text"), "")


def narrate(client, question: str, measures: str, frame: pd.DataFrame, truncated: bool) -> str:
    """Second call: turn rows into a short grounded answer."""
    shown = frame.head(100)
    status = (f"TRUNCATED - only the first {len(frame)} rows were returned" if truncated
              else f"complete - {len(frame)} row{'s' if len(frame) != 1 else ''}")
    if len(shown) < len(frame):
        status += f" (first {len(shown)} shown to you)"
    resp = client.messages.create(
        model=CHAT_MODEL, max_tokens=4000,
        system=NARRATE_SYSTEM,
        messages=[{"role": "user", "content":
                   f"Question: {question}\n"
                   f"MEASURES: {measures or '(not stated)'}\n"
                   f"RESULT: {status}\n\n"
                   f"{shown.to_csv(index=False)}"}])
    return _text(resp).strip()


def ask(client, con, question: str) -> dict:
    """Returns a dict:
         status    'ok' | 'cannot' | 'error'
         sql       generated SQL ('' when cannot)
         measures  one-line plain-English definition of what was computed
         rows      DataFrame or None
         answer    prose for the user
         truncated whether rows were capped at MAX_ROWS
       Retries once on a SQL error or a malformed reply."""
    msgs = [{"role": "user", "content": question}]
    sql, measures, err, frame, truncated = "", "", "", None, False
    for _attempt in range(2):
        resp = client.messages.create(
            model=CHAT_MODEL, max_tokens=8000,
            cache_control={"type": "ephemeral"},     # schema prompt is stable
            system=SCHEMA_DOC, messages=msgs)
        text = _text(resp)
        parsed = parse_reply(text)
        if parsed["cannot"] and not parsed["sql"]:
            return {"status": "cannot", "sql": "", "measures": "", "rows": None,
                    "truncated": False,
                    "answer": f"I can't answer that from the data: {parsed['cannot']}"}
        sql, measures = parsed["sql"], parsed["measures"]
        err = _guard(sql)
        if err is None:
            try:
                frame, truncated = run_sql(con, sql)
                break
            except Exception as e:                    # bad SQL -> one retry with the error
                err = str(e)
        msgs += [{"role": "assistant", "content": text},
                 {"role": "user", "content":
                  f"That reply failed with: {err}. Return a corrected reply in the "
                  "same format (one '-- measures:' line, then a single SELECT), "
                  "nothing else."}]
    if frame is None:
        return {"status": "error", "sql": sql, "measures": measures, "rows": None,
                "truncated": False,
                "answer": f"Could not produce a working query ({err})."}
    answer = narrate(client, question, measures, frame, truncated)
    return {"status": "ok", "sql": sql, "measures": measures, "rows": frame,
            "truncated": truncated, "answer": answer}
