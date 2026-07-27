"""Runs a band-touch study asynchronously and reports live progress.

Same shape as BacktestService — a date-range scan over the whole exchange
still means real batched network calls, so it runs on a background thread
with polled progress rather than blocking a request.

Two data sources, chosen via `source`:
  "bhavcopy" (default) — NSE's own daily file, one request per TRADING DAY.
      Fast and immune to Yahoo's per-symbol rate limiting (verified: a
      200-symbol Yahoo batch dropped to 22% success after a day of heavy use,
      while bhavcopy kept working). The narrow corporate-action cross-check
      (see engine.backtest.cross_check_corporate_actions) still uses Yahoo,
      but only for the handful of resulting touches, not the whole exchange.
  "yahoo" — the original per-symbol path, kept for comparison/fallback.

    POST /band-study/run     -> service.start()   (returns immediately, 202)
    GET  /band-study/status  -> service.snapshot() (the live progress bar)
    GET  /band-study/runs    -> list of past runs, from SQLite
    GET  /band-study/runs/<id> -> one run's full detail, from SQLite
"""

from __future__ import annotations

import threading
from datetime import date, datetime

from engine import backtest, universe
from engine.prices import IST
from engine.repository import BandStudyRepository


class BandStudyService:
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
        start_date: date,
        end_date: date,
        band_low: float = backtest.DEFAULT_BAND_LOW,
        band_high: float = backtest.DEFAULT_BAND_HIGH,
        direction: str = "up",
        min_volume: int = backtest.DEFAULT_MIN_VOLUME,
        chunk_size: int = 100,
        source: str = "bhavcopy",
        bhav_data=None,
        follow_days: int = backtest.DEFAULT_FOLLOW_DAYS,
    ) -> bool:
        """Kick off a band-touch study in the background. Returns False if one
        is already running. `data` is the Yahoo provider — used as the sole
        source when source="yahoo", and for the narrow corporate-action
        cross-check when source="bhavcopy" (may be None to skip that check).
        `bhav_data` is an already-constructed BhavcopyProvider — required when
        source="bhavcopy" (injectable so tests can fake it, same as `data`)."""
        if source == "yahoo" and data is None:
            return False
        if source == "bhavcopy" and bhav_data is None:
            return False
        with self._lock:
            if self._state["status"] == "running":
                return False
            self._state.update(
                status="running", done=0, total=0, percent=0,
                message="Starting band study…", run_id=None, error=None,
                started_at=datetime.now(IST).isoformat(), finished_at=None,
            )
        self._thread = threading.Thread(
            target=self._run,
            args=(db_path, cache_path, bhav_data, data, start_date, end_date,
                  band_low, band_high, direction, min_volume, chunk_size, source, follow_days),
            name="band-study-run",
            daemon=True,
        )
        self._thread.start()
        return True

    def _run(self, db_path, cache_path, bhav_data, data, start_date, end_date,
              band_low, band_high, direction, min_volume, chunk_size, source, follow_days) -> None:
        repo = BandStudyRepository(db_path)
        run_id = None
        try:
            run_id = repo.create_run(
                start_date.isoformat(), end_date.isoformat(), band_low, band_high, direction,
                source=source, follow_days=follow_days,
            )
            self._set(run_id=run_id)

            def progress(done, total, message):
                pct = int(done / total * 100) if total else 0
                self._set(done=done, total=total, percent=pct, message=message)

            if source == "bhavcopy":
                rows, _uni_source = universe.load_universe(cache_path)
                self._set(message=f"Scanning {start_date}–{end_date} via NSE bhavcopy…")
                result = backtest.scan_band_touches_bhavcopy(
                    rows, bhav_data, data, start_date, end_date, band_low=band_low, band_high=band_high,
                    direction=direction, min_volume=min_volume, progress=progress,
                    follow_days=follow_days,
                )
            else:
                rows, _uni_source = universe.load_universe(cache_path)
                self._set(total=len(rows), message=f"Scanning {len(rows)} stocks for {start_date}–{end_date}…")
                result = backtest.scan_band_touches(
                    rows, data, start_date, end_date, band_low=band_low, band_high=band_high,
                    direction=direction, min_volume=min_volume, chunk_size=chunk_size, progress=progress,
                    follow_days=follow_days,
                )

            repo.finish_run(run_id, result)
            summary = result.summary()
            flagged = sum(1 for t in result.touches if t.corporate_action_suspected)
            message = (
                f"{summary.total_touches} touches found — "
                f"{summary.up_pct}% up / {summary.down_pct}% down on day 2"
                if summary.total_touches else "No stocks touched that band in this range"
            )
            if flagged:
                message += f" ({flagged} flagged as possible corporate action)"
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
                message="Band study failed", finished_at=datetime.now(IST).isoformat(),
            )
