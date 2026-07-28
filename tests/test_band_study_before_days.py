"""Tests for the configurable before-days window: N trading days tracked
BEFORE each touch (the ramp-in to the spike), mirroring test_band_study_follow_days.py
but walking backward instead of forward."""

from datetime import date

from engine.backtest import (
    DEFAULT_BEFORE_DAYS,
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


def test_default_before_days_is_zero():
    assert DEFAULT_BEFORE_DAYS == 0


def test_no_before_days_by_default():
    """Existing callers that never pass before_days must see an empty list,
    not an error — before-tracking is opt-in."""
    days = {
        "2026-01-04": [_brow("X", 90, 91, 89, 90, 90)],
        "2026-01-05": [_brow("X", 100, 120, 99, 118, 100)],  # touch
    }
    result = scan_band_touches_bhavcopy(
        [{"symbol": "X", "name": "X"}], FakeBhav(days), None,
        date(2026, 1, 1), date(2026, 1, 31), follow_days=1,
    )
    assert result.before_days == 0
    assert result.touches[0].before_days == []


def test_before_days_walk_backward_nearest_first():
    """offset -1 is the trading day immediately before the touch, -2 the one
    before that, etc — nearest to the touch first."""
    days = {
        "2026-01-01": [_brow("X", 80, 81, 79, 80, 80)],   # Thursday -> offset -3
        "2026-01-02": [_brow("X", 80, 85, 79, 84, 80)],   # Friday   -> offset -2
        # Jan 3/4 weekend
        "2026-01-05": [_brow("X", 84, 90, 83, 88, 84)],   # Monday   -> offset -1
        "2026-01-06": [_brow("X", 100, 120, 99, 118, 100)],  # Tuesday: touch
    }
    result = scan_band_touches_bhavcopy(
        [{"symbol": "X", "name": "X"}], FakeBhav(days), None,
        date(2026, 1, 1), date(2026, 1, 31), follow_days=1, before_days=3,
    )
    t = result.touches[0]
    assert [(f.offset, f.date) for f in t.before_days] == [
        (-1, "2026-01-05"), (-2, "2026-01-02"), (-3, "2026-01-01"),
    ]


def test_before_days_pct_from_touch_uses_the_touch_close_as_anchor():
    days = {
        "2026-01-04": [_brow("X", 80, 81, 79, 80, 80)],    # -20% vs touch close of 100
        "2026-01-05": [_brow("X", 100, 120, 99, 100, 100)],  # touch closes at 100 for easy maths
    }
    result = scan_band_touches_bhavcopy(
        [{"symbol": "X", "name": "X"}], FakeBhav(days), None,
        date(2026, 1, 1), date(2026, 1, 31), follow_days=1, before_days=1,
    )
    d = result.touches[0].before_days[0]
    assert d.pct_from_touch == -20.0


def test_before_days_limit_is_respected():
    days = {"2026-01-15": [_brow("X", 100, 120, 99, 118, 100)]}
    for i in range(1, 11):  # ten trading days available before the touch
        days[f"2026-01-{15 - i:02d}"] = [_brow("X", 100, 101, 99, 100 - i, 100)]
    result = scan_band_touches_bhavcopy(
        [{"symbol": "X", "name": "X"}], FakeBhav(days), None,
        date(2026, 1, 1), date(2026, 1, 31), follow_days=1, before_days=3,
    )
    assert len(result.touches[0].before_days) == 3  # capped, not all ten


def test_shorter_before_window_when_history_runs_out():
    """A touch near the start of available data gets a shorter before-window,
    not an error and not padding."""
    days = {
        "2026-01-04": [_brow("X", 90, 91, 89, 90, 90)],
        "2026-01-05": [_brow("X", 100, 120, 99, 118, 100)],  # only 1 day of history exists before this
    }
    result = scan_band_touches_bhavcopy(
        [{"symbol": "X", "name": "X"}], FakeBhav(days), None,
        date(2026, 1, 1), date(2026, 1, 31), follow_days=1, before_days=5,
    )
    assert len(result.touches[0].before_days) == 1


def test_symbol_missing_earlier_in_window_truncates_rather_than_shifting_offsets():
    days = {
        "2026-01-01": [_brow("X", 70, 71, 69, 70, 70)],
        "2026-01-02": [_brow("OTHER", 50, 51, 49, 50, 50)],  # X absent this day
        "2026-01-05": [_brow("X", 84, 90, 83, 88, 84)],
        "2026-01-06": [_brow("X", 100, 120, 99, 118, 100)],  # touch
    }
    result = scan_band_touches_bhavcopy(
        [{"symbol": "X", "name": "X"}], FakeBhav(days), None,
        date(2026, 1, 1), date(2026, 1, 31), follow_days=1, before_days=5,
    )
    t = result.touches[0]
    assert [f.date for f in t.before_days] == ["2026-01-05"]  # stops, doesn't jump back to Jan 1


def test_before_days_cross_the_month_boundary_backward():
    """A touch on the FIRST day of the scan range must still get its lead-in
    days, even though they fall before start_date."""
    days = {
        "2025-12-29": [_brow("Z", 90, 91, 89, 90, 90)],
        "2025-12-30": [_brow("Z", 90, 95, 89, 94, 90)],
        "2025-12-31": [_brow("Z", 94, 96, 93, 95, 94)],
        "2026-01-01": [_brow("Z", 100, 120, 99, 118, 100)],  # touch, first day of the scan range
    }
    result = scan_band_touches_bhavcopy(
        [{"symbol": "Z", "name": "Z Ltd"}], FakeBhav(days), None,
        date(2026, 1, 1), date(2026, 1, 31), follow_days=1, before_days=3,
    )
    t = result.touches[0]
    assert [f.date for f in t.before_days] == ["2025-12-31", "2025-12-30", "2025-12-29"]


def test_per_before_day_summary_is_ordered_chronologically():
    """per_before_day reads furthest-back first, so it lines up left-to-right
    into the touch (opposite iteration order from how before_days is built)."""
    days = {
        "2026-01-03": [_brow("X", 80, 81, 79, 80, 80)],
        "2026-01-04": [_brow("X", 80, 85, 79, 90, 80)],
        "2026-01-05": [_brow("X", 100, 120, 99, 100, 100)],  # touch, closes at 100
    }
    result = scan_band_touches_bhavcopy(
        [{"symbol": "X", "name": "X"}], FakeBhav(days), None,
        date(2026, 1, 1), date(2026, 1, 31), follow_days=1, before_days=2,
    )
    per_before = result.summary().per_before_day
    assert [d.offset for d in per_before] == [-2, -1]
    assert per_before[0].avg_pct_down == -20.0  # -2: close 80 vs touch close 100
    assert per_before[1].avg_pct_up == -10.0 or per_before[1].avg_pct_down == -10.0


def test_result_carries_its_before_days_limit():
    days = {
        "2026-01-04": [_brow("X", 90, 91, 89, 90, 90)],
        "2026-01-05": [_brow("X", 100, 120, 99, 118, 100)],
    }
    result = scan_band_touches_bhavcopy(
        [{"symbol": "X", "name": "X"}], FakeBhav(days), None,
        date(2026, 1, 1), date(2026, 1, 31), follow_days=1, before_days=4,
    )
    assert result.before_days == 4
    assert len(result.summary().per_before_day) == 4  # a row per tracked day, even if unscored


def test_yahoo_path_also_tracks_before_days():
    bars = {"X": [
        _ybar("2026-01-02", 80, 81, 79, 80),
        _ybar("2026-01-03", 80, 85, 79, 90),
        _ybar("2026-01-04", 90, 92, 88, 92),
        _ybar("2026-01-05", 100, 110, 99, 105),  # touch: high 110 is +19.57% vs prev close 92
    ]}
    result = scan_band_touches(
        [{"symbol": "X", "name": "X"}], FakeOhlc(bars),
        date(2026, 1, 1), date(2026, 1, 31), follow_days=1, before_days=2,
    )
    t = result.touches[0]
    assert [(f.offset, f.date) for f in t.before_days] == [
        (-1, "2026-01-04"), (-2, "2026-01-03"),
    ]


def test_before_and_after_can_be_combined():
    days = {
        "2026-01-04": [_brow("X", 90, 91, 89, 90, 90)],
        "2026-01-05": [_brow("X", 100, 120, 99, 100, 100)],  # touch
        "2026-01-06": [_brow("X", 100, 100, 90, 95, 100)],
    }
    result = scan_band_touches_bhavcopy(
        [{"symbol": "X", "name": "X"}], FakeBhav(days), None,
        date(2026, 1, 1), date(2026, 1, 31), follow_days=1, before_days=1,
    )
    t = result.touches[0]
    assert len(t.before_days) == 1
    assert len(t.follow_days) == 1
    assert t.before_days[0].date == "2026-01-04"
    assert t.follow_days[0].date == "2026-01-06"
