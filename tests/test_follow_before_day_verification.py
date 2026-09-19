"""Verification tests for follow-day and before-day CALCULATIONS.

Task 2: _build_follow_days() and _build_before_days() in engine/backtest.py.

EXISTING COVERAGE (not duplicated here):
  test_band_study_follow_days.py:
    - pct_from_touch cumulative + pct_from_prev_day daily step (bhavcopy path)
    - open_close_pct that session's own move (bhavcopy path)
    - follow-day offsets and dates, cross-month boundary, weekend skip
    - follow_days limit respected, shorter window when data runs out
    - symbol missing mid-window truncates (no offset shift)
    - per-day cohort summary, excludes missing-data touches
    - Yahoo path also tracks N follow days

  test_band_study_before_days.py:
    - default before_days=0, opt-in
    - negative offsets nearest-first ordering
    - pct_from_touch anchored to touch-day close (bhavcopy path)
    - before_days limit, shorter window, truncation, cross-month
    - per_before_day summary ordered chronologically
    - Yahoo path also tracks before_days

GAPS FILLED BY THIS FILE:
  A. pct_from_touch: positive, negative, zero, multi-day cumulative (Yahoo path)
  B. pct_from_prev_day: positive, negative, zero, EACH day uses its own prior close
  C. open_close_pct: positive, negative, zero, None when open is missing/zero
  D. Offset/date assignment via _build_follow_days directly (unit level)
  E. Overnight gap: 3-metric split pct_from_touch vs pct_from_prev_day vs open_close_pct
  F. Before-day: pct_from_touch (Yahoo path), negative offsets, pct_from_prev_day walks backward
  G. Edge cases: no bars, 1 bar, mid-gap None close, open=None → open_close_pct=None
  H. Rounding: values that need round(..., 2)

No production code is modified. All tests call the engine directly via scan_band_touches
(Yahoo path) so _build_follow_days and _build_before_days are exercised end-to-end.
"""

from datetime import date

from engine.backtest import _build_follow_days, _build_before_days, scan_band_touches


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class FakeOhlc:
    """Deterministic in-memory provider. No network."""
    def __init__(self, bars: dict):
        self.bars = bars

    def history(self, tickers, start, end):
        out = {}
        for t in tickers:
            if t not in self.bars:
                continue
            out[t] = [
                b for b in self.bars[t]
                if start.isoformat() <= b["date"] <= end.isoformat()
            ]
        return out


SCAN_START = date(2026, 1, 1)
SCAN_END   = date(2026, 1, 31)

# These prices produce a qualifying touch via scan_band_touches (19-20% band).
# prev_close=100, high=119.5 -> touch_pct=19.5 -> qualifies.
PREV_CLOSE   = 100.0
TOUCH_CLOSE  = 100.0  # touch-day closes at 100 for clean arithmetic


def _bar(d: str, o, h, lo, c, v=200_000) -> dict:
    return {"date": d, "open": o, "high": h, "low": lo, "close": c, "volume": v}


def _run_yahoo(bars_list, *, follow_days=5, before_days=0):
    """Run scan_band_touches (Yahoo path) and return the single touch."""
    data = FakeOhlc({"SYM": bars_list})
    result = scan_band_touches(
        [{"symbol": "SYM", "name": "SYM"}],
        data, SCAN_START, SCAN_END,
        follow_days=follow_days, before_days=before_days,
    )
    assert len(result.touches) == 1, f"Expected 1 touch, got {len(result.touches)}"
    return result.touches[0]


# ---------------------------------------------------------------------------
# Helper to call _build_follow_days directly with synthetic bars
# ---------------------------------------------------------------------------

def _fbar(d: str, o, h, lo, c) -> dict:
    """Minimal bar dict for _build_follow_days / _build_before_days."""
    return {"date": d, "open": o, "high": h, "low": lo, "close": c}


# ===========================================================================
# SECTION A: pct_from_touch
# ===========================================================================

class TestPctFromTouch:
    """
    Formula (L701): pct_from_touch = round((close - touch_close) / touch_close * 100, 2)
    The anchor is ALWAYS the touch day's close, not the previous bar's close.
    """

    def test_pct_from_touch_positive_move(self):
        """
        touch_close=100. Day 1 closes at 105.
        Manual: (105 - 100) / 100 * 100 = 5.00
        """
        touch_close = 100.0
        bars = [_fbar("2026-01-06", 100, 106, 99, 105)]
        fd = _build_follow_days(touch_close, bars, limit=1)
        assert len(fd) == 1
        expected = round((105 - 100) / 100 * 100, 2)
        assert expected == 5.00
        assert fd[0].pct_from_touch == expected

    def test_pct_from_touch_negative_move(self):
        """
        touch_close=100. Day 1 closes at 88.
        Manual: (88 - 100) / 100 * 100 = -12.00
        """
        touch_close = 100.0
        bars = [_fbar("2026-01-06", 100, 100, 85, 88)]
        fd = _build_follow_days(touch_close, bars, limit=1)
        expected = round((88 - 100) / 100 * 100, 2)
        assert expected == -12.00
        assert fd[0].pct_from_touch == expected

    def test_pct_from_touch_zero_move(self):
        """
        touch_close=100. Day 1 closes at 100.
        Manual: (100 - 100) / 100 * 100 = 0.00
        """
        touch_close = 100.0
        bars = [_fbar("2026-01-06", 100, 102, 98, 100)]
        fd = _build_follow_days(touch_close, bars, limit=1)
        assert fd[0].pct_from_touch == 0.00

    def test_pct_from_touch_is_cumulative_from_touch_close(self):
        """
        touch_close=100.
        Day 1: closes at 90  -> pct_from_touch = -10.00
        Day 2: closes at 80  -> pct_from_touch = -20.00  (NOT from day 1's close)
        Day 3: closes at 110 -> pct_from_touch = +10.00  (NOT from day 2's close)

        Manual:
          d1: (90  - 100) / 100 * 100 = -10.00
          d2: (80  - 100) / 100 * 100 = -20.00
          d3: (110 - 100) / 100 * 100 = +10.00
        """
        touch_close = 100.0
        bars = [
            _fbar("2026-01-06", 90,  91,  88,  90),
            _fbar("2026-01-07", 90,  90,  78,  80),
            _fbar("2026-01-08", 80, 115,  78, 110),
        ]
        fd = _build_follow_days(touch_close, bars, limit=3)
        assert len(fd) == 3
        assert fd[0].pct_from_touch == -10.00
        assert fd[1].pct_from_touch == -20.00
        assert fd[2].pct_from_touch == 10.00

    def test_pct_from_touch_non_round_anchor(self):
        """
        Non-integer touch_close to verify rounding.
        touch_close=118.5. Day 1: close=100.
        Manual: (100 - 118.5) / 118.5 * 100 = -15.6118...
        Expected: round(-15.6118..., 2) = -15.61
        """
        touch_close = 118.5
        bars = [_fbar("2026-01-06", 100, 100, 98, 100)]
        fd = _build_follow_days(touch_close, bars, limit=1)
        raw = (100 - 118.5) / 118.5 * 100
        expected = round(raw, 2)
        assert expected == -15.61
        assert fd[0].pct_from_touch == expected

    def test_pct_from_touch_via_full_scan(self):
        """
        End-to-end via scan_band_touches (Yahoo path) to verify the full pipeline.
        prev_bar: close=100. Touch bar: high=119.5, close=100 (touch_close=100).
        Follow day 1: close=95 -> pct_from_touch = (95-100)/100*100 = -5.00
        Follow day 2: close=90 -> pct_from_touch = (90-100)/100*100 = -10.00 (cumulative)
        """
        bars = [
            _bar("2026-01-04", 100, 100,  99, 100),  # prev_close = 100
            _bar("2026-01-05", 100, 119.5, 95, 100), # touch: touch_close = 100
            _bar("2026-01-06",  95,  96,  92,  95),  # day 1
            _bar("2026-01-07",  95,  95,  88,  90),  # day 2
        ]
        t = _run_yahoo(bars, follow_days=2)
        assert t.follow_days[0].pct_from_touch == -5.00
        assert t.follow_days[1].pct_from_touch == -10.00


# ===========================================================================
# SECTION B: pct_from_prev_day
# ===========================================================================

class TestPctFromPrevDay:
    """
    Formula (L702): pct_from_prev_day = round((close - prev_close) / prev_close * 100, 2)
    where prev_close starts as touch_close and is updated each iteration (L706).
    So: day 1 uses touch_close as prev, day 2 uses day 1's close as prev, etc.
    """

    def test_pct_from_prev_day_positive(self):
        """
        touch_close=100. Day 1: close=108.
        Manual: (108 - 100) / 100 * 100 = 8.00
        """
        touch_close = 100.0
        bars = [_fbar("2026-01-06", 105, 110, 104, 108)]
        fd = _build_follow_days(touch_close, bars, limit=1)
        assert fd[0].pct_from_prev_day == 8.00

    def test_pct_from_prev_day_negative(self):
        """
        touch_close=100. Day 1: close=92.
        Manual: (92 - 100) / 100 * 100 = -8.00
        """
        touch_close = 100.0
        bars = [_fbar("2026-01-06", 95, 97, 90, 92)]
        fd = _build_follow_days(touch_close, bars, limit=1)
        assert fd[0].pct_from_prev_day == -8.00

    def test_pct_from_prev_day_zero(self):
        """
        touch_close=100. Day 1: close=100.
        Manual: (100 - 100) / 100 * 100 = 0.00
        """
        touch_close = 100.0
        bars = [_fbar("2026-01-06", 98, 102, 97, 100)]
        fd = _build_follow_days(touch_close, bars, limit=1)
        assert fd[0].pct_from_prev_day == 0.00

    def test_pct_from_prev_day_uses_prior_day_close_not_touch_close(self):
        """
        KEY TEST: Each day's pct_from_prev_day uses THAT day's prior close,
        NOT the touch_close.

        touch_close=100.
        Day 1: close=90   -> pct_from_prev_day = (90 - 100) / 100 * 100 = -10.00
        Day 2: close=81   -> pct_from_prev_day = (81 - 90)  / 90  * 100 = -10.00
                              If it incorrectly used touch_close: (81-100)/100 = -19.00
        Day 3: close=72.9 -> pct_from_prev_day = (72.9 - 81) / 81 * 100 = -10.00
                              If it incorrectly used touch_close: (72.9-100)/100 = -27.10

        Manual d2: (81 - 90) / 90 * 100 = -10.00 (exactly)
        Manual d3: (72.9 - 81) / 81 * 100 = -10.00 (exactly)
        """
        touch_close = 100.0
        bars = [
            _fbar("2026-01-06", 90, 92, 88, 90),
            _fbar("2026-01-07", 90, 92, 79, 81),
            _fbar("2026-01-08", 81, 83, 71, 72.9),
        ]
        fd = _build_follow_days(touch_close, bars, limit=3)

        # Day 1: prev is touch_close (100)
        assert fd[0].pct_from_prev_day == round((90   - 100) / 100  * 100, 2)  # -10.00
        # Day 2: prev is day 1 close (90), NOT touch_close (100)
        assert fd[1].pct_from_prev_day == round((81   -  90) /  90  * 100, 2)  # -10.00
        # Day 3: prev is day 2 close (81), NOT touch_close (100)
        assert fd[2].pct_from_prev_day == round((72.9 -  81) /  81  * 100, 2)  # -10.00

        # Demonstrate the wrong answer if touch_close were used for all days
        wrong_d2 = round((81 - 100) / 100 * 100, 2)  # -19.00
        assert fd[1].pct_from_prev_day != wrong_d2, (
            "pct_from_prev_day incorrectly using touch_close instead of prior-day close"
        )

    def test_pct_from_prev_day_rounding(self):
        """
        touch_close=90. Day 1: close=100.
        Manual: (100 - 90) / 90 * 100 = 11.1111...
        Expected: round(11.1111..., 2) = 11.11
        """
        touch_close = 90.0
        bars = [_fbar("2026-01-06", 95, 102, 94, 100)]
        fd = _build_follow_days(touch_close, bars, limit=1)
        expected = round((100 - 90) / 90 * 100, 2)
        assert expected == 11.11
        assert fd[0].pct_from_prev_day == expected


# ===========================================================================
# SECTION C: open_close_pct
# ===========================================================================

class TestOpenClosePct:
    """
    Formula (L703): open_close_pct = round((close - open_) / open_ * 100, 2) if open_ else None
    """

    def test_open_close_pct_positive(self):
        """
        Day: open=100, close=108.
        Manual: (108 - 100) / 100 * 100 = 8.00
        """
        bars = [_fbar("2026-01-06", 100, 110, 98, 108)]
        fd = _build_follow_days(100.0, bars, limit=1)
        expected = round((108 - 100) / 100 * 100, 2)
        assert expected == 8.00
        assert fd[0].open_close_pct == expected

    def test_open_close_pct_negative(self):
        """
        Day: open=100, close=93.
        Manual: (93 - 100) / 100 * 100 = -7.00
        """
        bars = [_fbar("2026-01-06", 100, 101, 90, 93)]
        fd = _build_follow_days(100.0, bars, limit=1)
        expected = round((93 - 100) / 100 * 100, 2)
        assert expected == -7.00
        assert fd[0].open_close_pct == expected

    def test_open_close_pct_zero(self):
        """
        Day: open=100, close=100.
        Manual: (100 - 100) / 100 * 100 = 0.00
        """
        bars = [_fbar("2026-01-06", 100, 103, 98, 100)]
        fd = _build_follow_days(100.0, bars, limit=1)
        assert fd[0].open_close_pct == 0.00

    def test_open_close_pct_uses_that_days_own_open_not_prev_close(self):
        """
        touch_close=100. Day 1: open=105 (gap up), close=98.
        open_close_pct must use 105 as the open — NOT 100 (touch_close).

        Manual: (98 - 105) / 105 * 100 = -6.666...  -> round = -6.67
        If incorrectly used touch_close as open: (98 - 100) / 100 * 100 = -2.00
        """
        touch_close = 100.0
        bars = [_fbar("2026-01-06", 105, 107, 96, 98)]
        fd = _build_follow_days(touch_close, bars, limit=1)
        expected = round((98 - 105) / 105 * 100, 2)
        assert expected == -6.67
        assert fd[0].open_close_pct == expected

        wrong = round((98 - 100) / 100 * 100, 2)  # -2.00
        assert fd[0].open_close_pct != wrong, (
            "open_close_pct incorrectly using touch_close instead of that day's open"
        )

    def test_open_close_pct_is_none_when_open_is_missing(self):
        """
        When open_ is None: `if open_ else None` -> None.
        """
        bar = {"date": "2026-01-06", "high": 105, "low": 95, "close": 100}  # no "open" key
        fd = _build_follow_days(100.0, [bar], limit=1)
        assert fd[0].open_close_pct is None

    def test_open_close_pct_is_none_when_open_is_zero(self):
        """
        When open_=0: `if open_` is False -> None.
        (Division by zero guard — open=0 is not a valid price.)
        """
        bar = _fbar("2026-01-06", 0, 105, 95, 100)  # open=0
        fd = _build_follow_days(100.0, [bar], limit=1)
        assert fd[0].open_close_pct is None

    def test_open_close_pct_rounding(self):
        """
        Day: open=107, close=120.
        Manual: (120 - 107) / 107 * 100 = 12.1495...
        Expected: round(12.1495..., 2) = 12.15
        """
        bars = [_fbar("2026-01-06", 107, 121, 106, 120)]
        fd = _build_follow_days(100.0, bars, limit=1)
        raw = (120 - 107) / 107 * 100
        expected = round(raw, 2)
        assert expected == 12.15
        assert fd[0].open_close_pct == expected


# ===========================================================================
# SECTION D: Follow-day offset/date assignment — unit level
# ===========================================================================

class TestFollowDayOffsets:
    """
    Verify offset and date are correctly assigned for offsets 1, 2, 3.
    _build_follow_days: `for i, bar in enumerate(next_bars[:limit], start=1)` -> offset=i
    """

    def test_offsets_are_1_2_3_in_order(self):
        """
        Three bars -> offsets must be [1, 2, 3] in order.
        """
        bars = [
            _fbar("2026-01-06", 100, 102, 98, 101),
            _fbar("2026-01-07", 101, 104, 99, 103),
            _fbar("2026-01-08", 103, 107, 102, 106),
        ]
        fd = _build_follow_days(100.0, bars, limit=10)
        assert len(fd) == 3
        assert [f.offset for f in fd] == [1, 2, 3]

    def test_dates_match_bars_in_order(self):
        """
        The date on each FollowDay must match the corresponding bar's date.
        """
        bars = [
            _fbar("2026-01-06", 100, 102, 98, 101),
            _fbar("2026-01-07", 101, 104, 99, 103),
            _fbar("2026-01-08", 103, 107, 102, 106),
        ]
        fd = _build_follow_days(100.0, bars, limit=10)
        assert [f.date for f in fd] == ["2026-01-06", "2026-01-07", "2026-01-08"]

    def test_limit_caps_number_of_follow_days(self):
        """
        With limit=2 and 5 bars, only 2 FollowDays are produced.
        """
        bars = [_fbar(f"2026-01-{6+i:02d}", 100, 102, 98, 101+i) for i in range(5)]
        fd = _build_follow_days(100.0, bars, limit=2)
        assert len(fd) == 2
        assert fd[-1].offset == 2

    def test_fewer_bars_than_limit_returns_all(self):
        """
        With limit=10 but only 2 bars, returns 2 FollowDays (not padded).
        """
        bars = [
            _fbar("2026-01-06", 100, 102, 98, 101),
            _fbar("2026-01-07", 101, 104, 99, 103),
        ]
        fd = _build_follow_days(100.0, bars, limit=10)
        assert len(fd) == 2

    def test_empty_bars_returns_empty_list(self):
        """
        No follow bars -> empty list, not crash.
        """
        fd = _build_follow_days(100.0, [], limit=5)
        assert fd == []

    def test_single_follow_day(self):
        """
        follow_days=1: one bar, offset=1.
        """
        bars = [_fbar("2026-01-06", 100, 105, 98, 103)]
        fd = _build_follow_days(100.0, bars, limit=1)
        assert len(fd) == 1
        assert fd[0].offset == 1
        assert fd[0].date == "2026-01-06"

    def test_mid_window_none_close_stops_at_gap(self):
        """
        If a bar's close is None mid-window, the engine stops (L691: `break`).
        Bars: close=101, close=None, close=105 -> only 1 FollowDay returned.
        """
        bars = [
            _fbar("2026-01-06", 100, 102, 98, 101),
            {"date": "2026-01-07", "open": 101, "high": 104, "low": 99, "close": None},
            _fbar("2026-01-08", 103, 107, 102, 105),
        ]
        fd = _build_follow_days(100.0, bars, limit=5)
        assert len(fd) == 1
        assert fd[0].offset == 1


# ===========================================================================
# SECTION E: Overnight gap — 3-metric split
# ===========================================================================

class TestOvernightGap:
    """
    An overnight gap is the difference between touch_close and next day's open.
    The three metrics capture different parts of a trade:

    touch_close=100, Day 1: open=110 (gap up +10%), close=105 (settled)

    pct_from_touch:   (105 - 100) / 100 = +5.00%  [includes overnight gap]
    pct_from_prev_day:(105 - 100) / 100 = +5.00%  [day 1: prev_close = touch_close]
    open_close_pct:   (105 - 110) / 110 = -4.55%  [intraday move only, gap excluded]

    This shows the three metrics diverge when there's an overnight gap:
    pct_from_touch and pct_from_prev_day agree on day 1 (same prev=touch_close)
    but open_close_pct strips the gap out and shows the intraday direction.
    """

    def test_gap_up_three_metrics_diverge(self):
        """
        touch_close=100, Day 1: open=110, close=105.
        Manual:
          pct_from_touch    = (105 - 100) / 100 * 100 = 5.00
          pct_from_prev_day = (105 - 100) / 100 * 100 = 5.00  (prev=touch_close for day 1)
          open_close_pct    = (105 - 110) / 110 * 100 = -4.545...  -> round = -4.55
        """
        touch_close = 100.0
        bars = [_fbar("2026-01-06", 110, 112, 103, 105)]
        fd = _build_follow_days(touch_close, bars, limit=1)
        d1 = fd[0]

        expected_pct_from_touch    = round((105 - 100) / 100 * 100, 2)  # 5.00
        expected_pct_from_prev_day = round((105 - 100) / 100 * 100, 2)  # 5.00 (day 1)
        expected_open_close_pct    = round((105 - 110) / 110 * 100, 2)  # -4.55

        assert d1.pct_from_touch    == expected_pct_from_touch    == 5.00
        assert d1.pct_from_prev_day == expected_pct_from_prev_day == 5.00
        assert d1.open_close_pct    == expected_open_close_pct    == -4.55

        # The three metrics are all different values (pct_from_touch=5 > open_close_pct=-4.55)
        assert d1.open_close_pct != d1.pct_from_touch

    def test_gap_down_three_metrics_diverge(self):
        """
        touch_close=100, Day 1: open=90 (gap down -10%), close=95 (recovered partially).
        Manual:
          pct_from_touch    = (95 - 100) / 100 * 100 = -5.00
          pct_from_prev_day = (95 - 100) / 100 * 100 = -5.00  (day 1)
          open_close_pct    = (95 - 90)  / 90  * 100 = 5.555...  -> round = 5.56
        """
        touch_close = 100.0
        bars = [_fbar("2026-01-06", 90, 97, 88, 95)]
        fd = _build_follow_days(touch_close, bars, limit=1)
        d1 = fd[0]

        assert d1.pct_from_touch    == round((95 - 100) / 100 * 100, 2)  # -5.00
        assert d1.pct_from_prev_day == round((95 - 100) / 100 * 100, 2)  # -5.00
        assert d1.open_close_pct    == round((95 -  90) /  90 * 100, 2)  # 5.56

    def test_gap_and_multi_day_pct_from_prev_day_independence(self):
        """
        Day 2: gap on top of day 1 move. pct_from_prev_day must use day 1 close,
        while pct_from_touch uses touch_close, and open_close_pct uses day 2's open.

        touch_close=100.
        Day 1: open=100, close=110  -> pct_from_prev_day = +10.00, pct_from_touch = +10.00
        Day 2: open=120 (gap up), close=115
          pct_from_touch    = (115 - 100) / 100 * 100 = +15.00
          pct_from_prev_day = (115 - 110) / 110 * 100 = +4.545... -> 4.55
          open_close_pct    = (115 - 120) / 120 * 100 = -4.166... -> -4.17
        """
        touch_close = 100.0
        bars = [
            _fbar("2026-01-06", 100, 112, 98, 110),
            _fbar("2026-01-07", 120, 122, 113, 115),
        ]
        fd = _build_follow_days(touch_close, bars, limit=2)
        d2 = fd[1]

        assert d2.pct_from_touch    == round((115 - 100) / 100 * 100, 2)   # 15.00
        assert d2.pct_from_prev_day == round((115 - 110) / 110 * 100, 2)   # 4.55
        assert d2.open_close_pct    == round((115 - 120) / 120 * 100, 2)   # -4.17

    def test_gap_via_full_scan(self):
        """
        End-to-end via scan_band_touches to verify the full pipeline.
        prev_bar: close=100. Touch bar: high=119.5, close=118.
        Follow day 1: open=130 (gap up), close=125.
          pct_from_touch    = (125 - 118) / 118 * 100
          pct_from_prev_day = (125 - 118) / 118 * 100 (day 1: prev=touch_close)
          open_close_pct    = (125 - 130) / 130 * 100
        """
        touch_close = 118.0
        bars = [
            _bar("2026-01-04", 100, 100,  99, 100),
            _bar("2026-01-05", 100, 119.5, 95, touch_close),
            _bar("2026-01-06", 130, 132, 122, 125),
        ]
        t = _run_yahoo(bars, follow_days=1)
        d1 = t.follow_days[0]

        assert d1.pct_from_touch    == round((125 - touch_close) / touch_close * 100, 2)
        assert d1.pct_from_prev_day == round((125 - touch_close) / touch_close * 100, 2)
        assert d1.open_close_pct    == round((125 - 130) / 130 * 100, 2)


# ===========================================================================
# SECTION F: Before-day calculations
# ===========================================================================

class TestBeforeDayCalculations:
    """
    _build_before_days mirrors _build_follow_days but walks backward.
    Offsets: -1, -2, -3 (nearest-to-touch first).
    pct_from_touch: always anchored at touch_close (same formula as follow days).
    pct_from_prev_day: walks backward from touch_close, using each successive close.
    """

    def test_before_day_negative_offsets(self):
        """
        3 before-bars -> offsets must be [-1, -2, -3].
        """
        prev_bars = [
            _fbar("2026-01-04", 95,  97,  93,  95),   # -1
            _fbar("2026-01-03", 90,  92,  88,  90),   # -2
            _fbar("2026-01-02", 85,  87,  83,  85),   # -3
        ]
        bd = _build_before_days(100.0, prev_bars, limit=3)
        assert [b.offset for b in bd] == [-1, -2, -3]

    def test_before_day_dates_preserved(self):
        """
        Dates must be passed through from the bar dict unchanged.
        """
        prev_bars = [
            _fbar("2026-01-04", 95, 97, 93, 95),
            _fbar("2026-01-03", 90, 92, 88, 90),
        ]
        bd = _build_before_days(100.0, prev_bars, limit=2)
        assert [b.date for b in bd] == ["2026-01-04", "2026-01-03"]

    def test_before_day_pct_from_touch_anchored_at_touch_close(self):
        """
        pct_from_touch for before-days uses touch_close as anchor, just like follow days.
        touch_close=100.
        Day -1: close=80  -> pct_from_touch = (80 - 100) / 100 * 100 = -20.00
        Day -2: close=70  -> pct_from_touch = (70 - 100) / 100 * 100 = -30.00
        """
        touch_close = 100.0
        prev_bars = [
            _fbar("2026-01-04", 80, 81, 78, 80),   # -1: 20% below touch_close
            _fbar("2026-01-03", 70, 71, 68, 70),   # -2: 30% below touch_close
        ]
        bd = _build_before_days(touch_close, prev_bars, limit=2)
        assert bd[0].pct_from_touch == -20.00
        assert bd[1].pct_from_touch == -30.00

    def test_before_day_pct_from_touch_positive(self):
        """
        A before-day that was ABOVE the touch_close (stock fell into the touch).
        touch_close=100. Day -1: close=110 -> pct_from_touch = +10.00
        """
        touch_close = 100.0
        prev_bars = [_fbar("2026-01-04", 110, 112, 108, 110)]
        bd = _build_before_days(touch_close, prev_bars, limit=1)
        assert bd[0].pct_from_touch == 10.00

    def test_before_day_pct_from_prev_day_walks_backward(self):
        """
        pct_from_prev_day for before-days: the 'prev_close' variable starts at
        touch_close and is updated each iteration (same code as follow-days).

        So:
          Day -1: pct_from_prev_day = (close_-1 - touch_close) / touch_close * 100
          Day -2: pct_from_prev_day = (close_-2 - close_-1)    / close_-1    * 100

        touch_close=100.
        Day -1: close=80  -> pct_from_prev_day = (80-100)/100*100 = -20.00
        Day -2: close=60  -> pct_from_prev_day = (60-80)/80*100   = -25.00
                             If it used touch_close: (60-100)/100 = -40.00 (WRONG)
        """
        touch_close = 100.0
        prev_bars = [
            _fbar("2026-01-04", 80, 81, 78, 80),
            _fbar("2026-01-03", 60, 61, 58, 60),
        ]
        bd = _build_before_days(touch_close, prev_bars, limit=2)

        expected_d1 = round((80 - 100) / 100 * 100, 2)  # -20.00
        expected_d2 = round((60 -  80) /  80 * 100, 2)  # -25.00
        assert bd[0].pct_from_prev_day == expected_d1
        assert bd[1].pct_from_prev_day == expected_d2

        wrong_d2 = round((60 - 100) / 100 * 100, 2)  # -40.00
        assert bd[1].pct_from_prev_day != wrong_d2

    def test_before_day_open_close_pct(self):
        """
        open_close_pct = (close - open_) / open_ * 100 for before-days.
        Day -1: open=85, close=80 -> (80-85)/85*100 = -5.882... -> -5.88
        """
        touch_close = 100.0
        prev_bars = [_fbar("2026-01-04", 85, 87, 78, 80)]
        bd = _build_before_days(touch_close, prev_bars, limit=1)
        expected = round((80 - 85) / 85 * 100, 2)
        assert expected == -5.88
        assert bd[0].open_close_pct == expected

    def test_before_day_open_close_pct_none_when_open_missing(self):
        """
        Missing open -> open_close_pct = None.
        """
        bar = {"date": "2026-01-04", "high": 82, "low": 78, "close": 80}  # no "open"
        bd = _build_before_days(100.0, [bar], limit=1)
        assert bd[0].open_close_pct is None

    def test_before_day_via_full_scan_yahoo(self):
        """
        End-to-end via scan_band_touches (Yahoo path), before_days=2.
        prev_bar: close=100 (this is the touch-day prev_close, NOT before_days).
        Touch bar: high=119.5, close=100 (touch_close=100).
        Before bars: day -1 = close=90, day -2 = close=80.

        Manual:
          day -1: pct_from_touch = (90-100)/100*100 = -10.00
          day -2: pct_from_touch = (80-100)/100*100 = -20.00
        """
        bars = [
            _bar("2026-01-02", 80, 81, 79, 80),    # -2 before touch
            _bar("2026-01-04", 90, 91, 89, 90),    # -1 before touch (no bar on Jan 3). prev_close=90
            _bar("2026-01-05", 100, 107.55, 95, 100), # touch: high 107.55 is 19.5% above 90. touch_close=100
        ]
        t = _run_yahoo(bars, follow_days=1, before_days=2)
        bd = t.before_days

        # offset -1 is nearest: Jan 4
        assert bd[0].offset == -1
        assert bd[0].date   == "2026-01-04"
        assert bd[0].pct_from_touch == round((90 - 100) / 100 * 100, 2)  # -10.00

        # offset -2 is further: Jan 2
        assert bd[1].offset == -2
        assert bd[1].date   == "2026-01-02"
        assert bd[1].pct_from_touch == round((80 - 100) / 100 * 100, 2)  # -20.00


# ===========================================================================
# SECTION G: Edge cases
# ===========================================================================

class TestEdgeCases:

    def test_follow_days_zero_bars_returns_empty(self):
        """No bars available -> empty list, no crash."""
        fd = _build_follow_days(100.0, [], limit=5)
        assert fd == []

    def test_before_days_zero_bars_returns_empty(self):
        bd = _build_before_days(100.0, [], limit=5)
        assert bd == []

    def test_follow_days_limit_1_returns_exactly_1(self):
        bars = [_fbar("2026-01-06", 100, 102, 98, 101)]
        fd = _build_follow_days(100.0, bars, limit=1)
        assert len(fd) == 1
        assert fd[0].offset == 1

    def test_before_days_limit_1_returns_exactly_1(self):
        bars = [_fbar("2026-01-04", 90, 91, 88, 90)]
        bd = _build_before_days(100.0, bars, limit=1)
        assert len(bd) == 1
        assert bd[0].offset == -1

    def test_mid_window_none_close_stops_follow_days(self):
        """
        A None close mid-window causes a break (L690-691). Bars after it are ignored.
        """
        bars = [
            _fbar("2026-01-06", 100, 102, 98, 101),
            {"date": "2026-01-07", "open": 101, "high": 104, "low": 99, "close": None},
            _fbar("2026-01-08", 103, 107, 102, 106),  # this must NOT appear
        ]
        fd = _build_follow_days(100.0, bars, limit=10)
        assert len(fd) == 1
        assert fd[0].date == "2026-01-06"

    def test_mid_window_none_close_stops_before_days(self):
        """Same break logic for before-days."""
        bars = [
            _fbar("2026-01-04", 90, 91, 88, 90),
            {"date": "2026-01-03", "open": 85, "high": 87, "low": 83, "close": None},
            _fbar("2026-01-02", 80, 81, 78, 80),  # must NOT appear
        ]
        bd = _build_before_days(100.0, bars, limit=10)
        assert len(bd) == 1
        assert bd[0].date == "2026-01-04"

    def test_before_days_zero_returns_empty_in_scan(self):
        """
        before_days=0 (the default): the touch's before_days list is empty.
        """
        bars = [
            _bar("2026-01-04", 100, 100,  99, 100),
            _bar("2026-01-05", 100, 119.5, 95, 100),
        ]
        t = _run_yahoo(bars, follow_days=1, before_days=0)
        assert t.before_days == []

    def test_before_days_more_than_available_history(self):
        """
        before_days=5, but only 1 bar exists before the touch.
        Must return 1 before-day, not crash, not pad.
        """
        bars = [
            _bar("2026-01-04", 90,  91, 89, 90),
            _bar("2026-01-05", 100, 107.55, 95, 100),
        ]
        t = _run_yahoo(bars, follow_days=1, before_days=5)
        assert len(t.before_days) == 1


# ===========================================================================
# SECTION H: Rounding — explicit 2-decimal-place verification
# ===========================================================================

class TestRounding:

    def test_pct_from_touch_rounds_to_2_decimals(self):
        """
        touch_close=90. close=100.
        Manual: (100 - 90) / 90 * 100 = 11.1111...
        Expected: 11.11 (not 11.1 or 11.111...)
        """
        fd = _build_follow_days(90.0, [_fbar("2026-01-06", 95, 102, 94, 100)], limit=1)
        assert fd[0].pct_from_touch == 11.11
        assert round(fd[0].pct_from_touch, 2) == fd[0].pct_from_touch

    def test_pct_from_prev_day_rounds_to_2_decimals(self):
        """
        touch_close=90. close=100. pct_from_prev_day day 1 = 11.11...
        """
        fd = _build_follow_days(90.0, [_fbar("2026-01-06", 95, 102, 94, 100)], limit=1)
        assert fd[0].pct_from_prev_day == 11.11
        assert round(fd[0].pct_from_prev_day, 2) == fd[0].pct_from_prev_day

    def test_open_close_pct_rounds_to_2_decimals(self):
        """
        open=90, close=100. (100-90)/90*100 = 11.1111...
        """
        fd = _build_follow_days(80.0, [_fbar("2026-01-06", 90, 102, 89, 100)], limit=1)
        assert fd[0].open_close_pct == 11.11
        assert round(fd[0].open_close_pct, 2) == fd[0].open_close_pct

    def test_before_day_pct_from_touch_rounds_to_2_decimals(self):
        """
        touch_close=90. Before-day close=100.
        (100 - 90) / 90 * 100 = 11.1111... -> 11.11
        """
        bd = _build_before_days(90.0, [_fbar("2026-01-04", 98, 102, 98, 100)], limit=1)
        assert bd[0].pct_from_touch == 11.11
        assert round(bd[0].pct_from_touch, 2) == bd[0].pct_from_touch

    def test_all_three_metrics_are_rounded_on_same_bar(self):
        """
        One bar that produces non-terminating values for all three metrics.
        touch_close=90, prev_close starts at 90.
        open=90, close=100.

        pct_from_touch    = (100-90)/90*100 = 11.11...  -> 11.11
        pct_from_prev_day = (100-90)/90*100 = 11.11...  -> 11.11
        open_close_pct    = (100-90)/90*100 = 11.11...  -> 11.11
        (All three happen to be the same formula here: same prev=touch=open=90)
        """
        fd = _build_follow_days(90.0, [_fbar("2026-01-06", 90, 102, 89, 100)], limit=1)
        d = fd[0]
        assert d.pct_from_touch    == 11.11
        assert d.pct_from_prev_day == 11.11
        assert d.open_close_pct    == 11.11
        # All values are exactly representable to 2 decimal places
        assert d.pct_from_touch    == round(d.pct_from_touch,    2)
        assert d.pct_from_prev_day == round(d.pct_from_prev_day, 2)
        assert d.open_close_pct    == round(d.open_close_pct,    2)
