"""
research.py — Stage 4
Queries Gemini (with Google Search grounding) for real-time news, catalysts,
and sentiment per ticker. Falls back to Claude if Gemini is unavailable.

Uses the official google-generativeai SDK which handles AQ. key auth natively.
Falls back to Claude (Anthropic) if Gemini auth or quota fails.

Fields added per event:
  news_headline    — most important recent headline
  news_summary     — 2-3 sentence summary
  catalysts        — list of upcoming catalyst strings
  news_sentiment   — "bullish" | "bearish" | "neutral"
  news_sources     — list of URLs (Gemini) or [] (Claude fallback)
  research_ts      — ISO timestamp
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


# ── prompts ────────────────────────────────────────────────────────────────────

_GEMINI_SYSTEM = """\
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

_CLAUDE_SYSTEM = """\
You are a senior financial analyst. Given a company and its recent CEO/CFO insider purchase,
provide concise analysis based on your training knowledge.

Return ONLY a valid JSON object (no markdown, no prose) with these keys:
  headline   — string: the single most important known development or catalyst
  summary    — string: 2-3 sentences on business context and what this insider buy may signal
  catalysts  — list of strings: upcoming events that could move the stock. Empty list if none.
  sentiment  — one of: "bullish", "bearish", "neutral"
  sources    — [] (always empty)

Respond with ONLY the JSON object.\
"""


def _build_gemini_prompt(event: dict) -> str:
    company  = event.get("company", "")
    ticker   = event["ticker"]
    buy_px   = event.get("avg_price") or event.get("price") or 0
    total    = event.get("total_value", 0)
    role     = (event.get("title") or "").split(",")[0].strip()
    return (
        f"Company: {company} (ticker: {ticker})\n"
        f"Context: {role} bought ${total:,.0f} worth at ${buy_px:.2f}/share on "
        f"{event.get('event_start_date') or event.get('trade_date', 'recent date')}.\n\n"
        f"Search for news about {company} ({ticker}) from the last 14 days and return the JSON."
    )


def _build_claude_prompt(event: dict) -> str:
    company  = event.get("company", "")
    ticker   = event["ticker"]
    buy_px   = event.get("avg_price") or event.get("price") or 0
    total    = event.get("total_value", 0)
    role     = (event.get("title") or "").split(",")[0].strip()
    cluster  = event.get("cluster_tag", "SINGLE")
    vs200    = event.get("price_vs_ema200_pct")
    vel      = event.get("price_velocity_5d")
    ctx = []
    if vs200 is not None:
        ctx.append(f"price {vs200:+.1f}% vs 200d EMA")
    if vel is not None:
        ctx.append(f"5d momentum {vel:+.1f}%")
    return (
        f"Company: {company} (ticker: {ticker})\n"
        f"Signal: {role} bought ${total:,.0f} at ${buy_px:.2f}/share — {cluster}\n"
        f"Price context: {' | '.join(ctx) or 'N/A'}\n\n"
        f"Provide JSON analysis: business context, what this insider buy signals, catalysts."
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


def _parse_text(raw: str) -> dict:
    """Parse JSON from model response text, stripping any markdown fences."""
    raw = raw.strip()
    if raw.startswith("```"):
        parts = raw.split("```")
        raw = parts[1] if len(parts) > 1 else raw
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    return json.loads(raw)


# ── Gemini via SDK ─────────────────────────────────────────────────────────────

def _research_ticker_gemini(event: dict, api_key: str, model_name: str) -> Optional[dict]:
    """
    Call Gemini via the official SDK (handles AQ. key auth automatically).
    Returns None if auth fails so caller can switch to Claude fallback.
    Returns _empty_result(...) for other errors.
    """
    try:
        import google.generativeai as genai
        genai.configure(api_key=api_key)

        system_parts = [{"text": _GEMINI_SYSTEM}]
        prompt = _build_gemini_prompt(event)

        # Try with Google Search grounding first, then without
        for use_search in (True, False):
            try:
                model = genai.GenerativeModel(
                    model_name=model_name,
                    system_instruction=_GEMINI_SYSTEM,
                )
                gen_cfg = genai.GenerationConfig(temperature=0.1)

                if use_search:
                    response = model.generate_content(
                        prompt,
                        tools=[{"google_search": {}}],
                        generation_config=gen_cfg,
                    )
                else:
                    response = model.generate_content(
                        prompt,
                        generation_config=gen_cfg,
                    )

                raw = response.text
                parsed = _parse_text(raw)

                # Extract grounding sources if available
                sources = parsed.get("sources") or []
                if use_search and not sources:
                    try:
                        chunks = (
                            response.candidates[0]
                            .grounding_metadata.grounding_chunks
                        )
                        sources = [c.web.uri for c in chunks if hasattr(c, "web")]
                    except Exception:
                        pass

                result = {
                    "news_headline":  parsed.get("headline"),
                    "news_summary":   parsed.get("summary"),
                    "catalysts":      parsed.get("catalysts") or [],
                    "news_sentiment": parsed.get("sentiment"),
                    "news_sources":   sources,
                    "research_ts":    datetime.now().isoformat(),
                    "research_error": None if use_search else
                                      "search-grounding quota exceeded; used training knowledge",
                }
                return result

            except json.JSONDecodeError as e:
                return _empty_result(f"Gemini JSON parse error: {e}")
            except Exception as inner:
                err = str(inner).lower()
                if "quota" in err or "429" in err:
                    time.sleep(15)
                    continue
                if not use_search:
                    return _empty_result(f"Gemini error: {inner}")
                # Search failed — try without grounding
                continue

    except Exception as e:
        err = str(e).lower()
        # Auth failure → return None to trigger Claude fallback
        if any(w in err for w in ("401", "403", "invalid", "authentication", "permission")):
            return None
        return _empty_result(f"Gemini SDK error: {e}")

    return _empty_result("Gemini: all attempts exhausted")


# ── Claude fallback ────────────────────────────────────────────────────────────

def _research_ticker_claude(event: dict, api_key: str, model: str, timeout: int) -> dict:
    """Claude fallback — knowledge-based analysis, no live search."""
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
                "system":     _CLAUDE_SYSTEM,
                "messages":   [{"role": "user", "content": _build_claude_prompt(event)}],
            },
            timeout=timeout,
        )
        if not resp.ok:
            return _empty_result(f"Claude fallback error {resp.status_code}")

        parsed = _parse_text(resp.json()["content"][0]["text"])
        return {
            "news_headline":  parsed.get("headline"),
            "news_summary":   parsed.get("summary"),
            "catalysts":      parsed.get("catalysts") or [],
            "news_sentiment": parsed.get("sentiment"),
            "news_sources":   [],
            "research_ts":    datetime.now().isoformat(),
            "research_error": "Gemini unavailable — Claude training knowledge used (no live search)",
        }
    except Exception as e:
        return _empty_result(f"Claude fallback failed: {e}")


# ── public entry point ─────────────────────────────────────────────────────────

def run(
    events: list[dict] | None = None,
    config_path: str = "config.yaml",
) -> list[dict]:
    with open(config_path) as f:
        cfg = yaml.safe_load(f)

    gemini_cfg     = cfg.get("gemini", {})
    gemini_key     = gemini_cfg.get("api_key", "")
    gemini_model   = gemini_cfg.get("model", "gemini-2.5-flash")

    anthropic_cfg  = cfg["anthropic"]
    claude_key     = anthropic_cfg["api_key"]
    claude_model   = anthropic_cfg.get("model", "claude-haiku-4-5-20251001")
    claude_timeout = anthropic_cfg.get("request_timeout", 60)

    if events is None:
        events = enrich_run(config_path=config_path)

    if not events:
        print("[research] No events to research.")
        return []

    seen: dict[str, dict] = {}
    for e in events:
        if e["ticker"] not in seen:
            seen[e["ticker"]] = e

    print(f"[research] Gemini search for {len(seen)} ticker(s)…")

    gemini_ok = bool(gemini_key)

    cache: dict[str, dict] = {}
    for ticker, event in seen.items():
        company = event.get("company", "")
        print(f"[research]   {ticker}  {company}")

        result = None
        if gemini_ok:
            result = _research_ticker_gemini(event, gemini_key, gemini_model)
            if result is None:
                print("[research]   Gemini auth failed — switching to Claude fallback for all tickers")
                gemini_ok = False

        if result is None:
            result = _research_ticker_claude(event, claude_key, claude_model, claude_timeout)

        cache[ticker] = result
        err = result.get("research_error") or ""
        if err and "Claude training knowledge" not in err and "quota" not in err:
            print(f"[research]   ⚠ {ticker}: {err[:120]}")

        time.sleep(1)

    ok = sum(1 for r in cache.values() if r["research_error"] is None)
    print(f"[research] Done: {ok}/{len(cache)} tickers OK.")

    return [{**e, **cache.get(e["ticker"], _empty_result("no cache entry"))} for e in events]


# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Stage 4: Gemini (+ Claude fallback) research.")
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
            for cat in (e.get("catalysts") or []):
                print(f"  Catalyst:  {cat}")
            if e.get("news_sources"):
                for url in e["news_sources"][:2]:
                    print(f"  Source:    {url}")
            if e.get("research_error"):
                print(f"  ⚠ Note:    {e['research_error'][:120]}")
        print(f"{'═'*80}")
