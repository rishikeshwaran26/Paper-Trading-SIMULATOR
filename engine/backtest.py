"""Backtest: does a big one-day spike actually tend to partially reverse?

The question this answers
--------------------------
A stock that moves 20%+ in a day is often stretched, on the theory that such
an extreme move tends to partially reverse (short-term reversal) — the basis
for a mean-reversion short. That's a belief until you check it. This module
checks it, using real historical prices instead of a fresh live scan:

  1. Pick a past calendar date.
  2. Find every stock that spiked (or dropped) by the threshold that day —
     using yesterday's close vs that day's close.
  3. For each one, walk FORWARD day by day for a fixed window and record three
     things: how much further it ran before turning (the peak), how many days
     until the first red (down) day, and how many days until it fully round-
     tripped back to its pre-spike price (or never, within the window).
  4. Aggregate across every mover to get real numbers: reversion rate, average
     days to reverse, and whether the spike-day volume/RSI predicted it.

Why this can be done ENTIRELY with data we already have access to: unlike a
live scan (which needs today's price, unknowable in advance), a past date's
full price history — before AND after it — already exists on Yahoo Finance.
No waiting required; we can test any date in the past right now.

The "why" signal is intentionally narrow: news headlines are not available
for arbitrary past dates (Yahoo's news endpoint only returns *current*
headlines), so this module only uses volume and RSI at the spike — both
computable from historical prices for any date.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Callable, Optional, Protocol

from .indicators import rsi as _rsi

DEFAULT_THRESHOLD_PCT = 20.0
DEFAULT_WINDOW_DAYS = 30  # trading days to watch after the spike
DEFAULT_MIN_VOLUME = 50_000
VOLUME_BASELINE_DAYS = 30  # trading days used to compute "average volume"
RSI_LOOKBACK_DAYS = 14


# --- data provider seam ------------------------------------------------------
class BacktestData(Protocol):
    """Everything the backtest needs from the outside world. Swapped for a
    fake in tests so the walk-forward logic is verified with zero network
    calls."""

    def history(self, tickers: list[str], start: date, end: date) -> dict[str, list[dict]]:
        """Daily bars per symbol between start and end (inclusive), oldest
        first: [{date: "YYYY-MM-DD", open, high, low, close, volume}].

        open/high/low are what let a band-touch study tell "hit 19.5% intraday
        then closed at 15%" apart from "closed at 19.5%" — the close-only shape
        this module started with can't see the difference. Existing callers
        that only read `close`/`volume` keep working unchanged.
        """
        ...


# --- reliable fetching ---------------------------------------------------------
def _fetch_with_retry(
    data: BacktestData, chunk: list[str], start: date, end: date, retries: int = 1
) -> tuple[dict[str, list[dict]], list[str]]:
    """Fetch one chunk, retrying only the symbols that came back empty.

    Why this matters: Yahoo Finance is an unofficial, undocumented endpoint —
    an individual symbol inside a 100-name batch can transiently fail (rate
    limit, momentary hiccup) while the rest of the batch succeeds. Without a
    retry, that symbol silently vanishes from the result with no indication
    anything went wrong — for a study whose whole point is an accurate count,
    a silent gap is worse than a slow one. One retry, after a short pause,
    resolves the overwhelming majority of these; whatever is still missing
    after that is reported back to the caller instead of hidden.
    """
    try:
        history = data.history(chunk, start, end)
    except Exception:
        history = {}
    missing = [s for s in chunk if not history.get(s)]

    for attempt in range(retries):
        if not missing:
            break
        time.sleep(1.5)  # give the upstream a moment before asking again
        try:
            retried = data.history(missing, start, end)
        except Exception:
            retried = {}
        for sym, bars in retried.items():
            if bars:
                history[sym] = bars
        missing = [s for s in missing if not history.get(s)]

    return history, missing


# --- result shapes -------------------------------------------------------------
@dataclass
class BacktestMover:
    symbol: str
    name: str
    direction: str  # "up" or "down"
    spike_pct: float
    price_at_spike: float
    prev_close: float
    volume: int
    avg_volume: int
    vol_ratio: float
    rsi: Optional[float]

    # what happened AFTER the spike, within the tracking window
    peak_price: float
    peak_offset_days: int          # 0 = the spike day itself was the peak
    first_red_offset_days: Optional[int] = None   # None = never within window
    round_trip_offset_days: Optional[int] = None  # None = never fully reverted
    reverted: bool = False         # convenience: round_trip_offset_days is not None
    spark: list[float] = field(default_factory=list)  # prev_close + spike day + forward closes

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "name": self.name,
            "direction": self.direction,
            "spike_pct": self.spike_pct,
            "price_at_spike": self.price_at_spike,
            "prev_close": self.prev_close,
            "volume": self.volume,
            "avg_volume": self.avg_volume,
            "vol_ratio": self.vol_ratio,
            "rsi": self.rsi,
            "peak_price": self.peak_price,
            "peak_offset_days": self.peak_offset_days,
            "first_red_offset_days": self.first_red_offset_days,
            "round_trip_offset_days": self.round_trip_offset_days,
            "reverted": self.reverted,
            "spark": self.spark,
        }


@dataclass
class BacktestSummary:
    """Aggregate stats across every mover found on the target date — the
    actual answer to 'does this idea work'."""

    mover_count: int
    reverted_count: int
    reverted_pct: float
    avg_days_to_revert: Optional[float]     # among those that DID revert
    avg_days_to_first_red: Optional[float]  # among those that had one
    high_rsi_reverted_pct: Optional[float]  # movers with RSI >= 70
    low_rsi_reverted_pct: Optional[float]   # movers with RSI < 70 (or unknown)

    def to_dict(self) -> dict:
        return {
            "mover_count": self.mover_count,
            "reverted_count": self.reverted_count,
            "reverted_pct": self.reverted_pct,
            "avg_days_to_revert": self.avg_days_to_revert,
            "avg_days_to_first_red": self.avg_days_to_first_red,
            "high_rsi_reverted_pct": self.high_rsi_reverted_pct,
            "low_rsi_reverted_pct": self.low_rsi_reverted_pct,
        }


@dataclass
class BacktestResult:
    target_date: str
    direction: str
    threshold_pct: float
    window_days: int
    universe_count: int
    movers: list[BacktestMover] = field(default_factory=list)
    missing_symbols: list[str] = field(default_factory=list)  # couldn't be fetched even after retry

    def summary(self) -> BacktestSummary:
        return _summarize(self.movers)


# --- the core simulation -----------------------------------------------------
def _summarize(movers: list[BacktestMover]) -> BacktestSummary:
    n = len(movers)
    if n == 0:
        return BacktestSummary(0, 0, 0.0, None, None, None, None)

    reverted = [m for m in movers if m.reverted]
    had_red = [m for m in movers if m.first_red_offset_days is not None]
    high_rsi = [m for m in movers if m.rsi is not None and m.rsi >= 70]
    low_rsi = [m for m in movers if m.rsi is not None and m.rsi < 70]

    def pct_reverted(group: list[BacktestMover]) -> Optional[float]:
        return round(sum(1 for m in group if m.reverted) / len(group) * 100, 1) if group else None

    return BacktestSummary(
        mover_count=n,
        reverted_count=len(reverted),
        reverted_pct=round(len(reverted) / n * 100, 1),
        avg_days_to_revert=(
            round(sum(m.round_trip_offset_days for m in reverted) / len(reverted), 1)
            if reverted else None
        ),
        avg_days_to_first_red=(
            round(sum(m.first_red_offset_days for m in had_red) / len(had_red), 1)
            if had_red else None
        ),
        high_rsi_reverted_pct=pct_reverted(high_rsi),
        low_rsi_reverted_pct=pct_reverted(low_rsi),
    )


def _trading_days_needed(window_days: int) -> timedelta:
    """Calendar-day span that comfortably covers `window_days` TRADING days,
    padding for weekends/holidays (roughly 7/5 calendar days per trading day,
    plus a safety margin)."""
    return timedelta(days=int(window_days * 1.6) + 10)


def run_backtest(
    rows: list[dict],
    data: BacktestData,
    target_date: date,
    direction: str = "up",
    threshold_pct: float = DEFAULT_THRESHOLD_PCT,
    window_days: int = DEFAULT_WINDOW_DAYS,
    min_volume: int = DEFAULT_MIN_VOLUME,
    chunk_size: int = 100,
    progress: Optional[Callable[[int, int, str], None]] = None,
) -> BacktestResult:
    """Replay `target_date` across `rows` ([{symbol, name}]) and measure what
    happened next. `direction` is "up" (spikes, for the short thesis) or
    "down" (drops, for a bounce thesis).
    """
    lookback_start = target_date - timedelta(days=max(VOLUME_BASELINE_DAYS, RSI_LOOKBACK_DAYS) * 2 + 20)
    lookahead_end = target_date + _trading_days_needed(window_days)

    tickers = [r["symbol"] for r in rows]
    name_of = {r["symbol"]: r.get("name", r["symbol"]) for r in rows}
    movers: list[BacktestMover] = []
    missing_symbols: list[str] = []
    total = len(tickers)

    for start in range(0, total, chunk_size):
        chunk = tickers[start : start + chunk_size]
        history, missing = _fetch_with_retry(data, chunk, lookback_start, lookahead_end)
        missing_symbols.extend(missing)
        for sym in chunk:
            bars = history.get(sym) or []
            mover = _evaluate_symbol(
                sym, name_of.get(sym, sym), bars, target_date, direction,
                threshold_pct, window_days, min_volume,
            )
            if mover:
                movers.append(mover)
        if progress:
            done = min(start + chunk_size, total)
            progress(done, total, f"Replayed {done}/{total} stocks for {target_date.isoformat()}")

    return BacktestResult(
        target_date=target_date.isoformat(),
        direction=direction,
        threshold_pct=threshold_pct,
        window_days=window_days,
        universe_count=total,
        movers=movers,
        missing_symbols=missing_symbols,
    )


def _evaluate_symbol(
    symbol: str,
    name: str,
    bars: list[dict],
    target_date: date,
    direction: str,
    threshold_pct: float,
    window_days: int,
    min_volume: int,
) -> Optional[BacktestMover]:
    """Find `target_date` in this symbol's bar series and, if it qualifies as
    a spike, walk forward to measure what happened. Returns None if the symbol
    has no bar on that date or doesn't qualify."""
    idx = next((i for i, b in enumerate(bars) if b["date"] == target_date.isoformat()), None)
    if idx is None or idx == 0:
        return None  # no data for that day, or no prior day to compare against

    today = bars[idx]
    prev = bars[idx - 1]
    prev_close = prev["close"]
    price_at_spike = today["close"]
    if not prev_close or prev_close <= 0:
        return None

    pct = round((price_at_spike - prev_close) / prev_close * 100, 2)
    if direction == "up" and pct < threshold_pct:
        return None
    if direction == "down" and pct > -threshold_pct:
        return None
    volume = int(today.get("volume") or 0)
    if volume < min_volume:
        return None

    # volume baseline: the trading days strictly before the spike
    baseline_start = max(0, idx - VOLUME_BASELINE_DAYS)
    hist_vols = [int(b.get("volume") or 0) for b in bars[baseline_start:idx] if b.get("volume")]
    avg_volume = int(sum(hist_vols) / len(hist_vols)) if hist_vols else 0
    vol_ratio = round(volume / avg_volume, 2) if avg_volume > 0 else 0.0

    rsi_start = max(0, idx - RSI_LOOKBACK_DAYS * 3)
    closes_for_rsi = [b["close"] for b in bars[rsi_start : idx + 1] if b.get("close") is not None]
    spike_rsi = _rsi(closes_for_rsi)

    # --- walk forward -------------------------------------------------------
    forward = bars[idx + 1 : idx + 1 + window_days]
    peak_price = price_at_spike
    peak_offset = 0
    first_red_offset: Optional[int] = None
    round_trip_offset: Optional[int] = None
    prev_bar_close = price_at_spike
    spark = [prev_close, price_at_spike]  # the path a sparkline draws: before -> spike -> forward

    for offset, bar in enumerate(forward, start=1):
        close = bar.get("close")
        if close is None:
            continue
        spark.append(close)
        if close > peak_price:
            peak_price = close
            peak_offset = offset
        if first_red_offset is None and close < prev_bar_close:
            first_red_offset = offset
        if round_trip_offset is None:
            reverted_now = (
                close <= prev_close if direction == "up" else close >= prev_close
            )
            if reverted_now:
                round_trip_offset = offset
        prev_bar_close = close

    return BacktestMover(
        symbol=symbol,
        name=name,
        direction="up" if pct > 0 else "down",
        spike_pct=pct,
        price_at_spike=round(price_at_spike, 2),
        prev_close=round(prev_close, 2),
        volume=volume,
        avg_volume=avg_volume,
        vol_ratio=vol_ratio,
        rsi=spike_rsi,
        peak_price=round(peak_price, 2),
        peak_offset_days=peak_offset,
        first_red_offset_days=first_red_offset,
        round_trip_offset_days=round_trip_offset,
        reverted=round_trip_offset is not None,
        spark=[round(v, 2) for v in spark],
    )


# --- band-touch study (Phase 1: does the day AFTER a 19-20% touch tell us
# anything?) ------------------------------------------------------------------
# This is deliberately simpler than run_backtest()'s multi-week walk-forward:
# one date range, one tight % band, one single "what happened the very next
# day" measurement. The point is to prove the idea on a small, cheap slice
# before scaling up to a longer window or a wider band (Phase 2+).
#
# "Touch" is checked against the day's INTRADAY HIGH (or LOW for a down-band),
# not the close — a stock that spikes 19.5% intraday and fades back to close
# +15% still belongs in the study. Close-only detection (what run_backtest
# uses) would miss it entirely, which is exactly the gap this fixes.
DEFAULT_BAND_LOW = 19.0
DEFAULT_BAND_HIGH = 20.0

# How many TRADING days after a touch to follow it for, by default. The window
# deliberately runs past the scan's end_date — a stock touching the band on the
# last day of January still needs its February follow-up days, or the last few
# touches of every range would be silently unscored.
DEFAULT_FOLLOW_DAYS = 5
MAX_FOLLOW_DAYS = 30

# How many TRADING days BEFORE a touch to also track — lets a graph show the
# ramp-in to the spike, not just the reversion after it. 0 (the default) means
# "don't bother" — most studies only care about what happens next.
DEFAULT_BEFORE_DAYS = 0
MAX_BEFORE_DAYS = 30


@dataclass
class FollowDay:
    """One trading day after a touch. `offset` is 1 for the very next trading
    day, 2 for the one after, and so on — trading days, so weekends/holidays
    never consume an offset."""

    offset: int
    date: str
    open: Optional[float]
    high: Optional[float]
    low: Optional[float]
    close: float
    pct_from_touch: float        # close vs the TOUCH day's close — cumulative move since the spike
    pct_from_prev_day: float     # close vs the previous tracked day's close — that day's own step
    open_close_pct: Optional[float]  # this session's own open->close, what an intraday trade captures

    def to_dict(self) -> dict:
        return {
            "offset": self.offset,
            "date": self.date,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "pct_from_touch": self.pct_from_touch,
            "pct_from_prev_day": self.pct_from_prev_day,
            "open_close_pct": self.open_close_pct,
        }


@dataclass
class BandTouch:
    symbol: str
    name: str
    date: str            # the day it touched the band
    direction: str        # "up" or "down"
    prev_close: float
    high: float
    low: float
    touch_pct: float       # % move that qualified it (vs the day's high, or low if down-band)
    close: float
    close_pct: float        # standard day change: close vs prev_close, for reference
    volume: int

    # Every tracked trading day after the touch, oldest first. Length is capped
    # by the run's follow-days limit, and can be shorter if the data simply
    # runs out (a touch near the very end of available history).
    follow_days: list[FollowDay] = field(default_factory=list)

    # Every tracked trading day BEFORE the touch, nearest-to-touch first
    # (offset -1, -2, ... going further back) — the ramp-in to the spike.
    # Empty unless the run asked for before_days > 0.
    before_days: list[FollowDay] = field(default_factory=list)

    # Only meaningful for the bhavcopy path (raw, unadjusted prices): True if
    # Yahoo's split/bonus-ADJUSTED % change for this date disagrees sharply
    # with bhavcopy's raw touch_pct — the tell-tale sign the "spike" was a
    # corporate action (a mechanical price cut), not a real market move. See
    # cross_check_corporate_actions().
    corporate_action_suspected: bool = False

    # --- day-2 accessors -----------------------------------------------------
    # Day 2 is just follow_days[0]. Exposed as properties (not stored twice) so
    # the day-2 columns, the existing summary maths and the API shape all keep
    # working unchanged now that the study tracks N days instead of exactly one.
    def _day(self, offset: int) -> Optional[FollowDay]:
        days = self.follow_days if offset >= 0 else self.before_days
        for f in days:
            if f.offset == offset:
                return f
        return None

    @property
    def has_day2(self) -> bool:
        return self._day(1) is not None

    @property
    def day2_date(self) -> Optional[str]:
        d = self._day(1)
        return d.date if d else None

    @property
    def day2_open(self) -> Optional[float]:
        d = self._day(1)
        return d.open if d else None

    @property
    def day2_high(self) -> Optional[float]:
        d = self._day(1)
        return d.high if d else None

    @property
    def day2_low(self) -> Optional[float]:
        d = self._day(1)
        return d.low if d else None

    @property
    def day2_close(self) -> Optional[float]:
        d = self._day(1)
        return d.close if d else None

    @property
    def day2_pct_change(self) -> Optional[float]:
        d = self._day(1)
        return d.pct_from_touch if d else None

    @property
    def day2_open_close_pct(self) -> Optional[float]:
        d = self._day(1)
        return d.open_close_pct if d else None

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "name": self.name,
            "date": self.date,
            "direction": self.direction,
            "prev_close": self.prev_close,
            "high": self.high,
            "low": self.low,
            "touch_pct": self.touch_pct,
            "close": self.close,
            "close_pct": self.close_pct,
            "volume": self.volume,
            "follow_days": [f.to_dict() for f in self.follow_days],
            "before_days": [f.to_dict() for f in self.before_days],
            "day2_date": self.day2_date,
            "day2_open": self.day2_open,
            "day2_high": self.day2_high,
            "day2_low": self.day2_low,
            "day2_close": self.day2_close,
            "day2_pct_change": self.day2_pct_change,
            "day2_open_close_pct": self.day2_open_close_pct,
            "has_day2": self.has_day2,
            "corporate_action_suspected": self.corporate_action_suspected,
        }


@dataclass
class DaySummary:
    """How the whole cohort of touches had moved by ONE given day after the
    spike. `offset` 1 = the next trading day, 2 = the day after, and so on.

    Measured cumulatively from the touch day's close, so "day 3" answers
    "where is it now, 3 days on?" rather than "what did it do on day 3
    alone" — that's the question a trader deciding when to exit is asking.
    """

    offset: int
    scored: int          # touches that actually have data this far out
    up_count: int
    down_count: int
    flat_count: int
    up_pct: float
    down_pct: float
    avg_pct_up: Optional[float]
    avg_pct_down: Optional[float]

    def to_dict(self) -> dict:
        return {
            "offset": self.offset,
            "scored": self.scored,
            "up_count": self.up_count,
            "down_count": self.down_count,
            "flat_count": self.flat_count,
            "up_pct": self.up_pct,
            "down_pct": self.down_pct,
            "avg_pct_up": self.avg_pct_up,
            "avg_pct_down": self.avg_pct_down,
        }


@dataclass
class BandStudySummary:
    """Up and down movers are tracked and averaged SEPARATELY on purpose — a
    blended average would hide exactly the split the study exists to reveal.

    The top-level fields describe day 2 (the first day after the touch), which
    keeps the original headline intact; `per_day` carries the same breakdown
    for every tracked day, so the UI can show "by day 1 / 2 / 3 / all 5".
    """

    total_touches: int
    with_day2: int
    up_count: int
    down_count: int
    flat_count: int
    up_pct: float
    down_pct: float
    avg_day2_pct_up: Optional[float]
    avg_day2_pct_down: Optional[float]
    per_day: list[DaySummary] = field(default_factory=list)
    # Same breakdown for the days BEFORE the touch, ordered furthest-back
    # first (chronological) so it reads left-to-right into the touch itself.
    # Empty unless the run tracked before_days > 0.
    per_before_day: list[DaySummary] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "total_touches": self.total_touches,
            "with_day2": self.with_day2,
            "up_count": self.up_count,
            "down_count": self.down_count,
            "flat_count": self.flat_count,
            "up_pct": self.up_pct,
            "down_pct": self.down_pct,
            "avg_day2_pct_up": self.avg_day2_pct_up,
            "avg_day2_pct_down": self.avg_day2_pct_down,
            "per_day": [d.to_dict() for d in self.per_day],
            "per_before_day": [d.to_dict() for d in self.per_before_day],
        }


@dataclass
class BandStudyResult:
    start_date: str
    end_date: str
    band_low: float
    band_high: float
    direction: str
    universe_count: int
    touches: list[BandTouch] = field(default_factory=list)
    missing_symbols: list[str] = field(default_factory=list)  # couldn't be fetched even after retry
    follow_days: int = DEFAULT_FOLLOW_DAYS
    before_days: int = DEFAULT_BEFORE_DAYS

    def summary(self) -> BandStudySummary:
        return _summarize_band_touches(self.touches, self.follow_days, self.before_days)


def _summarize_one_day(touches: list[BandTouch], offset: int) -> DaySummary:
    """Cohort stats at a single day-offset. Touches without data that far out
    are excluded from `scored` rather than counted as flat — a missing day is
    not a zero move, and treating it as one would quietly drag averages toward
    nothing."""
    moves = []
    for t in touches:
        d = t._day(offset)
        if d is not None:
            moves.append(d.pct_from_touch)
    up = [m for m in moves if m > 0]
    down = [m for m in moves if m < 0]
    flat = [m for m in moves if m == 0]
    scored = len(moves)
    return DaySummary(
        offset=offset,
        scored=scored,
        up_count=len(up),
        down_count=len(down),
        flat_count=len(flat),
        up_pct=round(len(up) / scored * 100, 1) if scored else 0.0,
        down_pct=round(len(down) / scored * 100, 1) if scored else 0.0,
        avg_pct_up=round(sum(up) / len(up), 2) if up else None,
        avg_pct_down=round(sum(down) / len(down), 2) if down else None,
    )


def _summarize_band_touches(
    touches: list[BandTouch], follow_days: int = DEFAULT_FOLLOW_DAYS, before_days: int = DEFAULT_BEFORE_DAYS
) -> BandStudySummary:
    n = len(touches)
    per_day = [_summarize_one_day(touches, offset) for offset in range(1, follow_days + 1)]
    # Furthest-back first, so it reads chronologically into the touch.
    per_before_day = [_summarize_one_day(touches, -offset) for offset in range(before_days, 0, -1)]
    day2 = per_day[0] if per_day else _summarize_one_day(touches, 1)
    return BandStudySummary(
        total_touches=n,
        with_day2=day2.scored,
        up_count=day2.up_count,
        down_count=day2.down_count,
        flat_count=day2.flat_count,
        up_pct=day2.up_pct,
        down_pct=day2.down_pct,
        avg_day2_pct_up=day2.avg_pct_up,
        avg_day2_pct_down=day2.avg_pct_down,
        per_day=per_day,
        per_before_day=per_before_day,
    )


def _trading_days_buffer(days: int) -> timedelta:
    """Calendar-day padding that comfortably covers `days` TRADING days,
    accounting for weekends/holidays."""
    return timedelta(days=int(days * 1.6) + 10)


def _build_follow_days(touch_close: float, next_bars: list[dict], limit: int) -> list[FollowDay]:
    """Turn the bars AFTER a touch into FollowDay records, oldest first.

    `next_bars` must already be only trading days (both providers skip
    weekends/holidays), so list position maps straight onto trading-day offset:
    next_bars[0] is offset 1. Stops at `limit`, or earlier if the data runs
    out — a touch near the end of available history simply gets a shorter
    window rather than being dropped.
    """
    out: list[FollowDay] = []
    prev_close = touch_close
    for i, bar in enumerate(next_bars[:limit], start=1):
        close = bar.get("close")
        if close is None:
            break  # a gap mid-window would make every later offset misleading
        open_ = bar.get("open")
        out.append(
            FollowDay(
                offset=i,
                date=bar["date"],
                open=round(open_, 2) if open_ is not None else None,
                high=round(bar["high"], 2) if bar.get("high") is not None else None,
                low=round(bar["low"], 2) if bar.get("low") is not None else None,
                close=round(close, 2),
                pct_from_touch=round((close - touch_close) / touch_close * 100, 2),
                pct_from_prev_day=round((close - prev_close) / prev_close * 100, 2),
                open_close_pct=round((close - open_) / open_ * 100, 2) if open_ else None,
            )
        )
        prev_close = close
    return out


def _build_before_days(touch_close: float, prev_bars: list[dict], limit: int) -> list[FollowDay]:
    """Turn the bars BEFORE a touch into FollowDay records, nearest-to-touch
    first (offset -1, -2, ... -limit) — mirrors _build_follow_days but walking
    backward, so a graph can show the ramp-in to the spike as well as the
    reversion after it.

    `prev_bars` must already be ordered nearest-to-touch first (prev_bars[0]
    is the trading day immediately before the touch). Every `pct_from_touch`
    is still measured against the TOUCH day's close, so offset -1 and +1 read
    on the same scale either side of 0.
    """
    out: list[FollowDay] = []
    prev_close = touch_close
    for i, bar in enumerate(prev_bars[:limit], start=1):
        close = bar.get("close")
        if close is None:
            break  # a gap mid-window would make every earlier offset misleading
        open_ = bar.get("open")
        out.append(
            FollowDay(
                offset=-i,
                date=bar["date"],
                open=round(open_, 2) if open_ is not None else None,
                high=round(bar["high"], 2) if bar.get("high") is not None else None,
                low=round(bar["low"], 2) if bar.get("low") is not None else None,
                close=round(close, 2),
                pct_from_touch=round((close - touch_close) / touch_close * 100, 2),
                pct_from_prev_day=round((close - prev_close) / prev_close * 100, 2),
                open_close_pct=round((close - open_) / open_ * 100, 2) if open_ else None,
            )
        )
        prev_close = close
    return out


def scan_band_touches(
    rows: list[dict],
    data: BacktestData,
    start_date: date,
    end_date: date,
    band_low: float = DEFAULT_BAND_LOW,
    band_high: float = DEFAULT_BAND_HIGH,
    direction: str = "up",
    min_volume: int = DEFAULT_MIN_VOLUME,
    chunk_size: int = 100,
    progress: Optional[Callable[[int, int, str], None]] = None,
    follow_days: int = DEFAULT_FOLLOW_DAYS,
    before_days: int = DEFAULT_BEFORE_DAYS,
) -> BandStudyResult:
    """For every trading day in [start_date, end_date], find every stock whose
    intraday move touched [band_low, band_high]%, then track the next
    `follow_days` TRADING days (and, if requested, the `before_days` TRADING
    days leading up to it). `rows` is [{symbol, name}] — the universe.
    """
    # 10 days is "just enough for a prev_close before start_date"; before_days
    # needs its own buffer on top of that when a graph wants the ramp-in too.
    lookback_start = start_date - timedelta(days=10) - _trading_days_buffer(before_days)
    # The follow window deliberately runs PAST end_date: a touch on the last
    # day of the range still needs its follow-up days, which fall in the next
    # month. Without this, the final touches of every scan would go unscored.
    lookahead_end = end_date + _trading_days_buffer(follow_days)

    tickers = [r["symbol"] for r in rows]
    name_of = {r["symbol"]: r.get("name", r["symbol"]) for r in rows}
    touches: list[BandTouch] = []
    missing_symbols: list[str] = []
    total = len(tickers)

    for start in range(0, total, chunk_size):
        chunk = tickers[start : start + chunk_size]
        history, missing = _fetch_with_retry(data, chunk, lookback_start, lookahead_end)
        missing_symbols.extend(missing)
        for sym in chunk:
            bars = history.get(sym) or []
            touches.extend(
                _touches_for_symbol(
                    sym, name_of.get(sym, sym), bars, start_date, end_date,
                    band_low, band_high, direction, min_volume, follow_days, before_days,
                )
            )
        if progress:
            done = min(start + chunk_size, total)
            progress(done, total, f"Scanned {done}/{total} stocks for {start_date.isoformat()}–{end_date.isoformat()}")

    return BandStudyResult(
        start_date=start_date.isoformat(),
        end_date=end_date.isoformat(),
        band_low=band_low,
        band_high=band_high,
        direction=direction,
        universe_count=total,
        touches=touches,
        missing_symbols=missing_symbols,
        follow_days=follow_days,
        before_days=before_days,
    )


def _touches_for_symbol(
    symbol: str,
    name: str,
    bars: list[dict],
    start_date: date,
    end_date: date,
    band_low: float,
    band_high: float,
    direction: str,
    min_volume: int,
    follow_days: int = DEFAULT_FOLLOW_DAYS,
    before_days: int = DEFAULT_BEFORE_DAYS,
) -> list[BandTouch]:
    out: list[BandTouch] = []
    start_iso, end_iso = start_date.isoformat(), end_date.isoformat()
    for i in range(1, len(bars)):  # start at 1: every bar needs a previous close
        bar = bars[i]
        if not (start_iso <= bar["date"] <= end_iso):
            continue
        prev_close = bars[i - 1]["close"]
        if not prev_close or prev_close <= 0:
            continue
        high, low, close = bar.get("high"), bar.get("low"), bar.get("close")
        volume = int(bar.get("volume") or 0)
        if volume < min_volume:
            continue

        if direction == "up":
            touch_pct = round((high - prev_close) / prev_close * 100, 2)
            qualifies = band_low <= touch_pct <= band_high
        else:
            touch_pct = round((low - prev_close) / prev_close * 100, 2)
            qualifies = -band_high <= touch_pct <= -band_low
        if not qualifies:
            continue

        close_pct = round((close - prev_close) / prev_close * 100, 2)
        touch = BandTouch(
            symbol=symbol,
            name=name,
            date=bar["date"],
            direction=direction,
            prev_close=round(prev_close, 2),
            high=round(high, 2),
            low=round(low, 2),
            touch_pct=touch_pct,
            close=round(close, 2),
            close_pct=close_pct,
            volume=volume,
            follow_days=_build_follow_days(close, bars[i + 1 :], follow_days),
            before_days=_build_before_days(close, list(reversed(bars[max(0, i - before_days) : i])), before_days),
        )
        out.append(touch)
    return out


# --- bhavcopy-backed band study: broad-then-narrow ---------------------------
# The whole point of this pipeline: NSE's bhavcopy answers "who touched the
# band, across the WHOLE exchange, for a WHOLE month" in ~20 file downloads
# (one per trading day) instead of ~2,000 Yahoo per-symbol lookups — and it's
# the exchange's own authoritative, unadjusted tape. The one thing it can't
# tell us is whether a "touch" was a real price move or a mechanical split/
# bonus cut, because bhavcopy prices are NOT adjusted for corporate actions.
# So: scan broad with bhavcopy (fast, ~2,000 symbols), then cross-check ONLY
# the handful of resulting touches (typically under 100) against Yahoo's
# split/bonus-ADJUSTED prices for the same dates — a narrow, cheap lookup
# that Yahoo's rate limit has no trouble with.
CORPORATE_ACTION_DIVERGENCE_PCT = 5.0  # bhavcopy vs Yahoo-adjusted gap that flags a touch


class BhavcopyProvider(Protocol):
    """What the bhavcopy scan needs. Matches engine.bhavcopy.NSEBhavcopyData —
    typed narrowly here so this module doesn't import bhavcopy.py directly
    (keeps the dependency direction one-way: bhavcopy depends on nothing here
    except the BacktestData protocol it implements)."""

    def full_day(self, d: date) -> list[dict]:
        """Every EQ-series row for one trading day: [{symbol, date, open, high,
        low, close, prev_close, volume}]. Empty list if not a trading day."""
        ...


def scan_band_touches_bhavcopy(
    rows: list[dict],
    bhav: BhavcopyProvider,
    yf_data: Optional[BacktestData],
    start_date: date,
    end_date: date,
    band_low: float = DEFAULT_BAND_LOW,
    band_high: float = DEFAULT_BAND_HIGH,
    direction: str = "up",
    min_volume: int = DEFAULT_MIN_VOLUME,
    progress: Optional[Callable[[int, int, str], None]] = None,
    follow_days: int = DEFAULT_FOLLOW_DAYS,
    before_days: int = DEFAULT_BEFORE_DAYS,
) -> BandStudyResult:
    """Same question as scan_band_touches() — which stocks touched the band and
    what happened over the next `follow_days` trading days (and, if requested,
    the `before_days` leading up to it) — answered via NSE's own daily
    bhavcopy instead of per-symbol Yahoo queries. `rows` supplies symbol->name
    only; the actual day-by-day universe comes from whatever bhavcopy itself
    lists that day (more authoritative than any separately maintained symbol
    list).
    """
    name_of = {r["symbol"]: r.get("name", r["symbol"]) for r in rows}

    total_days = (end_date - start_date).days + 1
    touches: list[BandTouch] = []
    day_cache: dict[str, dict[str, dict]] = {}  # date iso -> {symbol: row}, populated as we walk

    def day_rows(d: date) -> dict[str, dict]:
        key = d.isoformat()
        if key not in day_cache:
            day_cache[key] = {r["symbol"]: r for r in bhav.full_day(d)}
        return day_cache[key]

    def next_trading_days(after: date, n: int) -> list[tuple[date, dict[str, dict]]]:
        """The next `n` actual TRADING days after `after`, skipping weekends and
        holidays (which bhavcopy reports as empty — see engine.bhavcopy).

        Deliberately walks past end_date: a touch on the last day of the range
        still needs its follow-up days, which fall in the following month.
        Capped by a generous calendar-day budget so a long market closure can
        never turn this into an unbounded walk.
        """
        found: list[tuple[date, dict[str, dict]]] = []
        budget = int(n * 1.6) + 10  # calendar days that comfortably contain n trading days
        for offset in range(1, budget + 1):
            d = after + timedelta(days=offset)
            rows_for_day = day_rows(d)
            if rows_for_day:
                found.append((d, rows_for_day))
                if len(found) >= n:
                    break
        return found

    def prev_trading_days(before: date, n: int) -> list[tuple[date, dict[str, dict]]]:
        """The previous `n` actual TRADING days before `before`, nearest first,
        skipping weekends and holidays — the mirror image of
        next_trading_days(), used to build the ramp-in to a touch."""
        found: list[tuple[date, dict[str, dict]]] = []
        budget = int(n * 1.6) + 10
        for offset in range(1, budget + 1):
            d = before - timedelta(days=offset)
            rows_for_day = day_rows(d)
            if rows_for_day:
                found.append((d, rows_for_day))
                if len(found) >= n:
                    break
        return found

    d = start_date
    day_num = 0
    while d <= end_date:
        day_num += 1
        rows_today = day_rows(d)
        if progress:
            progress(day_num, total_days, f"Scanned {d.isoformat()} ({day_num}/{total_days} days)")
        if not rows_today:
            d += timedelta(days=1)
            continue

        # Computed lazily and shared by every touch found today — the follow-up
        # (and lead-in) trading days are the same calendar days for all of them.
        forward: Optional[list[tuple[date, dict[str, dict]]]] = None
        backward: Optional[list[tuple[date, dict[str, dict]]]] = None

        for sym, row in rows_today.items():
            prev_close = row.get("prev_close")
            high, low, close = row.get("high"), row.get("low"), row.get("close")
            volume = int(row.get("volume") or 0)
            if not prev_close or prev_close <= 0 or volume < min_volume:
                continue

            if direction == "up":
                touch_pct = round((high - prev_close) / prev_close * 100, 2)
                qualifies = band_low <= touch_pct <= band_high
            else:
                touch_pct = round((low - prev_close) / prev_close * 100, 2)
                qualifies = -band_high <= touch_pct <= -band_low
            if not qualifies:
                continue

            close_pct = round((close - prev_close) / prev_close * 100, 2)

            if forward is None:
                forward = next_trading_days(d, follow_days)
            # Pull this symbol's bar out of each forward trading day. A symbol
            # missing from a day's file (suspended, delisted) ends its window
            # there rather than silently shifting later days into wrong offsets.
            next_bars: list[dict] = []
            for fd, frows in forward:
                frow = frows.get(sym)
                if not frow:
                    break
                next_bars.append({**frow, "date": fd.isoformat()})

            prev_bars: list[dict] = []
            if before_days > 0:
                if backward is None:
                    backward = prev_trading_days(d, before_days)
                for pd, prows in backward:
                    prow = prows.get(sym)
                    if not prow:
                        break  # same reasoning as forward: stop rather than shift offsets
                    prev_bars.append({**prow, "date": pd.isoformat()})

            touches.append(
                BandTouch(
                    symbol=sym, name=name_of.get(sym, sym), date=d.isoformat(), direction=direction,
                    prev_close=round(prev_close, 2), high=round(high, 2), low=round(low, 2),
                    touch_pct=touch_pct, close=round(close, 2), close_pct=close_pct, volume=volume,
                    follow_days=_build_follow_days(close, next_bars, follow_days),
                    before_days=_build_before_days(close, prev_bars, before_days),
                )
            )
        d += timedelta(days=1)

    if yf_data is not None:
        cross_check_corporate_actions(touches, yf_data)

    return BandStudyResult(
        start_date=start_date.isoformat(), end_date=end_date.isoformat(),
        band_low=band_low, band_high=band_high, direction=direction,
        universe_count=len(rows), touches=touches, missing_symbols=[],
        follow_days=follow_days, before_days=before_days,
    )


def cross_check_corporate_actions(
    touches: list[BandTouch], yf_data: BacktestData, threshold_pct: float = CORPORATE_ACTION_DIVERGENCE_PCT
) -> None:
    """Mutates `touches` in place, setting corporate_action_suspected=True on
    any whose bhavcopy (raw) close_pct disagrees with Yahoo's (split/bonus-
    ADJUSTED) close_pct by more than `threshold_pct` points.

    This is the narrow half of the pipeline — typically under 100 symbols,
    each needing only a couple of days of history, so it's cheap regardless
    of how throttled Yahoo is for large batches.
    """
    if not touches:
        return
    by_symbol: dict[str, list[BandTouch]] = {}
    for t in touches:
        by_symbol.setdefault(t.symbol, []).append(t)

    symbols = list(by_symbol.keys())
    dates = [date.fromisoformat(t.date) for t in touches]
    start = min(dates) - timedelta(days=5)
    end = max(dates) + timedelta(days=2)

    try:
        history = yf_data.history(symbols, start, end)
    except Exception:
        return  # cross-check is a safety NET, not a hard dependency — never block the study over it

    for sym, sym_touches in by_symbol.items():
        bars = {b["date"]: b for b in history.get(sym, [])}
        for t in sym_touches:
            bar = bars.get(t.date)
            if bar is None:
                continue  # Yahoo has no data for this date — nothing to compare, leave unflagged
            prev_bar_close = None
            # find the bar immediately before t.date in this symbol's series
            sorted_dates = sorted(bars.keys())
            idx = sorted_dates.index(t.date) if t.date in sorted_dates else -1
            if idx > 0:
                prev_bar_close = bars[sorted_dates[idx - 1]]["close"]
            if not prev_bar_close:
                continue
            yahoo_close_pct = (bar["close"] - prev_bar_close) / prev_bar_close * 100
            if abs(yahoo_close_pct - t.close_pct) > threshold_pct:
                t.corporate_action_suspected = True


# --- the real (yfinance) data provider --------------------------------------
class YFinanceBacktestData:
    """BacktestData backed by yfinance's historical daily bars — the same
    library the price charts use, just queried over an arbitrary past date
    range instead of "recent"."""

    def __init__(self, exchange: str = "NS"):
        self.suffix = f".{exchange}"

    def _tick(self, symbol: str) -> str:
        return symbol.strip().upper() + self.suffix

    def history(self, tickers: list[str], start: date, end: date) -> dict[str, list[dict]]:
        if not tickers:
            return {}
        import yfinance as yf

        yts = [self._tick(t) for t in tickers]
        out: dict[str, list[dict]] = {}
        try:
            data = yf.download(
                yts, start=start.isoformat(), end=(end + timedelta(days=1)).isoformat(),
                progress=False, auto_adjust=True, threads=False, group_by="ticker",
            )
        except Exception:
            return {}
        if data is None or data.empty:
            return {}
        for sym, yt in zip(tickers, yts):
            try:
                sub = data[yt] if yt in getattr(data, "columns", []) else data
                opens = sub["Open"]
                highs = sub["High"]
                lows = sub["Low"]
                closes = sub["Close"]
                vols = sub["Volume"]
                bars = []
                for idx in closes.index:
                    c = closes.get(idx)
                    if c is None or c != c:  # NaN
                        continue
                    o = opens.get(idx)
                    h = highs.get(idx)
                    lo = lows.get(idx)
                    v = vols.get(idx)
                    bars.append(
                        {
                            "date": idx.strftime("%Y-%m-%d"),
                            "open": float(o) if o == o else float(c),
                            "high": float(h) if h == h else float(c),
                            "low": float(lo) if lo == lo else float(c),
                            "close": float(c),
                            "volume": int(v) if v == v else 0,
                        }
                    )
                if bars:
                    out[sym] = bars
            except (KeyError, TypeError, IndexError):
                continue
        return out
