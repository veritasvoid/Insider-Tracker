"""
tag.py — Stage 2
1. Persists today's fresh buys (from stage 1) into SQLite
2. For each fresh ticker, pulls 30-day history from DB
3. Collapses same-insider buys within a 10-day window → one buy event per insider
4. Tags each ticker: CLUSTER (2+ distinct insiders in 30d) or SINGLE
5. Returns list of collapsed buy events with cluster tag

Usage:
    python tag.py              # reads stage 1 output, prints tagged events
    python tag.py --json       # raw JSON
"""

import json
import argparse
from datetime import datetime, date

import db
from scrape import run as scrape_run, run_history as scrape_history


COLLAPSE_WINDOW_DAYS = 5    # same-insider buys within this window → one event
HISTORY_DAYS         = 90   # look back this many days for cluster detection
CLUSTER_THRESHOLD    = 2    # distinct insiders needed to tag CLUSTER
REPEAT_THRESHOLD     = 3    # same insider buying this many times → REPEAT conviction


# ── collapse logic ─────────────────────────────────────────────────────────────

def parse_date(s: str) -> date:
    return datetime.strptime(s[:10], "%Y-%m-%d").date()


def collapse_insider_buys(buys: list[dict]) -> list[dict]:
    """
    Given all buys for ONE insider on ONE ticker (sorted by trade_date),
    collapse consecutive buys within COLLAPSE_WINDOW_DAYS into a single event.
    Returns list of collapsed event dicts.
    """
    if not buys:
        return []

    sorted_buys = sorted(buys, key=lambda b: b["trade_date"])
    events = []
    current = [sorted_buys[0]]

    for buy in sorted_buys[1:]:
        window_start = parse_date(current[0]["trade_date"])
        this_date    = parse_date(buy["trade_date"])
        if (this_date - window_start).days <= COLLAPSE_WINDOW_DAYS:
            current.append(buy)
        else:
            events.append(_merge(current))
            current = [buy]

    events.append(_merge(current))
    return events


def _merge(buys: list[dict]) -> dict:
    """Merge a list of buys for the same insider into one event dict."""
    total_value = sum(b["value"] for b in buys)
    total_qty   = sum(b["qty"] or 0 for b in buys)
    prices      = [b["price"] for b in buys if b.get("price")]
    avg_price   = round(sum(prices) / len(prices), 4) if prices else None

    return {
        "ticker":           buys[0]["ticker"],
        "company":          buys[0]["company"],
        "insider_name":     buys[0]["insider_name"],
        "title":            buys[0]["title"],
        "event_start_date": buys[0]["trade_date"][:10],
        "event_end_date":   buys[-1]["trade_date"][:10],
        "total_value":      total_value,
        "total_qty":        total_qty,
        "avg_price":        avg_price,
        "delta_own":        buys[-1]["delta_own"],  # most recent
        "n_filings":        len(buys),
    }


# ── main tagging logic ─────────────────────────────────────────────────────────

def tag_ticker(ticker: str, run_date: str) -> list[dict]:
    """
    Pull 30d history for ticker, collapse per insider, tag CLUSTER/SINGLE.
    Returns one dict per distinct insider buy event.
    """
    history = db.get_history(ticker, days=HISTORY_DAYS)
    if not history:
        return []

    # Group by insider
    by_insider: dict[str, list[dict]] = {}
    for row in history:
        by_insider.setdefault(row["insider_name"], []).append(row)

    # Collapse each insider's buys
    all_events: list[dict] = []
    for insider_buys in by_insider.values():
        all_events.extend(collapse_insider_buys(insider_buys))

    distinct_buyers  = len({e["insider_name"] for e in all_events})
    max_filings      = max((e.get("n_filings", 1) for e in all_events), default=1)

    if distinct_buyers >= CLUSTER_THRESHOLD:
        cluster_tag = "CLUSTER"
    elif max_filings >= REPEAT_THRESHOLD:
        cluster_tag = "REPEAT"
    else:
        cluster_tag = "SINGLE"

    for e in all_events:
        e["cluster_tag"]         = cluster_tag
        e["distinct_buyers_30d"] = distinct_buyers
        e["insider_count"]       = distinct_buyers   # used by score.py D4 + web.py display
        e["run_date"]            = run_date

    return all_events


def run(fresh_buys: list[dict] | None = None, config_path: str = "config.yaml") -> list[dict]:
    """
    Stage 2 entry point.
    fresh_buys: output from stage 1 (if None, stage 1 runs automatically).
    Returns tagged buy events for today's fresh tickers only.
    """
    db.init_db()

    # Seed DB with the full page history (no date filter) so cluster detection
    # reflects the true 30-day picture from OpenInsider, not just what's been
    # accumulated in the DB from daily 3-day filing windows.
    history_rows = scrape_history(config_path=config_path)
    db.insert_buys(history_rows)
    print(f"[tag] Seeded DB with {len(history_rows)} history rows "
          f"(duplicates silently skipped).")

    if fresh_buys is None:
        fresh_buys = scrape_run(config_path=config_path)

    if not fresh_buys:
        print("[tag] No fresh buys from stage 1.")
        return []

    inserted = db.insert_buys(fresh_buys)
    print(f"[tag] Inserted {inserted} new rows into insider_buys "
          f"({len(fresh_buys) - inserted} duplicates skipped).")

    today_tickers = list({b["ticker"] for b in fresh_buys})
    run_date      = date.today().isoformat()

    print(f"[tag] Tagging {len(today_tickers)} tickers using {HISTORY_DAYS}d history…")

    all_events: list[dict] = []
    for ticker in sorted(today_tickers):
        all_events.extend(tag_ticker(ticker, run_date))

    db.insert_buy_events(all_events)
    db.prune_old_buys(days=90)

    clusters = sum(1 for e in all_events if e["cluster_tag"] == "CLUSTER")
    singles  = len(all_events) - clusters
    print(f"[tag] {len(all_events)} buy events — {clusters} CLUSTER, {singles} SINGLE.")

    return all_events


# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Stage 2: collapse + tag insider buys.")
    parser.add_argument("--json",   action="store_true", help="Output raw JSON")
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()

    events = run(config_path=args.config)

    if args.json:
        print(json.dumps(events, indent=2, default=str))
    else:
        if not events:
            print("No events to display.")
        else:
            print(f"\n{'─'*100}")
            for e in sorted(events, key=lambda x: (x["cluster_tag"] != "CLUSTER", -x["total_value"])):
                tag    = f"[{e['cluster_tag']}]"
                buyers = f"{e['distinct_buyers_30d']} buyer{'s' if e['distinct_buyers_30d'] > 1 else ''} in 30d"
                print(
                    f"{e['event_start_date']}  {e['ticker']:<6}  {tag:<9}  "
                    f"{buyers:<18}  {e['insider_name'][:28]:<28}  "
                    f"{e['title'][:22]:<22}  ${e['total_value']:>12,.0f}  "
                    f"ΔOwn: {e['delta_own']}"
                )
            print(f"{'─'*100}")
            clusters = sum(1 for e in events if e["cluster_tag"] == "CLUSTER")
            print(f"Total: {len(events)} events  |  CLUSTER tickers: {clusters}")
