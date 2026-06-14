"""
research.py — Stage 4
For each enriched event, queries Gemini 2.0 Flash (with Google Search grounding)
for recent news, upcoming catalysts, and sentiment.

One Gemini call per ticker (deduped), result shared across all events for that ticker.

Fields added per event:
  news_headline    — most important recent headline
  news_summary     — 2-3 sentence summary of recent news
  catalysts        — list of upcoming catalyst strings (earnings, FDA, etc.)
  news_sentiment   — "bullish" | "bearish" | "neutral"
  news_sources     — list of URLs Gemini cited
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

_GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models"
    "/{model}:generateContent"
)


# ── prompt ─────────────────────────────────────────────────────────────────────

_SYSTEM = """\
You are a financial analyst assistant.
Given a company and its recent insider buy, search for news from the last 14 days
and return ONLY a valid JSON object (no markdown, no prose) with these keys:

  headline   — string: the single most important recent headline
  summary    — string: 2-3 sentences summarising key news and what it means for the stock
  catalysts  — list of strings: upcoming events that could move the stock
               (e.g. "Q2 earnings ~July 28", "FDA PDUFA date Aug 15", "Phase 3 data readout Q3")
               Empty list if none known.
  sentiment  — one of: "bullish", "bearish", "neutral"
  sources    — list of URL strings you cited (from Google Search)

Respond with ONLY the JSON object. No code fences, no explanation.\
"""


def _build_prompt(event: dict) -> str:
    company  = event.get("company", "")
    ticker   = event["ticker"]
    buy_px   = event.get("avg_price") or event.get("price") or 0
    total    = event.get("total_value", 0)
    role     = (event.get("title") or "").split(",")[0].strip()
    lookback = 14  # days

    return (
        f"Company: {company} (ticker: {ticker})\n"
        f"Context: {role} bought ${total:,.0f} worth at ${buy_px:.2f}/share on "
        f"{event.get('event_start_date') or event.get('trade_date', 'recent date')}.\n\n"
        f"Search for news about {company} ({ticker}) from the last {lookback} days "
        f"and return the JSON."
    )


# ── Gemini call ────────────────────────────────────────────────────────────────

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


def _call_gemini(prompt: str, api_key: str, model: str, timeout: int,
                 use_search: bool = True) -> dict:
    """
    POST to Gemini. With use_search=True, requests Google Search grounding.
    Returns the raw requests.Response.
    """
    payload = {
        "systemInstruction": {"parts": [{"text": _SYSTEM}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.1},
    }
    if use_search:
        payload["tools"] = [{"googleSearch": {}}]

    return requests.post(
        _GEMINI_URL.format(model=model),
        params={"key": api_key},
        headers={"Content-Type": "application/json"},
        json=payload,
        timeout=timeout,
    )


def _parse_response(resp_json: dict) -> dict:
    candidate = resp_json["candidates"][0]
    raw = candidate["content"]["parts"][0]["text"].strip()

    if raw.startswith("```"):
        parts = raw.split("```")
        raw = parts[1] if len(parts) > 1 else raw
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    parsed = json.loads(raw)

    sources = parsed.get("sources") or []
    if not sources:
        try:
            chunks = candidate["groundingMetadata"]["groundingChunks"]
            sources = [c["web"]["uri"] for c in chunks if "web" in c]
        except (KeyError, TypeError):
            pass

    return {
        "news_headline":  parsed.get("headline"),
        "news_summary":   parsed.get("summary"),
        "catalysts":      parsed.get("catalysts") or [],
        "news_sentiment": parsed.get("sentiment"),
        "news_sources":   sources,
        "research_ts":    datetime.now().isoformat(),
        "research_error": None,
    }


def _research_ticker(event: dict, api_key: str, model: str, timeout: int) -> dict:
    prompt = _build_prompt(event)
    raw: str = ""

    for use_search in (True, False):          # try grounded first, then plain
        for attempt in range(3):              # up to 3 retries per mode
            try:
                resp = _call_gemini(prompt, api_key, model, timeout, use_search)

                if resp.status_code == 429:
                    wait = 15 * (attempt + 1)
                    print(f"[research]   429 rate-limit (search={use_search}), "
                          f"retrying in {wait}s…")
                    time.sleep(wait)
                    continue

                if not resp.ok:
                    return _empty_result(f"HTTP {resp.status_code}: {resp.text[:300]}")

                result = _parse_response(resp.json())
                if not use_search:
                    result["news_sources"] = []   # no real URLs without grounding
                    result["research_error"] = "search-grounding quota exceeded; used training knowledge"
                return result

            except json.JSONDecodeError as exc:
                return _empty_result(f"JSON parse error: {exc} | raw={raw[:300]}")
            except Exception as exc:
                return _empty_result(str(exc))

    return _empty_result("all retries exhausted (rate-limited)")


# ── public entry point ─────────────────────────────────────────────────────────

def run(
    events: list[dict] | None = None,
    config_path: str = "config.yaml",
) -> list[dict]:
    with open(config_path) as f:
        cfg = yaml.safe_load(f)

    gemini_cfg = cfg["gemini"]
    api_key = gemini_cfg["api_key"]
    model   = gemini_cfg.get("model", "gemini-2.0-flash")
    timeout = gemini_cfg.get("request_timeout", 30)

    if events is None:
        events = enrich_run(config_path=config_path)

    if not events:
        print("[research] No events to research.")
        return []

    # One Gemini call per unique ticker
    seen: dict[str, dict] = {}
    for e in events:
        if e["ticker"] not in seen:
            seen[e["ticker"]] = e

    print(f"[research] Gemini search for {len(seen)} ticker(s)…")

    cache: dict[str, dict] = {}
    for ticker, event in seen.items():
        company = event.get("company", "")
        print(f"[research]   {ticker}  {company}")
        cache[ticker] = _research_ticker(event, api_key, model, timeout)
        if cache[ticker]["research_error"]:
            print(f"[research]   ⚠ {ticker}: {cache[ticker]['research_error'][:120]}")
        time.sleep(1)   # polite rate limiting

    ok = sum(1 for r in cache.values() if r["research_error"] is None)
    print(f"[research] Done: {ok}/{len(cache)} tickers OK.")

    return [{**e, **cache.get(e["ticker"], _empty_result("no cache entry"))} for e in events]


# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Stage 4: Gemini news research per ticker.")
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
            if e.get("news_sources"):
                for url in e["news_sources"][:3]:
                    print(f"  Source:    {url}")
            if e.get("research_error"):
                print(f"  ⚠ Error:   {e['research_error']}")

        print(f"{'═'*80}")
