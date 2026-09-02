"""Kestrel Supply Chain Control Tower - Streamlit dashboard.

    streamlit run app.py

One screen, worst performers first, regional managers get their own view via
the region filter. Every number comes from metrics.py / the semantic layer,
so the dashboard and the ask-anything chat can never disagree.
"""
import pandas as pd
import streamlit as st

import metrics as M
from config import IN_FULL_THRESHOLD, ON_TIME_GRACE_MIN

# Validated categorical palette (dataviz reference instance; slots 1-3 pass
# all-pairs colour-vision checks in both modes).
C1, C2, C3 = "#2a78d6", "#eb6834", "#1baf7a"

st.set_page_config(page_title="Kestrel Control Tower", page_icon="🗼", layout="wide")

LAST_FULL_MONTH = "2026-06"     # last complete month in the data


@st.cache_resource
def get_con():
    return M.connect()


con = get_con()

# ---------------------------------------------------------------- sidebar --
with st.sidebar:
    st.title("Kestrel Control Tower")
    qs = M.quarters(con)
    default_q = qs.index("FY27 Q1") if "FY27 Q1" in qs else len(qs) - 1
    quarter = st.selectbox("Fiscal quarter (Apr-Mar year)", qs, index=default_q)
    regs = M.regions(con)
    region_name = st.selectbox("Region", ["All regions"] + regs.region_name.tolist())
    region_id = (None if region_name == "All regions"
                 else int(regs.loc[regs.region_name == region_name, "region_id"].iloc[0]))
    uom = st.radio("Fill rate basis", ["each", "case"], horizontal=True,
                   help="Customers penalise on units (eaches); ops has always "
                        "reported cases. Both are one click away - see FINDINGS F1.")
    st.caption(
        f"**Definitions.** On-time: within {ON_TIME_GRACE_MIN} min of plan. "
        f"In-full: ≥{IN_FULL_THRESHOLD:.0%} of ordered units (no order in this data "
        "is ever 100% filled - FINDINGS F3). Service metrics cover DELIVERED + "
        "PARTIAL orders. Rankings exclude test/closed/deleted outlets; totals keep them.")

k = M.kpi_summary(con, quarter=quarter, region_id=region_id, uom=uom)
k_other = M.kpi_summary(con, quarter=quarter, region_id=region_id,
                        uom=("case" if uom == "each" else "each"))

# --------------------------------------------------------------- headline --
st.subheader(f"{quarter} · {region_name}")
t = st.columns(5)
t[0].metric(f"Fill rate ({uom}es)", f"{k['fill_rate_pct']:.1f}%",
            f"{k['fill_rate_pct']-k_other['fill_rate_pct']:+.2f}pt vs {'case' if uom=='each' else 'each'} basis",
            delta_color="off")
t[1].metric("OTIF", f"{k['otif_pct']:.1f}%",
            f"on-time {k['on_time_pct']:.0f}% · in-full {k['in_full_pct']:.0f}%",
            delta_color="off")
t[2].metric("Excursions / 100 chilled", f"{k['excursions_per_100_chilled']:.1f}")
t[3].metric("Returns vs dispatch", f"{k['returns_pct_of_dispatch']:.2f}%",
            f"₹{k['returns_value_inr']/1e5:.1f} lakh", delta_color="off")
if k.get("freight_per_delivered_case_inr"):
    t[4].metric("Freight / delivered case", f"₹{k['freight_per_delivered_case_inr']:.1f}",
                f"₹{k['freight_inr']/1e7:.1f} cr billed", delta_color="off")
elif M.has_table(con, "fct_freight"):
    t[4].metric("Freight / delivered case", "n/a by region",
                "invoices carry no region", delta_color="off")
else:
    t[4].metric("Freight / delivered case", "n/a", "run etl/fetch_freight.py",
                delta_color="off")

tabs = st.tabs(["Service", "Cold chain", "Money", "Price position",
                "Ask anything", "Data health"])

# ---------------------------------------------------------------- service --
with tabs[0]:
    st.markdown("#### Worst performers first")
    c1, c2 = st.columns(2)
    with c1:
        st.markdown(f"**Lowest fill-rate outlets** ({uom} basis)")
        st.dataframe(M.worst_outlets(con, quarter, region_id, uom), hide_index=True,
                     width="stretch")
    with c2:
        st.markdown("**Lowest OTIF routes**")
        st.dataframe(M.worst_routes(con, quarter, region_id), hide_index=True,
                     width="stretch")
    c3, c4 = st.columns(2)
    with c3:
        st.markdown("**Warehouses by fill rate**")
        st.dataframe(M.worst_warehouses(con, quarter, region_id, uom), hide_index=True,
                     width="stretch")
    with c4:
        st.markdown("**Regions**")
        st.dataframe(M.fill_by_region(con, quarter, uom), hide_index=True,
                     width="stretch")

    st.markdown("#### 18-month trend (fill rate & OTIF, %)")
    trend = M.monthly_service_trend(con, region_id, uom)
    trend = trend[trend.month_label <= LAST_FULL_MONTH].set_index("month_label")
    st.line_chart(trend[["fill_pct", "otif_pct"]], color=[C1, C2], height=260)
    st.caption(f"Open orders in scope: {k['open_orders']} "
               f"(₹{k['open_orders_value_inr']/1e7:.1f} cr) - excluded from service "
               "metrics: they carry delivered quantities but no delivery notes (F9).")

# -------------------------------------------------------------- cold chain --
with tabs[1]:
    exc = M.excursion_trend(con, region_id)
    exc = exc[exc.month_label <= LAST_FULL_MONTH]
    amb_share = exc.chilled_on_ambient_vehicle.sum() / max(exc.chilled_deliveries.sum(), 1)
    c1, c2 = st.columns([2, 1])
    with c1:
        st.markdown("#### Temperature excursions per 100 chilled deliveries")
        st.line_chart(exc.set_index("month_label")[["excursions_per_100_chilled"]],
                      color=[C1], height=260)
    with c2:
        st.metric("Chilled deliveries on NON-reefer vehicles",
                  f"{amb_share:.0%}",
                  "the structural cold-chain risk (F16)", delta_color="off")
        st.caption("A 'chilled delivery' carries ≥1 chilled SKU. 61% of them move "
                   "on ambient vehicles; excursion flags on fully-ambient loads "
                   "(391) are excluded as physically meaningless.")
    st.markdown("#### Stock at risk: expiring within 30 days (latest snapshot)")
    st.dataframe(M.near_expiry_stock(con), hide_index=True, width="stretch")
    st.markdown("#### Cold-chain and expiry returns")
    rr = M.returns_by_reason(con, quarter, region_id)
    st.dataframe(rr[rr.return_reason.isin(["Cold chain breach", "Near expiry"])],
                 hide_index=True, width="stretch")

# ------------------------------------------------------------------ money --
with tabs[2]:
    c1, c2 = st.columns(2)
    with c1:
        st.markdown("#### Returns value by category (₹ lakh)")
        rc = M.returns_by_category(con, quarter, region_id)
        st.bar_chart(rc.set_index("category")["returns_lakh"], color=C1, height=260)
        st.dataframe(rc, hide_index=True, width="stretch")
    with c2:
        st.markdown("#### Returns by reason")
        st.dataframe(M.returns_by_reason(con, quarter, region_id), hide_index=True,
                     width="stretch")
        st.markdown("#### Leakage: discontinued SKUs still being ordered")
        st.dataframe(M.discontinued_still_ordered(con, quarter).head(10),
                     hide_index=True, width="stretch")

    st.markdown("#### Freight (carrier-billed, from partner API)")
    if M.has_table(con, "fct_freight"):
        c3, c4 = st.columns(2)
        with c3:
            st.markdown("**Cost per delivered case, by warehouse** "
                        "(warehouse × month grain - invoices carry no delivery key)")
            st.dataframe(M.freight_per_case_by_warehouse(con, quarter),
                         hide_index=True, width="stretch")
        with c4:
            st.markdown("**By carrier**")
            st.dataframe(M.freight_by_carrier(con, quarter), hide_index=True,
                         width="stretch")
        st.caption("Driver-entered fuel_cost_inr is NOT used anywhere: the partner "
                   "API invoices are the only billed freight numbers.")
    else:
        st.info("Freight invoices not fetched yet - run `python etl/fetch_freight.py` "
                "(walks the partner API once, ~2 min, cached locally).")

# --------------------------------------------------------- price position --
with tabs[3]:
    if M.has_table(con, "price_observation"):
        st.markdown("#### Competitor price vs our MRP - average gap by city & category")
        gap = M.price_position_by_city_category(con)
        pivot = gap.pivot_table(index="category", columns="city",
                                values="avg_gap_vs_mrp_pct")

        def diverging_tint(v):
            """Two-hue diverging fill, neutral at zero (no matplotlib needed).
            Orange = competitors undercut our MRP; blue = they price above it."""
            if pd.isna(v):
                return ""
            a = min(abs(v) / 30, 1) * 0.55
            r, g, b = (235, 104, 52) if v < 0 else (42, 120, 214)
            return f"background-color: rgba({r},{g},{b},{a:.2f})"

        st.dataframe(pivot.style.format("{:+.1f}%").map(diverging_tint),
                     width="stretch")
        unmatched = M.df(con, """select count(*) as n from price_listing
                                 where product_id is null""").iloc[0, 0]
        matched = M.df(con, "select count(*) as n from price_listing "
                            "where product_id is not null").iloc[0, 0]
        st.caption(f"Negative = competitors sell below our MRP. Prices scraped from "
                   f"BazaarPulse; {matched} listings matched to SKUs, {unmatched} "
                   "unmatched (listed under Data health - never silently dropped).")
        city = st.selectbox("City for SKU-level view",
                            sorted(gap.city.unique().tolist()))
        st.markdown(f"#### Top 20 SKUs by sales value: our MRP vs lowest observed price in {city}")
        st.dataframe(M.mrp_vs_lowest_competitor(con, city=city), hide_index=True,
                     width="stretch")
    else:
        st.info("Competitor prices not scraped yet - run "
                "`python etl/scrape_bazaarpulse.py` (serves the bundled site locally, "
                "respects robots.txt, caches results).")

# ------------------------------------------------------------ ask anything --
with tabs[4]:
    import chat
    chat.render(con)

# ------------------------------------------------------------- data health --
with tabs[5]:
    st.markdown("#### Outlets excluded from rankings")
    st.dataframe(M.df(con, """
        select exclude_reason, count(*) as outlets from dim_outlet
        where exclude_reason is not null group by 1 order by 2 desc"""),
        hide_index=True)
    if M.has_table(con, "price_listing"):
        st.markdown("#### Competitor listings we could NOT match to a SKU")
        st.dataframe(M.df(con, """
            select listing_id, city, retailer, listing_title, pack, category_site,
                   price_inr from price_listing where product_id is null
            order by city, listing_title"""), hide_index=True, width="stretch")
    st.markdown("#### Build metadata")
    st.dataframe(M.df(con, "select * from meta_build"), hide_index=True)
    st.markdown("---")
    findings = (M.ANALYTICS_DB.parent / "FINDINGS.md")
    if findings.exists():
        st.markdown(findings.read_text(encoding="utf-8"))
