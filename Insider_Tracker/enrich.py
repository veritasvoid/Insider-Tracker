"""
enrich.py — Stage 3
Enriches each buy event (from stage 2) with price context.

Data sources (in order of preference):
  1. Alpaca IEX feed  — free tier, covers most liquid US stocks
  2. Polygon.io       — fallback for tickers not on IEX (small-caps, ADRs)
  3. Finviz           — short float %

Per ticker adds:
  current_price       — latest closing price (market close)
  ema_50              — 50-day exponential moving average
  ema_200             — 200-day exponential moving average
  price_vs_ema50_pct  — % above/below 50 EMA  (+ = above)
  price_vs_ema200_pct — % above/below 200 EMA
  price_velocity_5d   — 5-day price change %
  short_float_pct     — short interest as % of float (from Finviz)
  data_source         — "alpaca" or "polygon"
  enrich_error        — error string if price data unavailable, else None

Usage:
    python enrich.py          # runs stages 1-3, prints enriched events
    python enrich.py --json   # raw JSON output
"""

import json
import argparse
from datetime import datetime, timedelta
from typing import Optional

import requests
import yaml
from bs4 import BeautifulSoup

from tag import run as tag_run


# ── Alpaca ─────────────────────────────────────────────────────────────────────

def _alpaca_headers(cfg: dict) -> dict:
    return {
        "APCA-API-KEY-ID":     cfg["api_key"],
        "APCA-API-SECRET-KEY": cfg["api_secret"],
        "Accept":              "application/json",
    }


def fetch_alpaca_batch(tickers: list[str], cfg: dict) -> dict[str, list[float]]:
    """Batch-fetch daily closes from Alpaca IEX. Returns {ticker: [closes asc]}."""
    if not tickers:
        return {}

    start = (datetime.now() - timedelta(days=300)).strftime("%Y-%m-%dT00:00:00Z")
    params = {
        "symbols":   ",".join(tickers),
        "timeframe": "1Day",
        "start":     start,
        "limit":     cfg["bars_limit"],
        "sort":      "asc",
        "feed":      "iex",
    }
    try:
        resp = requests.get(
            f"{cfg['data_url']}/stocks/bars",
            headers=_alpaca_headers(cfg),
            params=params,
            timeout=cfg["request_timeout"],
        )
        if not resp.ok:
            print(f"[enrich] Alpaca error {resp.status_code}: {resp.text[:200]}")
            return {}
        data = resp.json()
    except requests.RequestException as e:
        print(f"[enrich] Alpaca request failed: {e}")
        return {}

    result = {}
    for sym, bars in (data.get("bars") or {}).items():
        closes = [b["c"] for b in bars if "c" in b]
        if closes:
            result[sym] = closes
    return result


# ── Polygon ────────────────────────────────────────────────────────────────────

def fetch_polygon_ticker(ticker: str, cfg: dict, retries: int = 3) -> list[float]:
    """
    Fetch daily closes for one ticker from Polygon.
    Retries up to `retries` times on 429 (rate limit) with exponential back-off.
    Returns [closes asc] or [].
    """
    import time as _time
    end   = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=300)).strftime("%Y-%m-%d")
    url   = f"{cfg['base_url']}/aggs/ticker/{ticker}/range/1/day/{start}/{end}"
    params = {"adjusted": "true", "sort": "asc", "limit": 200, "apiKey": cfg["api_key"]}
    for attempt in range(retries):
        try:
            resp = requests.get(url, params=params, timeout=cfg["request_timeout"])
            if resp.status_code == 429:
                wait = 15 * (2 ** attempt)   # 15s → 30s → 60s
                print(f"[enrich] Polygon 429 for {ticker}, retrying in {wait}s…")
                _time.sleep(wait)
                continue
            if not resp.ok:
                print(f"[enrich] Polygon error {resp.status_code} for {ticker}: {resp.text[:150]}")
                return []
            data = resp.json()
            return [r["c"] for r in (data.get("results") or []) if "c" in r]
        except requests.RequestException as e:
            print(f"[enrich] Polygon request failed for {ticker}: {e}")
            return []
    print(f"[enrich] Polygon gave up on {ticker} after {retries} retries.")
    return []


def fetch_polygon_batch(tickers: list[str], cfg: dict) -> dict[str, list[float]]:
    """Fetch Polygon closes for multiple tickers with a small delay between calls."""
    import time as _time
    result = {}
    for i, t in enumerate(tickers):
        closes = fetch_polygon_ticker(t, cfg)
        if closes:
            result[t] = closes
        if i < len(tickers) - 1:
            _time.sleep(0.25)   # 250ms between calls to stay under free-tier rate limit
    return result


# ── Finviz short float ─────────────────────────────────────────────────────────

_FINVIZ_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html",
}


def _finviz_label_value(soup, label: str) -> Optional[str]:
    """Generic Finviz snapshot-table extractor: find label → return next td's text."""
    for label_div in soup.find_all("div", class_="snapshot-td-label"):
        if label in label_div.get_text():
            parent_td = label_div.parent
            value_td  = parent_td.find_next_sibling("td")
            if value_td:
                content = value_td.find("div", class_="snapshot-td-content")
                if content:
                    return content.get_text(strip=True)
    return None


def fetch_finviz_data(ticker: str, timeout: int = 10) -> dict:
    """
    Scrape Finviz quote page for short float % AND sector.
    Returns {"short_float_pct": float|None, "sector": str|None}
    """
    url = f"https://finviz.com/quote.ashx?t={ticker}&ty=c&ta=1&p=d"
    result = {"short_float_pct": None, "sector": None}
    try:
        resp = requests.get(url, headers=_FINVIZ_HEADERS, timeout=timeout)
        if not resp.ok:
            return result
        soup = BeautifulSoup(resp.text, "html.parser")

        # Short float
        sf_raw = _finviz_label_value(soup, "Short Float")
        if sf_raw:
            try:
                result["short_float_pct"] = float(
                    sf_raw.replace("%","").replace(",","").split("/")[0].strip()
                )
            except ValueError:
                pass

        # Sector (appears as a plain link in the header area, not snapshot table)
        # Finviz puts sector in <a> tags inside the quote header
        for a in soup.find_all("a", href=True):
            href = a.get("href", "")
            if "sec=" in href and "screener" in href:
                result["sector"] = a.get_text(strip=True)
                break

    except requests.RequestException:
        pass
    return result


def fetch_finviz_batch(tickers: list[str], timeout: int = 10) -> dict[str, dict]:
    """Fetch short float + sector for each ticker from Finviz."""
    return {t: fetch_finviz_data(t, timeout) for t in tickers}


# Keep old name as alias so nothing else breaks
def fetch_short_floats(tickers: list[str], timeout: int = 10) -> dict[str, Optional[float]]:
    batch = fetch_finviz_batch(tickers, timeout)
    return {t: v["short_float_pct"] for t, v in batch.items()}


# ── calculations ───────────────────────────────────────────────────────────────

def ema(closes: list[float], period: int) -> Optional[float]:
    if len(closes) < period:
        return None
    k = 2 / (period + 1)
    val = sum(closes[:period]) / period  # seed with SMA of first N bars
    for price in closes[period:]:
        val = price * k + val * (1 - k)
    return round(val, 4)


def pct_vs(price: float, reference: Optional[float]) -> Optional[float]:
    if reference is None or reference == 0:
        return None
    return round((price - reference) / reference * 100, 2)


def velocity(closes: list[float], days: int) -> Optional[float]:
    if len(closes) < days + 1:
        return None
    old = closes[-(days + 1)]
    if old == 0:
        return None
    return round((closes[-1] - old) / old * 100, 2)


def compute_context(closes: list[float], velocity_days: int, source: str) -> dict:
    if not closes:
        return {
            "current_price":       None,
            "ema_50":              None,
            "ema_200":             None,
            "price_vs_ema50_pct":  None,
            "price_vs_ema200_pct": None,
            "price_velocity_5d":   None,
            "short_float_pct":     None,
            "data_source":         None,
            "enrich_error":        "no price data returned",
        }

    price = closes[-1]
    e50   = ema(closes, 50)
    e200  = ema(closes, 200)

    return {
        "current_price":       round(price, 4),
        "ema_50":              e50,
        "ema_200":             e200,
        "price_vs_ema50_pct":  pct_vs(price, e50),
        "price_vs_ema200_pct": pct_vs(price, e200),
        "price_velocity_5d":   velocity(closes, velocity_days),
        "short_float_pct":     None,  # filled in separately from Finviz
        "data_source":         source,
        "enrich_error":        None,
    }


# ── main ───────────────────────────────────────────────────────────────────────

def run(
    events: list[dict] | None = None,
    config_path: str = "config.yaml",
) -> list[dict]:
    with open(config_path) as f:
        cfg = yaml.safe_load(f)

    alpaca_cfg    = cfg["alpaca"]
    polygon_cfg   = cfg["polygon"]
    velocity_days = cfg["enrich"]["velocity_days"]

    if events is None:
        events = tag_run(config_path=config_path)

    if not events:
        print("[enrich] No events to enrich.")
        return []

    tickers = list({e["ticker"] for e in events})
    print(f"[enrich] Fetching price data for {len(tickers)} ticker(s)…")

    # ── Step 1: Alpaca IEX batch ───────────────────────────────────────────────
    bars: dict[str, list[float]] = {}
    sources: dict[str, str] = {}

    alpaca_bars = fetch_alpaca_batch(tickers, alpaca_cfg)
    for t, closes in alpaca_bars.items():
        bars[t]    = closes
        sources[t] = "alpaca"
    if alpaca_bars:
        print(f"[enrich] Alpaca IEX: {', '.join(sorted(alpaca_bars))}")

    # ── Step 2: Polygon fallback ───────────────────────────────────────────────
    missing = [t for t in tickers if t not in bars]
    if missing:
        print(f"[enrich] Polygon fallback for: {', '.join(missing)}")
        polygon_bars = fetch_polygon_batch(missing, polygon_cfg)
        for t, closes in polygon_bars.items():
            bars[t]    = closes
            sources[t] = "polygon"
        if polygon_bars:
            print(f"[enrich] Polygon:     {', '.join(sorted(polygon_bars))}")

    still_missing = [t for t in tickers if t not in bars]
    if still_missing:
        print(f"[enrich] No data from either source: {', '.join(still_missing)}")

    # ── Step 3: Finviz (short float + sector) ─────────────────────────────────
    print(f"[enrich] Fetching Finviz data (short float + sector)…")
    finviz = fetch_finviz_batch(tickers)
    found_sf = [t for t, v in finviz.items() if v["short_float_pct"] is not None]
    if found_sf:
        print(f"[enrich] Finviz OK: {', '.join(sorted(found_sf))}")
    else:
        print(f"[enrich] Finviz: no data (may be blocking)")

    # ── Compute context and merge ──────────────────────────────────────────────
    enriched = []
    for event in events:
        ticker  = event["ticker"]
        closes  = bars.get(ticker, [])
        source  = sources.get(ticker, "none")
        context = compute_context(closes, velocity_days, source)
        fv = finviz.get(ticker, {})
        context["short_float_pct"] = fv.get("short_float_pct")
        context["sector"]          = fv.get("sector")
        enriched.append({**event, **context})

    ok    = sum(1 for e in enriched if e["enrich_error"] is None)
    fails = len(enriched) - ok
    print(f"[enrich] Enriched {ok}/{len(enriched)} events ({fails} with no price data).")

    return enriched


# ── CLI ────────────────────────────────────────────────────────────────────────

def _fmt_pct(val: Optional[float], prefix: str) -> str:
    if val is None:
        return f"{prefix}:N/A"
    sign = "▲" if val > 0 else "▼"
    return f"{prefix}:{sign}{abs(val):.1f}%"


def _fmt_signed(val: Optional[float], suffix: str = "%") -> str:
    if val is None:
        return f"N/A"
    sign = "+" if val > 0 else ""
    return f"{sign}{val:.1f}{suffix}"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Stage 3: enrich buy events with price context.")
    parser.add_argument("--json",   action="store_true", help="Output raw JSON")
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()

    enriched = run(config_path=args.config)

    if args.json:
        print(json.dumps(enriched, indent=2, default=str))
    else:
        if not enriched:
            print("No enriched events.")
        else:
            W = 100
            print()
            for e in sorted(enriched, key=lambda x: (x["cluster_tag"] != "CLUSTER", -x["total_value"])):
                tag      = f"[{e['cluster_tag']}]"
                qty      = f"{e['total_qty']:,}" if e.get("total_qty") else "N/A"
                buy_px   = f"${e['avg_price']:.2f}" if e.get("avg_price") else "N/A"
                curr_px  = f"${e['current_price']:.2f}" if e["current_price"] else "N/A"
                src      = e.get("data_source") or "?"
                e50      = _fmt_pct(e["price_vs_ema50_pct"],  "EMA50")
                e200     = _fmt_pct(e["price_vs_ema200_pct"], "EMA200")
                vel      = _fmt_signed(e["price_velocity_5d"])
                sf       = (f"{e['short_float_pct']:.1f}%" if e.get("short_float_pct") is not None else "N/A")
                delta    = e.get("delta_own") or "N/A"
                err      = f"  ⚠ {e['enrich_error']}" if e["enrich_error"] else ""

                # Clean title: strip ownership % suffix (e.g. "Co-CEO, 10%" → "Co-CEO")
                role = (e.get("title") or "").split(",")[0].strip()

                print(f"{'─'*W}")
                # Line 1: who bought what
                print(
                    f"  {e['event_start_date']}  {e['ticker']:<6} {tag:<9}  "
                    f"{role:<10}  "
                    f"Bought: {buy_px} × {qty} shares  ΔOwn: {delta}  "
                    f"Total: ${e['total_value']:,.0f}"
                )
                # Line 2: market context
                print(
                    f"  {'':20}  "
                    f"Now: {curr_px} ({src})  "
                    f"{e50:<14}  {e200:<15}  "
                    f"5d: {vel}  Short: {sf}"
                    f"{err}"
                )
            print(f"{'─'*W}")
