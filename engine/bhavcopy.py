"""NSE's official daily "bhavcopy" — the whole exchange's OHLCV in one file.

Why this exists, alongside the yfinance-backed BacktestData
-------------------------------------------------------------
The backtest/band-study replays a DATE RANGE across the WHOLE exchange
(~2,000 symbols). Doing that via yfinance means ~20 batched per-symbol
requests — Yahoo's unofficial endpoint throttles under that kind of repeated
load (verified: a 200-symbol batch dropped to 22% success after a day of
heavy use). NSE's bhavcopy is the opposite shape: ONE HTTP request per
TRADING DAY returns every listed stock's full day (open/high/low/close/prev
close/volume) at once — a month of data is ~20 requests total, not ~2,000,
and it's the exchange's own authoritative file rather than a third-party
scrape.

This module implements the same `BacktestData` protocol (see engine/backtest.py)
so it's a drop-in replacement for YFinanceBacktestData — nothing in the
scan/walk-forward logic needs to change, only which provider is passed in.

Known limitation, on purpose left unfixed here
------------------------------------------------
Bhavcopy prices are NOT split/bonus-adjusted (yfinance's are, via
auto_adjust=True). A stock that split 1:5 during the date range will show a
mechanical ~80% "crash" in raw bhavcopy that never really happened. This
module does not correct for that — the caller (the band-study scan) is
expected to cross-check any qualifying touch against yfinance's adjusted
price for the same date and discard/flag ones that disagree sharply, since
that divergence IS the signal that something mechanical (not a real market
move) happened. Keeping that check at the call site, not here, is deliberate:
this module's only job is "what NSE's own tape says," unmodified.
"""

from __future__ import annotations

import csv
import io
import os
from datetime import date, timedelta

NSE_BHAVCOPY_URL = "https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{ddmmyyyy}.csv"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
    ),
    "Accept": "text/csv,application/csv,*/*",
}


def _cache_path(cache_dir: str, d: date) -> str:
    return os.path.join(cache_dir, f"{d.isoformat()}.csv")


def _parse_bhavcopy_csv(text: str, requested_date: date) -> list[dict] | None:
    """Parse one day's raw bhavcopy CSV into EQ-series rows.

    Returns None if this wasn't actually a trading day for `requested_date`.
    NSE's server does NOT 404 for a holiday/weekend — it silently serves the
    PREVIOUS trading day's file instead, with that earlier date inside the
    DATE1 column. Without checking DATE1 against what we asked for, a holiday
    would silently duplicate the prior day's numbers under the wrong date —
    exactly the kind of quiet corruption this whole project has been trying
    to rule out. So: read DATE1 off the first data row and bail if it doesn't
    match what we requested.
    """
    reader = csv.DictReader(io.StringIO(text))
    rows: list[dict] = []
    seen_date: str | None = None
    for raw in reader:
        row = {(k or "").strip(): (v or "").strip() for k, v in raw.items()}
        row_date_str = row.get("DATE1", "")
        if seen_date is None and row_date_str:
            seen_date = row_date_str
        if row.get("SERIES", "").upper() != "EQ":
            continue
        rows.append(row)

    if not rows or seen_date is None:
        return None
    try:
        # NSE's DATE1 format: "16-Jan-2026"
        from datetime import datetime as _dt

        parsed = _dt.strptime(seen_date, "%d-%b-%Y").date()
    except ValueError:
        return None
    if parsed != requested_date:
        return None  # holiday/weekend: server served a stale prior-day file

    return rows


def fetch_day(cache_dir: str, d: date, force_refresh: bool = False) -> list[dict] | None:
    """One trading day's EQ-series bhavcopy rows: [{SYMBOL, OPEN_PRICE, HIGH_PRICE,
    LOW_PRICE, CLOSE_PRICE, PREV_CLOSE, TTL_TRD_QNTY, ...}]. None if `d` wasn't
    a trading day (weekend/holiday) — never raises for that, since "no market
    that day" is an expected, common case, not a failure.

    Cached to disk per date: a bhavcopy file never changes once published, so
    once we have it there's no reason to ask NSE again.
    """
    path = _cache_path(cache_dir, d)
    if not force_refresh and os.path.exists(path):
        with open(path, "r", newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        return rows if rows else None
    # A cached "no trading that day" marker: an empty file distinguishes
    # "we checked and there's nothing" from "we haven't checked yet".
    if not force_refresh and os.path.exists(path + ".empty"):
        return None

    try:
        import requests

        url = NSE_BHAVCOPY_URL.format(ddmmyyyy=d.strftime("%d%m%Y"))
        resp = requests.get(url, headers=_HEADERS, timeout=20)
        resp.raise_for_status()
    except Exception:
        return None

    rows = _parse_bhavcopy_csv(resp.text, d)
    os.makedirs(cache_dir, exist_ok=True)
    if rows is None:
        # remember "not a trading day" so we don't re-request it every run
        open(path + ".empty", "w").close()
        return None

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    return rows


class NSEBhavcopyData:
    """BacktestData backed by NSE's own daily bhavcopy files — see module
    docstring for why this exists alongside YFinanceBacktestData."""

    def __init__(self, cache_dir: str):
        self.cache_dir = cache_dir

    def history(self, tickers: list[str], start: date, end: date) -> dict[str, list[dict]]:
        wanted = set(t.strip().upper() for t in tickers)
        out: dict[str, list[dict]] = {t: [] for t in wanted}

        d = start
        while d <= end:
            rows = fetch_day(self.cache_dir, d)
            if rows:
                for row in rows:
                    sym = row.get("SYMBOL", "").strip().upper()
                    if sym not in wanted:
                        continue
                    try:
                        out[sym].append(
                            {
                                "date": d.isoformat(),
                                "open": float(row["OPEN_PRICE"]),
                                "high": float(row["HIGH_PRICE"]),
                                "low": float(row["LOW_PRICE"]),
                                "close": float(row["CLOSE_PRICE"]),
                                "prev_close": float(row["PREV_CLOSE"]),
                                "volume": int(float(row["TTL_TRD_QNTY"])),
                            }
                        )
                    except (KeyError, ValueError):
                        continue
            d += timedelta(days=1)

        return {sym: bars for sym, bars in out.items() if bars}

    def full_day(self, d: date) -> list[dict]:
        """Every EQ-series row for one day, shaped like history()'s per-bar
        dicts plus `symbol`/`name` — used by the band-study scan to read the
        WHOLE market for a day at once instead of one symbol at a time."""
        rows = fetch_day(self.cache_dir, d)
        if not rows:
            return []
        out = []
        for row in rows:
            try:
                out.append(
                    {
                        "symbol": row["SYMBOL"].strip().upper(),
                        "date": d.isoformat(),
                        "open": float(row["OPEN_PRICE"]),
                        "high": float(row["HIGH_PRICE"]),
                        "low": float(row["LOW_PRICE"]),
                        "close": float(row["CLOSE_PRICE"]),
                        "prev_close": float(row["PREV_CLOSE"]),
                        "volume": int(float(row["TTL_TRD_QNTY"])),
                    }
                )
            except (KeyError, ValueError):
                continue
        return out
