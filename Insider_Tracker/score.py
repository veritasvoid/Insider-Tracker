"""
score.py — Stage 5
Institutional-grade composite scoring of insider buy events.

Architecture (mirrors BlackRock Systematic / Stockopedia QVM):
  ┌─────────────────────────────────────────────────────────┐
  │  QUANT LAYER (deterministic, 0-65 pts)                  │
  │    D1  Insider Signal Quality   25 pts                  │
  │    D2  Price / Technical        20 pts                  │
  │    D3  Short Interest Risk      10 pts                  │
  │    D4  Cluster Bonus            10 pts                  │
  ├─────────────────────────────────────────────────────────┤
  │  AI LAYER — Claude (qualitative, 0-35 pts)              │
  │    D5  Fundamental Quality      12 pts                  │
  │    D6  Catalyst Strength        13 pts                  │
  │    D7  News / Sentiment Fit     10 pts                  │
  └─────────────────────────────────────────────────────────┘
  Total 0-100 → stars 1-5
    80-100  ★★★★★  Strong Buy
    60-79   ★★★★   Buy
    40-59   ★★★    Watch
    20-39   ★★     Weak
    0-19    ★      Skip

Academic basis:
  - Lakonishok & Lee (2002): heavy insider buyers beat market +4.8% / 12m
  - Cohen, Malloy & Pomorski (2012): opportunistic buys +5.2% alpha / 6m
  - Cluster buys in small caps: +7.4% abnormal return / 12m
  - Buying below EMA200 (dip-buying) amplifies signal reliability

Usage:
    python score.py          # pretty table output
    python score.py --json   # raw JSON
"""

import json
import argparse
import time
from typing import Optional

import requests
import yaml

from research import run as research_run


# ── Star mapping ───────────────────────────────────────────────────────────────

STARS = {5: "★★★★★", 4: "★★★★☆", 3: "★★★☆☆", 2: "★★☆☆☆", 1: "★☆☆☆☆"}
LABELS = {5: "STRONG BUY", 4: "BUY", 3: "WATCH", 2: "WEAK", 1: "SKIP"}


def composite_to_stars(score: float) -> int:
    if score >= 80: return 5
    if score >= 60: return 4
    if score >= 40: return 3
    if score >= 20: return 2
    return 1


# ── D1: Insider Signal Quality (0-25) ─────────────────────────────────────────

def score_insider_signal(event: dict) -> tuple[float, list[str]]:
    """
    Scores the raw insider buy signal quality.
    Factors: dollar size, delta ownership, role seniority, dip-buying.
    Academic basis: Cohen et al (2012) — opportunistic + large = highest alpha.
    """
    pts = 0.0
    notes = []

    total_val = event.get("total_value") or event.get("value") or 0

    # Dollar size (0-10 pts) — log-scaled, $100k floor (already filtered)
    if total_val >= 50_000_000:
        pts += 10; notes.append(f"Massive buy ${total_val/1e6:.1f}M (+10)")
    elif total_val >= 10_000_000:
        pts += 8;  notes.append(f"Very large buy ${total_val/1e6:.1f}M (+8)")
    elif total_val >= 5_000_000:
        pts += 6;  notes.append(f"Large buy ${total_val/1e6:.1f}M (+6)")
    elif total_val >= 1_000_000:
        pts += 4;  notes.append(f"Significant buy ${total_val/1e6:.1f}M (+4)")
    elif total_val >= 500_000:
        pts += 2;  notes.append(f"Meaningful buy ${total_val/1e3:.0f}K (+2)")
    else:
        pts += 1;  notes.append(f"Small buy ${total_val/1e3:.0f}K (+1)")

    # Delta ownership (0-8 pts) — higher % change = more conviction
    delta_raw = str(event.get("delta_own") or "0").replace("+", "").replace("%", "").strip()
    try:
        delta = float(delta_raw.replace(">", "").replace("New", "999"))
        if delta >= 100 or delta_raw.lower() == "new":
            pts += 8; notes.append("NEW position or doubled (+8)")
        elif delta >= 50:
            pts += 6; notes.append(f"δOwn +{delta:.0f}% (+6)")
        elif delta >= 20:
            pts += 5; notes.append(f"δOwn +{delta:.0f}% (+5)")
        elif delta >= 5:
            pts += 3; notes.append(f"δOwn +{delta:.0f}% (+3)")
        elif delta >= 1:
            pts += 1; notes.append(f"δOwn +{delta:.0f}% (+1)")
    except (ValueError, TypeError):
        pass

    # Dip-buying bonus (0-5 pts): buying below EMA200 = opportunistic (vs routine)
    vs200 = event.get("price_vs_ema200_pct")
    if vs200 is not None:
        if vs200 <= -30:
            pts += 5; notes.append(f"Buying {abs(vs200):.1f}% below EMA200 — deep dip (+5)")
        elif vs200 <= -15:
            pts += 4; notes.append(f"Buying {abs(vs200):.1f}% below EMA200 (+4)")
        elif vs200 <= -5:
            pts += 2; notes.append(f"Buying {abs(vs200):.1f}% below EMA200 (+2)")
        elif vs200 > 15:
            pts -= 2; notes.append(f"Buying {vs200:.1f}% ABOVE EMA200 — momentum chase (-2)")

    # Role seniority (0-2 pts)
    title = (event.get("title") or "").upper()
    if "CEO" in title or "CHIEF EXECUTIVE" in title:
        pts += 2; notes.append("CEO/Co-CEO (+2)")
    elif "CFO" in title or "CHIEF FINANCIAL" in title:
        pts += 1; notes.append("CFO (+1)")

    return min(pts, 25), notes


# ── D2: Price / Technical Context (0-20) ──────────────────────────────────────

def score_price_context(event: dict) -> tuple[float, list[str]]:
    """
    EMA positioning + 5-day velocity.
    Buying during a pullback below both EMAs = highest conviction context.
    """
    pts = 0.0
    notes = []

    vs50  = event.get("price_vs_ema50_pct")
    vs200 = event.get("price_vs_ema200_pct")
    vel   = event.get("price_velocity_5d")

    # EMA50 position (0-8 pts)
    if vs50 is not None:
        if vs50 <= -20:
            pts += 8; notes.append(f"Price {abs(vs50):.1f}% below EMA50 — oversold (+8)")
        elif vs50 <= -10:
            pts += 6; notes.append(f"Price {abs(vs50):.1f}% below EMA50 (+6)")
        elif vs50 <= -3:
            pts += 4; notes.append(f"Price {abs(vs50):.1f}% below EMA50 (+4)")
        elif vs50 <= 5:
            pts += 2; notes.append(f"Price near EMA50 (+2)")
        else:
            pts += 0; notes.append(f"Price {vs50:.1f}% above EMA50 — extended (0)")

    # EMA200 position (0-8 pts)
    if vs200 is not None:
        if vs200 <= -20:
            pts += 8; notes.append(f"Price {abs(vs200):.1f}% below EMA200 — long-term discount (+8)")
        elif vs200 <= -10:
            pts += 6; notes.append(f"Price {abs(vs200):.1f}% below EMA200 (+6)")
        elif vs200 <= -3:
            pts += 4; notes.append(f"Price {abs(vs200):.1f}% below EMA200 (+4)")
        elif vs200 <= 5:
            pts += 2
        else:
            pts += 0

    # 5-day velocity (0-4 pts): stock falling into the buy = stronger signal
    if vel is not None:
        if vel <= -15:
            pts += 4; notes.append(f"Stock fell {abs(vel):.1f}% in 5d before buy — conviction buy (+4)")
        elif vel <= -7:
            pts += 3; notes.append(f"Stock down {abs(vel):.1f}% in 5d (+3)")
        elif vel <= -3:
            pts += 2; notes.append(f"Stock down {abs(vel):.1f}% in 5d (+2)")
        elif vel > 10:
            pts -= 2; notes.append(f"Stock up {vel:.1f}% in 5d — chasing momentum (-2)")

    return min(pts, 20), notes


# ── D3: Short Interest (0-10) ─────────────────────────────────────────────────

def score_short_interest(event: dict) -> tuple[float, list[str]]:
    """
    High short float = potential squeeze catalyst (positive) BUT
    extreme short float (>40%) = crowded short, elevated risk.
    Net: moderate short float maximises score.
    """
    sf = event.get("short_float_pct")
    if sf is None:
        return 3, ["Short float unknown (+3 neutral)"]

    if 10 <= sf <= 25:
        return 8, [f"Short float {sf:.1f}% — squeeze potential (+8)"]
    elif 25 < sf <= 40:
        return 6, [f"Short float {sf:.1f}% — high squeeze potential, elevated risk (+6)"]
    elif sf > 40:
        return 3, [f"Short float {sf:.1f}% — extreme short, binary risk (+3)"]
    elif 5 <= sf < 10:
        return 5, [f"Short float {sf:.1f}% — moderate (+5)"]
    else:
        return 2, [f"Short float {sf:.1f}% — low, limited squeeze (+2)"]


# ── D4: Cluster Bonus (0-10) ──────────────────────────────────────────────────

def score_cluster(event: dict) -> tuple[float, list[str]]:
    """
    Academic consensus: multi-insider cluster buys produce highest alpha.
    Lakonishok & Lee: cluster = 2x signal strength vs single insider.
    """
    tag = event.get("cluster_tag", "SINGLE")
    if tag == "CLUSTER":
        count = event.get("insider_count", 2)
        pts = min(6 + (count - 2) * 2, 10)
        return pts, [f"CLUSTER buy ({count} insiders) — highest-alpha pattern (+{pts:.0f})"]
    return 0, ["Single insider buy (0)"]


# ── D5-D7: AI Layer — Claude qualitative scoring ───────────────────────────────

_CLAUDE_SYSTEM = """\
You are a senior quantitative analyst at a top-tier hedge fund.
You score insider buy events using an institutional framework inspired by
BlackRock Systematic, Stockopedia QVM, and academic insider-signal research.

You will receive a structured data packet about a stock and its recent CEO/CFO
insider purchase. Score THREE dimensions:

D5  FUNDAMENTAL QUALITY (0-12 pts)
    Criteria: revenue growth trajectory, profitability direction (improving/declining),
    balance sheet strength (debt load, cash position), free cash flow generation,
    margin expansion or contraction.
    12 = excellent fundamentals, growing profitably, strong balance sheet
    0  = deteriorating revenue, losses widening, high debt, burning cash

D6  CATALYST STRENGTH (0-13 pts)
    Criteria: is there a specific, dated, near-term binary catalyst?
    FDA PDUFA date / clinical readout in <6 months = 11-13
    Earnings expected to beat with guidance raise = 8-10
    Specific corporate event (merger vote, product launch) = 6-8
    Vague "improving business" = 2-4
    No identifiable catalyst = 0-1

D7  NEWS / SENTIMENT ALIGNMENT (0-10 pts)
    Criteria: does the news corroborate the insider buy thesis?
    10 = news strongly supports buy, analyst upgrades, positive sector tailwind
    0  = news contradicts buy, analyst downgrades, negative sector trends

Respond ONLY with a valid JSON object — no prose, no markdown:
{
  "d5_fundamental_score": <int 0-12>,
  "d5_reasoning": "<2 sentences max>",
  "d6_catalyst_score": <int 0-13>,
  "d6_reasoning": "<2 sentences max — name the specific catalyst and date if known>",
  "d7_sentiment_score": <int 0-10>,
  "d7_reasoning": "<2 sentences max>",
  "key_risk": "<single biggest risk to this trade in one sentence>"
}\
"""


def _build_claude_prompt(event: dict) -> str:
    ticker   = event["ticker"]
    company  = event.get("company", "")
    role     = (event.get("title") or "").split(",")[0].strip()
    buy_px   = event.get("avg_price") or event.get("price") or 0
    total    = event.get("total_value", 0)
    delta    = event.get("delta_own", "N/A")
    vs50     = event.get("price_vs_ema50_pct")
    vs200    = event.get("price_vs_ema200_pct")
    vel      = event.get("price_velocity_5d")
    sf       = event.get("short_float_pct")
    headline = event.get("news_headline", "N/A")
    summary  = event.get("news_summary", "N/A")
    cats     = event.get("catalysts") or []
    sentiment= event.get("news_sentiment", "N/A")
    cluster  = event.get("cluster_tag", "SINGLE")

    return f"""TICKER: {ticker} — {company}
INSIDER BUY: {role} purchased ${total:,.0f} at ${buy_px:.2f}/share  ΔOwnership: {delta}
SIGNAL TYPE: {cluster}

PRICE CONTEXT:
  vs EMA50:  {f'{vs50:+.1f}%' if vs50 is not None else 'N/A'}
  vs EMA200: {f'{vs200:+.1f}%' if vs200 is not None else 'N/A'}
  5d velocity: {f'{vel:+.1f}%' if vel is not None else 'N/A'}
  Short float: {f'{sf:.1f}%' if sf is not None else 'N/A'}

NEWS RESEARCH:
  Sentiment: {sentiment}
  Headline: {headline}
  Summary: {summary}
  Catalysts identified:
{chr(10).join(f'    - {c}' for c in cats) if cats else '    - None identified'}

Score D5 (fundamentals), D6 (catalysts), D7 (news alignment) per the rubric."""


def score_qualitative(event: dict, api_key: str, model: str, timeout: int) -> tuple[dict, list[str]]:
    prompt = _build_claude_prompt(event)

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": model,
                "max_tokens": 512,
                "system": _CLAUDE_SYSTEM,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=timeout,
        )
        if not resp.ok:
            return {"d5": 0, "d6": 0, "d7": 0, "key_risk": "Claude API error — AI scores zeroed"}, \
                   [f"Claude error {resp.status_code}: AI scores set to 0"]

        data = resp.json()
        raw  = data["content"][0]["text"].strip()

        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"): raw = raw[4:]
            raw = raw.strip()

        parsed = json.loads(raw)
        scores = {
            "d5": int(parsed.get("d5_fundamental_score", 6)),
            "d6": int(parsed.get("d6_catalyst_score",   6)),
            "d7": int(parsed.get("d7_sentiment_score",  5)),
            "key_risk": parsed.get("key_risk", "N/A"),
        }
        notes = [
            f"[D5 Fundamentals +{scores['d5']}] {parsed.get('d5_reasoning','')}",
            f"[D6 Catalysts   +{scores['d6']}] {parsed.get('d6_reasoning','')}",
            f"[D7 Sentiment   +{scores['d7']}] {parsed.get('d7_reasoning','')}",
        ]
        return scores, notes

    except json.JSONDecodeError as e:
        return {"d5": 0, "d6": 0, "d7": 0, "key_risk": "JSON parse error — AI scores zeroed"}, \
               [f"Claude JSON error: {e}"]
    except Exception as e:
        return {"d5": 0, "d6": 0, "d7": 0, "key_risk": str(e)}, \
               [f"Claude exception: {e}"]


# ── Public entry point ─────────────────────────────────────────────────────────

def run(
    events: list[dict] | None = None,
    config_path: str = "config.yaml",
) -> list[dict]:
    with open(config_path) as f:
        cfg = yaml.safe_load(f)

    claude_cfg = cfg["anthropic"]
    api_key    = claude_cfg["api_key"]
    model      = claude_cfg.get("model", "claude-haiku-4-5-20251001")
    timeout    = claude_cfg.get("request_timeout", 60)

    if events is None:
        events = research_run(config_path=config_path)

    if not events:
        print("[score] No events to score.")
        return []

    # One Claude call per unique ticker (quant is per-event, same data)
    seen_tickers: dict[str, dict] = {}
    for e in events:
        if e["ticker"] not in seen_tickers:
            seen_tickers[e["ticker"]] = e

    print(f"[score] Scoring {len(events)} event(s) across {len(seen_tickers)} ticker(s)…")

    ai_cache: dict[str, tuple[dict, list]] = {}
    for ticker, event in seen_tickers.items():
        print(f"[score]   Claude qualitative → {ticker}")
        ai_cache[ticker] = score_qualitative(event, api_key, model, timeout)
        time.sleep(0.5)

    scored = []
    for event in events:
        ticker = event["ticker"]
        ai, n5 = ai_cache.get(ticker, ({"d5": 0, "d6": 0, "d7": 0, "key_risk": "N/A"}, []))

        d1, n1 = score_insider_signal(event)
        d2, n2 = score_price_context(event)
        d3, n3 = score_short_interest(event)
        d4, n4 = score_cluster(event)
        d5, d6, d7 = min(ai["d5"], 12), min(ai["d6"], 13), min(ai["d7"], 10)

        total = d1 + d2 + d3 + d4 + d5 + d6 + d7
        stars = composite_to_stars(total)

        scored.append({
            **event,
            "score_total":     round(total, 1),
            "score_stars":     stars,
            "score_label":     LABELS[stars],
            "score_breakdown": {
                "D1_insider_signal":  round(d1, 1),
                "D2_price_technical": round(d2, 1),
                "D3_short_interest":  round(d3, 1),
                "D4_cluster_bonus":   round(d4, 1),
                "D5_fundamentals":    d5,
                "D6_catalysts":       d6,
                "D7_sentiment":       d7,
            },
            "score_notes":    n1 + n2 + n3 + n4 + n5,
            "score_key_risk": ai.get("key_risk", "N/A"),
            "score_error":    None,
        })

    scored.sort(key=lambda x: -x["score_total"])
    print(f"[score] Done. Top pick: {scored[0]['ticker']} {STARS[scored[0]['score_stars']]} "
          f"({scored[0]['score_total']:.0f}/100)")
    return scored


# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Stage 5: Score insider buy events.")
    parser.add_argument("--json",   action="store_true")
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()

    results = run(config_path=args.config)

    if args.json:
        print(json.dumps(results, indent=2, default=str))
    else:
        if not results:
            print("No results.")
        else:
            W = 100
            print(f"\n{'═'*W}")
            print(f"  {'INSIDER TRACKER — SCORED RESULTS':^{W-2}}")
            print(f"{'═'*W}")
            for e in results:
                bd  = e["score_breakdown"]
                tag = f"[{e['cluster_tag']}]"
                role = (e.get("title") or "").split(",")[0].strip()

                print(f"\n  {STARS[e['score_stars']]}  {e['score_label']:<12}  "
                      f"{e['score_total']:.0f}/100   "
                      f"{e['ticker']:<6} {tag:<9} {role}")
                print(f"  {e.get('company','')}")
                print(f"  Bought: ${e.get('avg_price',0):.2f} × {e.get('total_qty',0):,} shares  "
                      f"Total: ${e.get('total_value',0):,.0f}  ΔOwn: {e.get('delta_own','N/A')}")
                print(f"  Now:    ${e.get('current_price',0):.2f} ({e.get('data_source','?')})  "
                      f"vs EMA50: {e.get('price_vs_ema50_pct',0):+.1f}%  "
                      f"vs EMA200: {e.get('price_vs_ema200_pct',0):+.1f}%  "
                      f"5d: {e.get('price_velocity_5d',0):+.1f}%  "
                      f"Short: {e.get('short_float_pct','N/A')}%")
                print(f"  Breakdown: "
                      f"Signal={bd['D1_insider_signal']}/25  "
                      f"Tech={bd['D2_price_technical']}/20  "
                      f"Short={bd['D3_short_interest']}/10  "
                      f"Cluster={bd['D4_cluster_bonus']}/10  "
                      f"Fund={bd['D5_fundamentals']}/12  "
                      f"Cat={bd['D6_catalysts']}/13  "
                      f"Sent={bd['D7_sentiment']}/10")
                if e.get("news_headline"):
                    print(f"  News:    {e['news_headline']}")
                if e.get("catalysts"):
                    print(f"  Cats:    {' | '.join(e['catalysts'][:2])}")
                print(f"  Risk:    {e.get('score_key_risk','N/A')}")
                print(f"  {'─'*W}")
            print()
