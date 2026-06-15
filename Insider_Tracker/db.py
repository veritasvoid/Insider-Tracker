"""
db.py — SQLite persistence layer
Tables:
  insider_buys  — raw individual filings ingested daily (rolling history)
  buy_events    — collapsed buy events after stage 2 processing
  picks         — scored picks for stage 7 performance tracking
"""

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "data" / "insider_tracker.db"


def get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    """Create all tables and migrate existing schemas if needed."""
    with get_conn() as conn:
        # ── Step 1: base tables (safe to run on existing DB) ──────────────────
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS insider_buys (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            filing_date   TEXT NOT NULL,
            trade_date    TEXT NOT NULL,
            ticker        TEXT NOT NULL,
            company       TEXT,
            insider_name  TEXT NOT NULL,
            title         TEXT,
            price         REAL,
            qty           INTEGER,
            owned         INTEGER,
            delta_own     TEXT,
            value         REAL NOT NULL,
            trade_type    TEXT,
            ingested_at   TEXT DEFAULT (datetime('now')),
            UNIQUE(trade_date, ticker, insider_name, value)
        );

        CREATE TABLE IF NOT EXISTS buy_events (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date            TEXT NOT NULL,
            ticker              TEXT NOT NULL,
            company             TEXT,
            insider_name        TEXT NOT NULL,
            title               TEXT,
            event_start_date    TEXT NOT NULL,
            event_end_date      TEXT NOT NULL,
            total_value         REAL NOT NULL,
            total_qty           INTEGER,
            avg_price           REAL,
            delta_own           TEXT,
            cluster_tag         TEXT NOT NULL,
            distinct_buyers_30d INTEGER NOT NULL,
            created_at          TEXT DEFAULT (datetime('now')),
            UNIQUE(run_date, ticker, insider_name, event_start_date)
        );

        CREATE TABLE IF NOT EXISTS picks (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date      TEXT NOT NULL,
            ticker        TEXT NOT NULL,
            company       TEXT,
            cluster_tag   TEXT,
            score         INTEGER,
            price_at_pick REAL,
            price_3d      REAL,
            price_8d      REAL,
            price_15d     REAL,
            price_30d     REAL,
            price_90d     REAL,
            created_at    TEXT DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_buys_ticker_date
            ON insider_buys(ticker, trade_date);
        CREATE INDEX IF NOT EXISTS idx_buys_insider
            ON insider_buys(insider_name, ticker, trade_date);
        CREATE INDEX IF NOT EXISTS idx_events_run
            ON buy_events(run_date, ticker);
        """)

        # ── Step 2: migrate picks table — add columns introduced after v1 ─────
        # SQLite has no ALTER TABLE ADD COLUMN IF NOT EXISTS, so use try/except.
        _picks_migrations = [
            ("score_stars",    "INTEGER"),
            ("score_label",    "TEXT"),
            ("score_breakdown","TEXT"),
            ("score_notes",    "TEXT"),
            ("score_key_risk", "TEXT"),
            ("news_headline",  "TEXT"),
            ("catalysts",      "TEXT"),
            ("news_sentiment", "TEXT"),
            ("buy_price",      "REAL"),
            ("event_date",     "TEXT"),
            ("rationale",      "TEXT"),
            ("cited_inputs",   "TEXT"),
            # v2: one-row-per-ticker with purchases JSON
            ("purchases",        "TEXT"),
            ("first_seen",       "TEXT"),
            ("last_updated",     "TEXT"),
            ("distinct_buyers",  "INTEGER"),
            ("total_value",      "REAL"),
            ("updated_at",       "TEXT"),
        ]
        for col, typ in _picks_migrations:
            try:
                conn.execute(f"ALTER TABLE picks ADD COLUMN {col} {typ}")
            except sqlite3.OperationalError:
                pass  # column already exists

        # ── Step 3: migrate to one-row-per-ticker (v2) ────────────────────────
        # Seed first_seen / last_updated from existing data
        conn.execute("""
            UPDATE picks
            SET first_seen   = COALESCE(event_date, run_date),
                last_updated = run_date
            WHERE first_seen IS NULL
        """)

        # Collapse duplicate ticker rows from old schema — keep the most recent
        conn.execute("""
            DELETE FROM picks WHERE id NOT IN (
                SELECT MAX(id) FROM picks GROUP BY ticker
            )
        """)

        # Drop old per-event unique index; replace with per-ticker index
        conn.execute("DROP INDEX IF EXISTS idx_picks_unique_event")
        try:
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_picks_ticker ON picks(ticker)"
            )
        except sqlite3.OperationalError:
            pass

    print(f"[db] Database ready at {DB_PATH}")


def insert_buys(buys: list[dict]) -> int:
    """
    Insert fresh buys, skipping duplicates (same trade_date + ticker + insider + value).
    Returns number of new rows inserted.
    """
    with get_conn() as conn:
        cur = conn.executemany("""
            INSERT OR IGNORE INTO insider_buys
                (filing_date, trade_date, ticker, company, insider_name,
                 title, price, qty, owned, delta_own, value, trade_type)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """, [
            (b["filing_date"], b["trade_date"], b["ticker"], b["company"],
             b["insider_name"], b["title"], b["price"], b["qty"],
             b["owned"], b["delta_own"], b["value"], b["trade_type"])
            for b in buys
        ])
    return cur.rowcount


def get_history(ticker: str, days: int = 30) -> list[dict]:
    """Return all insider_buys for a ticker within the last N days by trade_date."""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT * FROM insider_buys
            WHERE ticker = ?
              AND trade_date >= date('now', ? || ' days')
            ORDER BY insider_name, trade_date
        """, (ticker, f"-{days}")).fetchall()
    return [dict(r) for r in rows]


def get_all_tickers_in_history(days: int = 30) -> list[str]:
    """Return distinct tickers seen in the last N days."""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT DISTINCT ticker FROM insider_buys
            WHERE trade_date >= date('now', ? || ' days')
        """, (f"-{days}",)).fetchall()
    return [r["ticker"] for r in rows]


def insert_buy_events(events: list[dict]) -> None:
    """Persist collapsed buy events from stage 2, skipping duplicates."""
    with get_conn() as conn:
        conn.executemany("""
            INSERT OR IGNORE INTO buy_events
                (run_date, ticker, company, insider_name, title,
                 event_start_date, event_end_date, total_value, total_qty,
                 avg_price, delta_own, cluster_tag, distinct_buyers_30d)
            VALUES
                (:run_date, :ticker, :company, :insider_name, :title,
                 :event_start_date, :event_end_date, :total_value, :total_qty,
                 :avg_price, :delta_own, :cluster_tag, :distinct_buyers_30d)
        """, events)


def prune_old_buys(days: int = 90) -> int:
    """Delete raw buys older than N days to keep DB lean."""
    with get_conn() as conn:
        cur = conn.execute("""
            DELETE FROM insider_buys
            WHERE trade_date < date('now', ? || ' days')
        """, (f"-{days}",))
    return cur.rowcount


def save_picks(scored_events: list[dict]) -> int:
    """
    Persist scored picks — ONE row per ticker.

    On first encounter: inserts a new row with purchases JSON and sets first_seen.
    On repeat encounter: merges any new purchase entries into the purchases JSON,
    updates cluster_tag, score, and news metadata with the freshest data.

    Returns number of NEW tickers inserted (updates don't count).
    """
    import json as _json
    from datetime import date as _date

    today = str(_date.today())

    # Group all events by ticker (a run may have multiple insiders per ticker)
    by_ticker: dict[str, list[dict]] = {}
    for e in scored_events:
        by_ticker.setdefault(e["ticker"], []).append(e)

    inserted = updated = 0
    with get_conn() as conn:
        for ticker, events in by_ticker.items():
            # Pull ALL raw individual transactions from insider_buys for this ticker.
            # This gives the full per-trade breakdown (price, qty, value per day)
            # rather than collapsed totals — crucial for showing buy cadence in history.
            raw_rows = conn.execute("""
                SELECT trade_date, insider_name, title, price, qty, value, delta_own
                FROM insider_buys
                WHERE ticker = ?
                  AND trade_date >= date('now', '-90 days')
                ORDER BY trade_date DESC
            """, (ticker,)).fetchall()
            all_purchases = [dict(r) for r in raw_rows]
            total_value   = sum(p.get("value") or 0 for p in all_purchases)

            # Use the highest-scored event for metadata fields
            best = max(events, key=lambda e: e.get("score_total") or 0)

            existing = conn.execute(
                "SELECT * FROM picks WHERE ticker = ?", (ticker,)
            ).fetchone()

            if existing:
                conn.execute("""
                    UPDATE picks SET
                        last_updated    = ?,
                        cluster_tag     = ?,
                        distinct_buyers = ?,
                        total_value     = ?,
                        score           = ?,
                        score_stars     = ?,
                        score_label     = ?,
                        score_breakdown = ?,
                        score_notes     = ?,
                        score_key_risk  = ?,
                        news_headline   = ?,
                        catalysts       = ?,
                        news_sentiment  = ?,
                        purchases       = ?,
                        updated_at      = datetime('now')
                    WHERE ticker = ?
                """, (
                    today,
                    best.get("cluster_tag"),
                    best.get("insider_count") or best.get("distinct_buyers_30d"),
                    total_value,
                    best.get("score_total"),
                    best.get("score_stars"),
                    best.get("score_label"),
                    _json.dumps(best.get("score_breakdown", {})),
                    _json.dumps(best.get("score_notes", [])),
                    best.get("score_key_risk"),
                    best.get("news_headline"),
                    _json.dumps(best.get("catalysts", [])),
                    best.get("news_sentiment"),
                    _json.dumps(all_purchases),
                    ticker,
                ))
                updated += 1

            else:
                # First time seeing this ticker — all_purchases already built above
                conn.execute("""
                    INSERT INTO picks (
                        run_date, ticker, company,
                        first_seen, last_updated,
                        cluster_tag, distinct_buyers, total_value,
                        score, score_stars, score_label,
                        score_breakdown, score_notes, score_key_risk,
                        news_headline, catalysts, news_sentiment,
                        purchases, buy_price, price_at_pick, event_date
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    today,                                         # run_date (legacy)
                    ticker,
                    best.get("company"),
                    today,                                         # first_seen
                    today,                                         # last_updated
                    best.get("cluster_tag"),
                    best.get("insider_count") or best.get("distinct_buyers_30d"),
                    total_value,
                    best.get("score_total"),
                    best.get("score_stars"),
                    best.get("score_label"),
                    _json.dumps(best.get("score_breakdown", {})),
                    _json.dumps(best.get("score_notes", [])),
                    best.get("score_key_risk"),
                    best.get("news_headline"),
                    _json.dumps(best.get("catalysts", [])),
                    best.get("news_sentiment"),
                    _json.dumps(all_purchases),
                    best.get("avg_price") or best.get("price"),   # insider buy price
                    best.get("current_price"),                     # market price at detection
                    best.get("event_start_date"),
                ))
                inserted += 1

    print(f"[db] save_picks: {inserted} new ticker(s) added, {updated} updated.")
    return inserted


def get_picks_for_tracking() -> list[dict]:
    """
    Return picks that still have NULL forward prices and are old enough to fill.
    Reference date is first_seen (when the ticker was first detected), aliased as
    run_date so track.py's existing code works unchanged.
    """
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT id, ticker,
                   COALESCE(first_seen, run_date) AS run_date,
                   price_at_pick,
                   price_3d, price_8d, price_15d, price_30d, price_90d
            FROM picks
            WHERE price_at_pick IS NOT NULL
              AND (
                  (price_3d  IS NULL AND julianday('now') - julianday(COALESCE(first_seen, run_date)) >= 3)  OR
                  (price_8d  IS NULL AND julianday('now') - julianday(COALESCE(first_seen, run_date)) >= 8)  OR
                  (price_15d IS NULL AND julianday('now') - julianday(COALESCE(first_seen, run_date)) >= 15) OR
                  (price_30d IS NULL AND julianday('now') - julianday(COALESCE(first_seen, run_date)) >= 30) OR
                  (price_90d IS NULL AND julianday('now') - julianday(COALESCE(first_seen, run_date)) >= 90)
              )
            ORDER BY COALESCE(first_seen, run_date) DESC
        """).fetchall()
    return [dict(r) for r in rows]


def update_pick_prices(pick_id: int, **prices: float) -> None:
    """
    Update one or more forward price columns on a pick row.
    Call as: update_pick_prices(42, price_3d=12.50, price_8d=13.10)
    """
    allowed = {"price_3d", "price_8d", "price_15d", "price_30d", "price_90d"}
    updates = {k: v for k, v in prices.items() if k in allowed and v is not None}
    if not updates:
        return
    cols = ", ".join(f"{k} = ?" for k in updates)
    vals = list(updates.values()) + [pick_id]
    with get_conn() as conn:
        conn.execute(f"UPDATE picks SET {cols} WHERE id = ?", vals)
