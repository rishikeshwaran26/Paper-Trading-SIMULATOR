"""SQLite repositories — the same public interfaces the JSON stores had.

Every class here is a drop-in replacement for a Phase 1–5 JSON store:

    PortfolioManager(path)        ->  SqlitePortfolioManager(db_path, user_id)
    AlertStore(path)              ->  AlertRepository(db_path, user_id)
    SnapshotStore(path)           ->  SnapshotRepository(db_path, user_id)
    PriceStore(path)              ->  PriceRepository(db_path)
    UserStore(path)               ->  UserRepository(db_path)

The method names and return types are deliberately unchanged, so the API routes
and the CLI keep working with almost no edits. That's the payoff of having kept
persistence behind a seam since Phase 1: swapping the storage engine touches the
storage layer and nothing else. The trading rules in portfolio.py are untouched.

SqlitePortfolioManager SUBCLASSES PortfolioManager and overrides only load/save,
so create_strategy / get / delete_strategy / names / leaderboard are inherited
rather than reimplemented — there's exactly one copy of that logic.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional

from .alerts import ACTIVE, DISMISSED, TRIGGERED, Alert
from .db import transaction
from .manager import PortfolioManager
from .models import ClosedLot, Holding, Transaction
from .portfolio import Portfolio
from .snapshots import Snapshot


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# --- portfolios --------------------------------------------------------------
class SqlitePortfolioManager(PortfolioManager):
    """One user's strategies, backed by SQLite."""

    def __init__(self, db_path: str, user_id: str):
        self.db_path = db_path
        self.user_id = user_id
        self.portfolios: dict[str, Portfolio] = {}
        # Set for the base class's benefit; nothing reads it in SQLite mode.
        self.path = db_path

    # -- load -----------------------------------------------------------------
    def load(self) -> "SqlitePortfolioManager":
        """Rebuild every Portfolio object for this user.

        Four queries total (portfolios, holdings, transactions, closed_lots)
        rather than one per portfolio — avoiding the N+1 query problem.
        """
        self.portfolios = {}
        with transaction(self.db_path) as conn:
            prows = conn.execute(
                "SELECT id, name, cash, starting_cash FROM portfolios WHERE user_id = ? ORDER BY id",
                (self.user_id,),
            ).fetchall()
            if not prows:
                return self

            by_id: dict[int, Portfolio] = {}
            for r in prows:
                p = Portfolio(name=r["name"], cash=r["cash"], starting_cash=r["starting_cash"])
                self.portfolios[r["name"]] = p
                by_id[r["id"]] = p

            ids = tuple(by_id)
            marks = ",".join("?" * len(ids))

            for r in conn.execute(
                f"SELECT portfolio_id, symbol, quantity, avg_price FROM holdings "
                f"WHERE portfolio_id IN ({marks})",
                ids,
            ):
                by_id[r["portfolio_id"]].holdings[r["symbol"]] = Holding(
                    symbol=r["symbol"], quantity=r["quantity"], avg_price=r["avg_price"]
                )

            txn_index: dict[str, Transaction] = {}
            for r in conn.execute(
                f"SELECT * FROM transactions WHERE portfolio_id IN ({marks}) ORDER BY portfolio_id, seq",
                ids,
            ):
                t = Transaction(
                    id=r["id"],
                    type=r["type"],
                    symbol=r["symbol"],
                    quantity=r["quantity"],
                    price=r["price"],
                    timestamp=r["timestamp"],
                    reason=r["reason"],
                    confidence=r["confidence"],
                    tags=json.loads(r["tags"]),
                    open_quantity=r["open_quantity"],
                    realized_pnl=r["realized_pnl"],
                    closed_lots=[],
                    review=r["review"],
                )
                by_id[r["portfolio_id"]].transactions.append(t)
                txn_index[t.id] = t

            if txn_index:
                lot_marks = ",".join("?" * len(txn_index))
                for r in conn.execute(
                    f"SELECT * FROM closed_lots WHERE sell_id IN ({lot_marks}) ORDER BY sell_id, position",
                    tuple(txn_index),
                ):
                    txn_index[r["sell_id"]].closed_lots.append(
                        ClosedLot(
                            buy_id=r["buy_id"],
                            quantity=r["quantity"],
                            buy_price=r["buy_price"],
                            sell_price=r["sell_price"],
                            confidence=r["confidence"],
                            tags=json.loads(r["tags"]),
                            holding_days=r["holding_days"],
                            lot_pnl=r["lot_pnl"],
                        )
                    )
        return self

    # -- save -----------------------------------------------------------------
    def save(self) -> None:
        """Persist the in-memory state for this user, atomically.

        Note on approach: we upsert every row for this user rather than tracking
        a precise dirty-set. Reason — a sell mutates OLD buy rows too (FIFO draws
        down their open_quantity), and a review edits an existing row, so "only
        insert new transactions" would be wrong. Upserting a few hundred rows
        inside one transaction takes single-digit milliseconds, and correctness
        beats micro-optimisation here. If a journal ever got huge, THIS is the
        method to make incremental — nothing else would change.
        """
        with transaction(self.db_path) as conn:
            existing = {
                r["name"]: r["id"]
                for r in conn.execute(
                    "SELECT id, name FROM portfolios WHERE user_id = ?", (self.user_id,)
                )
            }

            # strategies deleted in memory should disappear from the DB
            for name, pid in existing.items():
                if name not in self.portfolios:
                    conn.execute("DELETE FROM portfolios WHERE id = ?", (pid,))

            for name, p in self.portfolios.items():
                pid = existing.get(name)
                if pid is None:
                    cur = conn.execute(
                        "INSERT INTO portfolios (user_id, name, cash, starting_cash, created_at) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (self.user_id, name, p.cash, p.starting_cash, _now()),
                    )
                    pid = cur.lastrowid
                else:
                    conn.execute(
                        "UPDATE portfolios SET cash = ?, starting_cash = ? WHERE id = ?",
                        (p.cash, p.starting_cash, pid),
                    )

                # holdings: small and fully replaced (a sold-out symbol must go)
                conn.execute("DELETE FROM holdings WHERE portfolio_id = ?", (pid,))
                if p.holdings:
                    conn.executemany(
                        "INSERT INTO holdings (portfolio_id, symbol, quantity, avg_price) "
                        "VALUES (?, ?, ?, ?)",
                        [(pid, h.symbol, h.quantity, h.avg_price) for h in p.holdings.values()],
                    )

                if p.transactions:
                    conn.executemany(
                        "INSERT INTO transactions "
                        "(id, portfolio_id, seq, type, symbol, quantity, price, timestamp, reason, "
                        " confidence, tags, open_quantity, realized_pnl, review) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                        "ON CONFLICT(id) DO UPDATE SET "
                        "  open_quantity = excluded.open_quantity, "
                        "  realized_pnl  = excluded.realized_pnl, "
                        "  review        = excluded.review",
                        [
                            (
                                t.id, pid, seq, t.type, t.symbol, t.quantity, t.price,
                                t.timestamp, t.reason, t.confidence, json.dumps(t.tags),
                                t.open_quantity, t.realized_pnl, t.review,
                            )
                            for seq, t in enumerate(p.transactions)
                        ],
                    )
                    # closed lots are immutable once written; rewrite per sell
                    sells = [t for t in p.transactions if t.closed_lots]
                    if sells:
                        conn.executemany(
                            "DELETE FROM closed_lots WHERE sell_id = ?",
                            [(t.id,) for t in sells],
                        )
                        conn.executemany(
                            "INSERT INTO closed_lots "
                            "(sell_id, position, buy_id, quantity, buy_price, sell_price, "
                            " confidence, tags, holding_days, lot_pnl) "
                            "VALUES (?,?,?,?,?,?,?,?,?,?)",
                            [
                                (
                                    t.id, i, lot.buy_id, lot.quantity, lot.buy_price,
                                    lot.sell_price, lot.confidence, json.dumps(lot.tags),
                                    lot.holding_days, lot.lot_pnl,
                                )
                                for t in sells
                                for i, lot in enumerate(t.closed_lots)
                            ],
                        )


# --- journal analytics in SQL ------------------------------------------------
class JournalRepository:
    """Analytics computed by the database instead of Python loops.

    This is the concrete payoff of migrating. The Python version in journal.py
    loads every transaction and every closed lot into memory to answer "win rate
    by tag"; here it's one GROUP BY over the closed_lots table. json_each() is
    how SQLite lets us group by a value inside the JSON tags array.

    journal.py remains the reference implementation, and a test asserts these two
    produce identical numbers — so there's no risk of the two drifting apart.
    """

    def __init__(self, db_path: str, user_id: str):
        self.db_path = db_path
        self.user_id = user_id

    def performance_by_tag(self, strategy: Optional[str] = None) -> list[dict]:
        sql = """
            SELECT
                tag.value                                        AS label,
                COUNT(*)                                         AS closed_trades,
                ROUND(SUM(l.lot_pnl), 2)                         AS total_pnl,
                ROUND(AVG(l.lot_pnl), 2)                         AS avg_pnl,
                ROUND(AVG(l.holding_days), 2)                    AS avg_holding_days,
                SUM(CASE WHEN l.lot_pnl > 0 THEN 1 ELSE 0 END)   AS wins
            FROM closed_lots l
            JOIN json_each(l.tags) AS tag
            JOIN transactions t ON t.id = l.sell_id
            JOIN portfolios   p ON p.id = t.portfolio_id
            WHERE p.user_id = ?
        """
        params: list = [self.user_id]
        if strategy:
            sql += " AND p.name = ?"
            params.append(strategy)
        sql += " GROUP BY tag.value ORDER BY total_pnl DESC"

        with transaction(self.db_path) as conn:
            rows = conn.execute(sql, params).fetchall()
        return [
            {
                "label": r["label"],
                "closed_trades": r["closed_trades"],
                "total_pnl": r["total_pnl"],
                "avg_pnl": r["avg_pnl"],
                "avg_holding_days": r["avg_holding_days"],
                "win_rate": round(r["wins"] / r["closed_trades"], 4) if r["closed_trades"] else 0.0,
            }
            for r in rows
        ]


# --- alerts ------------------------------------------------------------------
class AlertRepository:
    """Same interface as engine.alerts.AlertStore, backed by SQLite."""

    def __init__(self, db_path: str, user_id: str):
        self.db_path = db_path
        self.user_id = user_id

    def _row_to_alert(self, r) -> Alert:
        return Alert(
            id=r["id"], symbol=r["symbol"], target_price=r["target_price"],
            direction=r["direction"], note=r["note"], status=r["status"],
            created_at=r["created_at"], triggered_at=r["triggered_at"],
            triggered_price=r["triggered_price"],
        )

    @property
    def alerts(self) -> list[Alert]:
        with transaction(self.db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM alerts WHERE user_id = ? ORDER BY created_at", (self.user_id,)
            ).fetchall()
        return [self._row_to_alert(r) for r in rows]

    def add(self, symbol: str, target_price: float, direction: str, note: str = "") -> Alert:
        from .alerts import ABOVE, BELOW
        import uuid

        if direction not in (ABOVE, BELOW):
            raise ValueError(f"direction must be '{ABOVE}' or '{BELOW}'")
        if target_price <= 0:
            raise ValueError("target_price must be positive")
        alert = Alert(
            id=uuid.uuid4().hex[:12], symbol=symbol.strip().upper(),
            target_price=round(float(target_price), 2), direction=direction, note=note.strip(),
        )
        with transaction(self.db_path) as conn:
            conn.execute(
                "INSERT INTO alerts (id, user_id, symbol, target_price, direction, note, status, created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (alert.id, self.user_id, alert.symbol, alert.target_price, alert.direction,
                 alert.note, alert.status, alert.created_at),
            )
        return alert

    def get(self, alert_id: str) -> Optional[Alert]:
        with transaction(self.db_path) as conn:
            r = conn.execute(
                "SELECT * FROM alerts WHERE id = ? AND user_id = ?", (alert_id, self.user_id)
            ).fetchone()
        return self._row_to_alert(r) if r else None

    def remove(self, alert_id: str) -> bool:
        with transaction(self.db_path) as conn:
            cur = conn.execute(
                "DELETE FROM alerts WHERE id = ? AND user_id = ?", (alert_id, self.user_id)
            )
            return cur.rowcount > 0

    def dismiss(self, alert_id: str) -> Optional[Alert]:
        with transaction(self.db_path) as conn:
            conn.execute(
                "UPDATE alerts SET status = ? WHERE id = ? AND user_id = ?",
                (DISMISSED, alert_id, self.user_id),
            )
        return self.get(alert_id)

    def active(self) -> list[Alert]:
        return [a for a in self.alerts if a.status == ACTIVE]

    def triggered(self) -> list[Alert]:
        return [a for a in self.alerts if a.status == TRIGGERED]

    def symbols(self) -> list[str]:
        with transaction(self.db_path) as conn:
            rows = conn.execute(
                "SELECT DISTINCT symbol FROM alerts WHERE user_id = ? AND status = ?",
                (self.user_id, ACTIVE),
            ).fetchall()
        return sorted(r["symbol"] for r in rows)

    def save(self) -> None:
        """No-op: every mutation above is already committed. Present so callers
        written against AlertStore keep working unchanged."""

    def check(self, prices: dict[str, float]) -> list[Alert]:
        """Fire any active alert whose target the price has crossed.

        The trigger RULE still lives on the Alert dataclass (engine/alerts.py) —
        we only changed where the data is read from and written to.
        """
        fired: list[Alert] = []
        for a in self.active():
            price = prices.get(a.symbol)
            if price is not None and a.should_trigger(price):
                a.trigger(price)
                fired.append(a)
        if fired:
            with transaction(self.db_path) as conn:
                conn.executemany(
                    "UPDATE alerts SET status = ?, triggered_at = ?, triggered_price = ? WHERE id = ?",
                    [(TRIGGERED, a.triggered_at, a.triggered_price, a.id) for a in fired],
                )
        return fired


# --- snapshots ---------------------------------------------------------------
class SnapshotRepository:
    """Same interface as engine.snapshots.SnapshotStore, backed by SQLite.

    The (user_id, date, strategy) primary key gives us the daily upsert for free:
    ON CONFLICT DO UPDATE replaces today's row instead of appending a duplicate
    point — which the JSON version had to do with a manual scan.
    """

    def __init__(self, db_path: str, user_id: str):
        self.db_path = db_path
        self.user_id = user_id

    @property
    def snapshots(self) -> list[Snapshot]:
        return self.all()

    def all(self) -> list[Snapshot]:
        with transaction(self.db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM snapshots WHERE user_id = ? ORDER BY date, strategy",
                (self.user_id,),
            ).fetchall()
        return [
            Snapshot(
                date=r["date"], timestamp=r["timestamp"], strategy=r["strategy"],
                total_value=r["total_value"], return_pct=r["return_pct"], cash=r["cash"],
                realized_pnl=r["realized_pnl"], unrealized_pnl=r["unrealized_pnl"],
            )
            for r in rows
        ]

    def capture(self, manager, prices: dict[str, float]) -> list[Snapshot]:
        now = datetime.now(timezone.utc)
        date = now.strftime("%Y-%m-%d")
        stamp = now.isoformat()
        taken = [
            Snapshot(
                date=date, timestamp=stamp, strategy=name,
                total_value=p.total_value(prices), return_pct=p.total_return_pct(prices),
                cash=p.cash, realized_pnl=p.realized_pnl(),
                unrealized_pnl=p.unrealized_pnl(prices),
            )
            for name, p in manager.portfolios.items()
        ]
        if taken:
            with transaction(self.db_path) as conn:
                conn.executemany(
                    "INSERT INTO snapshots "
                    "(user_id, date, timestamp, strategy, total_value, return_pct, cash, realized_pnl, unrealized_pnl) "
                    "VALUES (?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(user_id, date, strategy) DO UPDATE SET "
                    "  timestamp = excluded.timestamp, total_value = excluded.total_value, "
                    "  return_pct = excluded.return_pct, cash = excluded.cash, "
                    "  realized_pnl = excluded.realized_pnl, unrealized_pnl = excluded.unrealized_pnl",
                    [
                        (self.user_id, s.date, s.timestamp, s.strategy, s.total_value,
                         s.return_pct, s.cash, s.realized_pnl, s.unrealized_pnl)
                        for s in taken
                    ],
                )
        return taken

    def save(self) -> None:
        """No-op — capture() already committed."""

    def series(self, strategies: list[str] | None = None) -> list[dict]:
        """Chart-shaped: one row per date, one key per strategy."""
        wanted = set(strategies) if strategies else None
        by_date: dict[str, dict] = {}
        for s in self.all():
            if wanted and s.strategy not in wanted:
                continue
            by_date.setdefault(s.date, {"date": s.date})[s.strategy] = s.return_pct
        return [by_date[d] for d in sorted(by_date)]

    def strategy_names(self) -> list[str]:
        with transaction(self.db_path) as conn:
            rows = conn.execute(
                "SELECT DISTINCT strategy FROM snapshots WHERE user_id = ?", (self.user_id,)
            ).fetchall()
        return sorted(r["strategy"] for r in rows)


# --- prices (shared market data, not per-user) ------------------------------
class PriceRepository:
    """Last-known market prices, shared by all users, backed by SQLite."""

    def __init__(self, db_path: str):
        self.db_path = db_path

    def set_price(self, symbol: str, price: float) -> None:
        self.set_many({symbol: price})

    def set_many(self, prices: dict[str, float]) -> None:
        if not prices:
            return
        stamp = _now()
        with transaction(self.db_path) as conn:
            conn.executemany(
                "INSERT INTO prices (symbol, price, updated_at) VALUES (?,?,?) "
                "ON CONFLICT(symbol) DO UPDATE SET price = excluded.price, updated_at = excluded.updated_at",
                [(s.strip().upper(), round(float(p), 2), stamp) for s, p in prices.items()],
            )

    def get_all(self) -> dict[str, float]:
        with transaction(self.db_path) as conn:
            rows = conn.execute("SELECT symbol, price FROM prices").fetchall()
        return {r["symbol"]: r["price"] for r in rows}

    def get_prices(self, symbols: list[str]) -> dict[str, float]:
        if not symbols:
            return {}
        syms = [s.strip().upper() for s in symbols]
        marks = ",".join("?" * len(syms))
        with transaction(self.db_path) as conn:
            rows = conn.execute(
                f"SELECT symbol, price FROM prices WHERE symbol IN ({marks})", syms
            ).fetchall()
        return {r["symbol"]: r["price"] for r in rows}

    def updated_at(self) -> dict[str, str]:
        with transaction(self.db_path) as conn:
            rows = conn.execute("SELECT symbol, updated_at FROM prices").fetchall()
        return {r["symbol"]: r["updated_at"] for r in rows}


# --- watchlist ---------------------------------------------------------------
class WatchlistRepository:
    """A user's NAMED watchlists — e.g. "Swing", "Intraday" — each holding its
    own set of symbols. A user can have several; the same symbol can appear in
    more than one list at once (they're independent). Names are not required
    to be unique, on request — keep this simple rather than mirroring the
    strategy-name-uniqueness rule.

    Every method that takes a watchlist_id first confirms it belongs to this
    user (same principle as _get_portfolio's 404 in api/routes.py) — ids are
    plain sequential integers, so without this check one user could poke at
    another user's list by guessing an id.
    """

    def __init__(self, db_path: str, user_id: str):
        self.db_path = db_path
        self.user_id = user_id

    def _owns(self, conn, watchlist_id: int) -> bool:
        row = conn.execute(
            "SELECT 1 FROM watchlists WHERE id = ? AND user_id = ?",
            (watchlist_id, self.user_id),
        ).fetchone()
        return row is not None

    def list_all(self) -> list[dict]:
        """Every one of this user's watchlists, each with its own symbols,
        newest-created first isn't required — insertion order is fine."""
        with transaction(self.db_path) as conn:
            lists = conn.execute(
                "SELECT id, name, created_at FROM watchlists WHERE user_id = ? ORDER BY id",
                (self.user_id,),
            ).fetchall()
            out = []
            for wl in lists:
                items = conn.execute(
                    "SELECT symbol FROM watchlist_items WHERE watchlist_id = ? ORDER BY added_at",
                    (wl["id"],),
                ).fetchall()
                out.append(
                    {
                        "id": wl["id"],
                        "name": wl["name"],
                        "created_at": wl["created_at"],
                        "symbols": [i["symbol"] for i in items],
                    }
                )
        return out

    def create(self, name: str) -> dict:
        name = name.strip()
        if not name:
            raise ValueError("watchlist name is required")
        stamp = _now()
        with transaction(self.db_path) as conn:
            cur = conn.execute(
                "INSERT INTO watchlists (user_id, name, created_at) VALUES (?, ?, ?)",
                (self.user_id, name, stamp),
            )
            watchlist_id = cur.lastrowid
        return {"id": watchlist_id, "name": name, "created_at": stamp, "symbols": []}

    def delete(self, watchlist_id: int) -> bool:
        with transaction(self.db_path) as conn:
            if not self._owns(conn, watchlist_id):
                return False
            conn.execute("DELETE FROM watchlists WHERE id = ?", (watchlist_id,))
        return True

    def add_symbol(self, watchlist_id: int, symbol: str) -> str | None:
        """Returns the normalized symbol on success, None if this watchlist
        doesn't belong to this user (the route turns that into a 404)."""
        sym = symbol.strip().upper()
        with transaction(self.db_path) as conn:
            if not self._owns(conn, watchlist_id):
                return None
            # INSERT OR IGNORE: adding a symbol twice to the SAME list is a
            # harmless no-op, thanks to the (watchlist_id, symbol) primary key.
            conn.execute(
                "INSERT OR IGNORE INTO watchlist_items (watchlist_id, symbol, added_at) VALUES (?, ?, ?)",
                (watchlist_id, sym, _now()),
            )
        return sym

    def remove_symbol(self, watchlist_id: int, symbol: str) -> bool:
        with transaction(self.db_path) as conn:
            if not self._owns(conn, watchlist_id):
                return False
            cur = conn.execute(
                "DELETE FROM watchlist_items WHERE watchlist_id = ? AND symbol = ?",
                (watchlist_id, symbol.strip().upper()),
            )
            return cur.rowcount > 0

    def all_symbols(self) -> list[str]:
        """Every symbol across ALL of this user's watchlists, de-duplicated —
        used to know what to fetch live prices for (a stock watched in two
        lists only needs fetching once)."""
        with transaction(self.db_path) as conn:
            rows = conn.execute(
                "SELECT DISTINCT wi.symbol FROM watchlist_items wi "
                "JOIN watchlists w ON w.id = wi.watchlist_id "
                "WHERE w.user_id = ?",
                (self.user_id,),
            ).fetchall()
        return sorted(r["symbol"] for r in rows)


# --- users -------------------------------------------------------------------
class UserRepository:
    """Same interface as api.auth.UserStore, backed by SQLite.

    Password hashing still happens in the auth layer — this only stores what it
    is given. Keeping crypto policy out of the persistence layer means changing
    the hash algorithm never touches SQL.
    """

    def __init__(self, db_path: str):
        self.db_path = db_path

    def by_username(self, username: str) -> Optional[dict]:
        with transaction(self.db_path) as conn:
            # username is COLLATE NOCASE, so this is a case-insensitive match
            r = conn.execute(
                "SELECT * FROM users WHERE username = ?", (username.strip(),)
            ).fetchone()
        return dict(r) if r else None

    def by_id(self, user_id: str) -> Optional[dict]:
        with transaction(self.db_path) as conn:
            r = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return dict(r) if r else None

    def insert(self, user: dict) -> dict:
        with transaction(self.db_path) as conn:
            conn.execute(
                "INSERT INTO users (id, username, password_hash, created_at) VALUES (?,?,?,?)",
                (user["id"], user["username"], user["password_hash"], user["created_at"]),
            )
        return user

    def count(self) -> int:
        with transaction(self.db_path) as conn:
            return conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]

    def all_ids(self) -> list[str]:
        with transaction(self.db_path) as conn:
            rows = conn.execute("SELECT id FROM users ORDER BY created_at").fetchall()
        return [r["id"] for r in rows]


# --- screener ----------------------------------------------------------------
class ScreenerRepository:
    """Persistence for whole-market screener scans.

    Not user-scoped: the day's movers are the same for everyone, so a scan
    triggered by any user is stored once and read by all — exactly like the
    shared `prices` table. The in-progress *progress bar* lives in memory (in the
    API's ScreenerService); only the finished result is persisted here.
    """

    def __init__(self, db_path: str):
        self.db_path = db_path

    def create_run(self, scan_date: str) -> int:
        """Open a new run row in 'running' state, returning its id."""
        with transaction(self.db_path) as conn:
            cur = conn.execute(
                "INSERT INTO screener_runs (scan_date, started_at, status) VALUES (?, ?, 'running')",
                (scan_date, _now()),
            )
            return cur.lastrowid

    def finish_run(self, run_id: int, result) -> None:
        """Write all movers and mark the run done. `result` is a
        screener.ScanResult. Wrapped in one transaction so a run is never
        half-written."""
        buckets_flat = result.movers
        with transaction(self.db_path) as conn:
            conn.executemany(
                """INSERT OR REPLACE INTO screener_movers
                   (run_id, symbol, name, price, prev_close, pct_change, volume,
                    avg_volume, vol_ratio, vol_diff_1w_pct, bracket, direction,
                    week52_high, week52_low, week52_pct, near_high, near_low,
                    rsi, macd_bullish, macd_hist, spark, results_recent,
                    results_date, news, reasons)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [
                    (
                        run_id, m.symbol, m.name, m.price, m.prev_close, m.pct_change,
                        m.volume, m.avg_volume, m.vol_ratio, m.vol_diff_1w_pct,
                        m.bracket, m.direction, m.week52_high, m.week52_low, m.week52_pct,
                        1 if m.near_high else 0, 1 if m.near_low else 0,
                        m.rsi,
                        None if m.macd_bullish is None else (1 if m.macd_bullish else 0),
                        m.macd_hist, json.dumps(m.spark),
                        1 if m.results_recent else 0, m.results_date,
                        json.dumps(m.news), json.dumps(m.reasons),
                    )
                    for m in buckets_flat
                ],
            )
            conn.execute(
                """UPDATE screener_runs
                   SET status='done', finished_at=?, universe_count=?, scanned_count=?,
                       mover_count=?, source=?
                   WHERE id=?""",
                (_now(), result.universe_count, result.scanned_count,
                 len(buckets_flat), result.source, run_id),
            )

    def fail_run(self, run_id: int, error: str) -> None:
        with transaction(self.db_path) as conn:
            conn.execute(
                "UPDATE screener_runs SET status='error', finished_at=?, error=? WHERE id=?",
                (_now(), error[:500], run_id),
            )

    def latest_done(self) -> Optional[dict]:
        """The most recent successful scan, with its movers grouped by bracket.
        None if no scan has ever completed."""
        with transaction(self.db_path) as conn:
            run = conn.execute(
                "SELECT * FROM screener_runs WHERE status='done' ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if not run:
                return None
            movers = conn.execute(
                "SELECT * FROM screener_movers WHERE run_id=? ORDER BY ABS(pct_change) DESC",
                (run["id"],),
            ).fetchall()
        return self._shape(run, movers)

    def last_run(self) -> Optional[dict]:
        """The most recent run of ANY status (running/done/error) — used to show
        'last scanned' and to decide whether today's scan already happened."""
        with transaction(self.db_path) as conn:
            run = conn.execute(
                "SELECT * FROM screener_runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return dict(run) if run else None

    def done_today(self, scan_date: str) -> bool:
        with transaction(self.db_path) as conn:
            row = conn.execute(
                "SELECT 1 FROM screener_runs WHERE scan_date=? AND status='done' LIMIT 1",
                (scan_date,),
            ).fetchone()
        return row is not None

    @staticmethod
    def _row_to_mover(r) -> dict:
        """Turn a screener_movers row into a JSON-ready dict (SQLite 0/1 ->
        bool, JSON columns -> lists)."""
        d = dict(r)
        d["near_high"] = bool(d["near_high"])
        d["near_low"] = bool(d["near_low"])
        d["results_recent"] = bool(d.get("results_recent"))
        # macd_bullish is 0/1/NULL -> True/False/None
        mb = d.get("macd_bullish")
        d["macd_bullish"] = None if mb is None else bool(mb)
        d["spark"] = json.loads(d.get("spark") or "[]")
        d["news"] = json.loads(d.get("news") or "[]")
        d["reasons"] = json.loads(d.get("reasons") or "[]")
        return d

    @classmethod
    def _shape(cls, run, mover_rows) -> dict:
        from engine.screener import BRACKETS

        # mover_rows already arrive ordered by |pct| desc.
        flat = [cls._row_to_mover(r) for r in mover_rows]
        buckets: dict[str, list[dict]] = {key: [] for key, *_ in BRACKETS}
        for d in flat:
            if d["bracket"] in buckets:
                buckets[d["bracket"]].append(d)
        return {
            "run": {
                "id": run["id"],
                "scan_date": run["scan_date"],
                "started_at": run["started_at"],
                "finished_at": run["finished_at"],
                "universe_count": run["universe_count"],
                "scanned_count": run["scanned_count"],
                "mover_count": run["mover_count"],
                "source": run["source"],
            },
            "buckets": buckets,
            "movers": flat,
        }


# --- backtest ------------------------------------------------------------------
class BacktestRepository:
    """Persistence for backtest runs — replaying a past date's movers and what
    happened afterward. Not user-scoped, same reasoning as ScreenerRepository:
    a backtest of March 1st gives the same answer for every user, so it's
    written once and shared."""

    def __init__(self, db_path: str):
        self.db_path = db_path

    def create_run(self, target_date: str, direction: str, threshold_pct: float, window_days: int) -> int:
        with transaction(self.db_path) as conn:
            cur = conn.execute(
                """INSERT INTO backtest_runs
                   (target_date, direction, threshold_pct, window_days, started_at, status)
                   VALUES (?, ?, ?, ?, ?, 'running')""",
                (target_date, direction, threshold_pct, window_days, _now()),
            )
            return cur.lastrowid

    def finish_run(self, run_id: int, result) -> None:
        """`result` is a backtest.BacktestResult. Writes every mover plus the
        aggregate summary onto the run row, in one transaction."""
        summary = result.summary()
        with transaction(self.db_path) as conn:
            conn.executemany(
                """INSERT OR REPLACE INTO backtest_movers
                   (run_id, symbol, name, direction, spike_pct, price_at_spike, prev_close,
                    volume, avg_volume, vol_ratio, rsi, peak_price, peak_offset_days,
                    first_red_offset_days, round_trip_offset_days, reverted, spark)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [
                    (
                        run_id, m.symbol, m.name, m.direction, m.spike_pct, m.price_at_spike,
                        m.prev_close, m.volume, m.avg_volume, m.vol_ratio, m.rsi, m.peak_price,
                        m.peak_offset_days, m.first_red_offset_days, m.round_trip_offset_days,
                        1 if m.reverted else 0, json.dumps(m.spark),
                    )
                    for m in result.movers
                ],
            )
            conn.execute(
                """UPDATE backtest_runs
                   SET status='done', finished_at=?, universe_count=?, mover_count=?,
                       reverted_count=?, reverted_pct=?, missing_count=?, missing_symbols=?
                   WHERE id=?""",
                (
                    _now(), result.universe_count, summary.mover_count,
                    summary.reverted_count, summary.reverted_pct,
                    len(result.missing_symbols), json.dumps(result.missing_symbols), run_id,
                ),
            )

    def fail_run(self, run_id: int, error: str) -> None:
        with transaction(self.db_path) as conn:
            conn.execute(
                "UPDATE backtest_runs SET status='error', finished_at=?, error=? WHERE id=?",
                (_now(), error[:500], run_id),
            )

    def get_run(self, run_id: int) -> Optional[dict]:
        with transaction(self.db_path) as conn:
            run = conn.execute("SELECT * FROM backtest_runs WHERE id=?", (run_id,)).fetchone()
            if not run:
                return None
            movers = conn.execute(
                "SELECT * FROM backtest_movers WHERE run_id=? ORDER BY ABS(spike_pct) DESC",
                (run_id,),
            ).fetchall()
        return self._shape(run, movers)

    def list_runs(self, limit: int = 20) -> list[dict]:
        """Most recent runs of ANY status, newest first — for a history list."""
        with transaction(self.db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM backtest_runs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._run_summary(r) for r in rows]

    @staticmethod
    def _run_summary(run) -> dict:
        return {
            "id": run["id"],
            "target_date": run["target_date"],
            "direction": run["direction"],
            "threshold_pct": run["threshold_pct"],
            "window_days": run["window_days"],
            "status": run["status"],
            "started_at": run["started_at"],
            "finished_at": run["finished_at"],
            "universe_count": run["universe_count"],
            "mover_count": run["mover_count"],
            "reverted_count": run["reverted_count"],
            "reverted_pct": run["reverted_pct"],
            "missing_count": run["missing_count"],
            "missing_symbols": json.loads(run["missing_symbols"] or "[]"),
            "error": run["error"],
        }

    @classmethod
    def _shape(cls, run, mover_rows) -> dict:
        movers = [
            dict(r) | {"reverted": bool(r["reverted"]), "spark": json.loads(r["spark"] or "[]")}
            for r in mover_rows
        ]
        return {"run": cls._run_summary(run), "movers": movers}


# --- band-touch study ----------------------------------------------------------
class BandStudyRepository:
    """Persistence for Phase-1 band-touch studies — a tight % band scanned
    across a date range, plus each touch's very-next-day result. Not
    user-scoped, same reasoning as BacktestRepository: a study of Jan 2026 at
    19-20% gives the same answer for every user."""

    def __init__(self, db_path: str):
        self.db_path = db_path

    def create_run(
        self, start_date: str, end_date: str, band_low: float, band_high: float,
        direction: str, source: str = "yahoo", follow_days: int = 1,
    ) -> int:
        with transaction(self.db_path) as conn:
            cur = conn.execute(
                """INSERT INTO band_study_runs
                   (start_date, end_date, band_low, band_high, direction, source,
                    follow_days, started_at, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'running')""",
                (start_date, end_date, band_low, band_high, direction, source, follow_days, _now()),
            )
            return cur.lastrowid

    def finish_run(self, run_id: int, result) -> None:
        """`result` is a backtest.BandStudyResult. Writes every touch plus the
        aggregate summary, in one transaction."""
        summary = result.summary()
        with transaction(self.db_path) as conn:
            conn.executemany(
                """INSERT INTO band_touches
                   (run_id, symbol, name, date, direction, prev_close, high, low, touch_pct,
                    close, close_pct, volume, day2_date, day2_open, day2_high, day2_low,
                    day2_close, day2_pct_change, day2_open_close_pct, corporate_action_suspected,
                    follow_days)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [
                    (
                        run_id, t.symbol, t.name, t.date, t.direction, t.prev_close, t.high, t.low,
                        t.touch_pct, t.close, t.close_pct, t.volume, t.day2_date, t.day2_open,
                        t.day2_high, t.day2_low, t.day2_close, t.day2_pct_change, t.day2_open_close_pct,
                        1 if t.corporate_action_suspected else 0,
                        json.dumps([f.to_dict() for f in t.follow_days]),
                    )
                    for t in result.touches
                ],
            )
            conn.execute(
                """UPDATE band_study_runs
                   SET status='done', finished_at=?, universe_count=?, total_touches=?,
                       with_day2=?, up_count=?, down_count=?, up_pct=?, down_pct=?,
                       avg_day2_pct_up=?, avg_day2_pct_down=?, missing_count=?, missing_symbols=?,
                       per_day=?
                   WHERE id=?""",
                (
                    _now(), result.universe_count, summary.total_touches, summary.with_day2,
                    summary.up_count, summary.down_count, summary.up_pct, summary.down_pct,
                    summary.avg_day2_pct_up, summary.avg_day2_pct_down,
                    len(result.missing_symbols), json.dumps(result.missing_symbols),
                    json.dumps([d.to_dict() for d in summary.per_day]), run_id,
                ),
            )

    def fail_run(self, run_id: int, error: str) -> None:
        with transaction(self.db_path) as conn:
            conn.execute(
                "UPDATE band_study_runs SET status='error', finished_at=?, error=? WHERE id=?",
                (_now(), error[:500], run_id),
            )

    def get_run(self, run_id: int) -> Optional[dict]:
        with transaction(self.db_path) as conn:
            run = conn.execute("SELECT * FROM band_study_runs WHERE id=?", (run_id,)).fetchone()
            if not run:
                return None
            touches = conn.execute(
                "SELECT * FROM band_touches WHERE run_id=? ORDER BY date, symbol", (run_id,)
            ).fetchall()
        shaped_touches = [
            dict(t) | {
                "corporate_action_suspected": bool(t["corporate_action_suspected"]),
                "follow_days": json.loads(t["follow_days"] or "[]"),
            }
            for t in touches
        ]
        return {"run": self._run_summary(run), "touches": shaped_touches}

    def list_runs(self, limit: int = 20) -> list[dict]:
        with transaction(self.db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM band_study_runs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._run_summary(r) for r in rows]

    @staticmethod
    def _run_summary(run) -> dict:
        return {
            "id": run["id"],
            "start_date": run["start_date"],
            "end_date": run["end_date"],
            "band_low": run["band_low"],
            "band_high": run["band_high"],
            "direction": run["direction"],
            "source": run["source"],
            "follow_days": run["follow_days"],
            "per_day": json.loads(run["per_day"] or "[]"),
            "status": run["status"],
            "started_at": run["started_at"],
            "finished_at": run["finished_at"],
            "universe_count": run["universe_count"],
            "total_touches": run["total_touches"],
            "with_day2": run["with_day2"],
            "up_count": run["up_count"],
            "down_count": run["down_count"],
            "up_pct": run["up_pct"],
            "down_pct": run["down_pct"],
            "avg_day2_pct_up": run["avg_day2_pct_up"],
            "avg_day2_pct_down": run["avg_day2_pct_down"],
            "missing_count": run["missing_count"],
            "missing_symbols": json.loads(run["missing_symbols"] or "[]"),
            "error": run["error"],
        }
