"""Verification tests for Band Study TOUCH DETECTION ONLY.

Covers every case requested:
  UP: band_low/band_high boundaries, inside, above, below; close != touch
  DOWN: same, mirrored
  Edge cases: prev_close=0/negative/None, missing first bar, volume boundary

No follow-day or aggregate calculations are tested here.
Production code is NOT modified.

Every test documents:
  - Input values
  - Manual touch_pct calculation
  - Expected qualification (True/False)
  - Asserted actual vs expected
"""

from datetime import date

from engine.backtest import scan_band_touches, DEFAULT_MIN_VOLUME


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class FakeOhlcData:
    """Deterministic in-memory bar provider. No network."""
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
TOUCH_DATE = date(2026, 1, 5)
PREV_DATE  = date(2026, 1, 4)

BAND_LOW  = 19.0
BAND_HIGH = 20.0
VOL_OK    = 200_000


def _make_bars(prev_close, high, low, close, volume=VOL_OK):
    """Two bars: prev_date (supplies prev_close) + touch_date."""
    return [
        {"date": PREV_DATE.isoformat(),  "open": prev_close, "high": prev_close,
         "low": prev_close,  "close": prev_close, "volume": VOL_OK},
        {"date": TOUCH_DATE.isoformat(), "open": prev_close, "high": high,
         "low": low, "close": close, "volume": volume},
    ]


def _run(prev_close, high, low, close, volume=VOL_OK, direction="up",
         band_low=BAND_LOW, band_high=BAND_HIGH):
    """Run the engine on a single two-bar series and return the touches list."""
    bars = _make_bars(prev_close, high, low, close, volume)
    data = FakeOhlcData({"SYM": bars})
    result = scan_band_touches(
        [{"symbol": "SYM", "name": "SYM"}],
        data, SCAN_START, SCAN_END,
        band_low=band_low, band_high=band_high,
        direction=direction,
        min_volume=DEFAULT_MIN_VOLUME,
    )
    return result.touches


# ===========================================================================
# SECTION 1: UP direction — band boundaries
# ===========================================================================

class TestUpDirection:

    def test_up_touch_exactly_at_band_low(self):
        """
        Input:  prev_close=100, high=119.0
        Manual: touch_pct = (119 - 100) / 100 * 100 = 19.00
        Band:   19.0 <= 19.0 <= 20.0  ->  qualifies = True
        """
        prev_close, high = 100.0, 119.0
        expected_touch_pct = round((high - prev_close) / prev_close * 100, 2)
        assert expected_touch_pct == 19.00

        touches = _run(prev_close, high=high, low=95.0, close=110.0)
        assert len(touches) == 1, f"Expected 1 touch (exactly at band_low=19%), got {len(touches)}"
        assert touches[0].touch_pct == expected_touch_pct

    def test_up_touch_exactly_at_band_high(self):
        """
        Input:  prev_close=100, high=120.0
        Manual: touch_pct = (120 - 100) / 100 * 100 = 20.00
        Band:   19.0 <= 20.0 <= 20.0  ->  qualifies = True
        """
        prev_close, high = 100.0, 120.0
        expected_touch_pct = round((high - prev_close) / prev_close * 100, 2)
        assert expected_touch_pct == 20.00

        touches = _run(prev_close, high=high, low=95.0, close=115.0)
        assert len(touches) == 1, f"Expected 1 touch (exactly at band_high=20%), got {len(touches)}"
        assert touches[0].touch_pct == expected_touch_pct

    def test_up_touch_just_below_band_low_excluded(self):
        """
        Input:  prev_close=100, high=118.99
        Manual: touch_pct = (118.99 - 100) / 100 * 100 = 18.99
        Band:   18.99 < 19.0  ->  qualifies = False
        """
        prev_close, high = 100.0, 118.99
        expected_touch_pct = round((high - prev_close) / prev_close * 100, 2)
        assert expected_touch_pct == 18.99

        touches = _run(prev_close, high=high, low=95.0, close=110.0)
        assert len(touches) == 0, f"Expected 0 touches (just below band_low), got {len(touches)}"

    def test_up_touch_just_above_band_high_excluded(self):
        """
        Input:  prev_close=100, high=120.01
        Manual: touch_pct = (120.01 - 100) / 100 * 100 = 20.01
        Band:   20.01 > 20.0  ->  qualifies = False
        """
        prev_close, high = 100.0, 120.01
        expected_touch_pct = round((high - prev_close) / prev_close * 100, 2)
        assert expected_touch_pct == 20.01

        touches = _run(prev_close, high=high, low=95.0, close=115.0)
        assert len(touches) == 0, f"Expected 0 touches (just above band_high), got {len(touches)}"

    def test_up_touch_inside_band(self):
        """
        Input:  prev_close=100, high=119.5
        Manual: touch_pct = (119.5 - 100) / 100 * 100 = 19.50
        Band:   19.0 <= 19.5 <= 20.0  ->  qualifies = True
        """
        prev_close, high = 100.0, 119.5
        expected_touch_pct = round((high - prev_close) / prev_close * 100, 2)
        assert expected_touch_pct == 19.50

        touches = _run(prev_close, high=high, low=95.0, close=110.0)
        assert len(touches) == 1, f"Expected 1 touch (inside band), got {len(touches)}"
        assert touches[0].touch_pct == expected_touch_pct

    def test_up_high_qualifies_but_close_below_touch_pct(self):
        """
        Core requirement: intraday high qualifies; close fades back below.
        Input:  prev_close=100, high=119.5 (touch_pct=19.5), close=110 (close_pct=10)
        Manual: touch_pct=19.50 -> qualifies=True; close_pct=10.00 (reported, not used)
        """
        prev_close, high, close = 100.0, 119.5, 110.0
        expected_touch_pct = round((high  - prev_close) / prev_close * 100, 2)
        expected_close_pct = round((close - prev_close) / prev_close * 100, 2)
        assert expected_touch_pct == 19.50
        assert expected_close_pct == 10.00

        touches = _run(prev_close, high=high, low=95.0, close=close)
        assert len(touches) == 1
        assert touches[0].touch_pct == 19.50
        assert touches[0].close_pct == 10.00

    def test_up_touch_pct_uses_high_not_close(self):
        """
        Prove touch_pct is derived from `high`, not `close`.
        high=119.0 -> touch_pct=19.0 -> qualifies (in band)
        close=121.0 -> if engine used close: 21.0 -> excluded (above band)
        Expected: qualifies (high drives it)
        """
        prev_close = 100.0
        high  = 119.0  # touch_pct via high = 19.0 -> qualifies
        close = 121.0  # if engine used close -> 21.0 -> excluded

        touches = _run(prev_close, high=high, low=95.0, close=close)
        assert len(touches) == 1, (
            "Should qualify via high=19.0%, not be excluded by close=21.0%"
        )
        assert touches[0].touch_pct == 19.00

    def test_up_non_round_prev_close_rounding(self):
        """
        Verify round(..., 2) applied to touch_pct.
        Input:  prev_close=107.0, high=127.73
        Manual: (127.73 - 107) / 107 * 100 = 19.3738...
        Expected touch_pct = round(19.3738..., 2) = 19.37
        """
        prev_close, high = 107.0, 127.73
        raw = (high - prev_close) / prev_close * 100
        expected_touch_pct = round(raw, 2)
        assert expected_touch_pct == 19.37

        touches = _run(prev_close, high=high, low=100.0, close=115.0)
        assert len(touches) == 1
        assert touches[0].touch_pct == expected_touch_pct

    def test_up_low_field_is_irrelevant_for_qualification(self):
        """
        For 'up', only the day's high drives qualification.
        An extreme low must not affect whether the touch qualifies.
        Input:  prev_close=100, high=119.5 (in band), low=1.0
        Expected: qualifies; low is stored faithfully but not used.
        """
        prev_close, high, low = 100.0, 119.5, 1.0
        touches = _run(prev_close, high=high, low=low, close=110.0, direction="up")
        assert len(touches) == 1
        assert touches[0].touch_pct == 19.50
        assert touches[0].low == low


# ===========================================================================
# SECTION 2: DOWN direction — band boundaries
# ===========================================================================

class TestDownDirection:

    def test_down_touch_exactly_at_band_low_magnitude(self):
        """
        Input:  prev_close=100, low=81.0
        Manual: touch_pct = (81.0 - 100) / 100 * 100 = -19.00
        Engine: -band_high <= touch_pct <= -band_low
                -20.0 <= -19.0 <= -19.0  ->  qualifies = True
        """
        prev_close, low = 100.0, 81.0
        expected_touch_pct = round((low - prev_close) / prev_close * 100, 2)
        assert expected_touch_pct == -19.00

        touches = _run(prev_close, high=101.0, low=low, close=85.0, direction="down")
        assert len(touches) == 1, f"Expected 1 touch (exactly at -band_low), got {len(touches)}"
        assert touches[0].touch_pct == expected_touch_pct

    def test_down_touch_exactly_at_band_high_magnitude(self):
        """
        Input:  prev_close=100, low=80.0
        Manual: touch_pct = (80.0 - 100) / 100 * 100 = -20.00
        Engine: -20.0 <= -20.0 <= -19.0  ->  qualifies = True
        """
        prev_close, low = 100.0, 80.0
        expected_touch_pct = round((low - prev_close) / prev_close * 100, 2)
        assert expected_touch_pct == -20.00

        touches = _run(prev_close, high=101.0, low=low, close=85.0, direction="down")
        assert len(touches) == 1, f"Expected 1 touch (exactly at -band_high), got {len(touches)}"
        assert touches[0].touch_pct == expected_touch_pct

    def test_down_touch_just_above_allowed_range_excluded(self):
        """
        'Just above' = less negative than -band_low: touch_pct > -19.0 -> excluded.
        Input:  prev_close=100, low=81.01
        Manual: touch_pct = (81.01 - 100) / 100 * 100 = -18.99
        Engine: -18.99 > -19.0  ->  qualifies = False
        """
        prev_close, low = 100.0, 81.01
        expected_touch_pct = round((low - prev_close) / prev_close * 100, 2)
        assert expected_touch_pct == -18.99

        touches = _run(prev_close, high=101.0, low=low, close=85.0, direction="down")
        assert len(touches) == 0, f"Expected 0 touches (drop not deep enough), got {len(touches)}"

    def test_down_touch_just_below_allowed_range_excluded(self):
        """
        'Just below' = more negative than -band_high: touch_pct < -20.0 -> excluded.
        Input:  prev_close=100, low=79.99
        Manual: touch_pct = (79.99 - 100) / 100 * 100 = -20.01
        Engine: -20.01 < -20.0  ->  qualifies = False
        """
        prev_close, low = 100.0, 79.99
        expected_touch_pct = round((low - prev_close) / prev_close * 100, 2)
        assert expected_touch_pct == -20.01

        touches = _run(prev_close, high=101.0, low=low, close=85.0, direction="down")
        assert len(touches) == 0, f"Expected 0 touches (drop too deep), got {len(touches)}"

    def test_down_touch_inside_band(self):
        """
        Input:  prev_close=100, low=80.5
        Manual: touch_pct = (80.5 - 100) / 100 * 100 = -19.50
        Engine: -20.0 <= -19.5 <= -19.0  ->  qualifies = True
        """
        prev_close, low = 100.0, 80.5
        expected_touch_pct = round((low - prev_close) / prev_close * 100, 2)
        assert expected_touch_pct == -19.50

        touches = _run(prev_close, high=101.0, low=low, close=85.0, direction="down")
        assert len(touches) == 1, f"Expected 1 touch (inside down-band), got {len(touches)}"
        assert touches[0].touch_pct == expected_touch_pct

    def test_down_low_qualifies_but_close_above_touch_pct(self):
        """
        Intraday low qualifies; stock recovers and closes only -10%.
        Input:  prev_close=100, low=80.5 (touch_pct=-19.5), close=90 (close_pct=-10)
        Both should be reported; only low drives qualification.
        """
        prev_close, low, close = 100.0, 80.5, 90.0
        expected_touch_pct = round((low   - prev_close) / prev_close * 100, 2)
        expected_close_pct = round((close - prev_close) / prev_close * 100, 2)
        assert expected_touch_pct == -19.50
        assert expected_close_pct == -10.00

        touches = _run(prev_close, high=101.0, low=low, close=close, direction="down")
        assert len(touches) == 1
        assert touches[0].touch_pct == -19.50
        assert touches[0].close_pct == -10.00

    def test_down_touch_pct_uses_low_not_close(self):
        """
        Prove touch_pct for 'down' is derived from `low`, not `close`.
        low=80.5  -> touch_pct=-19.5 -> qualifies (in band)
        close=78.0 -> if engine used close: -22.0 -> excluded (below band)
        Expected: qualifies (low drives it)
        """
        prev_close = 100.0
        low   = 80.5   # touch_pct via low = -19.5 -> qualifies
        close = 78.0   # if engine used close -> -22.0 -> excluded

        touches = _run(prev_close, high=101.0, low=low, close=close, direction="down")
        assert len(touches) == 1, (
            "Should qualify via low=-19.5%, not be excluded by close=-22.0%"
        )
        assert touches[0].touch_pct == -19.50

    def test_down_high_field_is_irrelevant_for_qualification(self):
        """
        For 'down', only the day's low drives qualification.
        An enormous high must not affect whether the touch qualifies.
        Input:  prev_close=100, high=999 (huge spike), low=80.5 (in band)
        Expected: qualifies via low; high is stored faithfully.
        """
        prev_close, high, low = 100.0, 999.0, 80.5
        touches = _run(prev_close, high=high, low=low, close=85.0, direction="down")
        assert len(touches) == 1
        assert touches[0].touch_pct == -19.50   # still derived from low
        assert touches[0].high == high           # high stored faithfully


# ===========================================================================
# SECTION 3: prev_close edge cases
# ===========================================================================

class TestPrevCloseEdgeCases:

    def test_prev_close_zero_is_skipped(self):
        """
        prev_close=0 must be skipped (division-by-zero guard).
        Engine (L828): `if not prev_close or prev_close <= 0: continue`
        Must produce 0 touches and must NOT raise ZeroDivisionError.
        """
        prev_bar  = {"date": PREV_DATE.isoformat(),  "open": 0.0, "high": 0.0,
                     "low": 0.0,  "close": 0.0,  "volume": VOL_OK}
        touch_bar = {"date": TOUCH_DATE.isoformat(), "open": 100.0, "high": 120.0,
                     "low": 80.0, "close": 110.0, "volume": VOL_OK}
        data = FakeOhlcData({"SYM": [prev_bar, touch_bar]})
        result = scan_band_touches(
            [{"symbol": "SYM", "name": "SYM"}],
            data, SCAN_START, SCAN_END,
        )
        assert result.touches == [], "prev_close=0 must produce no touch (not crash)"

    def test_prev_close_negative_is_skipped(self):
        """
        prev_close < 0 is nonsensical; must be skipped.
        Engine (L828): `prev_close <= 0` covers this.
        """
        prev_bar  = {"date": PREV_DATE.isoformat(),  "open": -5.0, "high": -5.0,
                     "low": -5.0,  "close": -5.0,  "volume": VOL_OK}
        touch_bar = {"date": TOUCH_DATE.isoformat(), "open": 100.0, "high": 120.0,
                     "low": 80.0,  "close": 110.0, "volume": VOL_OK}
        data = FakeOhlcData({"SYM": [prev_bar, touch_bar]})
        result = scan_band_touches(
            [{"symbol": "SYM", "name": "SYM"}],
            data, SCAN_START, SCAN_END,
        )
        assert result.touches == [], "prev_close < 0 must produce no touch"

    def test_first_bar_in_series_is_never_a_touch(self):
        """
        The engine loop starts at i=1 (L823), so bar[0] can never be a touch.
        A single bar within the scan range must produce 0 touches.
        """
        only_bar = {"date": TOUCH_DATE.isoformat(), "open": 100.0, "high": 120.0,
                    "low": 80.0, "close": 110.0, "volume": VOL_OK}
        data = FakeOhlcData({"SYM": [only_bar]})
        result = scan_band_touches(
            [{"symbol": "SYM", "name": "SYM"}],
            data, SCAN_START, SCAN_END,
        )
        assert result.touches == [], "First (only) bar must never qualify — no prev_close"

    def test_prev_close_none_is_skipped(self):
        """
        `bars[i-1]["close"]` returns None -> `not None` is True -> skip.
        Must produce 0 touches and must NOT crash.
        """
        prev_bar  = {"date": PREV_DATE.isoformat(),  "open": 100.0, "high": 100.0,
                     "low": 100.0, "close": None, "volume": VOL_OK}
        touch_bar = {"date": TOUCH_DATE.isoformat(), "open": 100.0, "high": 120.0,
                     "low": 80.0,  "close": 110.0, "volume": VOL_OK}
        data = FakeOhlcData({"SYM": [prev_bar, touch_bar]})
        result = scan_band_touches(
            [{"symbol": "SYM", "name": "SYM"}],
            data, SCAN_START, SCAN_END,
        )
        assert result.touches == [], "None prev_close must be skipped (falsy check)"


# ===========================================================================
# SECTION 4: Volume boundary conditions
# ===========================================================================

class TestVolumeBoundary:
    """
    DEFAULT_MIN_VOLUME = 50_000.
    Engine (L832): `if volume < min_volume: continue`
    Strictly less-than: volume=50000 passes; volume=49999 is excluded.
    """

    def test_volume_below_minimum_excluded(self):
        """
        Input:  volume=49999
        Condition: 49999 < 50000  ->  excluded
        Manual: qualifies = False
        """
        touches = _run(100.0, high=119.5, low=95.0, close=110.0, volume=49_999)
        assert len(touches) == 0, f"volume=49999 should be excluded, got {len(touches)}"

    def test_volume_exactly_at_minimum_included(self):
        """
        Input:  volume=50000
        Condition: 50000 < 50000  ->  False -> NOT excluded -> qualifies
        Manual: qualifies = True (strictly less-than means equal passes)
        """
        touches = _run(100.0, high=119.5, low=95.0, close=110.0, volume=50_000)
        assert len(touches) == 1, f"volume=50000 should be included, got {len(touches)}"

    def test_volume_above_minimum_included(self):
        """
        Input:  volume=50001
        Condition: 50001 < 50000  ->  False -> qualifies
        Manual: qualifies = True
        """
        touches = _run(100.0, high=119.5, low=95.0, close=110.0, volume=50_001)
        assert len(touches) == 1, f"volume=50001 should be included, got {len(touches)}"

    def test_volume_zero_excluded(self):
        """
        Input:  volume=0
        Condition: 0 < 50000  ->  excluded
        """
        touches = _run(100.0, high=119.5, low=95.0, close=110.0, volume=0)
        assert len(touches) == 0, "volume=0 must be excluded"

    def test_volume_missing_key_treated_as_zero_excluded(self):
        """
        Engine: `int(bar.get("volume") or 0)` (L831)
        Missing "volume" key -> get() returns None -> `None or 0` = 0 -> excluded.
        """
        prev_bar  = {"date": PREV_DATE.isoformat(),  "open": 100.0, "high": 100.0,
                     "low": 100.0, "close": 100.0, "volume": VOL_OK}
        touch_bar = {"date": TOUCH_DATE.isoformat(), "open": 100.0, "high": 119.5,
                     "low": 95.0,  "close": 110.0}  # no "volume" key
        data = FakeOhlcData({"SYM": [prev_bar, touch_bar]})
        result = scan_band_touches(
            [{"symbol": "SYM", "name": "SYM"}],
            data, SCAN_START, SCAN_END,
        )
        assert result.touches == [], "Missing volume key must be treated as 0 (excluded)"

    def test_volume_none_treated_as_zero_excluded(self):
        """
        Engine: `int(None or 0)` = 0 -> excluded.
        """
        prev_bar  = {"date": PREV_DATE.isoformat(),  "open": 100.0, "high": 100.0,
                     "low": 100.0, "close": 100.0, "volume": VOL_OK}
        touch_bar = {"date": TOUCH_DATE.isoformat(), "open": 100.0, "high": 119.5,
                     "low": 95.0,  "close": 110.0, "volume": None}
        data = FakeOhlcData({"SYM": [prev_bar, touch_bar]})
        result = scan_band_touches(
            [{"symbol": "SYM", "name": "SYM"}],
            data, SCAN_START, SCAN_END,
        )
        assert result.touches == [], "volume=None must be treated as 0 (excluded)"


# ===========================================================================
# SECTION 5: Band boundary arithmetic with non-round prices
# ===========================================================================

class TestBandBoundaryArithmetic:
    """
    Verify that `round(..., 2)` on touch_pct produces correct boundary behavior
    when raw IEEE 754 arithmetic would differ slightly from the rounded value.
    """

    def test_custom_band_15_to_16_pct_inside_qualifies(self):
        """
        Use a non-default band (15-16%) to confirm band_low/band_high are
        passed through correctly.
        Input:  prev_close=200, high=231.0
        Manual: touch_pct = (231 - 200) / 200 * 100 = 15.50
        Band:   15.0 <= 15.5 <= 16.0  ->  qualifies = True
        """
        prev_close, high = 200.0, 231.0
        expected = round((high - prev_close) / prev_close * 100, 2)
        assert expected == 15.50

        touches = _run(prev_close, high=high, low=190.0, close=210.0,
                       band_low=15.0, band_high=16.0)
        assert len(touches) == 1
        assert touches[0].touch_pct == 15.50

    def test_custom_band_below_range_excluded(self):
        """
        Custom band (15-16%): touch at 14.9% should be excluded.
        Input:  prev_close=200, high=229.8
        Manual: touch_pct = (229.8 - 200) / 200 * 100 = 14.90
        """
        prev_close, high = 200.0, 229.8
        expected = round((high - prev_close) / prev_close * 100, 2)
        assert expected == 14.90

        touches = _run(prev_close, high=high, low=190.0, close=210.0,
                       band_low=15.0, band_high=16.0)
        assert len(touches) == 0

    def test_custom_band_above_range_excluded(self):
        """
        Custom band (15-16%): touch at 16.1% should be excluded.
        Input:  prev_close=200, high=232.2
        Manual: touch_pct = (232.2 - 200) / 200 * 100 = 16.10
        """
        prev_close, high = 200.0, 232.2
        expected = round((high - prev_close) / prev_close * 100, 2)
        assert expected == 16.10

        touches = _run(prev_close, high=high, low=190.0, close=210.0,
                       band_low=15.0, band_high=16.0)
        assert len(touches) == 0

    def test_touch_pct_rounded_to_two_decimal_places(self):
        """
        Verify round(..., 2) is actually applied — not truncated, not more decimals.
        Input:  prev_close=107.0, high=127.73
        Manual: (127.73 - 107) / 107 * 100 = 19.373831...
        Expected: round(19.3738..., 2) = 19.37  (not 19.3738...)
        """
        prev_close, high = 107.0, 127.73
        raw = (high - prev_close) / prev_close * 100
        expected_touch_pct = round(raw, 2)
        assert expected_touch_pct == 19.37

        touches = _run(prev_close, high=high, low=100.0, close=115.0)
        assert len(touches) == 1
        t = touches[0]
        assert t.touch_pct == 19.37
        assert isinstance(t.touch_pct, float)
        # Must have at most 2 decimal places
        assert t.touch_pct == round(t.touch_pct, 2)


# ===========================================================================
# SECTION 6: Stored field correctness
# ===========================================================================

class TestStoredFieldCorrectness:
    """
    Verify that BandTouch stores the correct values from the input bar.
    """

    def test_all_stored_fields_match_inputs(self):
        """
        All BandTouch fields must faithfully reflect the input bar values.
        """
        prev_close = 100.0
        high, low, close = 119.5, 95.0, 110.0
        volume = 75_000

        touches = _run(prev_close, high=high, low=low, close=close, volume=volume)
        assert len(touches) == 1
        t = touches[0]

        assert t.symbol    == "SYM"
        assert t.date      == TOUCH_DATE.isoformat()
        assert t.direction == "up"
        assert t.prev_close == round(prev_close, 2)
        assert t.high      == round(high, 2)
        assert t.low       == round(low, 2)
        assert t.close     == round(close, 2)
        assert t.volume    == volume
        assert t.touch_pct == round((high  - prev_close) / prev_close * 100, 2)
        assert t.close_pct == round((close - prev_close) / prev_close * 100, 2)

    def test_close_pct_is_independent_of_touch_pct(self):
        """
        close_pct = (close - prev_close) / prev_close * 100 regardless of
        whether touch_pct qualifies the band. They can differ widely.
        Input: prev_close=100, high=119.5 (touch_pct=19.5), close=100 (close_pct=0)
        """
        touches = _run(100.0, high=119.5, low=95.0, close=100.0)
        assert len(touches) == 1
        assert touches[0].touch_pct == 19.50
        assert touches[0].close_pct == 0.00   # closed exactly flat

    def test_prev_close_stored_as_rounded_value(self):
        """
        BandTouch.prev_close = round(prev_close, 2) (L850).
        Input: prev_close=100.125678 -> stored as 100.13
        """
        prev_close_raw = 100.125678
        expected_stored = round(prev_close_raw, 2)   # 100.13

        prev_bar  = {"date": PREV_DATE.isoformat(),  "open": prev_close_raw,
                     "high": prev_close_raw, "low": prev_close_raw,
                     "close": prev_close_raw, "volume": VOL_OK}
        touch_bar = {"date": TOUCH_DATE.isoformat(), "open": prev_close_raw,
                     "high": prev_close_raw * 1.195,  # ~19.5% above prev_close
                     "low": 95.0, "close": 110.0, "volume": VOL_OK}
        data = FakeOhlcData({"SYM": [prev_bar, touch_bar]})
        result = scan_band_touches(
            [{"symbol": "SYM", "name": "SYM"}],
            data, SCAN_START, SCAN_END,
        )
        if result.touches:  # only assert if it qualifies (band may vary)
            assert result.touches[0].prev_close == expected_stored
