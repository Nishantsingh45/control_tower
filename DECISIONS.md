# DECISIONS

**What I built.** A semantic layer, not a dashboard. Divya's actual complaint is
four people bringing four numbers for the same thing, so the architecture makes
that impossible: `build.py` applies every cleaning rule once into
`analytics.sqlite`, `metrics.py` defines each KPI exactly once, and the
dashboard, the smoke test and the ask-anything chat all read the same
definitions. The chat writes SQL against the *cleaned* layer and shows the SQL
and rows under every answer. V1 = profiling + layer + dashboard + chat; V2 =
freight API client and the competitor-price scraper, both cached locally so the
app never depends on a live fetch.

**The contradiction, resolved.** Divya wants fill rate in cases, Rakesh in
eaches. Both are right for their audience, so both are computed on every fact
row and the UI carries a toggle, defaulting to **eaches** because that is how
customers penalise. The real problem is neither basis: 72% of orders mix units
on their lines, and the naive mixed-unit sum (88.1%) overstates the true rate
(85.6% eaches / 85.9% cases) — which is likely *why* their numbers never match
the customers'. "Q1" is taken as fiscal Apr–Jun (FY runs April–March), shown
with explicit date ranges so nobody has to guess.

**Judgement calls where the data forced one** (evidence in FINDINGS.md):
in-full = ≥90% of ordered eaches, because nothing in 511,516 lines is ever
100% delivered and even 95% carries no signal (F3); OTIF trusts `delay_minutes`
because both vendors' arrival timestamps have hour-corrupted values (F4);
"chilled delivery" means *carries chilled product* — 78% of them run on ambient
vehicles, which is the real cold-chain story (F16); outlet dedup merges only
GST-proven pairs (2) rather than name-matching 158 lookalikes (F8); test and
closed outlets are excluded from rankings but kept in historical totals, since
they hold ₹267 crore of real history (F12); OPEN orders are excluded from
service metrics — they carry delivered quantities with no delivery notes (F9);
freight per case is computed at warehouse × month because invoices carry no
delivery key, and anything finer would be invented precision.

**Deliberately not built.** Auth/RBAC (region filter covers the regional-manager
need for a demo); scheduled refresh (static extract); weather/holiday
enrichment (worth testing, not on this budget — "it was available" isn't a
reason); shipment-event ingestion (41,500 flaky calls for event trails that
join to nothing); fuzzy outlet dedup; forecasting. The scraper never touches
`/internal/` — robots.txt disallows it, treated as binding on a local copy too.

**With two more weeks.** Incremental builds, not full rebuilds; a real warehouse
(DuckDB/Postgres, dbt-tested models); embedding-based price matching plus human
review of the unmatched bucket; the chat eval in CI; alerting on
worst-performer deltas; weather join to separate reefer failures from heat
waves; per-user saved questions.

**What breaks first in production.** The full rebuild — O(all history) in
in-process SQLite — is fine at 820K rows and wrong at 100×; it needs
incremental loads keyed on order/delivery IDs and a real warehouse. Second:
the title-based SKU matcher (100% here, but only after a rewrite table for
retailer abbreviations like "Inst."/"Frzn") degrades as competitors reword
titles; needs a curated mapping table with confidence decay. Third, and
recurring: an unverified status field going straight into a sum. The chat's
first version priced "orders on discontinued SKUs" at the lifetime value of
every SKU discontinued *today* (real rows, 8× too big) — fixed structurally,
not with a prompt tweak: the dashboard's own reference queries and house rules
are in the prompt, every answer states what it measured, the model may
decline, and `eval_chat.py` checks twelve questions against `metrics.py`. The
same pattern turned up a second time in the canonical layer itself: freight
cost summed every invoice regardless of status, including the ~1-in-5 marked
DISPUTED, overstating the headline freight-per-case KPI by up to 25% (F18) —
fixed the way F2 handles PARTNER_API's inflated net (a confirmed figure by
default, the excluded amount always shown, never dropped). The undocumented
columns the dictionary flags were also checked, not skipped (F19); they carry
no signal. Next to audit: every status/validity field on an external feed
needs an explicit include/exclude call before a sum, and the chat needs the
eval in CI plus a query allowlist.
