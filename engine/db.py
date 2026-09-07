"""SQLite schema and connection handling.

Why migrate off JSON
--------------------
JSON was right for Phase 1: readable, zero setup, easy to eyeball while the data
model was still moving. It stops being right once you want to *ask questions*.
Every analytics call currently loads every transaction of every strategy into
Python and filters in a loop. With SQL, "win rate by tag" is a GROUP BY that the
database does for us, and it stays fast as the journal grows.

The other thing JSON couldn't give us: safe concurrent writes. The background
worker and a web request could previously both read-modify-write the same file
and silently clobber each other (last writer wins). SQLite gives us real
transactions and row-level correctness instead.

Schema notes
------------
- `closed_lots` is its own TABLE, not a JSON blob on the sell. This is the
  single most important modelling decision here: closed lots are what journal
  analytics aggregates over ("avg holding days for winners", "P&L by tag"), and
  putting them in a real table is what makes those queries SQL instead of Python
  loops.

- `tags` IS stored as a JSON array in a TEXT column. A fully normalised
  tag/tag_link pair of tables would be more "correct", but tags here are a short
  list of free-text labels attached to one transaction, always read together
  with it, and never updated independently. The join cost isn't worth it. Where
  we DO need to query by tag we use json_each(), which SQLite provides.

- Money is REAL (float rupees), matching the engine. For a real money system
  you'd store integer paise to avoid float dust — noted as a known simplification.

- Foreign keys use ON DELETE CASCADE and we enable `PRAGMA foreign_keys=ON` per
  connection (SQLite defaults it OFF, which surprises people).

- WAL mode lets the background worker read while a request writes, instead of
  the two blocking each other.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id            TEXT PRIMARY KEY,
    username      TEXT NOT NULL COLLATE NOCASE UNIQUE,
    password_hash TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS portfolios (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name          TEXT NOT NULL,
    cash          REAL NOT NULL,
    starting_cash REAL NOT NULL,
    created_at    TEXT NOT NULL,
    UNIQUE (user_id, name)
);

CREATE TABLE IF NOT EXISTS holdings (
    portfolio_id INTEGER NOT NULL REFERENCES portfolios(id) ON DELETE CASCADE,
    symbol       TEXT    NOT NULL,
    quantity     INTEGER NOT NULL,
    avg_price    REAL    NOT NULL,
    PRIMARY KEY (portfolio_id, symbol)
);

CREATE TABLE IF NOT EXISTS transactions (
    id            TEXT PRIMARY KEY,
    portfolio_id  INTEGER NOT NULL REFERENCES portfolios(id) ON DELETE CASCADE,
    seq           INTEGER NOT NULL,          -- preserves insertion order
    type          TEXT    NOT NULL,          -- BUY | SELL
    symbol        TEXT    NOT NULL,
    quantity      INTEGER NOT NULL,
    price         REAL    NOT NULL,
    timestamp     TEXT    NOT NULL,
    reason        TEXT    NOT NULL DEFAULT '',
    confidence    INTEGER,                   -- buys only
    tags          TEXT    NOT NULL DEFAULT '[]',
    open_quantity INTEGER NOT NULL DEFAULT 0,
    realized_pnl  REAL,                      -- sells only
    review        TEXT
);
CREATE INDEX IF NOT EXISTS idx_txn_portfolio ON transactions(portfolio_id, seq);

CREATE TABLE IF NOT EXISTS closed_lots (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    sell_id      TEXT    NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
    position     INTEGER NOT NULL,           -- order within the sell
    buy_id       TEXT    NOT NULL,
    quantity     INTEGER NOT NULL,
    buy_price    REAL    NOT NULL,
    sell_price   REAL    NOT NULL,
    confidence   INTEGER,
    tags         TEXT    NOT NULL DEFAULT '[]',
    holding_days REAL    NOT NULL,
    lot_pnl      REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lot_sell ON closed_lots(sell_id);

CREATE TABLE IF NOT EXISTS alerts (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    symbol          TEXT NOT NULL,
    target_price    REAL NOT NULL,
    direction       TEXT NOT NULL,           -- above | below
    note            TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL,           -- active | triggered | dismissed
    created_at      TEXT NOT NULL,
    triggered_at    TEXT,
    triggered_price REAL
);
CREATE INDEX IF NOT EXISTS idx_alert_user ON alerts(user_id, status);

CREATE TABLE IF NOT EXISTS snapshots (
    user_id        TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    date           TEXT NOT NULL,            -- YYYY-MM-DD, the upsert key
    timestamp      TEXT NOT NULL,
    strategy       TEXT NOT NULL,
    total_value    REAL NOT NULL,
    return_pct     REAL NOT NULL,
    cash           REAL NOT NULL,
    realized_pnl   REAL NOT NULL,
    unrealized_pnl REAL NOT NULL,
    PRIMARY KEY (user_id, date, strategy)
);

-- Named watchlists: a user can keep several (e.g. "Swing", "Intraday"), each
-- with its own set of symbols. No unique constraint on (user_id, name) —
-- duplicate names are allowed on purpose (kept simple on request).
CREATE TABLE IF NOT EXISTS watchlists (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name       TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_watchlists_user ON watchlists(user_id);

-- Symbols within one named watchlist. The composite key makes adding the same
-- symbol twice to the same list a natural no-op via INSERT OR IGNORE — but the
-- same symbol CAN appear in more than one watchlist (different watchlist_id).
CREATE TABLE IF NOT EXISTS watchlist_items (
    watchlist_id INTEGER NOT NULL REFERENCES watchlists(id) ON DELETE CASCADE,
    symbol       TEXT NOT NULL,
    added_at     TEXT NOT NULL,
    PRIMARY KEY (watchlist_id, symbol)
);

-- Prices are MARKET data: shared by every user, not scoped to one.
CREATE TABLE IF NOT EXISTS prices (
    symbol     TEXT PRIMARY KEY,
    price      REAL NOT NULL,
    updated_at TEXT NOT NULL
);

-- Backtest runs: replay a PAST date's whole-market movers and measure what
-- happened afterward, using history that already exists — it can be
-- re-created any time since it only reads historical prices. Shared market
-- data, not user-scoped, same as prices.
CREATE TABLE IF NOT EXISTS backtest_runs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    target_date    TEXT NOT NULL,            -- YYYY-MM-DD being replayed
    direction      TEXT NOT NULL,            -- up | down
    threshold_pct  REAL NOT NULL,
    window_days    INTEGER NOT NULL,
    started_at     TEXT NOT NULL,
    finished_at    TEXT,
    status         TEXT NOT NULL,            -- running | done | error
    universe_count INTEGER NOT NULL DEFAULT 0,
    mover_count    INTEGER NOT NULL DEFAULT 0,
    reverted_count INTEGER NOT NULL DEFAULT 0,
    reverted_pct   REAL,
    -- symbols that failed to fetch even after a retry — never silently
    -- dropped; a non-zero count here means this run's coverage is incomplete.
    missing_count   INTEGER NOT NULL DEFAULT 0,
    missing_symbols TEXT    NOT NULL DEFAULT '[]',
    error          TEXT
);
CREATE INDEX IF NOT EXISTS idx_backtest_runs_date ON backtest_runs(target_date);

CREATE TABLE IF NOT EXISTS backtest_movers (
    run_id                 INTEGER NOT NULL REFERENCES backtest_runs(id) ON DELETE CASCADE,
    symbol                 TEXT    NOT NULL,
    name                   TEXT    NOT NULL DEFAULT '',
    direction              TEXT    NOT NULL,
    spike_pct              REAL    NOT NULL,
    price_at_spike         REAL    NOT NULL,
    prev_close             REAL    NOT NULL,
    volume                 INTEGER NOT NULL DEFAULT 0,
    avg_volume             INTEGER NOT NULL DEFAULT 0,
    vol_ratio              REAL    NOT NULL DEFAULT 0,
    rsi                    REAL,
    peak_price             REAL    NOT NULL,
    peak_offset_days       INTEGER NOT NULL DEFAULT 0,
    first_red_offset_days  INTEGER,
    round_trip_offset_days INTEGER,
    reverted               INTEGER NOT NULL DEFAULT 0,
    spark                  TEXT    NOT NULL DEFAULT '[]',  -- JSON list of closes for a mini chart
    PRIMARY KEY (run_id, symbol)
);
CREATE INDEX IF NOT EXISTS idx_backtest_movers_run ON backtest_movers(run_id);

-- Band-touch studies: Phase-1 of the mean-reversion research — a tight %
-- band (e.g. 19-20%) over a date range, one row per (touch_date, symbol), plus
-- what its very next trading day did. Shared market data, same reasoning as
-- backtest_runs.
CREATE TABLE IF NOT EXISTS band_study_runs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    start_date     TEXT NOT NULL,
    end_date       TEXT NOT NULL,
    band_low       REAL NOT NULL,
    band_high      REAL NOT NULL,
    direction      TEXT NOT NULL,            -- up | down
    started_at     TEXT NOT NULL,
    finished_at    TEXT,
    status         TEXT NOT NULL,            -- running | done | error
    universe_count INTEGER NOT NULL DEFAULT 0,
    total_touches  INTEGER NOT NULL DEFAULT 0,
    with_day2      INTEGER NOT NULL DEFAULT 0,
    up_count       INTEGER NOT NULL DEFAULT 0,
    down_count     INTEGER NOT NULL DEFAULT 0,
    up_pct         REAL,
    down_pct       REAL,
    avg_day2_pct_up   REAL,
    avg_day2_pct_down REAL,
    -- symbols that failed to fetch even after a retry — never silently
    -- dropped; a non-zero count here means this run's coverage is incomplete.
    missing_count   INTEGER NOT NULL DEFAULT 0,
    missing_symbols TEXT    NOT NULL DEFAULT '[]',
    -- which data provider this run used: "yahoo" (per-symbol, rate-limit
    -- prone) or "bhavcopy" (NSE's own daily file, one request per day).
    source         TEXT NOT NULL DEFAULT 'yahoo',
    -- how many TRADING days after each touch this run tracked.
    follow_days    INTEGER NOT NULL DEFAULT 1,
    -- JSON: the cohort's up/down breakdown at each day-offset (1..follow_days).
    per_day        TEXT NOT NULL DEFAULT '[]',
    -- how many TRADING days BEFORE each touch this run also tracked (0 = none).
    before_days    INTEGER NOT NULL DEFAULT 0,
    -- JSON: same breakdown as per_day, for offsets -before_days..-1.
    per_before_day TEXT NOT NULL DEFAULT '[]',
    error          TEXT
);
CREATE INDEX IF NOT EXISTS idx_band_study_runs_dates ON band_study_runs(start_date, end_date);

CREATE TABLE IF NOT EXISTS band_touches (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id              INTEGER NOT NULL REFERENCES band_study_runs(id) ON DELETE CASCADE,
    symbol              TEXT    NOT NULL,
    name                TEXT    NOT NULL DEFAULT '',
    date                TEXT    NOT NULL,
    direction           TEXT    NOT NULL,
    prev_close          REAL    NOT NULL,
    high                REAL    NOT NULL,
    low                 REAL    NOT NULL,
    touch_pct           REAL    NOT NULL,
    close               REAL    NOT NULL,
    close_pct           REAL    NOT NULL,
    volume              INTEGER NOT NULL DEFAULT 0,
    day2_date           TEXT,
    day2_open           REAL,
    day2_high           REAL,
    day2_low            REAL,
    day2_close          REAL,
    day2_pct_change     REAL,
    day2_open_close_pct REAL,
    -- only meaningful on the bhavcopy path (raw, unadjusted prices): True if
    -- Yahoo's adjusted % change disagrees sharply, suggesting a split/bonus
    -- rather than a real market move. See cross_check_corporate_actions().
    corporate_action_suspected INTEGER NOT NULL DEFAULT 0,
    -- JSON list of every tracked trading day after the touch. The day2_*
    -- columns above are this list's first entry, kept for older rows and any
    -- caller still reading them.
    follow_days         TEXT    NOT NULL DEFAULT '[]',
    -- JSON list of every tracked trading day BEFORE the touch, empty unless
    -- the run asked for before_days > 0.
    before_days          TEXT    NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_band_touches_run ON band_touches(run_id);

-- Manually tracked touches: a personal shortlist a user picks out of a scan and
-- then follows day by day. Unlike band_study_runs (shared market data), this IS
-- user-scoped — it's a hand-made selection, not a fact about the market.
--
-- The touch-day figures are copied in rather than joined back to band_touches:
-- the scan run they came from can be re-run or deleted, and a tracked item
-- should survive that. `follow_days` is refreshed from bhavcopy on demand and
-- grows as real days pass, which is the whole point of tracking.
CREATE TABLE IF NOT EXISTS tracked_touches (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    symbol       TEXT NOT NULL,
    name         TEXT NOT NULL DEFAULT '',
    touch_date   TEXT NOT NULL,
    direction    TEXT NOT NULL DEFAULT 'up',
    touch_pct    REAL,
    close_pct    REAL,
    touch_close  REAL,
    note         TEXT NOT NULL DEFAULT '',
    follow_days  TEXT NOT NULL DEFAULT '[]',  -- JSON, refreshed from bhavcopy
    refreshed_at TEXT,
    created_at   TEXT NOT NULL,
    -- the same stock on the same day is one thing to track, so re-adding is a
    -- harmless no-op via INSERT OR IGNORE
    UNIQUE (user_id, symbol, touch_date)
);
CREATE INDEX IF NOT EXISTS idx_tracked_user ON tracked_touches(user_id, touch_date);

-- Saved backtest patterns: records a specific combination of filters, test date
-- range, and resulting stats from the backtest report for later discovery/lookup.
CREATE TABLE IF NOT EXISTS saved_patterns (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT    NOT NULL,
    test_start_date TEXT    NOT NULL,
    test_end_date   TEXT    NOT NULL,
    cases           INTEGER NOT NULL,
    fell_count      INTEGER NOT NULL,
    rose_count      INTEGER NOT NULL,
    fall_rate       REAL    NOT NULL,
    filters         TEXT    NOT NULL,
    created_at      TEXT    NOT NULL,
    status          TEXT    NOT NULL DEFAULT 'discovered'
);
CREATE INDEX IF NOT EXISTS idx_saved_patterns_created ON saved_patterns(created_at DESC);
"""

# Columns added to backtest_movers after its first release.
_BACKTEST_MOVER_ADDED_COLUMNS = [
    ("spark", "TEXT NOT NULL DEFAULT '[]'"),
]

# missing_count/missing_symbols: added to both run-tracking tables so a run
# with incomplete data coverage (a fetch that failed even after retry) is
# always visible, never silently indistinguishable from a fully clean run.
_MISSING_SYMBOLS_COLUMNS = [
    ("missing_count", "INTEGER NOT NULL DEFAULT 0"),
    ("missing_symbols", "TEXT NOT NULL DEFAULT '[]'"),
]

# `source`: which data provider a band study used ("yahoo" | "bhavcopy") — old
# rows predate the bhavcopy path entirely, so they default to "yahoo".
# `follow_days`: how many trading days after each touch the run tracked. Runs
# made before this was configurable tracked exactly one (day 2), so 1 is the
# honest default for them — not the current default of 5.
_BAND_STUDY_RUN_ADDED_COLUMNS = [
    ("source", "TEXT NOT NULL DEFAULT 'yahoo'"),
    ("follow_days", "INTEGER NOT NULL DEFAULT 1"),
    # per_day: JSON list of the cohort's up/down breakdown at each day-offset,
    # computed once when the run finishes (same place every other summary
    # number is written) rather than recomputed on every read.
    ("per_day", "TEXT NOT NULL DEFAULT '[]'"),
    # before_days/per_before_day: same idea, for the days BEFORE the touch.
    # Runs made before this was configurable tracked none, so 0 is honest.
    ("before_days", "INTEGER NOT NULL DEFAULT 0"),
    ("per_before_day", "TEXT NOT NULL DEFAULT '[]'"),
]

# corporate_action_suspected: only meaningful on the bhavcopy path (raw,
# unadjusted prices) — see cross_check_corporate_actions() in engine/backtest.py.
# follow_days: JSON list of every tracked day after the touch; day-2 columns
# are kept alongside it as the first entry, so older rows and any code reading
# them keep working.
_BAND_TOUCH_ADDED_COLUMNS = [
    ("corporate_action_suspected", "INTEGER NOT NULL DEFAULT 0"),
    ("follow_days", "TEXT NOT NULL DEFAULT '[]'"),
    ("before_days", "TEXT NOT NULL DEFAULT '[]'"),
]


def connect(db_path: str) -> sqlite3.Connection:
    """Open a connection with the pragmas we depend on.

    We open a fresh connection per unit of work rather than sharing one. SQLite
    connections are not safe to share across threads, and the background worker
    runs in its own thread — a new connection is cheap (microseconds) and removes
    that whole class of bug.
    """
    directory = os.path.dirname(os.path.abspath(db_path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=10.0)
    conn.row_factory = sqlite3.Row  # rows behave like dicts: row["cash"]
    conn.execute("PRAGMA foreign_keys = ON")   # OFF by default in SQLite!
    conn.execute("PRAGMA journal_mode = WAL")  # readers don't block the writer
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


@contextmanager
def transaction(db_path: str):
    """Run a unit of work in a single transaction.

    Commits on success, rolls back on any exception. This is the guarantee JSON
    could never give us: a buy that updates cash, holdings AND appends a
    transaction row either happens completely or not at all.
    """
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path: str) -> None:
    """Create the schema if it doesn't exist. Safe to call on every startup."""
    with transaction(db_path) as conn:
        conn.executescript(SCHEMA)
        row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO meta (key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
        _migrate_legacy_watchlist(conn)
        _migrate_added_columns(conn, "backtest_movers", _BACKTEST_MOVER_ADDED_COLUMNS)
        _migrate_added_columns(conn, "backtest_runs", _MISSING_SYMBOLS_COLUMNS)
        _migrate_added_columns(conn, "band_study_runs", _MISSING_SYMBOLS_COLUMNS)
        _migrate_added_columns(conn, "band_study_runs", _BAND_STUDY_RUN_ADDED_COLUMNS)
        _migrate_added_columns(conn, "band_touches", _BAND_TOUCH_ADDED_COLUMNS)


def _migrate_added_columns(conn: sqlite3.Connection, table: str, added: list[tuple[str, str]]) -> None:
    """Add columns introduced after a table's first release to a pre-existing
    database (SQLite has no "ADD COLUMN IF NOT EXISTS"). Idempotent: checks the
    current columns first and only ALTERs in the missing ones, so it's a no-op
    on every startup after the first."""
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if not existing:
        return  # table not created yet (fresh DB just built it with all columns)
    for name, decl in added:
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def _migrate_legacy_watchlist(conn: sqlite3.Connection) -> None:
    """One-time migration: watchlists used to be one flat, unnamed list per
    user. They're now named and a user can have several. Existing entries move
    into a single watchlist called "Watchlist" per user, then the old table is
    dropped — after this runs once, the old table is gone and this is a no-op
    on every later startup.
    """
    exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='watchlist'"
    ).fetchone()
    if not exists:
        return

    rows = conn.execute("SELECT user_id, symbol, added_at FROM watchlist").fetchall()
    by_user: dict[str, list[tuple[str, str]]] = {}
    for r in rows:
        by_user.setdefault(r["user_id"], []).append((r["symbol"], r["added_at"]))

    for user_id, items in by_user.items():
        cur = conn.execute(
            "INSERT INTO watchlists (user_id, name, created_at) VALUES (?, ?, ?)",
            (user_id, "Watchlist", datetime.now(timezone.utc).isoformat()),
        )
        watchlist_id = cur.lastrowid
        conn.executemany(
            "INSERT OR IGNORE INTO watchlist_items (watchlist_id, symbol, added_at) VALUES (?, ?, ?)",
            [(watchlist_id, symbol, added_at) for symbol, added_at in items],
        )

    conn.execute("DROP TABLE watchlist")


def schema_version(db_path: str) -> int:
    with transaction(db_path) as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        return int(row["value"]) if row else 0
