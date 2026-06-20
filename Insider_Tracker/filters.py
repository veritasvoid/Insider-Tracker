"""
filters.py — Optional technical screen, feeds the "Filtered" tab ONLY.

Self-contained on purpose: fetches its OWN OHLCV bars (separate functions
from enrich.py) so a bug or API failure here can NEVER affect enrich.py,
score.py, the DB, or the Today's Picks / History / Performance tabs.
If this module raises or returns [], the rest of the pipeline is unaffected.

Logic ported from two ThinkOrSwim studies:

  1. Relative Volume w/ Buyer/Seller Control
       rvol = volume / average(volume, 20)
       signal when rvol >= threshold (1.5)
       buyer control if close > open, seller control if close < open

  2. 50 & 200 EMA ± 0.25 SD Bands
       band = EMA(period) ± 0.25 * StDev(close, period)
       signal when today's open OR close sits inside the band

A ticker PASSES this filter when BOTH:
  - rvol >= RVOL_THRESHOLD
  - close/open is inside the 50-EMA band OR the 200-EMA band

Uses the most recent COMPLETE daily bar (drops an in-progress bar dated
today, since the GitHub Actions run fires ~15 min after market open).

Usage:
    from filters import run as filters_run
    signals = filters_run(["AAPL", "MSFT"], config_path="config.yaml")
"""

import statistics
from datetime import datetime, timedelta
from typing import Optional

import requests
import yaml

RVOL_LENGTH    = 20
RVOL_THRESHOLD = 1.5
SD_MULT        = 0.25
EMA_PERIODS    = (50, 200)


# ── fetch (isolated copy — own failure mode, never touches enrich.py) ─────────

def _alpaca_headers(cfg: dict) -> dict:
    return {
        "APCA-API-KEY-ID":     cfg["api_key"],
        "APCA-API-SECRET-KEY": cfg["api_secret"],
        "Accept":              "application/json",
    }


def fetch_alpaca_ohlcv(tickers: list[str], cfg: dict) -> dict[str, list[dict]]:
    """Batch-fetch daily OHLCV bars from Alpaca IEX. Returns {ticker: [{"t","o","c","v"} asc]}."""
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
            print(f"[filters] Alpaca error {resp.status_code}: {resp.text[:200]}")
            return {}
        data = resp.json()
    except requests.RequestException as e:
        print(f"[filters] Alpaca request failed: {e}")
        return {}

    result: dict[str, list[dict]] = {}
    for sym, bars in (data.get("bars") or {}).items():
        rows = [
            {"t": b.get("t"), "o": b.get("o"), "c": b.get("c"), "v": b.get("v")}
            for b in bars
            if b.get("o") is not None and b.get("c") is not None and b.get("v") is not None
        ]
        if rows:
            result[sym] = rows
    return result


def fetch_polygon_ohlcv(ticker: str, cfg: dict, retries: int = 3) -> list[dict]:
    """Fetch daily OHLCV bars for one ticker from Polygon. Returns [{"t","o","c","v"} asc] or []."""
    import time as _time
    end   = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=300)).strftime("%Y-%m-%d")
    url   = f"{cfg['base_url']}/aggs/ticker/{ticker}/range/1/day/{start}/{end}"
    params = {"adjusted": "true", "sort": "asc", "limit": 200, "apiKey": cfg["api_key"]}
    for attempt in range(retries):
        try:
            resp = requests.get(url, params=params, timeout=cfg["request_timeout"])
            if resp.status_code == 429:
                wait = 15 * (2 ** attempt)
                print(f"[filters] Polygon 429 for {ticker}, retrying in {wait}s…")
                _time.sleep(wait)
                continue
            if not resp.ok:
                print(f"[filters] Polygon error {resp.status_code} for {ticker}: {resp.text[:150]}")
                return []
            data = resp.json()
            return [
                {"t": r.get("t"), "o": r.get("o"), "c": r.get("c"), "v": r.get("v")}
                for r in (data.get("results") or [])
                if r.get("o") is not None and r.get("c") is not None and r.get("v") is not None
            ]
        except requests.RequestException as e:
            print(f"[filters] Polygon request failed for {ticker}: {e}")
            return []
    print(f"[filters] Polygon gave up on {ticker} after {retries} retries.")
    return []


def fetch_polygon_ohlcv_batch(tickers: list[str], cfg: dict) -> dict[str, list[dict]]:
    import time as _time
    result: dict[str, list[dict]] = {}
    for i, t in enumerate(tickers):
        bars = fetch_polygon_ohlcv(t, cfg)
        if bars:
            result[t] = bars
        if i < len(tickers) - 1:
            _time.sleep(0.25)
    return result


# ── math ───────────────────────────────────────────────────────────────────────

def _ema(closes: list[float], period: int) -> Optional[float]:
    if len(closes) < period:
        return None
    k = 2 / (period + 1)
    val = sum(closes[:period]) / period   # seed with SMA of first N bars
    for price in closes[period:]:
        val = price * k + val * (1 - k)
    return val


def _drop_incomplete_today(bars: list[dict]) -> list[dict]:
    """Drop the last bar if it's dated today (not yet closed at run time)."""
    if not bars:
        return bars
    today = datetime.now().strftime("%Y-%m-%d")
    last_date = (bars[-1].get("t") or "")[:10]
    return bars[:-1] if last_date == today else bars


def compute_signal(bars: list[dict]) -> Optional[dict]:
    """
    bars: ascending list of {"t","o","c","v"} daily bars.
    Returns None if there isn't enough history; otherwise the computed signal dict.
    """
    bars = _drop_incomplete_today(bars)
    if len(bars) < RVOL_LENGTH + 1:
        return None

    closes  = [b["c"] for b in bars]
    volumes = [b["v"] for b in bars]
    last    = bars[-1]
    o, c, v = last["o"], last["c"], last["v"]

    # ── Relative volume ──
    window  = volumes[-RVOL_LENGTH:]
    avg_vol = sum(window) / len(window) if window else 0
    rvol    = (v / avg_vol) if avg_vol else 0

    # ── EMA ± 0.25 SD bands (50 / 200) ──
    bands: dict[int, dict] = {}
    for period in EMA_PERIODS:
        if len(closes) < period:
            continue
        e = _ema(closes, period)
        if e is None:
            continue
        window_c = closes[-period:]
        sd = statistics.stdev(window_c) if len(window_c) > 1 else 0
        upper = e + SD_MULT * sd
        lower = e - SD_MULT * sd
        inside = (lower <= c <= upper) or (lower <= o <= upper)
        bands[period] = {
            "ema": round(e, 4), "upper": round(upper, 4), "lower": round(lower, 4),
            "inside": inside,
        }

    band_signal = any(b["inside"] for b in bands.values())
    rvol_signal = rvol >= RVOL_THRESHOLD
    control = "buyer" if c > o else "seller" if c < o else "neutral"

    return {
        "bar_date":    (last.get("t") or "")[:10],
        "open":        round(o, 4),
        "close":       round(c, 4),
        "volume":      v,
        "avg_volume":  round(avg_vol, 0),
        "rvol":        round(rvol, 2),
        "rvol_signal": rvol_signal,
        "control":     control,
        "bands":       bands,
        "band_signal": band_signal,
        "passes":      rvol_signal and band_signal,
    }


# ── public entry point ─────────────────────────────────────────────────────────

def run(tickers: list[str], config_path: str = "config.yaml") -> list[dict]:
    """
    For each ticker, fetch OHLCV and compute the RVol + EMA-band signal.
    Returns a list of dicts (one per ticker that PASSES the filter):
      {"ticker": str, **compute_signal() fields}

    Never raises — any failure (config, network, math) is caught and logged,
    and this just returns fewer (or zero) results. The Filtered tab will show
    "no matches" rather than the pipeline breaking.
    """
    if not tickers:
        return []

    try:
        with open(config_path) as f:
            cfg = yaml.safe_load(f)
        alpaca_cfg  = cfg["alpaca"]
        polygon_cfg = cfg["polygon"]
    except Exception as e:
        print(f"[filters] Could not load config: {e}")
        return []

    tickers = sorted(set(tickers))
    print(f"[filters] Screening {len(tickers)} ticker(s) for RVol/EMA-band signal…")

    bars_by_ticker: dict[str, list[dict]] = {}
    try:
        bars_by_ticker.update(fetch_alpaca_ohlcv(tickers, alpaca_cfg))
    except Exception as e:
        print(f"[filters] Alpaca fetch failed: {e}")

    missing = [t for t in tickers if t not in bars_by_ticker]
    if missing:
        try:
            bars_by_ticker.update(fetch_polygon_ohlcv_batch(missing, polygon_cfg))
        except Exception as e:
            print(f"[filters] Polygon fetch failed: {e}")

    results = []
    for t in tickers:
        bars = bars_by_ticker.get(t)
        if not bars:
            continue
        try:
            sig = compute_signal(bars)
        except Exception as e:
            print(f"[filters] Signal calc failed for {t}: {e}")
            continue
        if sig and sig["passes"]:
            results.append({"ticker": t, **sig})

    print(f"[filters] {len(results)}/{len(tickers)} ticker(s) currently pass the filter.")
    return results


# ── CLI (manual testing only — never called by the pipeline) ──────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Standalone test: RVol + EMA-band screen.")
    parser.add_argument("tickers", nargs="+", help="Tickers to screen, e.g. AAPL MSFT")
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()

    out = run(args.tickers, config_path=args.config)
    if not out:
        print("No tickers passed the filter.")
    for s in out:
        print(f"{s['ticker']}: rvol={s['rvol']}x  control={s['control']}  "
              f"bands={ {p: b['inside'] for p, b in s['bands'].items()} }")
