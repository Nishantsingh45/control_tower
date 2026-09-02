"""Every KPI is defined exactly once, here, as SQL against the semantic layer.

The dashboard, the smoke test and the ask-anything chat all call these
functions (or query the same tables), so the same question can never return
two different numbers from two parts of the app - which is the complaint
that opens Divya's brief.

Conventions (defended in FINDINGS.md / DECISIONS.md):
  * Service metrics use DELIVERED + PARTIAL orders only (F9).
  * Fill rate defaults to eaches - how customers penalise - with cases available
    everywhere (F1). `uom` argument accepts 'each' or 'case'.
  * OTIF: on-time = delay_minutes <= 30; in-full = order fill >= 98% (F3, F4).
  * Rankings exclude TEST/CLOSED/DELETED outlets; historical totals keep them (F12).
  * Money figures use recomputed net values, never PARTNER_API's inflated feed (F2).
"""
import sqlite3

import pandas as pd

from config import ANALYTICS_DB

# Rankings ignore outlets below this many ordered units in the period, so a
# 3-case outlet with one bad week cannot top the worst-performers table.
RANKING_MIN_ORDERED_EACH = 500


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{ANALYTICS_DB.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def df(con, sql: str, params=()) -> pd.DataFrame:
    return pd.read_sql_query(sql, con, params=params)


def _qty(uom: str) -> tuple[str, str]:
    assert uom in ("each", "case")
    return f"ordered_{uom}", f"delivered_{uom}"


def _filters(quarter=None, region_id=None, alias="") -> tuple[str, list]:
    """Shared WHERE fragment for quarter / region scoping."""
    a = f"{alias}." if alias else ""
    clauses, params = [], []
    if quarter:
        clauses.append(f"{a}fy_quarter = ?")
        params.append(quarter)
    if region_id:
        clauses.append(f"{a}region_id = ?")
        params.append(region_id)
    return (" and " + " and ".join(clauses)) if clauses else "", params


def quarters(con) -> list[str]:
    return [r[0] for r in con.execute(
        "select distinct fy_quarter from fct_order order by min(order_date)")]


def regions(con) -> pd.DataFrame:
    return df(con, "select region_id, region_name from dim_region order by region_name")


def has_table(con, name: str) -> bool:
    return bool(con.execute(
        "select 1 from sqlite_master where type='table' and name=?", (name,)).fetchone())


# --------------------------------------------------------------------------
# Headline KPIs
# --------------------------------------------------------------------------

def kpi_summary(con, quarter=None, region_id=None, uom="each") -> dict:
    o, d = _qty(uom)
    w, p = _filters(quarter, region_id)
    out = {}

    r = con.execute(f"""
        select sum({d})*100.0/sum({o}) as fill,
               sum(line_value_inr) as ordered_value,
               sum(delivered_value_inr) as dispatch_value
        from fct_order_line where service_measurable {w}""", p).fetchone()
    out["fill_rate_pct"] = r["fill"]
    out["dispatch_value_inr"] = r["dispatch_value"]

    r = con.execute(f"""
        select avg(on_time)*100 as on_time, avg(in_full)*100 as in_full,
               avg(otif)*100 as otif, count(*) as deliveries
        from fct_delivery where 1=1 {w}""", p).fetchone()
    out.update(on_time_pct=r["on_time"], in_full_pct=r["in_full"],
               otif_pct=r["otif"], deliveries=r["deliveries"])

    r = con.execute(f"""
        select sum(case when carries_chilled then temperature_excursion_flag else 0 end)
               *100.0/nullif(sum(carries_chilled),0) as exc
        from fct_delivery where 1=1 {w}""", p).fetchone()
    out["excursions_per_100_chilled"] = r["exc"]

    r = con.execute(f"""
        select sum(credit_note_value_inr) as ret_value from fct_return where 1=1 {w}""",
        p).fetchone()
    out["returns_value_inr"] = r["ret_value"] or 0
    out["returns_pct_of_dispatch"] = (
        out["returns_value_inr"] * 100.0 / out["dispatch_value_inr"]
        if out["dispatch_value_inr"] else None)

    r = con.execute(f"""
        select count(*) as n, sum(net_inr) as v from fct_order
        where order_status='OPEN' {w}""", p).fetchone()
    out.update(open_orders=r["n"], open_orders_value_inr=r["v"] or 0)

    # Freight only exists after the V2 fetch has run; degrade gracefully.
    if has_table(con, "fct_freight"):
        w2, p2 = _filters(quarter, None)   # freight has no region grain (see DECISIONS)
        r = con.execute(f"""
            select sum(amount_inr) as freight from fct_freight where 1=1 {w2}""", p2).fetchone()
        cases = con.execute(f"""
            select sum(delivered_case) from fct_order_line
            where service_measurable {w}""", p).fetchone()[0]
        out["freight_inr"] = r["freight"]
        out["freight_per_delivered_case_inr"] = (
            r["freight"] / cases if (r["freight"] and cases and not region_id) else None)
    return out


# --------------------------------------------------------------------------
# Worst performers (rankings exclude TEST/CLOSED/DELETED outlets - F12)
# --------------------------------------------------------------------------

def worst_outlets(con, quarter=None, region_id=None, uom="each", n=5) -> pd.DataFrame:
    o, d = _qty(uom)
    w, p = _filters(quarter, region_id, "l")
    return df(con, f"""
        select ou.outlet_code, ou.outlet_name, ou.city, ou.channel,
               round(sum(l.{d})*100.0/sum(l.{o}), 1) as fill_pct,
               round(sum(l.{o})) as ordered_{uom}
        from fct_order_line l
        join dim_outlet ou on ou.outlet_id = l.outlet_id
        where l.service_measurable and ou.exclude_reason is null {w}
        group by 1,2,3,4
        having sum(l.{o}) >= {RANKING_MIN_ORDERED_EACH}
        order by fill_pct asc limit {n}""", p)


def worst_routes(con, quarter=None, region_id=None, n=5) -> pd.DataFrame:
    w, p = _filters(quarter, region_id, "fd")
    return df(con, f"""
        select r.route_code, r.route_name, w.warehouse_name,
               round(avg(fd.otif)*100, 1) as otif_pct,
               round(avg(fd.on_time)*100, 1) as on_time_pct,
               round(avg(fd.delay_minutes), 0) as avg_delay_min,
               count(*) as deliveries
        from fct_delivery fd
        join dim_route r on r.route_id = fd.route_id
        join dim_warehouse w on w.warehouse_id = fd.warehouse_id
        where 1=1 {w}
        group by 1,2,3 having count(*) >= 10
        order by otif_pct asc limit {n}""", p)


def worst_warehouses(con, quarter=None, region_id=None, uom="each", n=8) -> pd.DataFrame:
    o, d = _qty(uom)
    w, p = _filters(quarter, region_id, "l")
    return df(con, f"""
        select wh.warehouse_code, wh.warehouse_name, wh.city,
               round(sum(l.{d})*100.0/sum(l.{o}), 1) as fill_pct,
               round(sum(l.delivered_value_inr)/1e7, 2) as dispatched_cr
        from fct_order_line l
        join dim_warehouse wh on wh.warehouse_id = l.warehouse_id
        where l.service_measurable {w}
        group by 1,2,3 order by fill_pct asc limit {n}""", p)


# --------------------------------------------------------------------------
# Trends and breakdowns
# --------------------------------------------------------------------------

def monthly_service_trend(con, region_id=None, uom="each") -> pd.DataFrame:
    o, d = _qty(uom)
    w, p = _filters(None, region_id, "l")
    fills = df(con, f"""
        select month_label, round(sum({d})*100.0/sum({o}),2) as fill_pct
        from fct_order_line l where service_measurable {w}
        group by 1 order by 1""", p)
    w2, p2 = _filters(None, region_id)
    otif = df(con, f"""
        select month_label, round(avg(otif)*100,2) as otif_pct,
               round(avg(on_time)*100,2) as on_time_pct
        from fct_delivery where 1=1 {w2} group by 1 order by 1""", p2)
    return fills.merge(otif, on="month_label", how="left")


def fill_by_region(con, quarter=None, uom="each") -> pd.DataFrame:
    o, d = _qty(uom)
    w, p = _filters(quarter, None, "l")
    return df(con, f"""
        select rg.region_name,
               round(sum(l.{d})*100.0/sum(l.{o}),1) as fill_pct,
               round(avg_otif*100,1) as otif_pct
        from fct_order_line l
        join dim_region rg on rg.region_id = l.region_id
        left join (select region_id, avg(otif) as avg_otif from fct_delivery
                   where 1=1 {w.replace('l.','')} group by 1) t
               on t.region_id = l.region_id
        where l.service_measurable {w}
        group by 1 order by fill_pct""", p + p)


def excursion_trend(con, region_id=None) -> pd.DataFrame:
    w, p = _filters(None, region_id)
    return df(con, f"""
        select month_label,
               round(sum(case when carries_chilled then temperature_excursion_flag else 0 end)
                     *100.0/nullif(sum(carries_chilled),0),2) as excursions_per_100_chilled,
               sum(case when carries_chilled then temperature_excursion_flag else 0 end) as excursions,
               sum(carries_chilled) as chilled_deliveries,
               sum(carries_chilled and not is_reefer_route) as chilled_on_ambient_vehicle
        from fct_delivery where 1=1 {w} group by 1 order by 1""", p)


def returns_by_category(con, quarter=None, region_id=None) -> pd.DataFrame:
    w, p = _filters(quarter, region_id, "r")
    return df(con, f"""
        select p.category,
               round(sum(r.credit_note_value_inr)/1e5, 1) as returns_lakh,
               (select return_reason from fct_return r2
                join dim_product p2 using(product_id)
                where p2.category = p.category {w.replace('r.', 'r2.')}
                group by return_reason order by sum(credit_note_value_inr) desc limit 1)
                   as top_reason
        from fct_return r join dim_product p using(product_id)
        where 1=1 {w}
        group by 1 order by 2 desc""", p + p)


def returns_by_reason(con, quarter=None, region_id=None) -> pd.DataFrame:
    w, p = _filters(quarter, region_id)
    return df(con, f"""
        select return_reason, round(sum(credit_note_value_inr)/1e5,1) as returns_lakh,
               count(*) as credit_notes
        from fct_return where 1=1 {w} group by 1 order by 2 desc""", p)


def near_expiry_stock(con, days=30) -> pd.DataFrame:
    """Latest snapshot only: value of saleable stock expiring within `days`."""
    return df(con, f"""
        select w.warehouse_name, p.category,
               round(sum(i.on_hand_value_inr)/1e5, 1) as at_risk_lakh,
               sum(i.on_hand_cases) as cases
        from fct_inventory i
        join dim_warehouse w using(warehouse_id)
        join dim_product p using(product_id)
        where i.snapshot_date = (select max(snapshot_date) from fct_inventory)
          and i.days_to_expiry between 0 and {days}
        group by 1,2 having at_risk_lakh > 0 order by 3 desc""")


def discontinued_still_ordered(con, quarter=None) -> pd.DataFrame:
    w, p = _filters(quarter, None, "l")
    return df(con, f"""
        select p.sku_code, p.product_name, p.discontinued_date,
               count(*) as lines_after_discontinuation,
               round(sum(l.line_value_inr)/1e5,1) as value_lakh
        from fct_order_line l join dim_product p using(product_id)
        where p.discontinued_date is not null and l.order_date > p.discontinued_date {w}
        group by 1,2,3 order by 4 desc""", p)


# --------------------------------------------------------------------------
# V2: freight (needs cache/fct_freight from etl/fetch_freight.py)
# --------------------------------------------------------------------------

def freight_per_case_by_warehouse(con, quarter=None) -> pd.DataFrame:
    """Freight invoices carry warehouse + service month but no delivery key, so
    cost-per-case is honest only at warehouse x month grain (see DECISIONS)."""
    w, p = _filters(quarter, None, "f")
    return df(con, f"""
        with freight as (
            select warehouse_code, sum(amount_inr) as freight_inr
            from fct_freight f where 1=1 {w} group by 1),
        cases as (
            select wh.warehouse_code, sum(l.delivered_case) as delivered_cases
            from fct_order_line l join dim_warehouse wh using(warehouse_id)
            where l.service_measurable {w.replace('f.','l.')} group by 1)
        select c.warehouse_code, round(f.freight_inr/1e7,2) as freight_cr,
               round(c.delivered_cases) as delivered_cases,
               round(f.freight_inr / c.delivered_cases, 1) as freight_per_case_inr
        from cases c join freight f using(warehouse_code)
        order by freight_per_case_inr desc""", p + p)


def freight_by_carrier(con, quarter=None) -> pd.DataFrame:
    w, p = _filters(quarter, None)
    return df(con, f"""
        select carrier_name, round(sum(amount_inr)/1e7,2) as freight_cr,
               count(*) as invoices,
               round(avg(amount_inr),0) as avg_invoice_inr,
               sum(status='DISPUTED') as disputed
        from fct_freight where 1=1 {w} group by 1 order by 2 desc""", p)


# --------------------------------------------------------------------------
# V2: price position (needs cache/price tables from etl/scrape_bazaarpulse.py)
# --------------------------------------------------------------------------

def price_position_by_city_category(con) -> pd.DataFrame:
    return df(con, """
        select m.city, p.category,
               count(distinct m.product_id) as skus_observed,
               round(avg((m.observed_price_inr - m.our_mrp_inr)*100.0/m.our_mrp_inr), 1)
                   as avg_gap_vs_mrp_pct
        from price_observation m join dim_product p using(product_id)
        group by 1,2 order by 1,2""")


def mrp_vs_lowest_competitor(con, city=None, top_n=20) -> pd.DataFrame:
    """Brief Q6: top SKUs by delivered value vs lowest observed shelf price."""
    city_w = "and m.city = ?" if city else ""
    params = [city] if city else []
    return df(con, f"""
        with top_skus as (
            select product_id, sum(delivered_value_inr) as value_inr
            from fct_order_line where service_measurable
            group by 1 order by 2 desc limit {top_n})
        select p.sku_code, p.product_name, round(t.value_inr/1e7,2) as sales_cr,
               p.mrp_current_inr as our_mrp,
               round(min(m.observed_price_inr),2) as lowest_competitor_price,
               round((min(m.observed_price_inr)-p.mrp_current_inr)*100.0/p.mrp_current_inr,1)
                   as gap_pct
        from top_skus t
        join dim_product p using(product_id)
        left join price_observation m on m.product_id = t.product_id {city_w}
        group by 1,2,3,4 order by t.value_inr desc""", params)
