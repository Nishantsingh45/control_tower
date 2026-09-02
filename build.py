"""Builds the semantic layer: analytics.sqlite, rebuilt from scratch on every run.

    python build.py

Reads the ops database READ-ONLY, applies every cleaning rule found in
FINDINGS.md, and writes conformed dimensions + fact tables with indexes.
The dashboard and the ask-anything chat both query ONLY this layer, so a
number can never disagree with itself between a chart and an answer.

Cleaning rules applied (see FINDINGS.md for evidence):
  F1  quantities normalised to BOTH eaches and cases on every fact row
  F2  net order value recomputed from components; feed value kept alongside
  F4  OTIF uses delay_minutes; arrival timestamps used for dates only
  F5  all timestamps parsed per source format and normalised to IST
  F6  test/migration outlets stamped exclude_reason='TEST'
  F7  city spellings canonicalised (Bangalore->Bengaluru, New Delhi->Delhi)
  F8  outlets merged only where the same GST number proves identity
  F9  OPEN orders excluded from service metrics (phantom delivered qty)
  F10 return quantities taken as ABS(); raw sign kept alongside
  F12 exclusions are stamped, not deleted - history stays intact
"""
import datetime as dt
import re
import sqlite3
import sys

from config import ANALYTICS_DB, IST, KESTREL_DB, ON_TIME_GRACE_MIN, IN_FULL_THRESHOLD

CITY_CANONICAL = {"Bangalore": "Bengaluru", "New Delhi": "Delhi"}          # F7
TEST_NAME_PATTERN = re.compile(r"^ZZ|TEST|DO NOT USE", re.IGNORECASE)      # F6
UTC = dt.timezone.utc

MONTHS = {m: i + 1 for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"])}

RETURN_REASONS = {  # labels from the data dictionary (codes verified present in data)
    "RT01": "Near expiry", "RT02": "Transit damage", "RT03": "Wrong SKU",
    "RT04": "Quality", "RT05": "Oversupply", "RT06": "Cold chain breach",
}


def fy_parts(d: dt.date):
    """Indian fiscal year Apr-Mar. Apr 2026 -> ('FY27', 'FY27 Q1')."""
    fy_end_year = d.year + 1 if d.month >= 4 else d.year
    fy = f"FY{fy_end_year % 100:02d}"
    quarter = (d.month - 4) % 12 // 3 + 1
    return fy, f"{fy} Q{quarter}"


def parse_created_at(source_system: str, raw: str) -> dt.datetime:
    """F5: three formats; PARTNER_API is UTC and must be shifted to IST."""
    if source_system == "PARTNER_API":                       # 2025-01-01T03:29:00Z
        t = dt.datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        return t.astimezone(IST).replace(tzinfo=None)
    if source_system == "ERP_WEB":                           # 01/01/2025 18:51 (day-first, verified in build)
        return dt.datetime.strptime(raw, "%d/%m/%Y %H:%M")
    return dt.datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")    # SFA_MOBILE


def verify_erp_day_first(con) -> None:
    """'01/02/2025' is ambiguous. Decide day-first vs month-first empirically:
    the parse whose date part lands closer to order_date wins."""
    rows = con.execute("""select created_at, order_date from src.orders
                          where source_system='ERP_WEB' limit 2000""").fetchall()
    def score(fmt):
        good = 0
        for raw, od in rows:
            try:
                d = dt.datetime.strptime(raw, fmt).date()
            except ValueError:
                continue
            if abs((d - dt.date.fromisoformat(od)).days) <= 2:
                good += 1
        return good
    day_first, month_first = score("%d/%m/%Y %H:%M"), score("%m/%d/%Y %H:%M")
    if day_first < month_first:
        sys.exit("ERP_WEB created_at looks month-first; parse_created_at needs flipping.")
    print(f"  ERP_WEB format check: day-first matches order_date on {day_first}/{len(rows)} "
          f"samples vs {month_first} month-first -> day-first confirmed")


def arrival_date(vendor: str, ts: str) -> str:
    """F4: actual-arrival hour fields are corrupt, so the delivery date is taken
    from planned_arrival, which is ISO-formatted for both vendors."""
    return ts[:10]


def main() -> None:
    ANALYTICS_DB.unlink(missing_ok=True)
    # uri=True on the main connection so the read-only ATTACH URI below is honoured
    con = sqlite3.connect(f"file:{ANALYTICS_DB.as_posix()}?mode=rwc", uri=True)
    con.execute("PRAGMA journal_mode=OFF")
    con.execute("PRAGMA synchronous=OFF")
    con.execute(f"ATTACH DATABASE 'file:{KESTREL_DB.as_posix()}?mode=ro' AS src")
    x = con.execute

    print("Building analytics.sqlite ...")
    verify_erp_day_first(con)

    # -- dim_date ----------------------------------------------------------
    x("""create table dim_date (date text primary key, year int, month int,
         month_label text, fy text, fy_quarter text)""")
    # calendar runs past the stated 30 Jun 2026 period end: 393 credit notes are
    # dated July 2026 (returns lag their orders) and must not be silently dropped
    d, end = dt.date(2025, 1, 1), dt.date(2026, 7, 31)
    rows = []
    while d <= end:
        fy, fq = fy_parts(d)
        rows.append((d.isoformat(), d.year, d.month, d.strftime("%Y-%m"), fy, fq))
        d += dt.timedelta(days=1)
    con.executemany("insert into dim_date values (?,?,?,?,?,?)", rows)

    # -- simple dims -------------------------------------------------------
    x("""create table dim_region as
         select region_id, region_code, region_name, hq_city from src.regions""")
    x("""create table dim_warehouse as
         select warehouse_id, warehouse_code, warehouse_name, city, region_id
         from src.warehouses""")
    x("""create table dim_route as
         select route_id, route_code, route_name, warehouse_id, region_id,
                is_reefer, vehicle_type, planned_stops, status
         from src.routes""")
    x("""create table dim_product as
         select product_id, sku_code, product_name, brand, category, subcategory,
                pack_size_value, pack_size_uom, case_pack, is_chilled,
                storage_temp_band, shelf_life_days, mrp_inr as mrp_current_inr,
                list_price_inr as list_price_current_inr, status, discontinued_date
         from src.products""")
    x("""create table price_history as
         select product_id, effective_from, effective_to, mrp_inr, list_price_inr
         from src.product_price_history""")

    # -- dim_outlet (F6, F7, F8, F12) --------------------------------------
    x("""create table dim_outlet as
         select outlet_id, outlet_code, outlet_name, channel, outlet_format,
                city as city_raw, city, state, region_id, route_id, chiller_available,
                credit_limit_inr, status, closed_date, is_deleted,
                null as exclude_reason, outlet_id as canonical_outlet_id
         from src.outlets""")
    for raw, canon in CITY_CANONICAL.items():
        x("update dim_outlet set city=? where city_raw=?", (canon, raw))
    x("update dim_outlet set exclude_reason='CLOSED'  where status='CLOSED'")
    x("update dim_outlet set exclude_reason='DELETED' where is_deleted=1 or status='DELETED'")
    for oid, name in x("select outlet_id, outlet_name from dim_outlet").fetchall():
        if TEST_NAME_PATTERN.search(name or ""):
            x("update dim_outlet set exclude_reason='TEST' where outlet_id=?", (oid,))
    # F8: merge only GST-proven duplicates -> lowest outlet_id survives
    for (gst,) in x("""select gst_number from src.outlets
                       where gst_number is not null and gst_number <> ''
                       group by 1 having count(*) > 1""").fetchall():
        ids = [r[0] for r in x("select outlet_id from src.outlets where gst_number=? order by outlet_id", (gst,))]
        keeper, dupes = ids[0], ids[1:]
        x(f"""update dim_outlet set canonical_outlet_id={keeper},
              exclude_reason=coalesce(exclude_reason,'DUP_OF_{keeper}')
              where outlet_id in ({','.join(map(str, dupes))})""")

    # -- fct_order (F2, F5, F9) --------------------------------------------
    x("""create table fct_order as
         select o.order_id, o.order_number, o.order_date, dd.month_label, dd.fy_quarter,
                o.outlet_id, o.channel, o.region_id, o.route_id, o.warehouse_id,
                o.salesperson_id, o.order_status, o.source_system, o.promo_code,
                o.order_value_gross_inr as gross_inr,
                o.discount_amount_inr   as discount_inr,
                o.tax_amount_inr        as tax_inr,
                round(o.order_value_gross_inr - o.discount_amount_inr + o.tax_amount_inr, 2)
                                        as net_inr,          -- F2: recomputed
                o.order_value_net_inr   as net_as_reported_inr,
                null as created_at_ist,
                o.order_status in ('DELIVERED','PARTIAL') as service_measurable  -- F9
         from src.orders o join dim_date dd on dd.date = o.order_date""")
    x("create unique index i_fo_id on fct_order(order_id)")   # before the per-row updates
    ts = [(parse_created_at(s, c).isoformat(sep=" "), oid) for oid, s, c in
          x("select order_id, source_system, created_at from src.orders").fetchall()]
    con.executemany("update fct_order set created_at_ist=? where order_id=?", ts)

    # -- fct_order_line (F1) -----------------------------------------------
    x("""create table fct_order_line as
         select l.order_line_id, l.order_id, o.order_date, o.month_label, o.fy_quarter,
                o.outlet_id, o.region_id, o.route_id, o.warehouse_id, o.channel,
                o.order_status, o.source_system, o.service_measurable,
                l.product_id, l.qty_uom as uom_as_booked, l.case_pack_at_order,
                -- F1: every quantity in BOTH units
                case when l.qty_uom='CASE' then l.ordered_qty*l.case_pack_at_order
                     else l.ordered_qty end                          as ordered_each,
                case when l.qty_uom='EACH' then l.ordered_qty*1.0/l.case_pack_at_order
                     else l.ordered_qty end                          as ordered_case,
                case when l.qty_uom='CASE' then l.delivered_qty*l.case_pack_at_order
                     else l.delivered_qty end                        as delivered_each,
                case when l.qty_uom='EACH' then l.delivered_qty*1.0/l.case_pack_at_order
                     else l.delivered_qty end                        as delivered_case,
                l.unit_price_inr, l.line_discount_pct, l.line_value_inr,
                round(l.line_value_inr * l.delivered_qty / nullif(l.ordered_qty,0), 2)
                                                                     as delivered_value_inr,
                l.short_reason_code, l.substitution_flag
         from src.order_lines l join fct_order o using(order_id)""")

    # -- fct_delivery (F4) -------------------------------------------------
    x(f"""create table fct_delivery as
         select d.delivery_id, d.order_id, o.outlet_id, o.region_id, d.route_id,
                d.warehouse_id, o.channel, o.source_system,
                null as delivery_date, null as month_label, null as fy_quarter,
                d.telematics_vendor, d.delivery_status, d.delay_minutes,
                d.delay_minutes <= {ON_TIME_GRACE_MIN} as on_time,   -- F4
                r.is_reefer as is_reefer_route,
                d.temperature_excursion_flag, d.max_temp_celsius,
                d.returned_cases, d.distance_km, d.pod_captured, d.failure_reason_code,
                d.fuel_cost_inr as fuel_cost_driver_entered_inr,
                null as order_fill_each, null as in_full, null as otif
         from src.deliveries d
         join fct_order o using(order_id)
         join dim_route r on r.route_id = d.route_id""")
    x("create unique index i_fd_id  on fct_delivery(delivery_id)")  # before per-row updates
    x("create unique index i_fd_ord on fct_delivery(order_id)")
    dates = [(arrival_date(v, ts_), did) for did, v, ts_ in
             x("select delivery_id, telematics_vendor, planned_arrival from src.deliveries").fetchall()]
    con.executemany("update fct_delivery set delivery_date=? where delivery_id=?", dates)
    x("""update fct_delivery set
           month_label=(select month_label from dim_date where date=delivery_date),
           fy_quarter =(select fy_quarter  from dim_date where date=delivery_date)""")
    # in-full at order level (eaches), then OTIF (F3: threshold, not literal 100%)
    x("create index i_fol_ord on fct_order_line(order_id)")
    x("""create temp table order_fill as
         select order_id, sum(delivered_each)*1.0/nullif(sum(ordered_each),0) as fill
         from fct_order_line group by order_id""")
    x("create unique index temp.i_of on order_fill(order_id)")
    x("""update fct_delivery set order_fill_each =
           (select fill from order_fill f where f.order_id = fct_delivery.order_id)""")
    x(f"update fct_delivery set in_full = order_fill_each >= {IN_FULL_THRESHOLD}")
    x("update fct_delivery set otif = on_time and in_full")

    # -- fct_return (F10) --------------------------------------------------
    x("""create table fct_return as
         select r.return_id, r.credit_note_number, r.return_date,
                dd.month_label, dd.fy_quarter,
                r.order_id, r.outlet_id, o.region_id, o.channel, r.product_id,
                r.return_reason_code,
                coalesce(rr.label, r.return_reason_code) as return_reason,
                r.disposition, r.status,
                case when r.return_qty < 0 then -1 else 1 end as qty_sign_as_reported,
                case when r.qty_uom='CASE' then abs(r.return_qty)*p.case_pack
                     else abs(r.return_qty) end as return_each,
                case when r.qty_uom='EACH' then abs(r.return_qty)*1.0/p.case_pack
                     else abs(r.return_qty) end as return_case,
                abs(r.credit_note_value_inr) as credit_note_value_inr
         from src.returns_credit_notes r
         join dim_date dd on dd.date = r.return_date
         left join fct_order o using(order_id)
         left join dim_product p using(product_id)
         left join (select 'RT01' code,'Near expiry' label union all
                    select 'RT02','Transit damage' union all
                    select 'RT03','Wrong SKU' union all
                    select 'RT04','Quality' union all
                    select 'RT05','Oversupply' union all
                    select 'RT06','Cold chain breach') rr
                on rr.code = r.return_reason_code""")

    # -- fct_inventory ------------------------------------------------------
    x("""create table fct_inventory as
         select s.snapshot_id, s.snapshot_date, dd.month_label, dd.fy_quarter,
                s.warehouse_id, s.product_id, s.batch_id,
                s.on_hand_cases, s.on_hand_eaches, s.available_cases,
                s.damaged_cases, s.blocked_cases, s.expiry_date, s.ageing_bucket,
                cast(julianday(s.expiry_date) - julianday(s.snapshot_date) as int)
                    as days_to_expiry,
                p.is_chilled, p.category,
                s.on_hand_cases * p.case_pack * ph.list_price_inr as on_hand_value_inr
         from src.inventory_snapshots s
         join dim_date dd on dd.date = s.snapshot_date
         left join dim_product p using(product_id)
         left join price_history ph
                on ph.product_id = s.product_id
               and ph.effective_from <= s.snapshot_date
               and (ph.effective_to is null or ph.effective_to >= s.snapshot_date)""")

    # -- indexes (the raw DB has none at all) ------------------------------
    for ddl in [
        "create index i_fo_date   on fct_order(order_date)",
        "create index i_fo_q      on fct_order(fy_quarter)",
        "create index i_fol_date  on fct_order_line(order_date)",
        "create index i_fol_q     on fct_order_line(fy_quarter, region_id)",
        "create index i_fol_prod  on fct_order_line(product_id)",
        "create index i_fd_date   on fct_delivery(delivery_date)",
        "create index i_fd_q      on fct_delivery(fy_quarter, region_id)",
        "create index i_fr_date   on fct_return(return_date)",
        "create index i_fi_date   on fct_inventory(snapshot_date)",
    ]:
        x(ddl)

    # -- build metadata ----------------------------------------------------
    x("""create table meta_build (built_at text, source_db text, table_name text, rows int)""")
    for (t,) in x("select name from sqlite_master where type='table' and name like 'fct%' or name like 'dim%'").fetchall():
        n = x(f"select count(*) from {t}").fetchone()[0]
        x("insert into meta_build values (datetime('now'), ?, ?, ?)", (str(KESTREL_DB), t, n))
        print(f"  {t:<16} {n:>9,} rows")
    con.commit()

    # sanity: the three fill-rate bases must reproduce FINDINGS F1 exactly
    naive_each, naive_case = x("""
        select round(sum(delivered_each)*100.0/sum(ordered_each),2),
               round(sum(delivered_case)*100.0/sum(ordered_case),2)
        from fct_order_line where service_measurable""").fetchone()
    assert abs(naive_each - 85.59) < 0.05 and abs(naive_case - 85.88) < 0.05, \
        f"fill-rate check failed: {naive_each} / {naive_case}"
    print(f"  check: fill rate eaches {naive_each}% / cases {naive_case}% (matches FINDINGS F1)")
    print(f"Done -> {ANALYTICS_DB}")


if __name__ == "__main__":
    main()
