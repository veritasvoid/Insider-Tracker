# Insider Tracker — Project Handoff

> Paste this file at the start of any new chat session. It contains everything needed for a seamless continuation.

---

## What This Project Is

A 7-stage automated pipeline that:
1. Scrapes CEO/CFO insider buys ≥ $100k from OpenInsider daily
2. Collapses multi-day buys per insider, tags CLUSTER vs SINGLE
3. Enriches each ticker with price context (EMA50/200, velocity, short float, sector)
4. Fetches news, catalysts, sentiment via Gemini 2.5 Flash
5. Scores each pick 0–100 using a quant layer (deterministic) + AI layer (Claude Haiku)
6. Writes JSON + Markdown reports and a Bloomberg-grade HTML dashboard
7. Tracks forward performance (3d / 8d / 15d / 30d / 90d returns)

The HTML dashboard is hosted on **GitHub Pages** (static, no server). It regenerates automatically via **GitHub Actions** every weekday at 8am ET.

---

## Folder Structure

```
Insider_Tracker/               ← repo root
├── .github/workflows/
│   └── daily.yml              ← GitHub Actions: runs pipeline, commits docs/index.html
├── .gitignore                 ← excludes config.yaml, __pycache__, data/reports/
├── docs/
│   ├── index.html             ← live dashboard (committed by Actions, served by Pages)
│   └── preview.html           ← local preview output (not committed)
└── Insider_Tracker/           ← Python package
    ├── config.yaml            ← API keys + filter thresholds (NEVER commit — gitignored)
    ├── requirements.txt
    ├── db.py                  ← SQLite persistence layer
    ├── scrape.py              ← Stage 1: OpenInsider scraper
    ├── tag.py                 ← Stage 2: collapse + CLUSTER/SINGLE tagging
    ├── enrich.py              ← Stage 3: Alpaca → Polygon → Finviz price/sector/short float
    ├── research.py            ← Stage 4: Gemini 2.5 Flash news + catalysts
    ├── score.py               ← Stage 5: quant scoring + Claude Haiku qualitative layer
    ├── output.py              ← Stage 6: write JSON/MD reports + trigger HTML generation
    ├── track.py               ← Stage 7: forward performance tracker
    ├── web.py                 ← Dashboard HTML generator (Bloomberg-grade)
    ├── preview.py             ← Local preview with hardcoded sample data
    └── data/
        ├── insider_tracker.db ← SQLite DB (3 tables: insider_buys, buy_events, picks)
        └── reports/           ← Daily JSON + MD reports (gitignored)
```

---

## Pipeline Flow

```
scrape.py → tag.py → enrich.py → research.py → score.py → output.py → track.py
   S1          S2        S3           S4           S5          S6          S7
```

Each stage imports the previous: `output.py` calls `score.run()`, which calls `research.run()`, etc. Running `python output.py` triggers the entire chain.

---

## Stage Details

### Stage 1 — scrape.py
- URL: `http://openinsider.com/screener?xp=1&vl=100&fd=2&cnt=500&Action=screener`
- Filters: CEO/CFO title keywords, `P - Purchase` trade type, value ≥ $100k, within `lookback_days`
- Returns list of dicts with: `filing_date`, `trade_date`, `ticker`, `company`, `insider_name`, `title`, `price`, `qty`, `value`, `delta_own`, `trade_type`
- **Important**: `trade_date` = when the insider actually traded (shown in dashboard as "Trade YYYY-MM-DD"). `filing_date` = when Form 4 was filed with SEC (typically 1–2 days later). OpenInsider's main column shows filing date — this is why dates appear to differ by 1 day.

### Stage 2 — tag.py
- Inserts fresh buys into `insider_buys` SQLite table (UNIQUE on `trade_date + ticker + insider_name + value`)
- Pulls 30-day history per ticker from DB
- Collapses same-insider buys within 10-day window into one event
- Tags CLUSTER (2+ distinct insiders in 30d) or SINGLE
- Returns `event_start_date` = first trade date in the collapsed window

### Stage 3 — enrich.py
- **Alpaca IEX** (batch): fetches 300 days of daily closes for all tickers
- **Polygon fallback**: for any ticker Alpaca misses (small-caps, ADRs)
- **Finviz scrape**: `short_float_pct` AND `sector` for each ticker
- Computes: `current_price`, `ema_50`, `ema_200`, `price_vs_ema50_pct`, `price_vs_ema200_pct`, `price_velocity_5d`
- EMA uses standard exponential: seeded with SMA for first N bars, then `k = 2/(period+1)`

### Stage 4 — research.py
- Calls Gemini 2.5 Flash with ticker + insider buy context
- Returns: `news_headline`, `news_summary`, `catalysts` (list of strings), `news_sentiment` (bullish/neutral/bearish)

### Stage 5 — score.py
Composite 0–100 score split two layers:

**Quant layer (0–65 pts, deterministic):**
| Dimension | Max | Logic |
|-----------|-----|-------|
| D1 Insider Signal | 25 | Dollar size (log-scaled) + delta ownership % + dip-buy bonus + CEO/CFO seniority |
| D2 Price/Technical | 20 | EMA50 position + EMA200 position + 5d velocity |
| D3 Short Interest | 10 | 10–25% short float = 8pts (squeeze potential); >40% = 3pts (binary risk) |
| D4 Cluster Bonus | 10 | CLUSTER = 6+pts; SINGLE = 0 |

**AI layer (0–35 pts, Claude Haiku):**
| Dimension | Max | Logic |
|-----------|-----|-------|
| D5 Fundamentals | 12 | Revenue growth, profitability direction, balance sheet |
| D6 Catalyst Strength | 13 | FDA PDUFA/trial readouts = 11–13; vague = 2–4 |
| D7 News Alignment | 10 | News corroborates insider buy thesis |

**Stars:**
- 80–100 → ★★★★★ STRONG BUY
- 60–79  → ★★★★  BUY
- 40–59  → ★★★   WATCH
- 20–39  → ★★    WEAK
- 0–19   → ★     SKIP

Claude Haiku is called once per unique ticker (not per insider event) and the AI scores are cached for CLUSTER picks with multiple events.

### Stage 6 — output.py
- Writes timestamped JSON + Markdown to `data/reports/`
- Calls `db.save_picks()` to persist picks to SQLite
- Calls `track.update_picks()` to fill forward prices for aged picks
- Calls `web.write_html()` to generate `docs/index.html`

### Stage 7 — track.py
- `get_picks_for_tracking()`: returns picks where forward price columns are NULL and enough time has passed (≥3/8/15/30/90 calendar days since `run_date`)
- Fetches prices from Polygon with a ±5-day window (handles weekends/holidays)
- Returns are vs `price_at_pick` (market price when pipeline ran), NOT vs insider's buy price
- `load_all_picks()`: deduplicates via `MIN(id) GROUP BY (ticker, event_date)` — prevents the same insider filing appearing twice if pipeline ran on consecutive days

---

## Database Schema

**`insider_buys`** — raw SEC filings (rolling 90-day history)
- UNIQUE: `(trade_date, ticker, insider_name, value)`

**`buy_events`** — collapsed events from Stage 2
- UNIQUE: `(run_date, ticker, insider_name, event_start_date)`

**`picks`** — scored picks for Stage 7 tracking
- UNIQUE INDEX: `(ticker, COALESCE(event_date, ''))` — prevents duplicate tracking of the same filing across consecutive run_dates
- Key columns: `run_date`, `ticker`, `buy_price` (insider's price), `price_at_pick` (market price at run time), `price_3d/8d/15d/30d/90d` (forward prices filled by track.py)

---

## Dashboard — web.py

The dashboard generates a single-file HTML with three tabs: **TODAY / HISTORY / PERFORMANCE**.

**Design requirements (non-negotiable):**
- Real glassmorphism: `backdrop-filter: blur(32px) saturate(170%)`, `rgba(7,14,34,0.74)` card backgrounds
- NO 3D card tilt/perspective animations — explicitly rejected as "annoyingly bad"
- Arizona timezone (MST = UTC-7, permanent, no DST): `timezone(timedelta(hours=-7))`
- Score ring: CSS `conic-gradient` + `radial-gradient` mask for donut shape — NO SVG (SVG inside backdrop-filter creates square artifact bounding box)
- Score ring animates with `requestAnimationFrame` cubic ease-out (1100ms) on page load
- Catalysts shown as large colored pill blocks (not small dots or bullet points) — color-coded by type
- Sector badge shown prominently on each card
- NO average score in hero stats
- Cards sorted by score descending

**Key color constants in web.py:**
```python
SCORE_COLORS = {5:"#00ffd4", 4:"#2979ff", 3:"#fbbf24", 2:"#ff6b35", 1:"#ff4757"}
STAR_LABELS  = {5:"STRONG BUY", 4:"BUY", 3:"WATCH", 2:"WEAK", 1:"SKIP"}
```

**Catalyst color types:**
- `teal` (#00ffd4): FDA, PDUFA, PHASE, NDA, TRIAL
- `blue` (#5b9bff): EARNINGS, REVENUE, GUIDANCE, EPS
- `amber` (#fbbf24): EGM, AGM, MERGER, ACQUI
- `red` (#ff4757): NYSE, NASDAQ, COMPLIANCE, DELISTING
- `def` (#8892a4): everything else

**Date label**: Cards show "Trade YYYY-MM-DD" (the SEC trade date, not the filing date).

**Hero stats (TODAY tab, 4 stats):**
1. Today's Picks / count / Unique tickers
2. Cluster Signals / count / Multi-insider buys
3. Top Pick / TICKER·score / label
4. Tracked Positions / count / In performance log

---

## Critical Technical Quirk — web.py and .pyc Cache

The Write/Edit tools write files with trailing null bytes (`\x00`). Python's import system rejects source files with null bytes (`ValueError: source code string cannot contain null bytes`), and also caches `.pyc` files which may be newer than the patched source.

**After every edit to web.py, run:**
```bash
cd Insider_Tracker/Insider_Tracker
python3 -c "d=open('web.py','rb').read().rstrip(b'\x00'); open('web.py','wb').write(d)"
touch web.py
```

`preview.py` bypasses the `.pyc` cache entirely using `importlib.util.spec_from_file_location` to force-load from source:
```python
import importlib.util as _ilu, sys as _sys
_spec = _ilu.spec_from_file_location("web", __file__.replace("preview.py", "web.py"))
_web  = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_web)
```

---

## Config File Structure

`Insider_Tracker/config.yaml` (gitignored — never commit):
```yaml
scrape:
  url: "http://openinsider.com/screener?xp=1&vl=100&fd=2&cnt=500&Action=screener"
  min_value: 100000
  lookback_days: 3
  max_results: 100
  request_timeout: 15
  ceo_cfo_titles: [CEO, CFO, Chief Executive, Chief Financial]

alpaca:
  api_key: "YOUR_KEY"
  api_secret: "YOUR_SECRET"
  data_url: "https://data.alpaca.markets/v2"
  bars_limit: 200
  request_timeout: 20

polygon:
  api_key: "YOUR_KEY"
  base_url: "https://api.polygon.io/v2"
  request_timeout: 20

enrich:
  history_days: 200
  velocity_days: 5

gemini:
  api_key: "YOUR_KEY"
  model: "gemini-2.5-flash"
  news_lookback_days: 14
  request_timeout: 30

anthropic:
  api_key: "YOUR_KEY"
  model: "claude-haiku-4-5-20251001"
  request_timeout: 60
```

---

## GitHub Actions — daily.yml

- **Schedule**: `cron: '0 13 * * 1-5'` → 8am ET weekdays (UTC-5 = 13:00 UTC)
- **Trigger**: also supports `workflow_dispatch` (manual trigger from GitHub Actions tab)
- **What it does**: checks out repo → writes `config.yaml` from Secrets → runs `python output.py` → commits `docs/index.html` back to repo
- **Working directory**: `Insider_Tracker/` (the Python package folder)

**5 GitHub Secrets required:**
| Secret Name | Purpose |
|-------------|---------|
| `ALPACA_API_KEY` | Alpaca data API |
| `ALPACA_API_SECRET` | Alpaca data API |
| `POLYGON_API_KEY` | Polygon.io fallback |
| `GEMINI_API_KEY` | Gemini 2.5 Flash (news research) |
| `ANTHROPIC_API_KEY` | Claude Haiku (qualitative scoring) |

GitHub Pages: set to serve from **`docs/` folder on `main` branch**.

---

## Running Locally

```bash
cd Insider_Tracker/Insider_Tracker
pip install -r requirements.txt

# Full pipeline (writes docs/index.html)
python output.py

# Preview dashboard with sample data (no API calls)
python preview.py
# Then open docs/preview.html in browser

# Individual stages
python scrape.py
python tag.py
python enrich.py
python research.py
python score.py
python track.py
```

---

## What's Done / Pending

**All 7 stages complete and tested.** Dashboard design finalized.

**Pending (Task 15 & 16):**
1. Push all files to GitHub repository
2. Enable GitHub Pages → Settings → Pages → Source: `docs/` folder on `main`
3. Add 5 Secrets: Settings → Secrets and variables → Actions
4. Trigger manual `workflow_dispatch` to verify end-to-end pipeline runs and `docs/index.html` is committed

---

## Known Issues / Design Decisions

| Issue | Decision |
|-------|----------|
| OpenInsider shows filing date, dashboard shows trade date | Correct behavior — dashboard labels it "Trade YYYY-MM-DD" |
| Same insider filing could appear in picks across two run_dates | Fixed: UNIQUE INDEX on `picks(ticker, event_date)` + `load_all_picks()` deduplication |
| SVG inside backdrop-filter creates square artifact | Fixed: replaced SVG score ring with CSS conic-gradient + mask |
| Write/Edit tool leaves null bytes in web.py | Fixed: strip + touch after every edit (see quirk section above) |
| Stale .pyc cache | Fixed: preview.py uses importlib force-load; strip + touch fixes production |
| 3D card tilt tried and rejected | Do not re-add — user explicitly rejected it |
| Avg Score in hero stats tried and rejected | Do not re-add |

---

## File Locations (Windows paths)

- Repo root: `C:\Users\jjami\OneDrive\Desktop\Insider_Tracker\`
- Python package: `C:\Users\jjami\OneDrive\Desktop\Insider_Tracker\Insider_Tracker\`
- Dashboard output: `C:\Users\jjami\OneDrive\Desktop\Insider_Tracker\docs\index.html`
- Preview: `C:\Users\jjami\OneDrive\Desktop\Insider_Tracker\docs\preview.html`
- DB: `C:\Users\jjami\OneDrive\Desktop\Insider_Tracker\Insider_Tracker\data\insider_tracker.db`
