"""Tests for the bhavcopy-backed band study (broad scan via bhavcopy, narrow
corporate-action cross-check via Yahoo). Both providers are faked, so this is
zero-network."""

import time
from datetime import date

import pytest

from api import create_app
from engine.backtest import (
    BandTouch,
    cross_check_corporate_actions,
    scan_band_touches_bhavcopy,
)


class FakeBhav:
    """days: {iso_date: [{symbol, date, open, high, low, close, prev_close,
    volume}, ...]}. Weekends/holidays are simply absent keys — matches
    NSEBhavcopyData.full_day()'s real behaviour of returning [] for those."""

    def __init__(self, days):
        self.days = days

    def full_day(self, d):
        return self.days.get(d.isoformat(), [])


def _row(symbol, o, h, lo, c, prev, v=200_000):
    return {"symbol": symbol, "open": o, "high": h, "low": lo, "close": c, "prev_close": prev, "volume": v}


START = date(2026, 1, 5)
END = date(2026, 1, 9)


def test_touch_found_via_bhavcopy_high():
    days = {
        "2026-01-05": [_row("X", 100, 119.5, 99, 115, 100)],  # touches 19.5% high, closes +15%
        "2026-01-06": [_row("X", 115, 116, 108, 109, 115)],   # day2: falls
    }
    result = scan_band_touches_bhavcopy([{"symbol": "X", "name": "X Ltd"}], FakeBhav(days), None, START, END)
    assert len(result.touches) == 1
    t = result.touches[0]
    assert t.touch_pct == 19.5
    assert t.close_pct == 15.0
    assert t.has_day2
    assert t.day2_pct_change == round((109 - 115) / 115 * 100, 2)


def test_weekend_gap_skipped_when_finding_day2():
    """Touch on a Friday must look forward PAST the weekend to Monday for
    day2, not treat Saturday/Sunday (absent from bhavcopy) as day2=None."""
    friday = date(2026, 1, 9)  # Jan 9 2026 is a Friday
    monday = date(2026, 1, 12)
    days = {
        friday.isoformat(): [_row("Y", 100, 120, 99, 118, 100)],
        # Sat/Sun: no bhavcopy entries at all (holiday-equivalent)
        monday.isoformat(): [_row("Y", 118, 119, 110, 112, 118)],
    }
    result = scan_band_touches_bhavcopy(
        [{"symbol": "Y", "name": "Y"}], FakeBhav(days), None, friday, friday
    )
    t = result.touches[0]
    assert t.has_day2
    assert t.day2_date == monday.isoformat()


def test_low_volume_excluded():
    days = {"2026-01-05": [_row("Z", 100, 119.5, 99, 115, 100, v=100)]}
    result = scan_band_touches_bhavcopy(
        [{"symbol": "Z", "name": "Z"}], FakeBhav(days), None, START, START, min_volume=50_000
    )
    assert result.touches == []


def test_no_symbol_list_needed_bhavcopy_is_authoritative():
    """A symbol appearing in bhavcopy but NOT in our separately-maintained
    universe list must still be found — bhavcopy's own day file is the
    source of truth for which stocks exist, not the equity-master CSV."""
    days = {"2026-01-05": [_row("UNLISTED_IN_ROWS", 100, 119.5, 99, 115, 100)]}
    result = scan_band_touches_bhavcopy([], FakeBhav(days), None, START, START)
    assert len(result.touches) == 1
    assert result.touches[0].symbol == "UNLISTED_IN_ROWS"
    assert result.touches[0].name == "UNLISTED_IN_ROWS"  # falls back to symbol when no name known


# --- corporate-action cross-check ---------------------------------------------
class FakeYahoo:
    def __init__(self, bars):
        self.bars = bars  # {symbol: [{date, close}, ...]}

    def history(self, tickers, start, end):
        out = {}
        for t in tickers:
            if t not in self.bars:
                continue
            out[t] = [b for b in self.bars[t] if start.isoformat() <= b["date"] <= end.isoformat()]
        return out


def _mk_touch(symbol, d, close_pct):
    return BandTouch(
        symbol=symbol, name=symbol, date=d, direction="up", prev_close=100.0,
        high=100 * (1 + close_pct / 100), low=99.0, touch_pct=close_pct,
        close=100 * (1 + close_pct / 100), close_pct=close_pct, volume=200_000,
    )


def test_agreeing_sources_not_flagged():
    """Bhavcopy says +19.5%, Yahoo (adjusted) agrees closely -> not flagged."""
    t = _mk_touch("X", "2026-01-05", 19.5)
    yahoo = FakeYahoo({"X": [
        {"date": "2026-01-02", "close": 100.0},
        {"date": "2026-01-05", "close": 119.4},  # yahoo pct ~= 19.4%, matches within tolerance
    ]})
    cross_check_corporate_actions([t], yahoo)
    assert t.corporate_action_suspected is False


def test_sharp_divergence_flagged_as_corporate_action():
    """Bhavcopy (raw) says +19.5% but Yahoo (split-adjusted) says the stock
    barely moved -> the gap is the split/bonus signature, must be flagged."""
    t = _mk_touch("X", "2026-01-05", 19.5)
    yahoo = FakeYahoo({"X": [
        {"date": "2026-01-02", "close": 500.0},   # yahoo already reflects a split baseline
        {"date": "2026-01-05", "close": 501.0},   # yahoo pct ~= 0.2% -> huge gap vs bhavcopy's 19.5%
    ]})
    cross_check_corporate_actions([t], yahoo)
    assert t.corporate_action_suspected is True


def test_missing_yahoo_data_leaves_unflagged_not_crashed():
    t = _mk_touch("NODATA", "2026-01-05", 19.5)
    cross_check_corporate_actions([t], FakeYahoo({}))
    assert t.corporate_action_suspected is False


def test_yahoo_failure_does_not_block_the_study():
    class BrokenYahoo:
        def history(self, tickers, start, end):
            raise ConnectionError("network down")

    t = _mk_touch("X", "2026-01-05", 19.5)
    cross_check_corporate_actions([t], BrokenYahoo())  # must not raise
    assert t.corporate_action_suspected is False


def test_scan_bhavcopy_runs_cross_check_when_yf_data_given():
    days = {
        "2026-01-05": [_row("X", 500, 597.5, 495, 575, 500)],  # +19.5% touch (raw)
        "2026-01-06": [_row("X", 575, 580, 560, 565, 575)],
    }
    # Yahoo (adjusted) shows this stock barely moved that day -> split signature
    yahoo = FakeYahoo({"X": [
        {"date": "2026-01-02", "close": 100.0},
        {"date": "2026-01-05", "close": 100.5},
    ]})
    result = scan_band_touches_bhavcopy(
        [{"symbol": "X", "name": "X Ltd"}], FakeBhav(days), yahoo, START, END
    )
    assert result.touches[0].corporate_action_suspected is True


# --- API: bhavcopy is now the DEFAULT source ----------------------------------
@pytest.fixture
def client(tmp_path):
    """`source` defaults to "bhavcopy" now, so a fake must be injected into
    BHAVCOPY_DATA — otherwise this would silently try to hit the real NSE
    network during a test run."""
    app = create_app(data_root=str(tmp_path), start_worker=False, price_source_mode="manual")
    app.config.update(TESTING=True)
    days = {
        "2026-01-05": [_row("XSTOCK", 100, 119.5, 99, 115, 100)],
        "2026-01-06": [_row("XSTOCK", 115, 116, 108, 109, 115)],
    }
    app.config["BHAVCOPY_DATA"] = FakeBhav(days)
    app.config["BACKTEST_DATA"] = None  # cross-check is optional; prove the study runs without it
    c = app.test_client()
    r = c.post("/auth/register", json={"username": "rishi", "password": "supersecret1"})
    c.environ_base["HTTP_AUTHORIZATION"] = f"Bearer {r.get_json()['token']}"
    import os
    with open(os.path.join(str(tmp_path), "nse_universe.csv"), "w", encoding="utf-8") as f:
        f.write("symbol,name,series\nXSTOCK,X Stock Ltd,EQ\n")
    return c


def test_band_study_defaults_to_bhavcopy_source(client):
    r = client.post("/band-study/run", json={"start_date": "2026-01-01", "end_date": "2026-01-31"})
    assert r.status_code == 202

    for _ in range(50):
        st = client.get("/band-study/status").get_json()
        if st["status"] in ("done", "error"):
            break
        time.sleep(0.05)
    assert st["status"] == "done", st

    detail = client.get(f"/band-study/runs/{st['run_id']}").get_json()
    assert detail["run"]["source"] == "bhavcopy"
    assert detail["run"]["total_touches"] == 1
    assert detail["touches"][0]["symbol"] == "XSTOCK"
    assert detail["touches"][0]["corporate_action_suspected"] is False  # no Yahoo data -> unflagged, not crashed


def test_band_study_bhavcopy_source_available_in_manual_mode(tmp_path):
    """bhavcopy has nothing to do with PAPERTRADING_PRICE_SOURCE — it must
    keep working even when the app's live-price mode is 'manual'."""
    app = create_app(data_root=str(tmp_path), start_worker=False, price_source_mode="manual")
    app.config["BHAVCOPY_DATA"] = FakeBhav({})
    c = app.test_client()
    r = c.post("/auth/register", json={"username": "bob", "password": "supersecret1"})
    c.environ_base["HTTP_AUTHORIZATION"] = f"Bearer {r.get_json()['token']}"
    import os
    with open(os.path.join(str(tmp_path), "nse_universe.csv"), "w", encoding="utf-8") as f:
        f.write("symbol,name,series\n")
    resp = c.post("/band-study/run", json={"start_date": "2026-01-01", "end_date": "2026-01-31"})
    assert resp.status_code == 202  # not 503 — bhavcopy doesn't need PRICE_SOURCE
