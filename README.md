# Kestrel Supply Chain Control Tower

One screen for the Head of Supply Chain Ops: service, cold chain, money and
price position, worst performers first, with an ask-anything box that answers
plain-English questions **and shows the SQL and rows behind every answer**.

Built for the FDE take-home. Read [`DECISIONS.md`](DECISIONS.md) first, then
[`FINDINGS.md`](FINDINGS.md) — the 17 verified data findings the design rests on.

## Cold start

Prerequisites: Python 3.11+ and the assignment pack's data.
This repo expects to sit **inside the assignment pack** (next to `data/`,
`bazaarpulse_site/`, `partner_api/`). If your layout differs, point the env
vars in `config.py` (`KESTREL_DB`, `BAZAARPULSE_SITE_DIR`, `PARTNER_API_SCRIPT`)
at the right paths. The database file is **not** committed — supply your own
copy of `kestrel_ops.db`.

```bash
pip install -r requirements.txt

python profile.py     # regenerates FINDINGS.md from the raw DB   (~15 s)
python build.py       # builds the semantic layer analytics.sqlite (~15 s)
python server.py      # dashboard -> http://localhost:8500
                      # Ask AI    -> http://localhost:8500/ask
```

The UI is dependency-free HTML/CSS/JS served by FastAPI - no frontend build,
no framework, nothing else to install.

That is the whole V1. Two optional externals feed the remaining tiles:

```bash
python etl/fetch_freight.py       # walks the partner API once (~3 min), caches,
                                  # loads fct_freight. Starts the mock server
                                  # itself if nothing answers on :8088.
python etl/scrape_bazaarpulse.py  # scrapes the bundled competitor site.
                                  # ~20 min first run: robots.txt asks for a
                                  # 1s crawl-delay and we honour it across
                                  # ~1,200 pages. Cached + resumable after.
                                  # Serves the site itself if :8080 is quiet.

python profile.py                 # re-run once fetch_freight.py has cached data,
                                  # to pick up F18 (disputed-invoice finding) -
                                  # profile.py only sees what's in cache/ so far.
```

Both are resumable and cache to `cache/`; re-running `build.py` re-materialises
their tables from cache without touching the network. Nothing here mutates the
ops database — it is opened read-only everywhere.

### Ask AI (optional API key)

`/ask` is a full-page chat. Each question becomes one SQL query against the
cleaned layer; the query runs; the answer is written from the rows that came
back. Every answer shows **what was measured** (one plain-English line), the
rows, and the SQL - and the model declines questions the tables cannot answer
(forecasts, NPS, weather) instead of guessing. Put `ANTHROPIC_API_KEY=sk-ant-...`
in `control_tower/.env` (gitignored; plain KEY=VALUE lines) or export it, then
restart the server. Without a key the page still works through pre-wired
questions; nothing else in the app needs the network.

### Verify without the UI

```bash
python smoke_test.py  # answers the brief's eight illustrative questions in the terminal
python eval_chat.py   # regression-tests the chat: 12 questions (the brief's eight, a
                      # known-bad one, three adversarial) checked against metrics.py.
                      # Needs the API key; ~20 calls.
```

## Layout

| File | Role |
|---|---|
| `profile.py` | Recomputes every claim in `FINDINGS.md` from the raw DB |
| `build.py` | Raw SQLite → cleaned semantic layer (`analytics.sqlite`), rebuilt from scratch each run |
| `metrics.py` | **Every KPI defined exactly once.** Dashboard, smoke test and chat all read from here |
| `server.py` | FastAPI backend: JSON endpoints over metrics.py + the ask endpoint; serves `/` and `/ask` |
| `web/index.html` | The dashboard - plain-English tiles with targets, worst performers first, six tabs |
| `web/ask.html` | The Ask AI page - chat UI with "measured as", data and SQL under every answer |
| `web/app.css`, `web/common.js` | Shared styles and helpers (tables, SVG charts, formatting); zero frontend dependencies |
| `asksql.py` | NL → SQL over the semantic layer; the dashboard's definitions are in the prompt as reference queries; SELECT-only, read-only connection |
| `eval_chat.py` | Chat regression eval: generated SQL checked against metrics.py at run time |
| `etl/fetch_freight.py` | Partner API walk: 429/503 retries, cursor checkpointing, paise→INR |
| `etl/scrape_bazaarpulse.py` | Robots-respecting scraper: four per-city price markups, two pagination schemes, SKU matcher |
| `smoke_test.py` | The brief's 8 questions, answered from the CLI |
| `config.py` | All paths and every analytical threshold, in one reviewable place |

## Metric definitions (the short version)

* **Fill rate** — delivered/ordered on DELIVERED+PARTIAL orders, in **eaches** by
  default (customers penalise on units) with a one-click **cases** toggle (how
  ops has always reported). The two differ by ~0.3pt; the *naive* mixed-unit
  number both teams were arguing about is ~2.5pt higher than either. See F1.
* **OTIF** — on-time = within 30 min of plan (from `delay_minutes`; the raw
  arrival timestamps have corrupted hour fields, F4). In-full = ≥90% of ordered
  eaches: nothing in this data is ever 100% filled (F3), so a literal in-full
  would be identically zero.
* **Cold chain** — a "chilled delivery" *carries chilled product*; 78% of them
  run on non-reefer vehicles (F16), which is the finding.
* **Money** — net values recomputed from components (one source system inflates
  its reported net by exactly 8.5%, F2). Freight uses carrier invoices only,
  never the driver-entered fuel column; cost per case is computed at
  warehouse × month grain because invoices carry no delivery key, and only from
  **confirmed** invoices (PAID/PENDING) — 1 in 5 invoices is DISPUTED and is
  shown as its own figure, never folded into cost (F18).
* Rankings exclude test/closed/deleted outlets; historical totals keep them (F12).
