"""
track.py — Stage 7
Forward performance tracker. Checks picks saved by output.py and fills in
price_3d / price_8d / price_15d / price_30d / price_90d as calendar time passes.

Price source: Polygon (same fallback logic as enrich.py).
Returns are calculated vs price_at_pick (market price when we ran the pipeline),
NOT vs the insider's buy price.

Usage:
    python track.py           # update all stale picks, print performance table
    python track.py --all     # show all tracked picks including complete ones
    python track.py --json    # raw JSON output
"""

import json
import argparse
from datetime import datetime, timedelta, date
from typing import Optional

import requests
import yaml

import db


# ── Price fetching (Polygon) ───────────────────────────────────────────────────

def _fetch_price_on_or_after(ticker: str, target_date: date, cfg: dict) -> Optional[float]:
    """
    Fetch the closing price for ticker on target_date or the next trading day
    within a ±5 day window. Returns None if unavailable.
    Uses Polygon /v2/aggs endpoint.
    """
    # Look in a ±5-day window to handle weekends/holidays
    start = (target_date - timedelta(days=1)).strftime("%Y-%m-%d")
    end   = (target_date + timedelta(days=5)).strftime("%Y-%m-%d")
    url   = (
        f"{cfg['base_url']}/aggs/ticker/{ticker}/range/1/day/{start}/{end}"
    )
    params = {
        "adjusted": "true",
        "sort":     "asc",
        "limit":    10,
        "apiKey":   cfg["api_key"],
    }
    try:
        resp = requests.get(url, params=params, timeout=cfg["request_timeout"])
        if not resp.ok:
            return None
        results = resp.json().get("results") or []
        if not results:
            return None
        # Find the first bar on or after target_date
        for bar in results:
            bar_date = datetime.utcfromtimestamp(bar["t"] / 1000).date()
            if bar_date >= target_date:
                return round(bar["c"], 4)
    except Exception:
        pass
    return None


# ── Return calculation ─────────────────────────────────────────────────────────

def _pct_return(base: float, current: Optional[float]) -> Optional[float]:
    if current is None or base == 0:
        return None
    return round((current - base) / base * 100, 2)


# ── Update stale picks ─────────────────────────────────────────────────────────

INTERVALS = {
    "price_3d":  3,
    "price_8d":  8,
    "price_15d": 15,
    "price_30d": 30,
    "price_90d": 90,
}


def update_picks(cfg: dict) -> list[dict]:
    """
    For every pick with NULL forward prices that are now due, fetch the price
    from Polygon and write it back to the DB.
    Returns list of updated pick dicts with return calculations.
    """
    polygon_cfg = cfg["polygon"]
    stale       = db.get_picks_for_tracking()

    if not stale:
        print("[track] No picks require price updates.")
        return []

    print(f"[track] Updating {len(stale)} pick(s)…")
    updated = []

    for pick in stale:
        pick_date  = datetime.strptime(pick["run_date"], "%Y-%m-%d").date()
        base_price = pick["price_at_pick"]
        ticker     = pick["ticker"]
        new_prices = {}

        for col, days in INTERVALS.items():
            if pick[col] is not None:
                continue                                    # already filled
            target = pick_date + timedelta(days=days)
            if date.today() < target:
                continue                                    # too early
            price = _fetch_price_on_or_after(ticker, target, polygon_cfg)
            if price is not None:
                new_prices[col] = price
                print(f"[track]   {ticker} {col}: ${price:.2f} "
                      f"({_pct_return(base_price, price):+.1f}%)")

        if new_prices:
            db.update_pick_prices(pick["id"], **new_prices)
            updated.append({**pick, **new_prices})
        else:
            updated.append(pick)

    return updated


# ── Load all picks for display ─────────────────────────────────────────────────

def load_all_picks() -> list[dict]:
    """Return all picks (one per ticker) sorted by first_seen desc, score desc."""
    with db.get_conn() as conn:
        rows = conn.execute("""
            SELECT id,
                   COALESCE(first_seen, run_date) AS run_date,
                   first_seen, last_updated,
                   ticker, company, cluster_tag, distinct_buyers,
                   score, score_stars, score_label, score_key_risk,
                   news_headline, news_sentiment,
                   buy_price, price_at_pick, purchases, total_value,
                   price_3d, price_8d, price_15d, price_30d, price_90d
            FROM picks
            ORDER BY COALESCE(first_seen, run_date) DESC, score DESC
        """).fetchall()
    return [dict(r) for r in rows]


# ── Display ────────────────────────────────────────────────────────────────────

STARS = {5: "★★★★★", 4: "★★★★☆", 3: "★★★☆☆", 2: "★★☆☆☆", 1: "★☆☆☆☆"}

def _ret_str(base: float, price: Optional[float]) -> str:
    r = _pct_return(base, price)
    if r is None:
        return "   —   "
    sign = "▲" if r > 0 else "▼"
    return f"{sign}{abs(r):5.1f}%"


def print_performance(picks: list[dict]) -> None:
    if not picks:
        print("[track] No picks in database yet.")
        return

    print(f"\n{'═'*110}")
    print(f"  {'INSIDER TRACKER — PERFORMANCE HISTORY':^108}")
    print(f"{'═'*110}")
    print(f"  {'Date':<12} {'Ticker':<7} {'Stars':<7} {'Score':>5}  "
          f"{'BuyPx':>7} {'PickPx':>7}  "
          f"{'3d':>8} {'8d':>8} {'15d':>8} {'30d':>8} {'90d':>8}  Signal")
    print(f"  {'─'*106}")

    for p in picks:
        base   = p["price_at_pick"] or 0
        stars  = STARS.get(p.get("score_stars") or 3, "★★★☆☆")
        score  = p.get("score") or 0
        buy_px = f"${p['buy_price']:.2f}" if p.get("buy_price") else "N/A"
        now_px = f"${base:.2f}" if base else "N/A"
        tag    = p.get("cluster_tag") or "SINGLE"

        print(
            f"  {p['run_date']:<12} {p['ticker']:<7} {stars:<7} {score:>5}  "
            f"{buy_px:>7} {now_px:>7}  "
            f"{_ret_str(base, p['price_3d']):>8} "
            f"{_ret_str(base, p['price_8d']):>8} "
            f"{_ret_str(base, p['price_15d']):>8} "
            f"{_ret_str(base, p['price_30d']):>8} "
            f"{_ret_str(base, p['price_90d']):>8}  "
            f"{tag}"
        )

    print(f"  {'─'*106}")

    # Summary: average return by interval for completed picks
    for col, label in [("price_3d","3d"), ("price_8d","8d"),
                       ("price_15d","15d"), ("price_30d","30d"), ("price_90d","90d")]:
        rets = [_pct_return(p["price_at_pick"], p[col])
                for p in picks if p.get("price_at_pick") and p.get(col)]
        if rets:
            avg = sum(rets) / len(rets)
            wins = sum(1 for r in rets if r > 0)
            print(f"  {label} avg: {avg:+.1f}%  win rate: {wins}/{len(rets)}")

    print(f"{'═'*110}\n")


# ── Public entry point ─────────────────────────────────────────────────────────

def run(config_path: str = "config.yaml") -> list[dict]:
    with open(config_path) as f:
        cfg = yaml.safe_load(f)

    update_picks(cfg)
    return load_all_picks()


# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Stage 7: Track forward performance of picks.")
    parser.add_argument("--all",    action="store_true", help="Show all picks (default: score >= 40)")
    parser.add_argument("--json",   action="store_true", help="Raw JSON output")
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()

    db.init_db()
    picks = run(config_path=args.config)

    if not args.all:
        picks = [p for p in picks if (p.get("score") or 0) >= 40]

    if args.json:
        print(json.dumps(picks, indent=2, default=str))
    else:
        print_performance(picks)
