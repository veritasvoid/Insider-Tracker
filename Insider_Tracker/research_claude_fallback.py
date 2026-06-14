"""
research.py — Stage 4
Queries Claude (Anthropic) for company context, upcoming catalysts, and
sentiment for each enriched buy event.

Replaces the previous Gemini implementation — Claude is more stable (non-expiring
API keys) and the Anthropic key is already required for Stage 5 scoring anyway.

Fields added per event:
  news_headline    — most important known development or catalyst
  news_summary     — 2-3 sentence business context summary
  catalysts        — list of upcoming catalyst strings
  news_sentiment   — "bullish" | "bearish" | "neutral"
  news_sources     — [] (no live web search)
  research_ts      — ISO timestamp of this research run
  research_error   — error string or None

Usage:
    python research.py          # pretty-prints per-ticker research
    python research.py --json   # raw JSON of all events
"""

import json
import argparse
import time
from datetime import datetime
from typing import Optional

import requests
import yaml

from enrich import run as enrich_run


# ── prompt ─────────────────────────────────────────────────────────────────────

_SYSTEM = """\
You are a senior financial analyst with deep knowledge of public US equities.
Given a company and its recent CEO/CFO insider purchase, provide concise analysis.

Return ONLY a valid JSON object (no markdown, no prose) with these exact keys:

  headline   — string: the single most important recent development or known catalyst
  summary    — string: 2-3 sentences on business context and what this insider buy may signal
  catalysts  — list of strings: specific upcoming events that could move the stock
               Examples: "Q2 earnings expected late July 2026", "FDA PDUFA date Aug 2026",
               "Phase 3 data readout H2 2026", "Debt refinancing due Q3 2026"
               Use empty list [] if no specific catalysts are identifiable.
  sentiment  — exactly one of: "bullish", "bearish", "neutral"
  sources    — [] (always empty)

Base analysis on your training knowledge. If the company is unfamiliar,
return neutral sentiment with honest 2-sentence summary. No hedging phrases like
"as of my knowledge cutoff" — just give the best analysis available.
Respond with ONLY the JSON object.\
"""


def _build_prompt(event: dict) -> str:
    company  = event.get("company", "")
    ticker   = event["ticker"]
    buy_px   = event.get("avg_price") or event.get("price") or 0
    total    = event.get("total_value", 0)
    role     = (event.get("title") or "").split(",")[0].strip()
    vs50     = event.get("price_vs_ema50_pct")
    vs200    = event.get("price_vs_ema200_pct")
    vel      = event.get("price_velocity_5d")
    sf       = event.get("short_float_pct")
    cluster  = event.get("cluster_tag", "SINGLE")

    ctx_parts = []
    if vs200 is not None:
        ctx_parts.append(f"price {vs200:+.1f}% vs 200d EMA")
    if vel is not None:
        ctx_parts.append(f"5d momentum {vel:+.1f}%")
    if sf is not None:
        ctx_parts.append(f"short float {sf:.1f}%")
    ctx_str = " | ".join(ctx_parts) if ctx_parts else "N/A"

    return (
        f"Company: {company} (ticker: {ticker})\n"
        f"Signal: {role} bought ${total:,.0f} worth at ${buy_px:.2f}/share — {cluster}\n"
        f"Price context: {ctx_str}\n\n"
        f"Provide JSON analysis of {company} ({ticker}): business context, "
        f"what this insider buy likely signals, and upcoming catalysts."
    )


# ── result helpers ─────────────────────────────────────────────────────────────

def _empty_result(error: str) -> dict:
    return {
        "news_headline":  None,
        "news_summary":   None,
        "catalysts":      [],
        "news_sentiment": None,
        "news_sources":   [],
        "research_ts":    datetime.now().isoformat(),
        "research_error": error,
    }


# ── Claude call ────────────────────────────────────────────────────────────────

def _research_ticker(event: dict, api_key: str, model: str, timeout: int) -> dict:
    prompt = _build_prompt(event)

    for attempt in range(3):
        try:
            resp = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key":         api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type":      "application/json",
                },
                json={
                    "model":      model,
                    "max_tokens": 512,
                    "system":     _SYSTEM,
                    "messages":   [{"role": "user", "content": prompt}],
                },
                timeout=timeout,
            )

            if resp.status_code == 429:
                wait = 15 * (attempt + 1)
                print(f"[research]   429 rate-limit, retrying in {wait}s…")
                time.sleep(wait)
                continue

            if not resp.ok:
                return _empty_result(
                    f"Claude API error {resp.status_code}: {resp.text[:200]}"
                )

            raw = resp.json()["content"][0]["text"].strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
                raw = raw.strip()

            parsed = json.loads(raw)
            return {
                "news_headline":  parsed.get("headline"),
                "news_summary":   parsed.get("summary"),
                "catalysts":      parsed.get("catalysts") or [],
                "news_sentiment": parsed.get("sentiment"),
                "news_sources":   [],
                "research_ts":    datetime.now().isoformat(),
                "research_error": None,
            }

        except json.JSONDecodeError as e:
            return _empty_result(f"JSON parse error: {e}")
        except Exception as e:
            return _empty_result(str(e))

    return _empty_result("all retries exhausted")


# ── public entry point ─────────────────────────────────────────────────────────

def run(
    events: list[dict] | None = None,
    config_path: str = "config.yaml",
) -> list[dict]:
    with open(config_path) as f:
        cfg = yaml.safe_load(f)

    # Use the same Anthropic key/model as score.py — no separate Gemini config needed
    anthropic_cfg = cfg["anthropic"]
    api_key = anthropic_cfg["api_key"]
    model   = anthropic_cfg.get("model", "claude-haiku-4-5-20251001")
    timeout = anthropic_cfg.get("request_timeout", 60)

    if events is None:
        events = enrich_run(config_path=config_path)

    if not events:
        print("[research] No events to research.")
        return []

    # One Claude call per unique ticker
    seen: dict[str, dict] = {}
    for e in events:
        if e["ticker"] not in seen:
            seen[e["ticker"]] = e

    print(f"[research] Claude research for {len(seen)} ticker(s)…")

    cache: dict[str, dict] = {}
    for ticker, event in seen.items():
        company = event.get("company", "")
        print(f"[research]   {ticker}  {company}")
        cache[ticker] = _research_ticker(event, api_key, model, timeout)
        if cache[ticker]["research_error"]:
            print(f"[research]   ⚠ {ticker}: {cache[ticker]['research_error'][:120]}")
        time.sleep(0.3)   # brief pause between calls

    ok = sum(1 for r in cache.values() if r["research_error"] is None)
    print(f"[research] Done: {ok}/{len(cache)} tickers OK.")

    return [{**e, **cache.get(e["ticker"], _empty_result("no cache entry"))} for e in events]


# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Stage 4: Claude research per ticker.")
    parser.add_argument("--json",   action="store_true", help="Raw JSON output")
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()

    results = run(config_path=args.config)

    if args.json:
        print(json.dumps(results, indent=2, default=str))
    else:
        if not results:
            print("No results.")
            raise SystemExit

        seen: set[str] = set()
        for e in results:
            t = e["ticker"]
            if t in seen:
                continue
            seen.add(t)

            sentiment = (e.get("news_sentiment") or "N/A").upper()
            print(f"\n{'═'*80}")
            print(f"  {t:<6}  {e.get('company', '')}  [{sentiment}]")
            if e.get("news_headline"):
                print(f"  Headline:  {e['news_headline']}")
            if e.get("news_summary"):
                print(f"  Summary:   {e['news_summary']}")
            if e.get("catalysts"):
                for cat in e["catalysts"]:
                    print(f"  Catalyst:  {cat}")
            if e.get("research_error"):
                print(f"  ⚠ Error:   {e['research_error']}")

        print(f"{'═'*80}")
                                                                       