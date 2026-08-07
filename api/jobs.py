"""Background jobs: the price-alert checker and the periodic snapshot capture.

Why a thread and not Celery/APScheduler? For a single-user local tool, a daemon
thread with a sleep loop is the smallest thing that works, has zero extra
dependencies, and is easy to reason about. If this ever became a real service
you'd move to a proper scheduler (or a cron-invoked management command) — the
job FUNCTIONS below are written to be callable on their own precisely so that
swap stays easy.

Two things to be careful about, both handled here:

  1. THE RELOADER RUNS YOUR CODE TWICE. Flask's debug mode restarts the app in a
     child process, so a naively-started thread runs in both the parent and the
     child — double-firing every job. We only start when WERKZEUG_RUN_MAIN is
     set (the child) or the reloader is off.

  2. JOBS MUST SWEEP ALL USERS. The checker iterates paths.all_user_ids() rather
     than assuming one user. This is the same anti-hardcoding rule as the
     request path — it's why multi-user later isn't a rewrite.
"""

from __future__ import annotations

import os
import threading
import time

from engine.prices import market_is_open
from engine.repository import (
    AlertRepository,
    PriceRepository,
    SnapshotRepository,
    SqlitePortfolioManager,
)

from .paths import all_user_ids, db_file


# --- the individual jobs (plain functions, callable from tests or a CLI) -----
def symbols_of_interest(root: str) -> list[str]:
    """Every symbol worth fetching a live price for: anything held in any
    strategy, plus anything with an active alert.

    Now a single SQL query instead of loading every user's whole portfolio file
    into Python — exactly the kind of thing the migration bought us.
    """
    from engine.db import transaction

    with transaction(db_file(root)) as conn:
        rows = conn.execute(
            "SELECT DISTINCT symbol FROM holdings "
            "UNION SELECT DISTINCT symbol FROM alerts WHERE status = 'active' "
            "UNION SELECT DISTINCT symbol FROM watchlist_items"
        ).fetchall()
    return sorted(r["symbol"] for r in rows)


def refresh_live_prices(root: str, source) -> dict[str, float]:
    """Fetch live prices and write them into the shared PriceStore.

    This is the ONLY place live data enters the system. Everything downstream —
    valuation, alerts, snapshots, the leaderboard — keeps reading the store, so
    a fetch failure just means prices stay at their last known values.
    """
    if source is None:
        return {}
    symbols = symbols_of_interest(root)
    if not symbols:
        return {}
    try:
        fresh = source.get_prices(symbols)
    except Exception:
        return {}
    if fresh:
        PriceRepository(db_file(root)).set_many(fresh)
    return fresh


def check_alerts_for_user(root: str, user_id: str) -> list:
    """Evaluate one user's active alerts against current prices."""
    db = db_file(root)
    repo = AlertRepository(db, user_id)
    if not repo.active():
        return []
    return repo.check(PriceRepository(db).get_all())


def capture_snapshot_for_user(root: str, user_id: str) -> list:
    """Record today's value for every one of this user's strategies."""
    db = db_file(root)
    manager = SqlitePortfolioManager(db, user_id).load()
    if not manager.portfolios:
        return []
    symbols = set()
    for p in manager.portfolios.values():
        symbols.update(p.holdings.keys())
    prices = PriceRepository(db).get_prices(list(symbols))
    return SnapshotRepository(db, user_id).capture(manager, prices)


def check_all_alerts(root: str) -> int:
    fired = 0
    for uid in all_user_ids(root):
        try:
            fired += len(check_alerts_for_user(root, uid))
        except Exception:  # one bad user's data must not kill the loop
            pass
    return fired


def capture_all_snapshots(root: str) -> int:
    taken = 0
    for uid in all_user_ids(root):
        try:
            taken += len(capture_snapshot_for_user(root, uid))
        except Exception:
            pass
    return taken


# --- the worker thread -------------------------------------------------------
class BackgroundWorker:
    """Runs the alert check and the snapshot sweep on their own cadences.

    Note on snapshot_interval: the DATA granularity is daily regardless of this
    value, because SnapshotStore.capture upserts on (date, strategy) — running
    twice in a day refreshes today's row rather than adding a second point.

    So we sweep every few minutes rather than every 24h, which makes the job
    self-healing: a user who registers at noon, or a server restarted at 3am,
    still gets today's point. A pure 24h timer would leave both with an empty
    chart until the next firing — that's the bug this design avoids.
    """

    def __init__(
        self,
        root: str,
        alert_interval: int = 15,
        snapshot_interval: int = 300,
        price_interval: int = 60,
        source=None,
    ):
        self.root = root
        self.alert_interval = alert_interval        # re-evaluate alerts every 15s
        self.snapshot_interval = snapshot_interval  # refresh today's row every 5min
        self.price_interval = price_interval        # pull live prices every 60s
        self.source = source
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._did_first_price_pull = False

    def _run(self) -> None:
        # -1e9 forces every job to run immediately on the first tick.
        last_alert = -1e9
        last_snapshot = -1e9
        last_prices = -1e9
        while not self._stop.is_set():
            now = time.monotonic()

            # 1. Pull live prices FIRST so the alert check below sees fresh data.
            #    Outside market hours prices don't move, so after one initial
            #    pull (to get the last close) we stop polling until it reopens.
            if now - last_prices >= self.price_interval:
                if market_is_open() or not self._did_first_price_pull:
                    refresh_live_prices(self.root, self.source)
                    self._did_first_price_pull = True
                last_prices = now

            if now - last_alert >= self.alert_interval:
                check_all_alerts(self.root)
                last_alert = now
            if now - last_snapshot >= self.snapshot_interval:
                capture_all_snapshots(self.root)
                last_snapshot = now
            # Wake up often enough to stay responsive to stop(), but not busy-loop.
            self._stop.wait(1.0)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        # daemon=True so the thread never blocks interpreter shutdown (Ctrl+C).
        self._thread = threading.Thread(target=self._run, name="bg-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)


def should_start_worker(debug: bool) -> bool:
    """Avoid double-starting under Flask's auto-reloader (see module docstring)."""
    if not debug:
        return True
    return os.environ.get("WERKZEUG_RUN_MAIN") == "true"
