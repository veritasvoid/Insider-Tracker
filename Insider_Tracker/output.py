"""
output.py — Stage 6
Produces two deliverables from scored events:
  1. JSON  — machine-readable full data packet, saved to data/reports/
  2. MD    — human-readable daily briefing, saved to data/reports/
Also persists picks to the DB (picks table) for Stage 7 tracking.

Usage:
    python output.py          # run full pipeline, write files, print summary
    python output.py --json   # also dump raw JSON to stdout
"""

import json
import argparse
from datetime import datetime
from pathlib import Path

import yaml

import db
from score import run as score_run, STARS, LABELS
from web import write_html


REPORTS_DIR = Path(__file__).parent / "data" / "reports"


# ── JSON output ────────────────────────────────────────────────────────────────

def _clean_event(e: dict) -> dict:
    """Strip internal pipeline keys that aren't useful in the final JSON."""
    skip = {"trade_type", "filing_date", "enrich_error", "research_error", "score_error"}
    return {k: v for k, v in e.items() if k not in skip}


def write_json(events: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now().isoformat(),
        "event_count":  len(events),
        "events":       [_clean_event(e) for e in events],
    }
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


# ── Markdown briefing ──────────────────────────────────────────────────────────

def write_markdown(events: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    now   = datetime.now()
    lines = []

    lines.append(f"# Insider Tracker — Daily Briefing")
    lines.append(f"**Generated:** {now.strftime('%B %d, %Y  %H:%M')}\n")
    lines.append(f"**Picks today:** {len(events)}  |  "
                 f"**Top score:** {events[0]['score_total']:.0f}/100 "
                 f"({STARS[events[0]['score_stars']]} {events[0]['ticker']})\n")
    lines.append("---\n")

    # Deduplicate by ticker for summary table
    seen: set[str] = set()
    unique: list[dict] = []
    for e in events:
        if e["ticker"] not in seen:
            seen.add(e["ticker"])
            unique.append(e)

    # Summary table
    lines.append("## Summary\n")
    lines.append("| Rank | Stars | Score | Ticker | Signal | Buy $ | Now $ | EMA200 | Short% | Sentiment |")
    lines.append("|------|-------|-------|--------|--------|-------|-------|--------|--------|-----------|")
    for i, e in enumerate(unique, 1):
        sf  = f"{e['short_float_pct']:.1f}%" if e.get("short_float_pct") is not None else "N/A"
        tag = e.get("cluster_tag", "SINGLE")
        lines.append(
            f"| {i} | {STARS[e['score_stars']]} | {e['score_total']:.0f} "
            f"| **{e['ticker']}** | {tag} "
            f"| ${e.get('avg_price') or 0:.2f} | ${e.get('current_price') or 0:.2f} "
            f"| {e.get('price_vs_ema200_pct') or 0:+.1f}% "
            f"| {sf} | {(e.get('news_sentiment') or 'N/A').upper()} |"
        )

    lines.append("\n---\n")

    # Detailed cards — one per ticker
    for e in unique:
        bd      = e.get("score_breakdown", {})
        stars_s = STARS[e["score_stars"]]
        label   = LABELS[e["score_stars"]]
        role    = (e.get("title") or "").split(",")[0].strip()
        tag     = e.get("cluster_tag", "SINGLE")
        sf      = f"{e['short_float_pct']:.1f}%" if e.get("short_float_pct") is not None else "N/A"
        cats    = e.get("catalysts") or []
        sources = (e.get("news_sources") or [])[:2]

        lines.append(f"## {stars_s} {e['ticker']} — {label} ({e['score_total']:.0f}/100)")
        lines.append(f"**{e.get('company','')}** | {role} | {tag}\n")

        lines.append("### Trade")
        lines.append(f"- **Insider buy price:** ${e.get('avg_price') or 0:.2f} × "
                     f"{e.get('total_qty') or 0:,} shares")
        lines.append(f"- **Total value:** ${e.get('total_value') or 0:,.0f}")
        lines.append(f"- **Δ Ownership:** {e.get('delta_own','N/A')}")
        lines.append(f"- **Trade date:** {e.get('event_start_date','N/A')}\n")

        lines.append("### Price Context")
        lines.append(f"- **Current price:** ${e.get('current_price') or 0:.2f} "
                     f"({e.get('data_source','?')})")
        lines.append(f"- **vs EMA50:** {e.get('price_vs_ema50_pct') or 0:+.1f}%")
        lines.append(f"- **vs EMA200:** {e.get('price_vs_ema200_pct') or 0:+.1f}%")
        lines.append(f"- **5-day velocity:** {e.get('price_velocity_5d') or 0:+.1f}%")
        lines.append(f"- **Short float:** {sf}\n")

        lines.append("### Score Breakdown")
        lines.append(f"| Dimension | Score | Max |")
        lines.append(f"|-----------|-------|-----|")
        lines.append(f"| D1 Insider Signal | {bd.get('D1_insider_signal',0)} | 25 |")
        lines.append(f"| D2 Price/Technical | {bd.get('D2_price_technical',0)} | 20 |")
        lines.append(f"| D3 Short Interest | {bd.get('D3_short_interest',0)} | 10 |")
        lines.append(f"| D4 Cluster Bonus | {bd.get('D4_cluster_bonus',0)} | 10 |")
        lines.append(f"| D5 Fundamentals | {bd.get('D5_fundamentals',0)} | 12 |")
        lines.append(f"| D6 Catalysts | {bd.get('D6_catalysts',0)} | 13 |")
        lines.append(f"| D7 Sentiment | {bd.get('D7_sentiment',0)} | 10 |")
        lines.append(f"| **TOTAL** | **{e['score_total']:.0f}** | **100** |\n")

        lines.append("### Research")
        if e.get("news_headline"):
            lines.append(f"**Headline:** {e['news_headline']}\n")
        if e.get("news_summary"):
            lines.append(f"{e['news_summary']}\n")
        if cats:
            lines.append("**Catalysts:**")
            for c in cats:
                lines.append(f"- {c}")
            lines.append("")
        if e.get("score_key_risk"):
            lines.append(f"**Key Risk:** {e['score_key_risk']}\n")
        if sources:
            lines.append("**Sources:**")
            for url in sources:
                lines.append(f"- {url}")
            lines.append("")

        lines.append("---\n")

    path.write_text("\n".join(lines), encoding="utf-8")


# ── Public entry point ─────────────────────────────────────────────────────────

def run(
    events: list[dict] | None = None,
    config_path: str = "config.yaml",
) -> tuple[Path, Path]:
    """
    Run full pipeline → score → write JSON + MD reports → save picks to DB.
    Returns (json_path, md_path).
    """
    with open(config_path) as f:
        yaml.safe_load(f)   # validate config parseable

    if events is None:
        events = score_run(config_path=config_path)

    if not events:
        print("[output] No events to output.")
        return None, None

    ts      = datetime.now().strftime("%Y-%m-%d_%H-%M")
    j_path  = REPORTS_DIR / f"{ts}.json"
    md_path = REPORTS_DIR / f"{ts}.md"

    write_json(events, j_path)
    print(f"[output] JSON  → {j_path}")

    write_markdown(events, md_path)
    print(f"[output] MD    → {md_path}")

    # Persist to DB for forward tracking
    n = db.save_picks(events)
    print(f"[output] DB    → {n} new pick(s) saved to picks table.")

    # HTML dashboard
    try:
        from track import load_all_picks, update_picks
        with open(config_path) as _f:
            _cfg = yaml.safe_load(_f)
        update_picks(_cfg)
        all_picks = load_all_picks()
    except Exception as _e:
        print(f"[output] WARN  track/load_all_picks failed ({_e}); using empty history")
        all_picks = []
    html_path = write_html(events, all_picks)
    print(f"[output] HTML  → {html_path}")

    return j_path, md_path


# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Stage 6: Write reports and persist picks.")
    parser.add_argument("--json",   action="store_true", help="Dump raw JSON to stdout too")
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()

    scored = score_run(config_path=args.config)
    j_path, md_path = run(events=scored, config_path=args.config)

    if args.json and j_path:
        print(j_path.read_text(encoding="utf-8"))

    if md_path:
        print("\n" + "═" * 80)
        print(md_path.read_text(encoding="utf-8"))
