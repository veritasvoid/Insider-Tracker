# Insider Tracker — Complete Project Handoff

**Last updated:** 2026-06-15  
**Dashboard live at:** https://veritasvoid.github.io/Insider-Tracker/  
**GitHub repo:** https://github.com/veritasvoid/Insider-Tracker  
**GitHub user:** veritasvoid  
**Local folder:** `C:\Users\jjami\OneDrive\Desktop\Insider_Tracker\`

---

## What This Project Is

A Bloomberg-style dashboard that automatically:
1. Scrapes CEO/CFO insider buy filings from OpenInsider every weekday at 10:30 AM ET
2. Collapses and tags buys as CLUSTER (2+ different insiders), REPEAT (same insider 3+ times), or SINGLE
3. Enriches with price/technical data (Alpaca + Polygon APIs)
4. Runs AI analysis via Claude Haiku to score catalysts, sentiment, fundamentals
5. Scores each pick 0-100 across 7 dimensions
6. Generates a Bloomberg-grade HTML dashboard published to GitHub Pages
7. Tracks forward performance at 3d / 8d / 15d / 30d / 90d

---

## File Structure

```
C:\Users\jjami\OneDrive\Desktop\Insider_Tracker\
│
├── HANDOFF.md                          <- this file
├── SETUP_GITHUB.md                     <- initial GitHub setup guide
├── requirements.txt                    <- top-level pip requirements
├── .gitignore
│
├── .github\workflows\
│   ├── daily_run.yml                   <- ACTIVE workflow (use this one)
│   └── daily.yml                       <- older duplicate, ignore
│
├── docs\
│   ├── index.html                      <- LIVE dashboard (GitHub Pages serves this)
│   ├── preview.html                    <- static design preview
│   └── preview_test.html               <- ignore
│
└── Insider_Tracker\                    <- Python package root (all scripts run from here)
    ├── main.py                         <- ENTRY POINT — runs all 7 stages
    ├── scrape.py                       <- Stage 1: OpenInsider scraper
    ├── tag.py                          <- Stage 2: collapse buys + tag CLUSTER/REPEAT/SINGLE
    ├── enrich.py                       <- Stage 3: price, EMA, short float via Alpaca/Polygon
    ├── research.py                     <- Stage 4: Claude AI news analysis + catalysts
    ├── score.py                        <- Stage 5: 0-100 composite scoring
    ├── output.py                       <- Stage 6: JSON/MD reports + HTML + DB save
    ├── track.py                        <- Stage 7: forward performance price fills
    ├── web.py                          <- HTML generator (ALL dashboard CSS + JS lives here)
    ├── db.py                           <- SQLite layer (all DB reads/writes)
    ├── config.template.yaml            <- template (copy -> config.yaml, fill secrets)
    ├── config.yaml                     <- NEVER committed (has real API keys)
    └── data\
        ├── insider_tracker.db          <- SQLite DB (committed to repo after each run)
        └── reports\                    <- JSON + MD reports per run (gitignored)
```

---

## Pipeline Architecture (7 Stages)

```
main.py calls output.run() which chains:

Stage 1  scrape.py    OpenInsider HTML scrape (CEO/CFO buys >=100K, lookback 3 days)
Stage 2  tag.py       DB insert -> collapse same-insider buys (5-day window) -> CLUSTER/REPEAT/SINGLE
Stage 3  enrich.py    Alpaca bars (EMA50/200, velocity) + Polygon short float + current price
Stage 4  research.py  Claude Haiku: headline, summary, catalysts, sentiment
Stage 5  score.py     Composite 0-100 score -> stars 1-5 -> label (STRONG BUY / BUY / WATCH / WEAK / SKIP)
Stage 6  output.py    Write JSON report, MD briefing, call db.save_picks(), call web.write_html()
Stage 7  track.py     update_picks() fills price_3d/8d/15d/30d/90d via Polygon as days pass
```

### Stage 6 internal flow (output.py):
1. `db.save_picks(scored_events)` — upserts ONE row per ticker in picks table, sets `last_updated = today`, stores raw purchases JSON blob
2. `track.update_picks(cfg)` — fills forward price columns for mature picks
3. `track.load_all_picks()` — returns all picks rows for history tab
4. `web.write_html(events, all_picks)` — generates docs/index.html

---

## Database Schema (insider_tracker.db)

**`insider_buys`** — raw individual filings (pruned to 90 days rolling)
- `trade_date, ticker, company, insider_name, title, price, qty, owned, delta_own, value`
- UNIQUE on `(trade_date, ticker, insider_name, value)`

**`buy_events`** — collapsed events from Stage 2
- `run_date, ticker, insider_name, event_start_date, event_end_date, total_value, cluster_tag, distinct_buyers_30d`

**`picks`** — scored picks, ONE ROW PER TICKER (upserted each detection)
- `ticker, company, cluster_tag, distinct_buyers, total_value`
- `score, score_stars, score_label, score_breakdown (JSON), score_key_risk`
- `first_seen` — date ticker was first detected
- `last_updated` — date of most recent pipeline run that detected this ticker
- `purchases` — JSON array of raw buy rows from insider_buys (for History tab drill-down)
- `price_at_pick` — market price at first detection
- `price_3d / price_8d / price_15d / price_30d / price_90d` — filled by track.py
- `news_headline, news_sentiment, catalysts (JSON)`

---

## Scoring System (score.py)

| Dimension | Max | Source |
|-----------|-----|--------|
| D1 Insider Signal (value, delta_own, title) | 25 | Quant |
| D2 Price/Technical (vs EMA50/200, velocity) | 20 | Quant |
| D3 Short Interest (short float %) | 10 | Quant |
| D4 Cluster Bonus (CLUSTER/REPEAT tag) | 10 | Quant |
| D5 Fundamental Quality | 12 | Claude AI |
| D6 Catalyst Strength | 13 | Claude AI |
| D7 News/Sentiment Fit | 10 | Claude AI |
| TOTAL | 100 | |

Star thresholds: 80-100 = STRONG BUY, 60-79 = BUY, 40-59 = WATCH, 20-39 = WEAK, 0-19 = SKIP

---

## Tagging Logic (tag.py)

- `COLLAPSE_WINDOW_DAYS = 5` — same insider buys within 5 days merge into one event
- `HISTORY_DAYS = 90` — look back 90 days for cluster detection
- `CLUSTER_THRESHOLD = 2` — 2+ distinct insiders in 90d = CLUSTER
- `REPEAT_THRESHOLD = 3` — same insider 3+ filings = REPEAT
- Priority: CLUSTER > REPEAT > SINGLE

---

## Dashboard (web.py) — Key Layout Details

`web.py` (~847 lines) generates ALL HTML inline. No separate CSS/JS files.

### Today's Picks Tab
- Cards rendered by `_card(e, rank)`
- 3-band layout: left (ticker/meta/price), center (score ring + cluster badge + technicals), right (news/catalysts)
- Score ring = CSS conic-gradient via `data-score` attribute, animated by JS at bottom of file

### History Tab
- `_history(picks)` — sorts by `last_updated` DESC, then `score` DESC
- TODAY chip shows when `last_updated == today`
- DUAL BADGE logic: CLUSTER shows both "CLUSTER x insiders" AND "x buys" when n_buys > unique_insiders
  - Computes unique_ins from the purchases JSON at render time (not from DB field)
- Layout (FIXED 2026-06-15): flex rows
  - `.hrl-l` fixed 290px (ticker + badges)
  - `.hrow-meta` flex:1 (total value + date)
  - `.hrow-rets` = 5-column sub-grid `repeat(5, 64px)` pushed right
  - This eliminates the large blank space that `1fr` caused on wide screens
- Buy drill-down rows: `.hrow-buys` div contains `.buy-hdr` + `.buy-row` (7-col grid)

### CSS grid constants for History (as of current version):
```css
.hist-hdr { display:flex; align-items:center; gap:14px; }
.hist-hdr-l { flex:0 0 290px }
.hist-hdr-m { flex:1; min-width:0 }
.hist-hdr-r { display:grid; grid-template-columns:repeat(5,64px); gap:0 8px; text-align:center }

.hrow-hdr { display:flex; align-items:center; gap:14px; }
.hrl-l    { flex:0 0 290px; display:flex; align-items:center; gap:8px; flex-wrap:wrap }
.hrow-meta { flex:1; ... }
.hrow-rets { display:grid; grid-template-columns:repeat(5,64px); gap:0 8px }
```

### Performance Tab
- `_perf(picks)` — summary table of all tracked picks with realized return %

---

## GitHub Actions Workflow

File: `.github/workflows/daily_run.yml`

- **Schedule:** `cron: '30 14 * * 1-5'` = 10:30 AM ET Mon-Fri / 7:30 AM Arizona
- **Manual trigger:** GitHub.com -> Actions -> "Daily Insider Tracker" -> "Run workflow"
- **Python version:** 3.11
- **Steps:** checkout -> pip install -r requirements.txt -> write config.yaml from secrets -> `python main.py` -> `git add docs/index.html Insider_Tracker/data/insider_tracker.db` -> commit -> push
- **Commits back:** only `docs/index.html` and `insider_tracker.db` (config.yaml never committed)

### GitHub Secrets Required
Settings at: https://github.com/veritasvoid/Insider-Tracker/settings/secrets/actions

| Secret | Purpose |
|--------|---------|
| ALPACA_API_KEY | Alpaca Markets API key (price bars / EMA) |
| ALPACA_API_SECRET | Alpaca Markets secret |
| POLYGON_API_KEY | Polygon.io API key (short float + forward price tracking) |
| ANTHROPIC_API_KEY | Claude API key (AI scoring D5/D6/D7) |
| GITHUB_TOKEN | Auto-provided by GitHub Actions |

---

## Local Development

### Run full pipeline:
```powershell
cd C:\Users\jjami\OneDrive\Desktop\Insider_Tracker\Insider_Tracker
python main.py
```

### Regenerate HTML from existing DB (no API calls):
```python
# Run from Insider_Tracker/ directory
from track import load_all_picks
from web import write_html
all_picks = load_all_picks()
write_html([], all_picks)   # empty events = no Today's Picks card, but History works
```

### Config file:
`Insider_Tracker/config.yaml` — NOT committed to git. Copy from `config.template.yaml` and fill in real API keys.

---

## Git Workflow

### Normal push:
```powershell
cd C:\Users\jjami\OneDrive\Desktop\Insider_Tracker
git add Insider_Tracker/web.py
git commit -m "fix: description"
git push
```

### If push rejected (workflow committed ahead of you):
```powershell
git pull --rebase
git push
```

If rebase conflict on docs/index.html:
```powershell
git pull --rebase
git checkout --theirs docs/index.html
git add docs/index.html
git rebase --continue
# if vim opens for commit message: type :wq and press Enter
git push
```

### CRITICAL — CRLF warning:
The repo was initialized on Windows. The GitHub Actions runner uses Linux LF endings.
If Claude's sandbox edits Python files via bash, it may mix CRLF/LF, which causes git Edit tool
mismatches and can truncate files. ALWAYS use Python `open(path, "wb").write(content.encode("utf-8"))`
for any file writes from the Linux sandbox. NEVER use bash `cat >>` or `echo >>` on existing Python files.

---

## Known Issues & Fixes Applied

### Blank space in History tab (FIXED 2026-06-15)
- Was: 7-column CSS grid with `1fr` summary column stretched to full width on wide screens
- Fix: changed to flex layout with `.hrow-rets` as a fixed 5x64px sub-grid

### SMMT dual badge (FIXED)
- Was: n_buys > distinct_buyers check used the stored DB integer which could equal n_buys
- Fix: compute `unique_ins = len({pur.get("insider_name","") for pur in purchases})` from raw purchases JSON at render time

### History sort (partially fixed, root cause in DB)
- Symptom: ticker shows old date in History even if it appeared in Today's Picks today
- Root cause: `last_updated` field in picks table only updates when `save_picks()` is called, which only runs if the pipeline finds that ticker as a fresh buy
- Investigation: check `db.save_picks()` print output in GitHub Actions logs; confirm `last_updated` is set to today for each ticker in today's run

### web.py truncation (FIXED — historical)
- Caused by Edit tool operating on CRLF file with LF search string
- The `JS = """` triple-quoted string (around line 690-700) is the most vulnerable point
- If it gets truncated, py_compile will give SyntaxError: EOF in multi-line string
- Fix: use Python open(wb) to write entire file at once rather than line-level edits

### Git index corruption (FIXED — historical)
- Caused by Windows NTFS + Linux FUSE mount + multiple concurrent git processes
- If it happens: `GIT_INDEX_FILE=/tmp/new git read-tree HEAD` then
  `python3 -c "open('.git/index','wb').write(open('/tmp/new','rb').read())"`

---

## Data Flow Diagram

```
OpenInsider (HTML scrape)
    |
scrape.py -> list of raw buy dicts
    |
tag.py -> insert_buys() -> DB [insider_buys]
       -> tag_ticker() -> collapse per insider (5d window)
       -> CLUSTER / REPEAT / SINGLE tag
       -> buy_events[] with cluster_tag, insider_count
    |
enrich.py -> add current_price, EMA50/200, velocity, short_float_pct
    |
research.py -> Claude Haiku -> add news_headline, catalysts, news_sentiment
    |
score.py -> D1-D7 scores -> score_total (0-100) -> score_stars (1-5)
    |
output.py -> write_json() -> data/reports/YYYY-MM-DD_HH-MM.json
          -> write_markdown() -> data/reports/YYYY-MM-DD_HH-MM.md
          -> db.save_picks() -> DB [picks] upsert (purchases JSON, last_updated=today)
          -> track.update_picks() -> fills price_3d/8d/15d/30d/90d
          -> track.load_all_picks() -> all_picks[]
          -> web.write_html(events, all_picks) -> docs/index.html
    |
GitHub Actions -> git commit docs/index.html + data/insider_tracker.db -> push
    |
GitHub Pages -> https://veritasvoid.github.io/Insider-Tracker/
```

---

## Key Function Reference

| Function | File | What it does |
|----------|------|-------------|
| `main()` | main.py | Entry point, calls output.run() |
| `run(config_path)` | output.py | Runs all 7 stages, returns (json_path, md_path) |
| `tag_ticker(ticker, run_date)` | tag.py | Returns buy events for one ticker with cluster tag |
| `save_picks(scored_events)` | db.py | Upserts picks — ONE row per ticker, sets last_updated=today |
| `load_all_picks()` | track.py | Returns all picks rows for History tab |
| `update_picks(cfg)` | track.py | Fills price_3d/8d/15d/30d/90d for mature picks |
| `write_html(events, all_picks)` | web.py | Generates docs/index.html |
| `_history(picks)` | web.py | Renders History tab HTML (sorted by last_updated DESC) |
| `_card(e, rank)` | web.py | Renders one Today's Picks card |
| `_ring(score, stars)` | web.py | CSS conic-gradient score ring HTML |
| `_ret(base, price)` | web.py | Calculates % return, returns (float, str) |

---

## Pending Work / Suggested Next Steps

1. **Push the pending merge commit** — run `git push` from the local folder. Commit `e3f5f0b` (history flex layout fix) has been built locally but not pushed yet.

2. **Verify last_updated freshness** — check GitHub Actions logs for `[db] save_picks:` line to confirm each run's tickers are getting `last_updated` set to today. If they aren't, the History tab will show stale dates.

3. **Short float reliability** — currently via Polygon. `debug_finviz.py` exists as a Finviz-based alternative if Polygon short float data is patchy.

4. **Lookback on Mondays** — `scrape.lookback_days: 3` means Monday runs may miss some Friday/Saturday filings. Consider bumping to 5 on Monday runs.

5. **Dead CSS cleanup** — `.hrl-r{display:none}` in the @media block references a removed element; safe to delete.

6. **Alert emails** — consider adding a GitHub Actions step to email a briefing when top score >= 60.
