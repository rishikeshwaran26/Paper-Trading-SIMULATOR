"""Tests for the configurable follow-days window: N trading days tracked after
each touch, including the case that matters most — a touch on the LAST day of
the scan range, whose follow-up days fall in the following month."""

from datetime import date, timedelta

import pytest

from engine.backtest import (
    DEFAULT_FOLLOW_DAYS,
    scan_band_touches,
    scan_band_touches_bhavcopy,
)


class FakeBhav:
    def __init__(self, days):
        self.days = days

    def full_day(self, d):
        return self.days.get(d.isoformat(), [])


class FakeOhlc:
    def __init__(self, bars):
        self.bars = bars

    def history(self, tickers, start, end):
        out = {}
        for t in tickers:
            if t not in self.bars:
                continue
            out[t] = [b for b in self.bars[t] if start.isoformat() <= b["date"] <= end.isoformat()]
        return out


def _brow(symbol, o, h, lo, c, prev, v=200_000):
    return {"symbol": symbol, "open": o, "high": h, "low": lo, "close": c, "prev_close": prev, "volume": v}


def _ybar(d, o, h, lo, c, v=200_000):
    return {"date": d, "open": o, "high": h, "low": lo, "close": c, "volume": v}


# =============================================================================
# The headline case: touch on Jan 30 (a Friday), follow days run into February.
# =============================================================================
def test_follow_days_cross_the_month_boundary():
    """A touch at the very end of the scan range must still get its follow-up
    days, even though they fall outside [start_date, end_date] — otherwise the
    last touches of every month would be silently unscored."""
    days = {
        # touch day: last trading day of January 2026 (Jan 30 is a Friday)
        "2026-01-30": [_brow("Z", 100, 120, 99, 118, 100)],
        # Jan 31 / Feb 1 are the weekend — absent, as bhavcopy reports them
        "2026-02-02": [_brow("Z", 118, 119, 110, 112, 118)],
        "2026-02-03": [_brow("Z", 112, 113, 105, 107, 112)],
        "2026-02-04": [_brow("Z", 107, 110, 100, 101, 107)],
        "2026-02-05": [_brow("Z", 101, 104, 98, 103, 101)],
        "2026-02-06": [_brow("Z", 103, 106, 100, 105, 103)],
    }
    result = scan_band_touches_bhavcopy(
        [{"symbol": "Z", "name": "Z Ltd"}], FakeBhav(days), None,
        date(2026, 1, 1), date(2026, 1, 31), follow_days=5,
    )
    assert len(result.touches) == 1
    t = result.touches[0]
    assert t.date == "2026-01-30"
    # all five follow days found, all in February, weekend skipped
    assert [f.date for f in t.follow_days] == [
        "2026-02-02", "2026-02-03", "2026-02-04", "2026-02-05", "2026-02-06",
    ]
    assert [f.offset for f in t.follow_days] == [1, 2, 3, 4, 5]


def test_offsets_are_trading_days_not_calendar_days():
    """A weekend in the middle of the window must not consume an offset."""
    days = {
        "2026-01-15": [_brow("X", 100, 120, 99, 118, 100)],  # Thursday
        "2026-01-16": [_brow("X", 118, 119, 112, 115, 118)],  # Friday      -> offset 1
        # Jan 17/18 weekend: absent
        "2026-01-19": [_brow("X", 115, 116, 108, 110, 115)],  # Monday      -> offset 2
        "2026-01-20": [_brow("X", 110, 112, 104, 105, 110)],  # Tuesday     -> offset 3
    }
    result = scan_band_touches_bhavcopy(
        [{"symbol": "X", "name": "X"}], FakeBhav(days), None,
        date(2026, 1, 1), date(2026, 1, 31), follow_days=3,
    )
    t = result.touches[0]
    assert [(f.offset, f.date) for f in t.follow_days] == [
        (1, "2026-01-16"), (2, "2026-01-19"), (3, "2026-01-20"),
    ]


def test_follow_days_limit_is_respected():
    days = {"2026-01-05": [_brow("X", 100, 120, 99, 118, 100)]}
    for i in range(1, 11):  # ten trading days available after the touch
        days[f"2026-01-{5 + i:02d}"] = [_brow("X", 118, 119, 110, 118 - i, 118)]
    result = scan_band_touches_bhavcopy(
        [{"symbol": "X", "name": "X"}], FakeBhav(days), None,
        date(2026, 1, 1), date(2026, 1, 31), follow_days=3,
    )
    assert len(result.touches[0].follow_days) == 3  # capped, not all ten


def test_shorter_window_when_data_runs_out():
    """A touch with only 2 days of data after it gets 2 follow days, not an
    error and not padding."""
    days = {
        "2026-01-05": [_brow("X", 100, 120, 99, 118, 100)],
        "2026-01-06": [_brow("X", 118, 119, 110, 112, 118)],
        "2026-01-07": [_brow("X", 112, 113, 105, 107, 112)],
    }
    result = scan_band_touches_bhavcopy(
        [{"symbol": "X", "name": "X"}], FakeBhav(days), None,
        date(2026, 1, 1), date(2026, 1, 31), follow_days=5,
    )
    assert len(result.touches[0].follow_days) == 2


# --- the numbers on each follow day -------------------------------------------
def test_pct_from_touch_is_cumulative_and_prev_day_is_the_step():
    days = {
        "2026-01-05": [_brow("X", 100, 120, 99, 100, 100)],   # touch closes at 100, easy maths
        "2026-01-06": [_brow("X", 100, 100, 90, 90, 100)],     # -10% from touch, -10% from prev
        "2026-01-07": [_brow("X", 90, 90, 80, 80, 90)],        # -20% from touch, -11.11% from prev
    }
    result = scan_band_touches_bhavcopy(
        [{"symbol": "X", "name": "X"}], FakeBhav(days), None,
        date(2026, 1, 1), date(2026, 1, 31), follow_days=2,
    )
    d1, d2 = result.touches[0].follow_days
    assert d1.pct_from_touch == -10.0
    assert d1.pct_from_prev_day == -10.0
    assert d2.pct_from_touch == -20.0                 # cumulative, from the touch close
    assert d2.pct_from_prev_day == round((80 - 90) / 90 * 100, 2)  # that day's own step


def test_open_close_pct_is_that_sessions_own_move():
    days = {
        "2026-01-05": [_brow("X", 100, 120, 99, 118, 100)],
        "2026-01-06": [_brow("X", 110, 115, 100, 104, 118)],  # opens 110, closes 104
    }
    result = scan_band_touches_bhavcopy(
        [{"symbol": "X", "name": "X"}], FakeBhav(days), None,
        date(2026, 1, 1), date(2026, 1, 31), follow_days=1,
    )
    d1 = result.touches[0].follow_days[0]
    assert d1.open_close_pct == round((104 - 110) / 110 * 100, 2)


def test_day2_accessors_still_map_to_the_first_follow_day():
    """day2_* is now derived from follow_days[0] — the older API shape must
    keep reporting exactly the same numbers."""
    days = {
        "2026-01-05": [_brow("X", 100, 120, 99, 118, 100)],
        "2026-01-06": [_brow("X", 118, 119, 108, 109, 118)],
    }
    result = scan_band_touches_bhavcopy(
        [{"symbol": "X", "name": "X"}], FakeBhav(days), None,
        date(2026, 1, 1), date(2026, 1, 31), follow_days=5,
    )
    t = result.touches[0]
    assert t.has_day2 is True
    assert t.day2_date == "2026-01-06"
    assert t.day2_close == 109
    assert t.day2_pct_change == t.follow_days[0].pct_from_touch


def test_symbol_missing_mid_window_truncates_rather_than_shifting_offsets():
    """If a stock is absent from a day's file (suspended), later days must NOT
    slide into its offset — that would silently mislabel which day is which."""
    days = {
        "2026-01-05": [_brow("X", 100, 120, 99, 118, 100)],
        "2026-01-06": [_brow("X", 118, 119, 110, 112, 118)],
        "2026-01-07": [_brow("OTHER", 50, 51, 49, 50, 50)],  # X absent this day
        "2026-01-08": [_brow("X", 112, 113, 105, 107, 112)],
    }
    result = scan_band_touches_bhavcopy(
        [{"symbol": "X", "name": "X"}], FakeBhav(days), None,
        date(2026, 1, 1), date(2026, 1, 31), follow_days=5,
    )
    t = result.touches[0]
    assert [f.date for f in t.follow_days] == ["2026-01-06"]  # stops, doesn't jump to the 8th


# --- per-day cohort summary ----------------------------------------------------
def test_per_day_summary_tracks_each_offset_separately():
    """Two stocks: one falls further every day, one recovers. The per-day
    breakdown must show the cohort shifting from down-heavy to split."""
    days = {
        "2026-01-05": [
            _brow("FALLER", 100, 120, 99, 100, 100),
            _brow("RISER", 100, 120, 99, 100, 100),
        ],
        "2026-01-06": [  # day 1: both down
            _brow("FALLER", 100, 100, 90, 95, 100),
            _brow("RISER", 100, 100, 90, 95, 100),
        ],
        "2026-01-07": [  # day 2: faller keeps falling, riser goes above the touch close
            _brow("FALLER", 95, 95, 85, 90, 95),
            _brow("RISER", 95, 110, 95, 105, 95),
        ],
    }
    result = scan_band_touches_bhavcopy(
        [{"symbol": "FALLER", "name": "F"}, {"symbol": "RISER", "name": "R"}],
        FakeBhav(days), None, date(2026, 1, 1), date(2026, 1, 31), follow_days=2,
    )
    per_day = result.summary().per_day
    assert [d.offset for d in per_day] == [1, 2]

    day1 = per_day[0]
    assert day1.scored == 2 and day1.down_count == 2 and day1.up_count == 0

    day2 = per_day[1]
    assert day2.scored == 2 and day2.down_count == 1 and day2.up_count == 1
    assert day2.avg_pct_up == 5.0     # RISER: 100 -> 105
    assert day2.avg_pct_down == -10.0  # FALLER: 100 -> 90


def test_per_day_excludes_touches_without_data_that_far_out():
    """A touch with only 1 follow day must count toward day 1's `scored` but
    not day 2's — a missing day is not a flat day."""
    days = {
        "2026-01-05": [_brow("SHORT", 100, 120, 99, 100, 100), _brow("LONG", 100, 120, 99, 100, 100)],
        "2026-01-06": [_brow("SHORT", 100, 100, 90, 95, 100), _brow("LONG", 100, 100, 90, 95, 100)],
        "2026-01-07": [_brow("LONG", 95, 95, 85, 90, 95)],  # SHORT has no data here
    }
    result = scan_band_touches_bhavcopy(
        [{"symbol": "SHORT", "name": "S"}, {"symbol": "LONG", "name": "L"}],
        FakeBhav(days), None, date(2026, 1, 1), date(2026, 1, 31), follow_days=2,
    )
    per_day = result.summary().per_day
    assert per_day[0].scored == 2  # both have a day 1
    assert per_day[1].scored == 1  # only LONG has a day 2
    assert per_day[1].flat_count == 0  # SHORT is absent, NOT counted as a flat 0%


def test_result_carries_its_follow_days_limit():
    days = {"2026-01-05": [_brow("X", 100, 120, 99, 118, 100)]}
    result = scan_band_touches_bhavcopy(
        [{"symbol": "X", "name": "X"}], FakeBhav(days), None,
        date(2026, 1, 1), date(2026, 1, 31), follow_days=7,
    )
    assert result.follow_days == 7
    assert len(result.summary().per_day) == 7  # a row per tracked day, even if unscored


# --- the Yahoo path gets the same treatment ------------------------------------
def test_yahoo_path_also_tracks_n_follow_days():
    bars = {"X": [
        _ybar("2026-01-02", 100, 101, 99, 100),
        _ybar("2026-01-05", 100, 120, 99, 118),   # touch
        _ybar("2026-01-06", 118, 119, 110, 112),
        _ybar("2026-01-07", 112, 113, 105, 107),
        _ybar("2026-01-08", 107, 108, 100, 102),
    ]}
    result = scan_band_touches(
        [{"symbol": "X", "name": "X"}], FakeOhlc(bars),
        date(2026, 1, 1), date(2026, 1, 31), follow_days=3,
    )
    t = result.touches[0]
    assert [f.date for f in t.follow_days] == ["2026-01-06", "2026-01-07", "2026-01-08"]
    assert t.day2_date == "2026-01-06"


def test_default_follow_days_is_five():
    assert DEFAULT_FOLLOW_DAYS == 5
