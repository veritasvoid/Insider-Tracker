"""
scrape.py — Stage 1
Fetches CEO/CFO open-market purchases from OpenInsider and returns a
clean list of dicts filtered to transactions >= min_value (default $100k).

Usage:
    python scrape.py              # pretty-prints results
    python scrape.py --json       # raw JSON output
"""

import re
import json
import argparse
from datetime import datetime, timedelta

import requests
import yaml
from bs4 import BeautifulSoup


# ── helpers ────────────────────────────────────────────────────────────────────

def parse_dollar(raw: str) -> float:
    """'+$2,490,633' → 2490633.0   |   '$11.37' → 11.37   |   '' → 0.0"""
    cleaned = re.sub(r"[+\-$,\s]", "", raw)
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def parse_qty(raw: str) -> int:
    """+219,005 → 219005"""
    cleaned = re.sub(r"[+,\s]", "", raw)
    try:
        return int(cleaned)
    except ValueError:
        return 0


def parse_pct(raw: str) -> str:
    """Keep delta-own as a plain string (+2%, >999%, New…)."""
    return raw.strip()


def is_ceo_cfo(title: str, keywords: list[str]) -> bool:
    title_upper = title.upper()
    return any(kw.upper() in title_upper for kw in keywords)


def row_is_recent(date_str: str, lookback_days: int) -> bool:
    """Return True if date_str is within the last lookback_days calendar days.
    Counts today as day 1 (matches OpenInsider screener convention):
      lookback_days=1 → today only
      lookback_days=3 → today, yesterday, day-before-yesterday
    """
    try:
        td     = datetime.strptime(date_str, "%Y-%m-%d").date()
        cutoff = (datetime.now() - timedelta(days=lookback_days - 1)).date()
        return td >= cutoff
    except ValueError:
        return True  # if unparseable, keep it


# ── core scraper ───────────────────────────────────────────────────────────────

def fetch_raw_html(url: str, timeout: int) -> str:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        )
    }
    try:
        resp = requests.get(url, headers=headers, timeout=timeout)
        resp.raise_for_status()
        return resp.text
    except requests.exceptions.Timeout:
        raise RuntimeError(f"[scrape] OpenInsider timed out after {timeout}s")
    except requests.exceptions.ConnectionError as e:
        raise RuntimeError(f"[scrape] Could not reach OpenInsider: {e}")
    except requests.exceptions.HTTPError as e:
        raise RuntimeError(f"[scrape] OpenInsider returned HTTP error: {e}")


def parse_table(html: str) -> list[dict]:
    """Parse the insider-trades HTML table into raw row dicts."""
    soup = BeautifulSoup(html, "html.parser")

    # Try known selectors in order of preference
    table = (
        soup.find("table", {"class": "tinytable"})
        or soup.find("table", {"id": "tinytable"})
        or soup.find("table", class_=lambda c: c and "tinytable" in " ".join(c))
    )

    # Fallback: find the data table by most rows, excluding screener/form tables.
    # Form tables have cells with text like "Tickers", "Filing Date", "Insider"
    # in their first row — real data tables have dates and ticker symbols there.
    _FORM_SIGNALS = {"tickers", "insider", "filing date", "trade date", "n days ago"}
    if table is None:
        candidates = []
        for t in soup.find_all("table"):
            all_trs = t.find_all("tr")
            if len(all_trs) < 5:
                continue
            # Sample the first row's text to reject form/filter tables
            first_row_text = {
                c.get_text(strip=True).lower()
                for c in all_trs[0].find_all(["th", "td"])
            }
            if first_row_text & _FORM_SIGNALS:
                continue   # screener form table — skip
            cols = len(all_trs[0].find_all(["th", "td"]))
            if cols >= 5:
                candidates.append((len(all_trs), cols, t))
        if candidates:
            candidates.sort(reverse=True)   # most rows wins
            table = candidates[0][2]
            print(f"[scrape] 'tinytable' class not found — using fallback "
                  f"({candidates[0][1]} cols, {candidates[0][0]} rows)")

    if table is None:
        all_tables = soup.find_all("table")
        debug = "\n".join(
            f"  [{i}] class={t.get('class')} id={t.get('id')} "
            f"cols={len((t.find_all('tr') or [{}])[0].find_all(['th','td']))} "
            f"rows={len(t.find_all('tr'))}"
            for i, t in enumerate(all_tables)
        )
        raise ValueError(
            f"Could not find data table on page. {len(all_tables)} table(s):\n{debug}\n"
            "Run debug_scrape.py to inspect the live HTML."
        )

    # tbody is optional — some OpenInsider pages omit it
    rows = []
    tbody = table.find("tbody")
    row_container = tbody if tbody else table
    all_trs = row_container.find_all("tr")

    for tr in all_trs:
        cells = tr.find_all("td")
        if len(cells) < 13:
            continue  # skip header rows and malformed rows

        # Column order on /latest-ceo-cfo-purchases-25k:
        # 0:flags  1:filing_date  2:trade_date  3:ticker  4:company
        # 5:insider_name  6:title  7:trade_type  8:price  9:qty
        # 10:owned  11:delta_own  12:value  13+:1d/1w/1m/6m (optional)
        rows.append({
            "filing_date":  cells[1].get_text(strip=True),
            "trade_date":   cells[2].get_text(strip=True),
            "ticker":       cells[3].get_text(strip=True),
            "company":      cells[4].get_text(strip=True),
            "insider_name": cells[5].get_text(strip=True),
            "title":        cells[6].get_text(strip=True),
            "trade_type":   cells[7].get_text(strip=True),
            "price":        cells[8].get_text(strip=True),
            "qty":          cells[9].get_text(strip=True),
            "owned":        cells[10].get_text(strip=True),
            "delta_own":    cells[11].get_text(strip=True),
            "value":        cells[12].get_text(strip=True),
        })
    return rows


def clean_and_filter(raw_rows: list[dict], cfg: dict, skip_date: bool = False) -> list[dict]:
    """
    Convert raw strings → typed values and apply filters:
      - trade_type must be P - Purchase
      - title must contain CEO or CFO keyword
      - value >= min_value
      - filing_date within lookback_days  (skipped when skip_date=True)
    """
    min_value    = cfg["min_value"]
    lookback     = cfg["lookback_days"]
    ceo_cfo_kws  = cfg["ceo_cfo_titles"]
    results = []

    for r in raw_rows:
        # ── filters ──
        if "P - Purchase" not in r["trade_type"]:
            continue
        if not is_ceo_cfo(r["title"], ceo_cfo_kws):
            continue

        value = parse_dollar(r["value"])
        if value < min_value:
            continue

        if not skip_date and not row_is_recent(r["filing_date"][:10], lookback):
            continue

        # ── typed record ──
        results.append({
            "filing_date":  r["filing_date"],
            "trade_date":   r["trade_date"],
            "ticker":       r["ticker"].upper(),
            "company":      r["company"],
            "insider_name": r["insider_name"],
            "title":        r["title"],
            "price":        parse_dollar(r["price"]),
            "qty":          parse_qty(r["qty"]),
            "owned":        parse_qty(r["owned"]),
            "delta_own":    parse_pct(r["delta_own"]),
            "value":        value,
            "trade_type":   r["trade_type"],
        })

    return results


# ── public entry point ─────────────────────────────────────────────────────────

def run(config_path: str = "config.yaml") -> list[dict]:
    """
    Main pipeline entry for Stage 1.
    Returns a list of clean, filtered insider-buy dicts.
    """
    with open(config_path, "r") as f:
        cfg = yaml.safe_load(f)["scrape"]

    print(f"[scrape] Fetching {cfg['url']} …")
    html = fetch_raw_html(cfg["url"], cfg["request_timeout"])

    raw_rows = parse_table(html)
    print(f"[scrape] Parsed {len(raw_rows)} raw rows from table.")

    results = clean_and_filter(raw_rows, cfg)
    results = results[: cfg["max_results"]]

    print(f"[scrape] {len(results)} rows passed filters "
          f"(CEO/CFO, P-type, ≥${cfg['min_value']:,}, filed last {cfg['lookback_days']}d).")
    return results


def run_history(config_path: str = "config.yaml") -> list[dict]:
    """
    Fetch ALL CEO/CFO purchases ≥$100k from the page with NO date filter.
    Returns every qualifying row available (typically ~100 rows spanning several weeks).

    Used by tag.py to seed the DB with enough history for accurate cluster detection,
    independent of how many days the pipeline has been running.
    """
    with open(config_path, "r") as f:
        cfg = yaml.safe_load(f)["scrape"]

    html     = fetch_raw_html(cfg["url"], cfg["request_timeout"])
    raw_rows = parse_table(html)
    results  = clean_and_filter(raw_rows, cfg, skip_date=True)
    results  = results[: cfg["max_results"]]
    print(f"[scrape] run_history: {len(results)} rows (no date filter) fetched for DB seeding.")
    return results


# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Scrape OpenInsider CEO/CFO purchases.")
    parser.add_argument("--json", action="store_true", help="Output raw JSON")
    parser.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    args = parser.parse_args()

    buys = run(config_path=args.config)

    if args.json:
        print(json.dumps(buys, indent=2))
    else:
        if not buys:
            print("No results matched the filters.")
        else:
            print(f"\n{'─'*80}")
            for b in buys:
                print(
                    f"{b['trade_date']}  {b['ticker']:<6}  {b['company'][:35]:<35}  "
                    f"{b['title'][:20]:<20}  {b['insider_name'][:25]:<25}  "
                    f"${b['value']:>12,.0f}  ΔOwn: {b['delta_own']}"
                )
            print(f"{'─'*80}")
            print(f"Total: {len(buys)} buys")
