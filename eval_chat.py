"""Regression eval for the ask-anything chat.

    python eval_chat.py             # all cases (needs ANTHROPIC_API_KEY; ~20 API calls)
    python eval_chat.py disc otif   # only cases whose id contains one of the words

Why this exists: the chat generates SQL that really runs, so it cannot invent
numbers - but it CAN write a plausible query that answers the wrong question.
(The first version priced "orders on discontinued SKUs" at the lifetime value
of every SKU that is discontinued today: real rows, 8x too big.) Each case
below asks a question the way Divya would and checks the returned rows against
the dashboard's own definitions in metrics.py, computed at run time - nothing is
hard-coded, so the expectations track the data.

Three groups: the brief's eight illustrative questions; adversarial questions
the tables cannot answer (must decline); and HELD-OUT questions that are in
neither the brief nor the chat's reference queries - because the brief says
"we will run a different set against your submission", and an eval that only
replays the questions you already taught the model measures memorisation.

A case passes when its `check` returns None; otherwise the string explains
what was wrong. Exit code 1 if anything fails.
"""
import re
import sys

import pandas as pd

import asksql
import metrics as M

LAST_MONTH, LAST_QUARTER = asksql.LAST_MONTH, asksql.LAST_QUARTER


# ------------------------------------------------------------------ helpers --

def numbers(frame: pd.DataFrame) -> list[float]:
    vals = []
    for c in frame.columns:
        if pd.api.types.is_numeric_dtype(frame[c]):
            vals += [float(v) for v in frame[c].dropna()]
    return vals


def has_value(frame, target: float, tol=0.015, scales=(1, 1e-5, 1e-7, 100, 0.01)) -> bool:
    """Does any numeric cell equal `target` (within tol, relative), allowing
    the value to be expressed in rupees / lakh / crore or pct / fraction?"""
    if frame is None or target == 0:
        return False
    for v in numbers(frame):
        for s in scales:
            t = target * s
            if t and abs(v - t) <= tol * abs(t):
                return True
    return False


def strings(frame: pd.DataFrame) -> set[str]:
    """All text cells. pandas 3 gives text columns a dedicated string dtype
    (not object), so test for 'not numeric' rather than 'object'."""
    out = set()
    for c in frame.columns:
        if not pd.api.types.is_numeric_dtype(frame[c]):
            out |= {str(v) for v in frame[c].dropna()}
    return out


def codes(frame, pattern) -> set[str]:
    rx = re.compile(pattern)
    return {s for s in strings(frame) if rx.match(s)}


# -------------------------------------------------------------------- cases --
# Each: id, question, check(res, con) -> None | failure string.
# `res` is asksql.ask()'s dict: status, sql, measures, rows (DataFrame|None), answer.

def needs_rows(res):
    if res["status"] != "ok" or res["rows"] is None:
        return f"no rows (status={res['status']}: {res['answer']})"
    return None


def c_discontinued(res, con):
    if (e := needs_rows(res)):
        return e
    ref = con.execute("""select sum(l.line_value_inr) from fct_order_line l
                         join dim_product p using(product_id)
                         where p.discontinued_date is not null
                           and l.order_date > p.discontinued_date""").fetchone()[0]
    wrong = con.execute("""select sum(l.line_value_inr) from fct_order_line l
                           join dim_product p using(product_id)
                           where p.discontinued_date is not null""").fetchone()[0]
    if not has_value(res["rows"], ref):
        return f"expected the post-discontinuation total ({ref/1e7:.2f} cr) somewhere in the rows"
    if has_value(res["rows"], wrong):
        return f"contains the lifetime value of discontinued SKUs ({wrong/1e7:.2f} cr) - date filter missing"
    if "discontinu" not in res["measures"].lower():
        return "measures line does not mention discontinuation"
    return None


def c_worst_outlets(res, con):
    if (e := needs_rows(res)):
        return e
    ref = M.df(con, f"""
        select ou.outlet_code from fct_order_line l join dim_outlet ou using(outlet_id)
        where l.service_measurable and ou.exclude_reason is null and l.month_label = ?
        group by 1 having sum(l.ordered_each) >= {M.RANKING_MIN_ORDERED_EACH}
        order by sum(l.delivered_each)*1.0/sum(l.ordered_each) limit 5""", (LAST_MONTH,))
    got = codes(res["rows"], r"^(OUT|TST)\d{5}$")
    if not got:
        return "no outlet codes in the result"
    excluded = set(M.df(con, "select outlet_code from dim_outlet where exclude_reason is not null").outlet_code)
    if got & excluded:
        return f"excluded (test/closed) outlets present: {sorted(got & excluded)}"
    overlap = got & set(ref.outlet_code)
    if len(overlap) < 3:
        return f"only {len(overlap)}/5 of the reference worst outlets present ({sorted(got)})"
    return None


def c_otif_region(res, con):
    if (e := needs_rows(res)):
        return e
    ref = M.df(con, """select rg.region_name, avg(d.otif)*100 as otif
                       from fct_delivery d join dim_region rg using(region_id)
                       where d.fy_quarter = ? group by 1""", (LAST_QUARTER,))
    hits = sum(has_value(res["rows"], v, tol=0.02) for v in ref.otif)
    if hits < 4:
        return f"only {hits}/5 regional OTIF values match FY27 Q1 (expected e.g. West {ref.otif.iloc[0]:.1f}%)"
    return None


def c_excursions(res, con):
    if (e := needs_rows(res)):
        return e
    ref = M.excursion_trend(con)
    first, last = ref.excursions_per_100_chilled.iloc[0], ref[ref.month_label == LAST_MONTH].excursions_per_100_chilled.iloc[0]
    if len(res["rows"]) < 17:
        return f"expected a monthly series (>=17 rows), got {len(res['rows'])}"
    if not (has_value(res["rows"], first, 0.03) and has_value(res["rows"], last, 0.03)):
        return f"per-100 values {first} (2025-01) / {last} ({LAST_MONTH}) not found"
    return None


def c_late_routes(res, con):
    if (e := needs_rows(res)):
        return e
    ref = M.late_routes(con, n=1000)
    got = codes(res["rows"], r"^RT\d{4}$")
    if not got:
        return "no route codes in the result"
    if abs(len(got) - len(ref)) > max(3, 0.15 * len(ref)):
        return f"{len(got)} routes returned vs {len(ref)} in the reference (>2h late on >10%)"
    if ref.route_code.iloc[0] not in got:
        return f"worst route {ref.route_code.iloc[0]} missing"
    return None


def c_freight(res, con):
    if (e := needs_rows(res)):
        return e
    ref = M.freight_per_case_by_warehouse(con, quarter=LAST_QUARTER)
    hits = sum(has_value(res["rows"], v, 0.02) for v in ref.freight_per_case_inr)
    if hits < 6:
        return f"only {hits}/8 warehouse freight-per-case values match (expected e.g. {ref.freight_per_case_inr.iloc[0]})"
    return None


def c_price_mumbai(res, con):
    if (e := needs_rows(res)):
        return e
    ref = M.mrp_vs_lowest_competitor(con, city="Mumbai").dropna(subset=["lowest_competitor_price"])
    got = codes(res["rows"], r"^SKU\d{5}$")
    if len(got) < 15:
        return f"expected ~20 SKUs, got {len(got)}"
    hits = sum(has_value(res["rows"], v, 0.01) for v in ref.lowest_competitor_price.head(5))
    if hits < 3:
        return "lowest Mumbai competitor prices do not match the reference"
    return None


def c_disc_outlets(res, con):
    if (e := needs_rows(res)):
        return e
    got = codes(res["rows"], r"^(OUT|TST)\d{5}$")
    if not got:
        return "no outlet codes in the result"
    return None


def c_returns_cat(res, con):
    if (e := needs_rows(res)):
        return e
    ref = M.returns_by_category(con)
    top = ref.iloc[0]
    if not has_value(res["rows"], top.returns_lakh * 1e5, 0.02):
        return f"top category value ({top.category} {top.returns_lakh} lakh) not found"
    got = strings(res["rows"])
    # the question says "reason code": accept the label ('Near expiry') or the code ('RT01_NEAR_EXPIRY')
    if not (any(r in got for r in ref.top_reason.unique()) or codes(res["rows"], r"^RT0\d")):
        return "no reason label or code in the result"
    return None


def c_cannot(res, con):
    if res["status"] == "cannot":
        return None
    if res["status"] == "error":
        return None      # refused by the guard is also acceptable
    return f"answered with rows instead of declining: {res['measures']!r}"


# ---- held-out: NOT in the brief and NOT in asksql's reference queries ----
# The brief says "we will run a different set against your submission". These
# cases exist so the eval measures generalisation, not memorisation of the
# eight questions we were shown.

def c_channel_compare(res, con):
    if (e := needs_rows(res)):
        return e
    got = strings(res["rows"])
    if not ({"HORECA", "MT"} <= got):
        return f"expected rows labelled HORECA and MT, got {sorted(got)[:6]}"
    ref = M.df(con, """select channel, sum(delivered_each)*100.0/sum(ordered_each) as fill
                       from fct_order_line where service_measurable and channel in ('HORECA','MT')
                       group by 1""")
    if sum(has_value(res["rows"], v, 0.02) for v in ref.fill) < 2:
        return "channel fill rates do not match the reference"
    return None


def c_worst_rep_named(res, con):
    if (e := needs_rows(res)):
        return e
    names = set(M.df(con, "select full_name from dim_salesperson").full_name)
    if not (strings(res["rows"]) & names):
        return "no salesperson NAME in the result - ids only (dim_salesperson not joined)"
    return None


def c_closed_still_ordering(res, con):
    if (e := needs_rows(res)):
        return e
    got = codes(res["rows"], r"^(OUT|TST)\d{5}$")
    ref = con.execute("""select count(distinct o.outlet_id) from fct_order o
                         join dim_outlet ou using(outlet_id)
                         where ou.status='CLOSED' and o.order_date > ou.closed_date""").fetchone()[0]
    if len(got) < 0.7 * ref:
        return f"{len(got)} closed outlets listed vs {ref} in the reference"
    return None


def c_promo_named_with_caveat(res, con):
    if (e := needs_rows(res)):
        return e
    names = set(M.df(con, "select promo_name from dim_promotion").promo_name)
    if not (strings(res["rows"]) & names):
        return "no promotion NAME in the result - codes only (dim_promotion not joined)"
    text = (res["measures"] + " " + res["answer"]).lower()
    if not any(w in text for w in ("window", "unreliab", "attribut", "f22", "outside", "caveat")):
        return "promotion answer carries no reliability caveat (F22)"
    return None


CASES = [
    ("discontinued-loss", "How much have we lost to orders on discontinued SKUs?", c_discontinued),
    ("worst-outlets", "Which five outlets had the lowest fill rate last month, excluding closed and test outlets?", c_worst_outlets),
    ("otif-region", "What was OTIF by region for the last complete quarter?", c_otif_region),
    ("returns-category", "Which categories drive the largest value of returns, and what is the leading reason code?", c_returns_cat),
    ("excursions-month", "Temperature excursions per hundred chilled deliveries, by month", c_excursions),
    ("late-routes", "Which routes are more than two hours late on more than one delivery in ten?", c_late_routes),
    ("price-mumbai", "For our top twenty SKUs by value, how does our MRP compare with the lowest observed competitor price in Mumbai?", c_price_mumbai),
    ("freight-warehouse", "Freight cost per delivered case, by warehouse, for the last quarter", c_freight),
    ("discontinued-outlets", "Which outlets ordered a discontinued SKU after its discontinuation date?", c_disc_outlets),
    ("adversarial-nps", "What's our NPS score by region?", c_cannot),
    ("adversarial-forecast", "What will fill rate be next month?", c_cannot),
    ("adversarial-delete", "Delete all cancelled orders.", c_cannot),
    # held-out: shapes and dimensions the brief's eight never touch
    ("heldout-channel", "Compare fill rate and OTIF between HORECA and modern trade outlets", c_channel_compare),
    ("heldout-rep", "Which salesperson has the worst fill rate?", c_worst_rep_named),
    ("heldout-closed", "Which closed outlets are still placing orders, and how much have they ordered since closing?", c_closed_still_ordering),
    ("heldout-promo", "Which promotion drove the most order value?", c_promo_named_with_caveat),
]


def main(argv):
    only = [a.lower() for a in argv]
    cases = [c for c in CASES if not only or any(o in c[0] for o in only)]
    client = asksql.get_client()
    if client is None:
        sys.exit("No Anthropic credential resolves - set ANTHROPIC_API_KEY in .env.")
    con = M.connect()
    failures = 0
    print(f"model: {asksql.CHAT_MODEL}   cases: {len(cases)}\n")
    for cid, q, check in cases:
        res = asksql.ask(client, con, q)
        problem = check(res, con)
        mark = "PASS" if problem is None else "FAIL"
        failures += problem is not None
        n = len(res["rows"]) if res["rows"] is not None else 0
        print(f"[{mark}] {cid:<22} status={res['status']:<6} rows={n:<4} {q}")
        print(f"       measures: {res['measures'] or '-'}")
        if problem:
            print(f"       PROBLEM : {problem}")
            if res["sql"]:
                print("       SQL     : " + res["sql"].replace("\n", "\n                 "))
        print()
    print(f"{len(cases) - failures}/{len(cases)} passed")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main(sys.argv[1:])
