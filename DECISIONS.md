# DECISIONS

**What I built.** A semantic layer, not a dashboard. Four people bring Divya
four numbers, so the design makes that impossible: `build.py` applies every
cleaning rule once into `analytics.sqlite` (source read-only, raw values kept
beside cleaned ones), `metrics.py` defines each KPI once, and the dashboard,
smoke test and chat read the same definitions. V2 adds a freight API client and
a competitor scraper; their caches are committed — pack mock sources, not
client data — so every tab opens from the three documented commands: "if it
does not open, I will not use it."

**A product, not a console.** Divya is an operator, so nothing on her screen is
about how it works. Five tabs for her five asks; the audit trail lives in
`FINDINGS.md` and `eval_chat.py` where engineers look, plus one footer line on
data currency and excluded outlets. The chat states **what was counted** in
plain words and shows the numbers, never the SQL — she asked for "an answer
with the numbers behind it". The API still returns the query and the eval still
checks it, so nothing is less auditable; it is audited where that belongs.

**The contradiction, resolved.** Divya wants fill rate in cases, Rakesh in
eaches. Both are computed on every fact row; the toggle defaults to **eaches**,
how customers penalise. The real problem is neither: 72% of orders mix units on
their own lines, and the naive sum (88.1%) overstates the truth (85.6% eaches /
85.9% cases) — *why* their numbers never matched the customers'. "Q1" is fiscal
Apr–Jun, shown with explicit dates.

**Judgement calls the data forced** (evidence in FINDINGS.md). In-full = ≥90%
of ordered eaches: no line is ever 100% delivered, so a literal OTIF is 0% for
18 months; I took the highest threshold that discriminates over reporting zero
(F3). OTIF trusts `delay_minutes` — both vendors' timestamps have corrupted
hours (F4). "Chilled delivery" means *carries chilled product*; 78% run on
ambient vehicles (F16). Outlet dedup merges only GST-proven pairs (F8). Test
and closed outlets leave rankings but stay in totals — ₹267 crore of history
(F12). OPEN orders leave service metrics (F9). Freight per case is warehouse ×
month on confirmed invoices; 1 in 5 is disputed (F18). Returns follow the
credit note, not the driver app (F20). The colour bands (fill 95/90, OTIF
70/50) are plausible targets, not Kestrel's — one dict in `config.py`, to be
replaced with theirs.

**Noticed, not fixed** (brief §8). Discontinued SKUs, closed outlets and exited
reps all keep transacting — order entry validates against no master (F11, F21).
Promo codes ignore the promotion's own dates on 95% of orders, so "did it work"
is unanswerable and the chat says so rather than inventing an uplift (F22).
Nine product names map to two SKUs each (F17). Each needs a client answer.

**Deliberately not built.** Auth/RBAC (the region filter covers regional
managers for a demo); scheduled refresh (static extract); weather enrichment
("it was available" isn't a reason); shipment-event ingestion (41,500 flaky
calls that join to nothing); fuzzy dedup; forecasting. The scraper never
touches `/internal/` — robots.txt disallows it, binding even locally.

**With two more weeks.** Incremental builds on a real warehouse
(DuckDB/Postgres, dbt-tested); embedding-based price matching with human
review; the eval in CI; alerting on worst-performer deltas; saved questions.

**What breaks first.** The full rebuild — O(all history) in in-process SQLite —
is fine at 820K rows and wrong at 100×. The title-based SKU matcher decays as
competitors reword titles. And the recurring one: an unverified status field
summed as if valid. It bit twice — the chat priced "orders on discontinued
SKUs" at those SKUs' lifetime value (8× too big); freight summed disputed
invoices (25% high). Fixed structurally: the dashboard's definitions sit in the
chat's prompt, every answer states its measure, the model may decline, and
`eval_chat.py` checks the brief's questions plus **held-out** ones it never
saw, since the graders will use a different set. Next audit: every status field
on an external feed gets an explicit include/exclude call before a sum.
