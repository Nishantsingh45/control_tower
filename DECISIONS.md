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

**What I deliberately did not build.** Auth/RBAC (the region filter serves the
regional-manager need for a demo); real-time or scheduled refresh (the data is
a static extract); weather/holiday enrichment (a hypothesis worth testing —
Open-Meteo max-temp vs excursion rate — but not on this budget, and "it was
available" is not a reason); shipment-event ingestion (41,500 extra flaky calls
for event trails that join to nothing); fuzzy outlet dedup; forecasting. The
scraper also never touches `/internal/` — robots.txt disallows it, and I treat
that as binding even on a local copy.

**With two more weeks.** Incremental builds instead of full rebuilds; a proper
warehouse (DuckDB/Postgres) with dbt-style tested models; the price matcher
extended with embedding similarity plus human review of the unmatched bucket;
alerting (worst-performer deltas pushed, not pulled); eval harness for the chat
(the brief's questions as regression tests); weather join to separate reefer
failures from heat waves; per-user views and saved questions.

**What breaks first in production.** The full rebuild — O(all history) in
in-process SQLite — is fine at 820K rows and wrong at 100×; it needs
incremental loads keyed on order/delivery IDs and a real warehouse. Second:
the title-based SKU matcher (100% here, but only after building a rewrite
table for retailer abbreviations like "Inst."/"Frzn" — real listings will not
match this cleanly) degrades as competitors reword titles; production needs a
curated mapping table with confidence decay and review of the unmatched bucket.
Third: the chat depends on an external LLM API — it degrades to pre-wired
questions, but a production version needs caching, evals and a query allowlist.
