"""
web.py — Bloomberg-grade dashboard generator.
"""
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

DOCS_DIR = Path(__file__).parent.parent / "docs"
AZ = timezone(timedelta(hours=-7))   # Arizona MST — permanent, no DST

SCORE_COLORS = {5:"#00ffd4", 4:"#2979ff", 3:"#fbbf24", 2:"#ff6b35", 1:"#ff4757"}
STAR_LABELS  = {5:"STRONG BUY", 4:"BUY", 3:"WATCH", 2:"WEAK", 1:"SKIP"}

SECTOR_PALETTE = {
    "Healthcare":             ("#f0abfc","#4a044e"),
    "Biotechnology":          ("#c4b5fd","#2e1065"),
    "Technology":             ("#7dd3fc","#082f49"),
    "Financial":              ("#6ee7b7","#022c22"),
    "Consumer Cyclical":      ("#fdba74","#431407"),
    "Consumer Defensive":     ("#fde68a","#451a03"),
    "Energy":                 ("#fef08a","#422006"),
    "Industrials":            ("#cbd5e1","#0f172a"),
    "Basic Materials":        ("#bef264","#1a2e05"),
    "Real Estate":            ("#fca5a5","#450a0a"),
    "Utilities":              ("#67e8f9","#083344"),
    "Communication Services": ("#d8b4fe","#3b0764"),
    "Education":              ("#86efac","#052e16"),
}

CAT_TYPE = {
    "FDA":"teal","PDUFA":"teal","PHASE":"teal","NDA":"teal","TRIAL":"teal",
    "EARNINGS":"blue","REVENUE":"blue","GUIDANCE":"blue","EPS":"blue",
    "EGM":"amber","AGM":"amber","MERGER":"amber","ACQUI":"amber",
    "NYSE":"red","NASDAQ":"red","COMPLIANCE":"red","DELISTING":"red",
}
CAT_COLORS = {
    "teal":  ("#00ffd4","rgba(0,255,212,0.1)","rgba(0,255,212,0.35)"),
    "blue":  ("#5b9bff","rgba(91,155,255,0.1)","rgba(91,155,255,0.35)"),
    "amber": ("#fbbf24","rgba(251,191,36,0.1)","rgba(251,191,36,0.35)"),
    "red":   ("#ff4757","rgba(255,71,87,0.1)","rgba(255,71,87,0.35)"),
    "def":   ("#8892a4","rgba(136,146,164,0.1)","rgba(136,146,164,0.25)"),
}

# ── helpers ────────────────────────────────────────────────────────────────────

def _az_now():
    return datetime.now(AZ)

def _pct(v):
    if v is None: return "—"
    return f"{'+'if v>=0 else ''}{v:.1f}%"

def _dcolor(v):
    if v is None: return "var(--muted)"
    return "#00ffd4" if v > 0 else "#ff4757" if v < 0 else "var(--muted)"

def _fmtval(v):
    if v is None: return "—"
    if v >= 1_000_000: return f"${v/1_000_000:.1f}M"
    if v >= 1_000:     return f"${v/1_000:.0f}K"
    return f"${v:.2f}"

def _ret(base, price):
    if not base or not price: return None, "—"
    r = (price - base) / base * 100
    return r, f"{'+'if r>0 else ''}{r:.1f}%"

# ── score ring — CSS conic-gradient, zero SVG, zero artifact ──────────────────

def _ring(score, stars):
    color = SCORE_COLORS.get(stars, "#888")
    label = STAR_LABELS.get(stars, "WATCH")
    pct   = min(score, 100)
    return f"""<div class="ring-outer" data-score="{pct}" data-color="{color}">
  <div class="ring-track"></div>
  <div class="ring-fill"></div>
  <div class="ring-text">
    <span class="ring-num" style="color:{color}">{pct:.0f}</span>
    <span class="ring-den">/100</span>
    <span class="ring-lbl" style="color:{color}">{label}</span>
  </div>
</div>"""

# ── sector badge ───────────────────────────────────────────────────────────────

def _sector(sector):
    if not sector: return ""
    fg, bg = SECTOR_PALETTE.get(sector, ("#94a3b8","#1e293b"))
    return (f'<div class="sector-tag" style="color:{fg};background:{bg};'
            f'border-color:color-mix(in srgb,{fg} 40%,transparent)">'
            f'{sector}</div>')

# ── catalyst pills ─────────────────────────────────────────────────────────────

def _cats_inline(cats):
    if not cats:
        return '<span class="no-cats">No near-term catalysts</span>'
    pills = ""
    for c in cats:
        t = "def"
        for kw, ct in CAT_TYPE.items():
            if kw in c.upper():
                t = ct; break
        fg, bg, border = CAT_COLORS[t]
        pills += (f'<span class="cat-chip" style="color:{fg};background:{bg};border-color:{border}">'
                  f'{c}</span>')
    return pills

# ── pick card ──────────────────────────────────────────────────────────────────

def _card(e, rank):
    stars  = e.get("score_stars", 3)
    color  = SCORE_COLORS.get(stars, "#888")
    role   = (e.get("title") or "").split(",")[0].strip()
    tag    = e.get("cluster_tag", "SINGLE")
    def _f(v): return float(v) if v is not None else None
    buy_px = float(e.get("avg_price") or e.get("price") or 0)
    sf     = _f(e.get("short_float_pct"))
    vs50   = _f(e.get("price_vs_ema50_pct"))
    vs200  = _f(e.get("price_vs_ema200_pct"))
    vel    = _f(e.get("price_velocity_5d"))

    srcs = "".join(
        f'<a href="{u}" target="_blank" class="src-a">↗{i+1}</a>'
        for i, u in enumerate((e.get("news_sources") or [])[:2])
    )

    sent = (e.get("news_sentiment") or "neutral").upper()
    scls = {"BULLISH":"s-bull","BEARISH":"s-bear"}.get(sent,"s-neut")

    cluster_badge = (
        f'<span class="cluster-tag">⚡ CLUSTER · {e.get("insider_count",2)} insiders</span>'
        if tag == "CLUSTER" else
        f'<span class="cluster-tag repeat-tag">🔄 REPEAT · {e.get("n_filings",3)} buys</span>'
        if tag == "REPEAT" else ""
    )

    sub_parts = []
    if e.get("total_value"):
        sub_parts.append(_fmtval(e.get("total_value")) + " total")
    if e.get("delta_own"):
        sub_parts.append(f'Δ own {e.get("delta_own")}')
    trade_sub = " · ".join(sub_parts)

    meta_parts = [f'<span class="meta-role">{role}</span>'] if role else []
    if e.get("event_start_date"):
        meta_parts += [f'<span class="meta-date">Trade {e.get("event_start_date")}</span>']
    if e.get("sector"):
        meta_parts += [_sector(e.get("sector"))]
    meta_html = " ".join(meta_parts)

    hl = e.get("news_headline") or ""
    hl_html = f'<span class="news-hl">"{hl}"</span>{" " + srcs if srcs else ""}' if hl else srcs

    return f"""<article class="card" style="--ac:{color}">
  <div class="card-stripe"></div>
  <div class="card-body">

    <div class="col-id">
      <div class="ci-header">
        <span class="card-rank">#{rank}</span>
        <span class="card-ticker">{e["ticker"]}</span>
        <span class="sent {scls}">{sent}</span>
      </div>
      <div class="card-co">{e.get("company","")}</div>
      <div class="b1-meta">{meta_html}</div>
      <div class="ci-buyprice">
        <span class="px-buy">${buy_px:.2f}</span>
        <span class="px-buy-lbl">buy price</span>
      </div>
      {f'<div class="b2-sub">{trade_sub}</div>' if trade_sub else ""}
    </div>

    <div class="col-score">
      {_ring(e.get("score_total",0), stars)}
      {f'<div class="ci-cluster">{cluster_badge}</div>' if cluster_badge else ""}
      <div class="b2-tech">
        <div class="tc"><span class="tk">EMA50</span><span class="tv" style="color:{_dcolor(vs50)}">{_pct(vs50)}</span></div>
        <div class="tc"><span class="tk">EMA200</span><span class="tv" style="color:{_dcolor(vs200)}">{_pct(vs200)}</span></div>
        <div class="tc"><span class="tk">Vel 5d</span><span class="tv" style="color:{_dcolor(vel)}">{_pct(vel)}</span></div>
        <div class="tc"><span class="tk">Short</span><span class="tv">{f"{sf:.1f}%" if sf is not None else "—"}</span></div>
      </div>
    </div>

    <div class="col-news">
      {f'<div class="news-line">{hl_html}</div>' if hl_html else ""}
      {f'<p class="news-body">{e.get("news_summary","")}</p>' if e.get("news_summary") else ""}
    </div>

    <div class="col-cats">
      <div class="cats-lbl">CATALYSTS</div>
      <div class="cats-chips">{_cats_inline(e.get("catalysts") or [])}</div>
      <div class="risk-line"><span class="risk-icon">⚠</span> {e.get("score_key_risk","—")}</div>
    </div>

  </div>
</article>"""

def _history(picks):
    import json as _json
    if not picks:
        return '<div class="empty">No history yet.</div>'

    # One card per ticker, most recently seen first
    sorted_picks = sorted(
        picks,
        key=lambda p: (p.get("first_seen") or p.get("run_date") or ""),
        reverse=True
    )

    out = ""
    for p in sorted_picks:
        sc      = p.get("score") or 0
        st      = p.get("score_stars") or 3
        col     = SCORE_COLORS.get(st, "#888")
        lbl     = STAR_LABELS.get(st, "WATCH")
        tag     = p.get("cluster_tag", "SINGLE")
        bp      = p.get("price_at_pick")
        buyers  = p.get("distinct_buyers") or 1
        first   = p.get("first_seen") or p.get("run_date", "")
        last    = p.get("last_updated") or first

        rets = ""
        for col2, lp in [("price_3d","3d"),("price_8d","8d"),("price_15d","15d"),
                          ("price_30d","30d"),("price_90d","90d")]:
            r, rs = _ret(bp, p.get(col2))
            rc = "#00ffd4" if r and r > 0 else "#ff4757" if r and r < 0 else "var(--muted)"
            rets += f'<div class="hr"><span style="color:{rc}">{rs}</span><span class="hrl">{lp}</span></div>'

        # Parse purchases JSON into individual buy rows
        purchases = []
        try:
            purchases = _json.loads(p.get("purchases") or "[]")
        except Exception:
            pass

        n_buys    = len(purchases) or 1
        total_val = p.get("total_value") or sum(pur.get("value", 0) for pur in purchases)

        buy_rows = ""
        for pur in purchases:
            td    = (pur.get("trade_date") or "")[:10]
            name  = pur.get("insider_name", "")
            role  = pur.get("title", "").split(",")[0].strip()
            price = pur.get("price") or 0
            qty   = pur.get("qty") or 0
            val   = pur.get("value") or 0
            delt  = pur.get("delta_own", "")
            buy_rows += f"""<div class="buy-row">
  <span class="br-date">{td}</span>
  <span class="br-name">{name}</span>
  <span class="br-role">{role}</span>
  <span class="br-px">${price:,.2f}</span>
  <span class="br-qty">{qty:,} sh</span>
  <span class="br-val">{_fmtval(val)}</span>
  <span class="br-dow">{delt}</span>
</div>"""

        cluster_badge = (
            f'<span class="hcl">⚡ CLUSTER · {buyers} insiders</span>'
            if tag == "CLUSTER" else
            f'<span class="hcl" style="color:#a78bfa">🔄 REPEAT</span>'
            if tag == "REPEAT" else ""
        )

        out += f"""<div class="hrow" style="border-left-color:{col}">
  <div class="hrow-hdr">
    <div class="hrl-l">
      <span class="htk" style="color:{col}">{p.get("ticker","")}</span>
      <span class="hsi" style="color:{col}">{lbl}</span>
      <span class="hsc">{sc}</span>
      {cluster_badge}
    </div>
    <div class="hrow-meta">
      <span class="hm-v">{_fmtval(total_val)} total · {n_buys} buy{"s" if n_buys != 1 else ""}</span>
      <span class="hm-d">First: {first}&nbsp;·&nbsp;Last: {last}</span>
    </div>
    <div class="hrl-r">{rets}</div>
  </div>
  <div class="hrow-buys">{buy_rows}</div>
</div>"""

    return out

# ── performance section ────────────────────────────────────────────────────────

def _perf(picks):
    import json as _json
    if not picks:
        return '<div class="empty">No tracked picks yet.</div>'

    intervals = [("price_3d","3d"),("price_8d","8d"),("price_15d","15d"),
                 ("price_30d","30d"),("price_90d","90d")]

    stats = ""
    for col, lbl in intervals:
        rets = [_ret(p.get("price_at_pick"), p.get(col))[0]
                for p in picks if p.get("price_at_pick") and p.get(col) is not None]
        if rets:
            avg  = sum(rets)/len(rets)
            wins = sum(1 for r in rets if r > 0)
            c    = "#00ffd4" if avg > 0 else "#ff4757"
            stats += f"""<div class="pst">
  <div class="pst-l">{lbl} avg</div>
  <div class="pst-v" style="color:{c}">{"+"if avg>0 else ""}{avg:.1f}%</div>
  <div class="pst-s">{wins}/{len(rets)} wins</div>
</div>"""

    rows = ""
    for p in sorted(picks,
                    key=lambda x: (x.get("first_seen") or x.get("run_date",""),
                                   -(x.get("score") or 0)),
                    reverse=True):
        st      = p.get("score_stars") or 3
        col     = SCORE_COLORS.get(st,"#888")
        lbl     = STAR_LABELS.get(st,"WATCH")
        tag     = p.get("cluster_tag","SINGLE")
        bp      = p.get("price_at_pick")
        buyers  = p.get("distinct_buyers") or 1
        first   = p.get("first_seen") or p.get("run_date","")

        purchases = []
        try:
            purchases = _json.loads(p.get("purchases") or "[]")
        except Exception:
            pass
        n_buys = len(purchases) or 1

        rcells = ""
        for c2, _ in intervals:
            r, rs = _ret(bp, p.get(c2))
            rc = "#00ffd4" if r and r>0 else "#ff4757" if r and r<0 else "var(--muted)"
            rcells += f'<td style="color:{rc};font-weight:600">{rs}</td>'

        cluster_cell = (
            f'<span class="tcl">⚡ {buyers}</span>' if tag == "CLUSTER" else "—"
        )

        rows += f"""<tr>
  <td class="td-m">{first}</td>
  <td><b style="color:{col}">{p.get("ticker","")}</b></td>
  <td style="color:{col}">{lbl}</td>
  <td style="color:{col};font-weight:700">{p.get("score") or 0}</td>
  <td>{cluster_cell}</td>
  <td class="td-m">{n_buys} buy{"s" if n_buys!=1 else ""}</td>
  <td class="td-m">${p.get("buy_price") or 0:.2f}</td>
  <td class="td-m">${bp or 0:.2f}</td>
  {rcells}
</tr>"""

    return f"""<div class="pstats">{stats or '<p class="empty" style="padding:16px">Returns populate as positions age.</p>'}</div>
<div class="ptw">
  <table class="ptbl">
    <thead><tr>
      <th>FIRST SEEN</th><th>TICKER</th><th>SIGNAL</th><th>SCORE</th><th>TYPE</th>
      <th>BUYS</th><th>AVG BUY&nbsp;$</th><th>PICK&nbsp;$</th>
      <th>+3D</th><th>+8D</th><th>+15D</th><th>+30D</th><th>+90D</th>
    </tr></thead>
    <tbody>{rows}</tbody>
  </table>
</div>"""

# ── CSS ────────────────────────────────────────────────────────────────────────

CSS = """
@import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600;700;800&family=DM+Sans:ital,wght@0,400;0,500;0,600;0,700;1,400&display=swap');

:root {
  --bg:    #050912;
  --card:  rgba(7,14,34,0.74);
  --teal:  #00ffd4;
  --amber: #fbbf24;
  --text:  #dde4f0;
  --muted: #6b7892;
  --bdr:   rgba(255,255,255,0.07);
  --mono:  'JetBrains Mono', monospace;
  --body:  'DM Sans', system-ui, sans-serif;
}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
html{scroll-behavior:smooth}
body{
  background:var(--bg); color:var(--text);
  font-family:var(--body); font-size:14px; line-height:1.55;
  min-height:100vh; overflow-x:hidden;
}
a{color:var(--amber);text-decoration:none}
a:hover{text-decoration:underline}

/* ── Static background depth ── */
body::before{
  content:''; position:fixed; inset:0; z-index:0; pointer-events:none;
  background:
    radial-gradient(ellipse 70% 60% at 10% 30%, rgba(41,121,255,0.10) 0%, transparent 65%),
    radial-gradient(ellipse 60% 50% at 90% 15%, rgba(0,255,212,0.07) 0%, transparent 65%),
    radial-gradient(ellipse 50% 60% at 50% 95%, rgba(100,0,255,0.05) 0%, transparent 65%);
}
.layout{position:relative;z-index:1}

/* ── Header ── */
.hdr{
  display:flex;align-items:center;justify-content:space-between;
  padding:16px 40px;
  background:rgba(5,9,18,0.9);
  backdrop-filter:blur(28px);-webkit-backdrop-filter:blur(28px);
  border-bottom:1px solid var(--bdr);
  position:sticky;top:0;z-index:100;
}
.brand{display:flex;align-items:center;gap:14px}
.brand-icon{font-size:26px}
.brand-name{
  font-family:var(--mono);font-size:17px;font-weight:800;
  letter-spacing:4px;color:var(--amber);
  text-shadow:0 0 24px rgba(251,191,36,0.35);
}
.brand-sub{font-size:10px;color:var(--muted);letter-spacing:1.5px;margin-top:2px}
.clock{text-align:right}
.clock-t{
  font-family:var(--mono);font-size:17px;font-weight:700;
  color:var(--text);letter-spacing:1px;
}
.clock-z{font-size:10px;color:var(--muted);letter-spacing:1.5px;margin-top:2px}

/* ── Nav ── */
.nav{
  display:flex;padding:0 40px;
  background:rgba(5,9,18,0.75);
  backdrop-filter:blur(14px);
  border-bottom:1px solid var(--bdr);
}
.tab-btn{
  font-family:var(--mono);font-size:11px;font-weight:700;
  letter-spacing:2px;text-transform:uppercase;
  padding:15px 26px;cursor:pointer;background:none;border:none;
  color:var(--muted);border-bottom:2px solid transparent;
  transition:color .18s,border-color .18s;
}
.tab-btn:hover{color:var(--text)}
.tab-btn.active{color:var(--amber);border-bottom-color:var(--amber)}

/* ── Panes ── */
.pane{display:none;padding:36px 40px}
.pane.active{display:block}

/* ── Hero row ── */
.hero{
  display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:32px;
}
.hc{
  background:var(--card);
  backdrop-filter:blur(24px);-webkit-backdrop-filter:blur(24px);
  border:1px solid var(--bdr);
  border-top:1px solid rgba(255,255,255,0.10);
  border-radius:14px;padding:20px 22px;
  box-shadow:inset 0 1px 0 rgba(255,255,255,0.06),0 8px 32px rgba(0,0,0,0.4);
}
.hc-l{font-size:10px;font-weight:700;letter-spacing:1.5px;color:var(--muted);text-transform:uppercase}
.hc-v{
  font-family:var(--mono);font-size:34px;font-weight:800;
  color:var(--amber);margin:6px 0 2px;line-height:1;
}
.hc-s{font-size:12px;color:var(--muted)}

/* ── Section label ── */
.sec{
  font-family:var(--mono);font-size:10px;font-weight:700;
  letter-spacing:2.5px;text-transform:uppercase;color:var(--muted);
  margin-bottom:20px;display:flex;align-items:center;gap:12px;
}
.sec::after{content:'';flex:1;height:1px;background:var(--bdr)}

/* ── PICK CARD ── */
.picks{display:flex;flex-direction:column;gap:14px}

.card{
  position:relative;border-radius:16px;overflow:hidden;
  background:var(--card);
  backdrop-filter:blur(32px) saturate(170%);
  -webkit-backdrop-filter:blur(32px) saturate(170%);
  box-shadow:
    0 24px 56px rgba(0,0,0,0.5),
    0  6px 20px rgba(0,0,0,0.3),
    inset 0 1px 0 rgba(255,255,255,0.07);
}
.card{border-color:color-mix(in srgb,var(--ac) 22%,transparent)}
.card{border-top-color:color-mix(in srgb,var(--ac) 44%,transparent)}
.card-stripe{
  position:absolute;left:0;top:0;bottom:0;width:4px;
  background:linear-gradient(180deg,var(--ac),color-mix(in srgb,var(--ac) 20%,transparent));
}
.card-body{display:flex;flex-direction:row;align-items:stretch;padding:0;gap:0}
.col-id{width:220px;flex-shrink:0;padding:16px 16px 16px 26px;border-right:1px solid var(--bdr);display:flex;flex-direction:column;gap:5px}
.ci-header{display:flex;align-items:center;gap:7px;flex-wrap:wrap}
.card-rank{font-family:var(--mono);font-size:11px;color:var(--muted)}
.card-ticker{font-family:var(--mono);font-size:28px;font-weight:800;line-height:1;color:var(--text);letter-spacing:-0.5px}
.card-co{font-size:12px;color:var(--muted);margin-top:2px}
.b1-meta{display:flex;flex-direction:column;gap:3px;font-size:11px;color:var(--muted)}
.meta-role{color:var(--muted)}
.meta-date{font-weight:600;color:var(--text)}
.ci-buyprice{display:flex;align-items:baseline;gap:6px;margin-top:6px}
.px-buy{font-family:var(--mono);font-size:20px;font-weight:800;color:var(--text)}
.px-buy-lbl{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:0.5px}
.b2-sub{font-size:11px;color:var(--muted)}
.sent{font-family:var(--mono);font-size:10px;font-weight:700;letter-spacing:1px;padding:2px 7px;border-radius:4px}
.s-bull{color:#00ffd4;background:rgba(0,255,212,0.1)}
.s-bear{color:#ff4757;background:rgba(255,71,87,0.1)}
.s-neut{color:var(--muted);background:rgba(107,120,146,0.1)}
.cluster-tag{font-family:var(--mono);font-size:10px;font-weight:700;color:var(--amber);background:rgba(251,191,36,0.1);border:1px solid rgba(251,191,36,0.3);border-radius:5px;padding:2px 8px}
.repeat-tag{color:#a78bfa;background:rgba(167,139,250,0.1);border-color:rgba(167,139,250,0.3)}
.sector-tag{display:inline-flex;align-items:center;font-family:var(--mono);font-size:10px;font-weight:700;letter-spacing:0.5px;padding:2px 7px;border-radius:4px;border:1px solid}
.col-score{width:220px;flex-shrink:0;padding:16px;border-right:1px solid var(--bdr);display:flex;flex-direction:column;align-items:center;gap:10px}
.ci-cluster{width:100%;text-align:center}
.b2-tech{display:flex;flex-direction:column;gap:5px;width:100%}
.tc{display:flex;justify-content:space-between;align-items:baseline;gap:6px}
.tk{font-size:11px;color:var(--muted);white-space:nowrap}
.tv{font-family:var(--mono);font-weight:700;font-size:12px;font-variant-numeric:tabular-nums}
.ring-outer{position:relative;width:64px;height:64px;flex-shrink:0;border-radius:50%}
.ring-track{position:absolute;inset:0;border-radius:50%;border:7px solid rgba(255,255,255,0.08)}
.ring-fill{position:absolute;inset:0;border-radius:50%;background:conic-gradient(transparent 0deg,transparent 360deg);-webkit-mask:radial-gradient(transparent 24px,black 25px);mask:radial-gradient(transparent 24px,black 25px)}
.ring-text{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;pointer-events:none}
.ring-num{font-family:var(--mono);font-size:16px;font-weight:800;line-height:1}
.ring-den{display:none}
.ring-lbl{font-family:var(--mono);font-size:7px;font-weight:800;letter-spacing:1px;margin-top:2px}
.col-news{flex:1;min-width:0;padding:16px 20px;border-right:1px solid var(--bdr);display:flex;flex-direction:column;gap:8px}
.news-line{font-size:13px;line-height:1.55;display:flex;align-items:baseline;gap:7px;flex-wrap:wrap}
.news-hl{font-style:italic;font-weight:600;color:#c8d5e8}
.news-body{font-size:12px;color:var(--muted);line-height:1.7}
.src-a{font-family:var(--mono);font-size:10px;color:var(--muted);padding:2px 6px;border:1px solid var(--bdr);border-radius:4px;white-space:nowrap}
.src-a:hover{color:var(--amber);border-color:rgba(251,191,36,0.4);text-decoration:none}
.col-cats{width:200px;flex-shrink:0;padding:16px 18px;display:flex;flex-direction:column;gap:7px}
.cats-lbl{font-family:var(--mono);font-size:9px;font-weight:700;letter-spacing:1.5px;color:var(--muted)}
.cats-chips{display:flex;flex-direction:column;gap:5px;flex:1}
.cat-chip{display:block;font-size:11px;font-weight:600;padding:3px 10px;border-radius:5px;border:1px solid}
.no-cats{font-size:11px;color:var(--muted);font-style:italic}
.risk-line{font-size:11px;color:#fca5a5;line-height:1.45;display:flex;align-items:flex-start;gap:5px;padding-top:8px;border-top:1px solid rgba(255,71,87,0.15);margin-top:auto}
.risk-icon{font-size:11px;flex-shrink:0}
/* ── History ── */
.hgroup{margin-bottom:28px}
.hg-hdr{display:flex;align-items:center;justify-content:space-between;margin-bottom:10px}
.hg-date{font-family:var(--mono);font-size:12px;font-weight:700;color:var(--text)}
.hg-cnt{font-family:var(--mono);font-size:11px;color:var(--muted)}
.hrow{
  display:flex;align-items:center;gap:16px;flex-wrap:wrap;
  background:var(--card);
  backdrop-filter:blur(12px);
  border:1px solid var(--bdr);border-left:3px solid;
  border-radius:10px;padding:12px 16px;margin-bottom:6px;
}
.hrl-l{display:flex;align-items:center;gap:10px;min-width:210px}
.htk{font-family:var(--mono);font-size:20px;font-weight:800;min-width:55px}
.hsi{font-family:var(--mono);font-size:10px;font-weight:700;letter-spacing:1px}
.hsc{font-family:var(--mono);font-size:12px;color:var(--muted)}
.hcl{
  font-family:var(--mono);font-size:10px;color:var(--amber);
  background:rgba(251,191,36,0.1);padding:2px 7px;border-radius:4px;
}
.hrl-p{
  display:flex;align-items:center;gap:6px;
  font-family:var(--mono);font-size:12px;color:var(--muted);
}
.hp-a{opacity:.4}
.hrl-r{display:flex;gap:14px;margin-left:auto;flex-wrap:wrap}
.hr{display:flex;flex-direction:column;align-items:center;gap:2px}
.hr span{font-family:var(--mono);font-size:12px;font-weight:700}
.hrl{font-size:10px;color:var(--muted);letter-spacing:.5px}

/* ── Performance ── */
.pstats{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:20px}
.pst{
  background:var(--card);backdrop-filter:blur(12px);
  border:1px solid var(--bdr);border-radius:10px;
  padding:14px 18px;min-width:105px;
  box-shadow:inset 0 1px 0 rgba(255,255,255,0.05);
}
.pst-l{font-family:var(--mono);font-size:10px;color:var(--muted);letter-spacing:1px}
.pst-v{font-family:var(--mono);font-size:22px;font-weight:800;margin:4px 0 2px}
.pst-s{font-size:11px;color:var(--muted)}
.ptw{overflow-x:auto;border-radius:12px;border:1px solid var(--bdr)}
.ptbl{width:100%;border-collapse:collapse;font-size:13px}
.ptbl th{
  text-align:left;padding:11px 14px;
  font-family:var(--mono);font-size:9px;font-weight:700;
  letter-spacing:1.5px;text-transform:uppercase;color:var(--muted);
  background:rgba(255,255,255,0.02);border-bottom:1px solid var(--bdr);
  white-space:nowrap;
}
.ptbl td{padding:11px 14px;border-bottom:1px solid rgba(255,255,255,0.03)}
.ptbl tr:last-child td{border-bottom:none}
.ptbl tr:hover td{background:rgba(255,255,255,0.02)}
.td-m{font-family:var(--mono);font-size:12px;color:var(--muted)}
.tcl{font-family:var(--mono);font-size:10px;color:var(--amber)}

/* ── History row v2: header + purchases sub-list ── */
.hrow-hdr{
  display:flex;align-items:center;gap:14px;flex-wrap:wrap;
  padding-bottom:10px;border-bottom:1px solid var(--bdr);margin-bottom:8px;
}
.hrow-meta{
  display:flex;flex-direction:column;gap:3px;
  font-size:11px;color:var(--muted);margin-left:4px;
}
.hm-v{font-family:var(--mono);color:var(--text);font-weight:600}
.hm-d{letter-spacing:.2px}

/* Individual purchase rows */
.hrow-buys{display:flex;flex-direction:column;gap:4px}
.buy-row{
  display:flex;align-items:center;gap:12px;flex-wrap:wrap;
  padding:6px 10px;
  background:rgba(255,255,255,0.025);
  border:1px solid rgba(255,255,255,0.05);
  border-radius:7px;
  font-size:12px;
}
.br-date{font-family:var(--mono);font-size:11px;color:var(--muted);min-width:76px}
.br-name{font-weight:600;color:var(--text);flex:1;min-width:120px}
.br-role{font-size:11px;color:var(--muted);min-width:50px}
.br-px{font-family:var(--mono);color:var(--teal);font-weight:700;min-width:56px}
.br-qty{font-family:var(--mono);font-size:11px;color:var(--muted);min-width:80px}
.br-val{font-family:var(--mono);font-weight:700;color:var(--text);min-width:56px}
.br-dow{font-family:var(--mono);font-size:11px;color:var(--amber);min-width:44px}

/* ── Empty ── */
.empty{
  text-align:center;color:var(--muted);padding:56px 24px;
  border:1px dashed var(--bdr);border-radius:14px;font-size:13px;
}

/* ── Footer ── */
footer{
  border-top:1px solid var(--bdr);padding:18px 40px;
  display:flex;justify-content:space-between;
  font-family:var(--mono);font-size:10px;color:var(--muted);letter-spacing:.5px;
}

/* ── Responsive ── */
@media(max-width:1100px){
  .hero{grid-template-columns:repeat(2,1fr)}
  .card-metrics{grid-template-columns:repeat(3,1fr)}
}
@media(max-width:700px){
  .hdr,.nav,.pane{padding-left:16px;padding-right:16px}
  .hero{grid-template-columns:1fr 1fr;gap:8px}
  .card-head{flex-direction:column}
  .card-metrics{grid-template-columns:repeat(2,1fr)}
  .hrl-r{display:none}
}
"""

JS = """
// ── Tab switching ──────────────────────────────────────────────────────────────
document.querySelectorAll('.tab-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.pane').forEach(p => p.classList.remove('active'));
    btn.classList.add('active');
    document.getElementById(btn.dataset.tab).classList.add('active');
  });
});

// ── Score ring animation (conic-gradient, RAF, plays once) ───────────────────
(function animateRings() {
  document.querySelectorAll('.ring-outer[data-score]').forEach(wrap => {
    const fill   = wrap.querySelector('.ring-fill');
    if (!fill) return;
    const score  = parseFloat(wrap.dataset.score  || 0);
    const color  = wrap.dataset.color || '#00ffd4';
    const target = score / 100 * 360;
    const TRACK  = 'transparent';
    const DUR    = 1100;
    const t0     = performance.now();

    function tick(now) {
      const p   = Math.min((now - t0) / DUR, 1.0);
      const ease = 1 - Math.pow(1 - p, 3);     // cubic ease-out
      const deg  = ease * target;
      fill.style.background =
        `conic-gradient(${color} ${deg.toFixed(2)}deg, ${TRACK} ${deg.toFixed(2)}deg)`;
      if (p < 1) requestAnimationFrame(tick);
    }
    setTimeout(() => requestAnimationFrame(tick), 300);
  });
}());

// ── Live Arizona clock ────────────────────────────────────────────────────────
(function clock() {
  const el = document.getElementById('az-clock');
  function tick() {
    if (el) el.textContent = new Date().toLocaleString('en-US', {
      timeZone: 'America/Phoenix',
      weekday:'short', month:'short', day:'numeric',
      hour:'2-digit', minute:'2-digit', second:'2-digit', hour12: false
    });
  }
  tick();
  setInterval(tick, 1000);
}());
"""

# ── generate ───────────────────────────────────────────────────────────────────

def generate_html(events, all_picks):
    now = _az_now()

    seen = {}
    for e in events:
        t = e["ticker"]
        if t not in seen or (e.get("score_total",0) > seen[t].get("score_total",0)):
            seen[t] = e
    unique  = sorted(seen.values(), key=lambda x: -x.get("score_total",0))
    n       = len(unique)
    top     = unique[0] if unique else {}
    cluster = sum(1 for e in unique if e.get("cluster_tag") == "CLUSTER")
    tracked = len(all_picks)

    hero = f"""
<div class="hc"><div class="hc-l">Today's Picks</div><div class="hc-v">{n}</div><div class="hc-s">Unique tickers</div></div>
<div class="hc"><div class="hc-l">Cluster Signals</div><div class="hc-v">{cluster}</div><div class="hc-s">Multi-insider buys</div></div>
<div class="hc"><div class="hc-l">Top Pick</div><div class="hc-v" style="font-size:22px">{top.get("ticker","—")}&nbsp;·&nbsp;{int(top.get("score_total",0))}</div><div class="hc-s">{STAR_LABELS.get(top.get("score_stars",3),"")}</div></div>
<div class="hc"><div class="hc-l">Tracked Positions</div><div class="hc-v">{tracked}</div><div class="hc-s">In performance log</div></div>"""

    cards = "\n".join(_card(e, i+1) for i, e in enumerate(unique)) or \
            '<div class="empty">No qualifying picks today.</div>'

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Insider Tracker · {now.strftime('%b %d %Y')}</title>
<style>{CSS}</style>
</head>
<body>
<div class="layout">

<header class="hdr">
  <div class="brand">
    <div class="brand-icon">📈</div>
    <div>
      <div class="brand-name">INSIDER TRACKER</div>
      <div class="brand-sub">CEO · CFO · Open Market Buys · Min $100K</div>
    </div>
  </div>
  <div class="clock">
    <div class="clock-t" id="az-clock">{now.strftime('%a, %b %d  %H:%M:%S')}</div>
    <div class="clock-z">ARIZONA TIME · MST · UTC−7</div>
  </div>
</header>

<nav class="nav">
  <button class="tab-btn active" data-tab="t-today">Today's Picks</button>
  <button class="tab-btn" data-tab="t-history">History</button>
  <button class="tab-btn" data-tab="t-perf">Performance</button>
</nav>

<main>

<div id="t-today" class="pane active">
  <div class="hero">{hero}</div>
  <div class="sec">{now.strftime('%B %d, %Y')} · Scored Picks</div>
  <div class="picks">{cards}</div>
</div>

<div id="t-history" class="pane">
  <div class="sec">All Picks · Most Recent First</div>
  {_history(all_picks)}
</div>

<div id="t-perf" class="pane">
  <div class="sec">Forward Returns · vs Price at Detection</div>
  {_perf(all_picks)}
</div>

</main>

<footer>
  <span>INSIDER TRACKER — informational only · not financial advice</span>
  <span>Generated {now.strftime('%Y-%m-%d %H:%M MST')}</span>
</footer>

</div>
<script>{JS}</script>
</body>
</html>"""


def write_html(events, all_picks, path=None):
    if path is None:
        path = DOCS_DIR / "index.html"
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(generate_html(events, all_picks), encoding="utf-8")
    return path