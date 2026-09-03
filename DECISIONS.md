# DECISIONS

**What I built.** A semantic layer, not a dashboard. Four people bring Divya
four numbers; the design makes that impossible: `build.py` applies every
cleaning rule once into `analytics.sqlite` (source read-only, raw values kept
beside cleaned ones), `metrics.py` defines each KPI exactly once, and the
dashboard, smoke test and chat read the same definitions. The chat writes SQL
against the cleaned layer and shows its measure, rows and SQL under every
answer. V1 = profiling + layer + dashboard + chat; V2 = freight API client and
competitor scraper, their caches committed (pack mock sources, not client
data) so every tab opens from three commands — "if it does not open, I will
not use it".

**The contradiction, resolved.** Divya wants fill rate in cases, Rakesh in
eaches. Both are computed on every fact row; a toggle defaults to **eaches**,
how customers penalise. The real problem is neither basis: 72% of orders mix
units on their own lines, and the naive mixed-unit sum (88.1%) overstates the
true rate (85.6% eaches / 85.9% cases) — which is *why* their numbers never
matched the customers'. "Q1" is fiscal Apr–Jun (FY runs April–March), shown
with explicit dates.

**Judgement calls where the data forced one** (evidence in FINDINGS.md).
In-full = ≥90% of ordered eaches: no line is ever 100% delivered, so literal
OTIF is 0% for 18 months; I took the highest threshold that discriminates, as
one constant, over reporting zero (F3). OTIF trusts `delay_minutes` — both
vendors' timestamps have corrupted hours (F4). "Chilled delivery" means
*carries chilled product*; 78% run on ambient vehicles (F16). Outlet dedup
merges only GST-proven pairs (F8). Test and closed outlets leave rankings but
stay in totals — ₹267 crore of history (F12). OPEN orders leave service
metrics (F9). Freight per case is warehouse × month on confirmed invoices;
1 in 5 is disputed (F18). Returns follow the credit note; the driver app logs
returns on different orders (F20). The front-page colour bands (fill 95/90,
OTIF 70/50, excursions 1/3 per 100, returns 0.5/1%) are plausible targets,
not Kestrel's — one dict in `config.py`, to be replaced with theirs.

**Noticed, not fixed** (brief §8). Discontinued SKUs, closed outlets and exited
reps all keep transacting — order entry validates against no master (F11,
F21). Promo codes ignore the promotion's own dates on 95% of orders, so "did it
work" is unanswerable and the chat says so rather than inventing an uplift
(F22). Nine product names map to two SKUs each (F17). All need a client answer,
not a guess.

**Deliberately not built.** Auth/RBAC (the region filter covers regional
managers for a demo); scheduled refresh (static extract); weather/holiday
enrichment ("it was available" isn't a reason); shipment-event ingestion
(41,500 flaky calls that join to nothing); fuzzy outlet dedup;
forecasting. The scraper never touches `/internal/` — robots.txt disallows it,
binding even on a local copy.

**With two more weeks.** Incremental builds on a real warehouse
(DuckDB/Postgres, dbt-tested); embedding-based price matching with human
review; the chat eval in CI; alerting on worst-performer deltas; a weather join
to separate reefer failures from heat waves; saved questions per user.

**What breaks first in production.** The full rebuild — O(all history) in
in-process SQLite — is fine at 820K rows and wrong at 100×. The title-based
SKU matcher (100% only after an abbreviation rewrite table) decays as
competitors reword titles. And the recurring one: an unverified status field
summed as if valid. It bit twice — the chat priced "orders on discontinued
SKUs" at those SKUs' lifetime value (8× too big); freight summed disputed
invoices (25% high). Fixed structurally: the dashboard's definitions sit in the
chat's prompt, every answer states its measure, the model may decline, and
`eval_chat.py` checks the brief's questions plus **held-out** ones it was never
shown, since the graders will use a different set. Next audit: every status
field on an external feed gets an explicit include/exclude call before a sum.
