"""Central configuration. Every path and every analytical threshold lives here,
so a reviewer can see (and change) each judgement call in one place."""
import os
from datetime import timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent

# Load KEY=VALUE pairs from an optional .env file (gitignored) so a reviewer
# can paste ANTHROPIC_API_KEY once instead of exporting it per shell.
_env = REPO_ROOT / ".env"
if _env.exists():
    for _line in _env.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

# The assignment pack ships the SQLite DB one level up from this repo.
# Override with KESTREL_DB when the reviewer supplies their own copy.
KESTREL_DB = Path(os.environ.get("KESTREL_DB", REPO_ROOT.parent / "data" / "kestrel_ops.db"))

# Derived analytics store (rebuilt from scratch by build.py; never committed).
ANALYTICS_DB = Path(os.environ.get("ANALYTICS_DB", REPO_ROOT / "analytics.sqlite"))

CACHE_DIR = REPO_ROOT / "cache"

# External surfaces (both ship with the pack and run on localhost).
BAZAARPULSE_URL = os.environ.get("BAZAARPULSE_URL", "http://localhost:8080")
BAZAARPULSE_SITE_DIR = Path(os.environ.get("BAZAARPULSE_SITE_DIR", REPO_ROOT.parent / "bazaarpulse_site"))
PARTNER_API_URL = os.environ.get("PARTNER_API_URL", "http://localhost:8088")
PARTNER_API_KEY = os.environ.get("PARTNER_API_KEY", "kp_live_7f3a9c21")
PARTNER_API_SCRIPT = Path(os.environ.get("PARTNER_API_SCRIPT", REPO_ROOT.parent / "partner_api" / "server.py"))

# IST is a fixed offset (no DST) -- a constant is safer than a tz database lookup.
IST = timezone(timedelta(hours=5, minutes=30), name="IST")

# --- Judgement calls (defended in DECISIONS.md) ---------------------------
# No order line in the data is ever 100% delivered (max observed 99.86%) and
# order-level fill tops out at 99.4% (p99 = 94.8%), so a literal - or even 98% -
# "in full" makes OTIF identically zero and carries no signal. 90% is the highest
# round threshold with discriminating power (18% of orders pass). See FINDINGS F3.
IN_FULL_THRESHOLD = 0.90
# On-time = actual arrival within this many minutes of plan.
ON_TIME_GRACE_MIN = 30

# Fiscal year runs April..March. FY label is the *ending* year: Apr 2026 -> FY27.
FY_START_MONTH = 4

# Front-page status colouring ("are we OK?" for a non-technical reader).
# A tile is green at/beyond `good`, amber between `good` and `warn`, red beyond
# `warn`. These are plausible distributor targets, NOT contractual ones - they
# are here so a reviewer can see and change them in one place.
KPI_TARGETS = {
    "fill_rate_pct":              {"good": 95,  "warn": 90,  "higher_is_better": True},
    "otif_pct":                   {"good": 70,  "warn": 50,  "higher_is_better": True},
    "excursions_per_100_chilled": {"good": 1.0, "warn": 3.0, "higher_is_better": False},
    "returns_pct_of_dispatch":    {"good": 0.5, "warn": 1.0, "higher_is_better": False},
}

# Ask-anything model (needs ANTHROPIC_API_KEY; the app degrades gracefully without it).
CHAT_MODEL = os.environ.get("KESTREL_CHAT_MODEL", "claude-opus-5")
