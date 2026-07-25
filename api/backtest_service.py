"""Runs a backtest asynchronously and reports live progress.

Same shape as ScreenerService, for the same reason: replaying the whole
exchange over a date range still means a few dozen batched Yahoo Finance
calls, which takes real seconds and must not block an HTTP request. One
long-lived BacktestService per app owns a single worker thread and the
transient progress state; the finished result is persisted via
BacktestRepository so it survives past the moment the run ends.

    POST /backtest/run     -> service.start()   (returns immediately, 202)
    GET  /backtest/status  -> service.snapshot() (the live progress bar)
    GET  /backtest/runs    -> list of past runs, from SQLite
    GET  /backtest/runs/<id> -> one run's full detail, from SQLite
"""

from __future__ import annotations

import threading
from datetime import date, datetime

from engine import backtest, universe
from engine.prices import IST
from engine.repository import BacktestRepository


class BacktestService:
    def __init__(self):
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._state: dict = {
            "status": "idle",     # idle | running | done | error
            "done": 0,
            "total": 0,
            "percent": 0,
            "message": "",
            "run_id": None,
            "error": None,
            "started_at": None,
            "finished_at": None,
        }

    # -- state ----------------------------------------------------------------
    def is_running(self) -> bool:
        with self._lock:
            return self._state["status"] == "running"

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._state)

    def _set(self, **kw) -> None:
        with self._lock:
            self._state.update(kw)

    # -- control --------------------------------------------------------------
    def start(
        self,
        db_path: str,
        cache_path: str,
        data,
        target_date: date,
        direction: str = "up",
        threshold_pct: float = backtest.DEFAULT_THRESHOLD_PCT,
        window_days: int = backtest.DEFAULT_WINDOW_DAYS,
        min_volume: int = backtest.DEFAULT_MIN_VOLUME,
        chunk_size: int = 100,
    ) -> bool:
        """Kick off a backtest in the background. Returns False if one is
        already running (the caller turns that into a 409) or no data
        provider is configured."""
        if data is None:
            return False
        with self._lock:
            if self._state["status"] == "running":
                return False
            self._state.update(
                status="running", done=0, total=0, percent=0,
                message="Starting backtest…", run_id=None, error=None,
                started_at=datetime.now(IST).isoformat(), finished_at=None,
            )
        self._thread = threading.Thread(
            target=self._run,
            args=(db_path, cache_path, data, target_date, direction, threshold_pct, window_days, min_volume, chunk_size),
            name="backtest-run",
            daemon=True,
        )
        self._thread.start()
        return True

    def _run(self, db_path, cache_path, data, target_date, direction, threshold_pct, window_days, min_volume, chunk_size) -> None:
        repo = BacktestRepository(db_path)
        run_id = None
        try:
            run_id = repo.create_run(target_date.isoformat(), direction, threshold_pct, window_days)
            self._set(run_id=run_id)

            def progress(done, total, message):
                pct = int(done / total * 100) if total else 0
                self._set(done=done, total=total, percent=pct, message=message)

            rows, _source = universe.load_universe(cache_path)
            self._set(total=len(rows), message=f"Replaying {len(rows)} stocks for {target_date}…")
            result = backtest.run_backtest(
                rows, data, target_date, direction=direction,
                threshold_pct=threshold_pct, window_days=window_days,
                min_volume=min_volume, chunk_size=chunk_size, progress=progress,
            )
            repo.finish_run(run_id, result)
            summary = result.summary()
            message = (
                f"{summary.mover_count} movers found — "
                f"{summary.reverted_pct}% reverted within {window_days} days"
                if summary.mover_count else "No qualifying movers on that date"
            )
            if result.missing_symbols:
                # Never silent: a run's coverage is only as good as its data.
                message += f" ({len(result.missing_symbols)} symbols could not be fetched)"
            self._set(status="done", percent=100, message=message, finished_at=datetime.now(IST).isoformat())
        except Exception as e:  # a run blowing up must not kill the thread silently
            if run_id is not None:
                try:
                    repo.fail_run(run_id, f"{type(e).__name__}: {e}")
                except Exception:
                    pass
            self._set(
                status="error", error=f"{type(e).__name__}: {e}",
                message="Backtest failed", finished_at=datetime.now(IST).isoformat(),
            )
