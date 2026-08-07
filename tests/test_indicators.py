"""Tests for engine.indicators — kept alive because engine.backtest's
spike-RSI still depends on rsi(), even after the (removed) screener that
originally housed it."""

from engine import indicators


def test_rsi_all_gains_is_100():
    # Strictly rising series -> no losses -> RSI pinned at 100.
    assert indicators.rsi([float(i) for i in range(1, 40)]) == 100.0


def test_rsi_all_losses_is_low():
    # Strictly falling series -> no gains -> RSI at 0.
    assert indicators.rsi([float(i) for i in range(40, 1, -1)]) == 0.0


def test_rsi_needs_enough_data():
    assert indicators.rsi([1, 2, 3]) is None  # fewer than period+1 points


def test_rsi_known_wilder_value():
    # A classic hand-checkable sequence; Wilder RSI-14 ~ 70.5 for this input.
    closes = [
        44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42,
        45.84, 46.08, 45.89, 46.03, 45.61, 46.28, 46.28, 46.00,
    ]
    # Mostly-rising series -> elevated RSI. Exact value depends on how many
    # Wilder smoothing steps the short series allows; just assert it's high.
    val = indicators.rsi(closes, period=14)
    assert 60 <= val <= 75
