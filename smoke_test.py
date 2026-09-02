"""Answers the eight illustrative questions from the assignment brief, from the
command line, using the same metric definitions the dashboard uses.

    python smoke_test.py

Questions 6 and 7 need the V2 external data (competitor scrape / freight API);
they degrade gracefully until those caches exist.
"""
import pandas as pd

import metrics as M

pd.set_option("display.width", 160)
pd.set_option("display.max_columns", 20)

LAST_MONTH = "2026-06"          # last complete month in the data
LAST_QUARTER = "FY27 Q1"        # Apr-Jun 2026: last complete fiscal quarter


def hdr(n, text):
    print(f"\n{'='*100}\nQ{n}. {text}\n{'='*100}")


def main():
    con = M.connect()

    hdr(1, "Five outlets with the lowest CASE fill rate last month (excl. closed/test outlets)")
    print(M.df(con, f"""
        select ou.outlet_code, ou.outlet_name, ou.city, ou.channel,
               round(sum(l.delivered_case)*100.0/sum(l.ordered_case),1) as case_fill_pct
        from fct_order_line l join dim_outlet ou using(outlet_id)
        where l.service_measurable and ou.exclude_reason is null and l.month_label = ?
        group by 1,2,3,4 having sum(l.ordered_each) >= {M.RANKING_MIN_ORDERED_EACH}
        order by case_fill_pct limit 5""", (LAST_MONTH,)).to_string(index=False))

    hdr(2, f"OTIF by region, last complete quarter ({LAST_QUARTER})")
    print(M.df(con, """
        select rg.region_name, round(avg(d.otif)*100,1) as otif_pct,
               round(avg(d.on_time)*100,1) as on_time_pct,
               round(avg(d.in_full)*100,1) as in_full_pct, count(*) as deliveries
        from fct_delivery d join dim_region rg using(region_id)
        where d.fy_quarter = ? group by 1 order by otif_pct""",
        (LAST_QUARTER,)).to_string(index=False))

    hdr(3, "Categories driving the largest value of returns, with leading reason code")
    print(M.returns_by_category(con).to_string(index=False))

    hdr(4, "Temperature excursions per hundred chilled deliveries, by month")
    print(M.excursion_trend(con).to_string(index=False))

    hdr(5, "Routes more than two hours late on more than one delivery in ten")
    print(M.df(con, """
        select r.route_code, r.route_name, w.warehouse_name, count(*) as deliveries,
               sum(d.delay_minutes > 120) as late_2h,
               round(sum(d.delay_minutes > 120)*100.0/count(*),1) as pct_late_2h
        from fct_delivery d join dim_route r using(route_id)
        join dim_warehouse w on w.warehouse_id = d.warehouse_id
        group by 1,2,3 having pct_late_2h > 10 order by pct_late_2h desc
        """).to_string(index=False, max_rows=20))

    hdr(6, "Top 20 SKUs by value: our MRP vs lowest observed competitor price in Mumbai")
    if M.has_table(con, "price_observation"):
        print(M.mrp_vs_lowest_competitor(con, city="Mumbai").to_string(index=False))
    else:
        print("  [needs V2 scrape cache - run: python etl/scrape_bazaarpulse.py]")

    hdr(7, f"Freight cost per delivered case, by warehouse, {LAST_QUARTER}")
    if M.has_table(con, "fct_freight"):
        print(M.freight_per_case_by_warehouse(con, quarter=LAST_QUARTER).to_string(index=False))
    else:
        print("  [needs V2 freight cache - run: python etl/fetch_freight.py]")

    hdr(8, "Outlets that ordered a discontinued SKU after its discontinuation date")
    print(M.df(con, """
        select ou.outlet_code, ou.outlet_name, count(distinct l.order_id) as orders,
               count(distinct l.product_id) as discontinued_skus,
               round(sum(l.line_value_inr)/1e5,1) as value_lakh
        from fct_order_line l
        join dim_product p using(product_id)
        join dim_outlet ou using(outlet_id)
        where p.discontinued_date is not null and l.order_date > p.discontinued_date
          and ou.exclude_reason is null
        group by 1,2 order by value_lakh desc limit 10""").to_string(index=False))
    print("\n(top 10 real outlets shown; every outlet - including the TEST ones - is affected.")
    print(" See FINDINGS F11: nothing blocks discontinued SKUs at order entry)")

    print("\nAll headline KPIs for the front page:", )
    for k, v in M.kpi_summary(con, quarter=LAST_QUARTER).items():
        print(f"  {k:<32} {v if v is None else (round(v,2) if isinstance(v,float) else v)}")


if __name__ == "__main__":
    main()
