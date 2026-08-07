"""Tests for the Phase-1 band-touch study: find every stock whose INTRADAY
HIGH touched a tight % band (e.g. 19-20%) across a date range, then measure
what its very next trading day did. A fake OHLC data provider makes this
zero-network."""

import time
from datetime import date, timedelta

import pytest

from api import create_app
from engine.backtest import scan_band_touches
from engine.db import init_db
from engine.repository import BandStudyRepository


class FakeOhlcData:
    """bars: {symbol: [{date, open, high, low, close, volume}, ...]}."""

    def __init__(self, bars):
        self.bars = bars

    def history(self, tickers, start, end):
        out = {}
        for t in tickers:
            if t not in self.bars:
                continue
            out[t] = [b for b in self.bars[t] if start.isoformat() <= b["date"] <= end.isoformat()]
        return out


def _bar(d, o, h, lo, c, v=200_000):
    return {"date": d.isoformat(), "open": o, "high": h, "low": lo, "close": c, "volume": v}


START = date(2026, 1, 1)
END = date(2026, 1, 31)


def _days(n):
    return [START - timedelta(days=i) for i in range(n, 0, -1)]  # n days BEFORE START, ascending


def test_touch_detected_via_high_even_when_close_fades():
    """The core requirement: a stock that spikes 19.5% intraday then fades to
    close at only +15% must still be counted — close-only detection would
    miss this entirely."""
    lead_in = [_bar(d, 100, 101, 99, 100) for d in _days(5)]
    spike_day = date(2026, 1, 5)
    # prev close 100. High touches 119.5 (+19.5%), but closes at 115 (+15%).
    spike = [_bar(spike_day, 100, 119.5, 99, 115)]
    day2 = [_bar(date(2026, 1, 6), 115, 116, 108, 109)]  # day2 falls
    bars = {"X": lead_in + spike + day2}

    result = scan_band_touches([{"symbol": "X", "name": "X Ltd"}], FakeOhlcData(bars), START, END)
    assert len(result.touches) == 1
    t = result.touches[0]
    assert t.touch_pct == 19.5
    assert t.close_pct == 15.0  # close-based change is reported too, just not what qualified it
    assert t.date == "2026-01-05"


def test_below_band_excluded():
    lead_in = [_bar(d, 100, 101, 99, 100) for d in _days(5)]
    spike = [_bar(date(2026, 1, 5), 100, 112, 99, 110)]  # only +12% high
    bars = {"Y": lead_in + spike}
    result = scan_band_touches([{"symbol": "Y", "name": "Y"}], FakeOhlcData(bars), START, END)
    assert result.touches == []


def test_above_band_excluded():
    """25% high is above the 19-20 band -> not counted in THIS study, even
    though it's a genuine big move by any wider threshold."""
    lead_in = [_bar(d, 100, 101, 99, 100) for d in _days(5)]
    spike = [_bar(date(2026, 1, 5), 100, 125, 99, 120)]
    bars = {"Z": lead_in + spike}
    result = scan_band_touches([{"symbol": "Z", "name": "Z"}], FakeOhlcData(bars), START, END)
    assert result.touches == []


def test_day2_metrics_computed():
    lead_in = [_bar(d, 100, 101, 99, 100) for d in _days(5)]
    spike = [_bar(date(2026, 1, 5), 100, 119, 99, 118)]  # +19% high, closes 118
    day2 = [_bar(date(2026, 1, 6), 118, 120, 110, 112)]  # opens 118, closes 112
    bars = {"A": lead_in + spike + day2}
    result = scan_band_touches([{"symbol": "A", "name": "A"}], FakeOhlcData(bars), START, END)
    t = result.touches[0]
    assert t.has_day2 is True
    assert t.day2_date == "2026-01-06"
    assert t.day2_pct_change == round((112 - 118) / 118 * 100, 2)  # day2 close vs day1 close
    assert t.day2_open_close_pct == round((112 - 118) / 118 * 100, 2)  # open==close here so same


def test_last_day_in_range_has_no_day2():
    """A touch on the very last bar of data has nothing to look forward to —
    must not crash, must report has_day2=False."""
    lead_in = [_bar(d, 100, 101, 99, 100) for d in _days(5)]
    spike = [_bar(date(2026, 1, 5), 100, 119, 99, 118)]
    bars = {"B": lead_in + spike}  # no day after
    result = scan_band_touches([{"symbol": "B", "name": "B"}], FakeOhlcData(bars), START, END)
    t = result.touches[0]
    assert t.has_day2 is False
    assert t.day2_pct_change is None


def test_low_volume_excluded():
    lead_in = [_bar(d, 100, 101, 99, 100) for d in _days(5)]
    spike = [_bar(date(2026, 1, 5), 100, 119, 99, 118, v=100)]  # tiny volume
    bars = {"C": lead_in + spike}
    result = scan_band_touches(
        [{"symbol": "C", "name": "C"}], FakeOhlcData(bars), START, END, min_volume=50_000
    )
    assert result.touches == []


def test_down_direction_uses_low_not_high():
    lead_in = [_bar(d, 100, 101, 99, 100) for d in _days(5)]
    # -19.5% via the LOW, close only -15%
    spike = [_bar(date(2026, 1, 5), 100, 101, 80.5, 85)]
    day2 = [_bar(date(2026, 1, 6), 85, 92, 84, 90)]  # bounces
    bars = {"D": lead_in + spike + day2}
    result = scan_band_touches(
        [{"symbol": "D", "name": "D"}], FakeOhlcData(bars), START, END, direction="down"
    )
    t = result.touches[0]
    assert t.direction == "down"
    assert t.touch_pct == -19.5
    assert t.close_pct == -15.0


def test_multiple_touches_across_the_month_all_collected():
    lead_in = [_bar(d, 100, 101, 99, 100) for d in _days(5)]
    spike1 = [_bar(date(2026, 1, 5), 100, 119, 99, 115)]
    mid = [_bar(date(2026, 1, 6), 115, 116, 114, 115)]
    spike2 = [_bar(date(2026, 1, 20), 115, 137, 114, 130)]  # another +19.1% touch later in month
    bars = {"E": lead_in + spike1 + mid + spike2}
    result = scan_band_touches([{"symbol": "E", "name": "E"}], FakeOhlcData(bars), START, END)
    assert len(result.touches) == 2
    assert {t.date for t in result.touches} == {"2026-01-05", "2026-01-20"}


# --- summary: up/down tracked separately --------------------------------------
def test_summary_separates_up_and_down_never_blends():
    lead_in = [_bar(d, 100, 101, 99, 100) for d in _days(5)]

    def stock(sym, day2_close):
        spike = [_bar(date(2026, 1, 5), 100, 119, 99, 118)]
        day2 = [_bar(date(2026, 1, 6), 118, max(118, day2_close), min(118, day2_close), day2_close)]
        return sym, lead_in + spike + day2

    bars = dict([
        stock("UP1", 130),   # day2 pct = +10.17
        stock("UP2", 124),   # day2 pct = +5.08
        stock("DOWN1", 100), # day2 pct = -15.25
    ])
    rows = [{"symbol": s, "name": s} for s in bars]
    result = scan_band_touches(rows, FakeOhlcData(bars), START, END)
    s = result.summary()
    assert s.total_touches == 3
    assert s.with_day2 == 3
    assert s.up_count == 2
    assert s.down_count == 1
    assert s.up_pct == round(2 / 3 * 100, 1)
    assert s.down_pct == round(1 / 3 * 100, 1)
    # averages computed WITHIN each group, never blended together
    assert s.avg_day2_pct_up == round((10.17 + 5.08) / 2, 2)
    assert s.avg_day2_pct_down == -15.25


def test_summary_empty():
    result = scan_band_touches([{"symbol": "X", "name": "X"}], FakeOhlcData({}), START, END)
    s = result.summary()
    assert s.total_touches == 0
    assert s.avg_day2_pct_up is None
    assert s.avg_day2_pct_down is None


# --- reliability: transient fetch failures must retry, not silently vanish --
class FlakyOnceData:
    """Fails the FIRST call for the given symbols (simulating a transient
    Yahoo hiccup), succeeds on any later call for the same symbols."""

    def __init__(self, bars, flaky_symbols):
        self.bars = bars
        self.flaky_symbols = set(flaky_symbols)
        self.calls = 0

    def history(self, tickers, start, end):
        self.calls += 1
        # First time any flaky symbol is requested, drop it from the response
        # entirely (as if Yahoo returned nothing for it that call).
        first_time_flaky = self.flaky_symbols.intersection(tickers)
        if first_time_flaky:
            self.flaky_symbols -= first_time_flaky  # only flaky on the FIRST call
            tickers = [t for t in tickers if t not in first_time_flaky]
        out = {}
        for t in tickers:
            if t not in self.bars:
                continue
            out[t] = [b for b in self.bars[t] if start.isoformat() <= b["date"] <= end.isoformat()]
        return out


def test_transient_failure_recovered_via_retry():
    lead_in = [_bar(d, 100, 101, 99, 100) for d in _days(5)]
    spike = [_bar(date(2026, 1, 5), 100, 119.5, 99, 118)]
    day2 = [_bar(date(2026, 1, 6), 118, 119, 108, 109)]
    bars = {"FLAKY": lead_in + spike + day2}
    data = FlakyOnceData(bars, flaky_symbols=["FLAKY"])

    result = scan_band_touches([{"symbol": "FLAKY", "name": "Flaky Ltd"}], data, START, END)
    assert len(result.touches) == 1  # recovered via retry, not silently dropped
    assert result.missing_symbols == []
    assert data.calls == 2  # one initial call + one retry


def test_permanent_failure_reported_not_hidden():
    """A symbol that NEVER returns data (not just once) must show up in
    missing_symbols — never silently disappear with no trace."""

    class AlwaysEmptyData:
        def history(self, tickers, start, end):
            return {}

    result = scan_band_touches(
        [{"symbol": "GONE", "name": "Gone Ltd"}], AlwaysEmptyData(), START, END
    )
    assert result.touches == []
    assert result.missing_symbols == ["GONE"]


# --- repository ----------------------------------------------------------------
@pytest.fixture
def db(tmp_path):
    p = str(tmp_path / "band_study.db")
    init_db(p)
    return p


def test_repository_roundtrip(db):
    repo = BandStudyRepository(db)
    assert repo.list_runs() == []

    lead_in = [_bar(d, 100, 101, 99, 100) for d in _days(5)]
    spike = [_bar(date(2026, 1, 5), 100, 119.5, 99, 115)]
    day2 = [_bar(date(2026, 1, 6), 115, 116, 108, 109)]
    bars = {"X": lead_in + spike + day2}
    result = scan_band_touches([{"symbol": "X", "name": "X Ltd"}], FakeOhlcData(bars), START, END)

    run_id = repo.create_run("2026-01-01", "2026-01-31", 19.0, 20.0, "up")
    repo.finish_run(run_id, result)

    detail = repo.get_run(run_id)
    assert detail["run"]["status"] == "done"
    assert detail["run"]["total_touches"] == 1
    assert detail["touches"][0]["symbol"] == "X"
    assert detail["touches"][0]["touch_pct"] == 19.5

    runs = repo.list_runs()
    assert len(runs) == 1
    assert runs[0]["start_date"] == "2026-01-01"


def test_repository_fail_run(db):
    repo = BandStudyRepository(db)
    run_id = repo.create_run("2026-01-01", "2026-01-31", 19.0, 20.0, "up")
    repo.fail_run(run_id, "network error")
    detail = repo.get_run(run_id)
    assert detail["run"]["status"] == "error"
    assert detail["run"]["error"] == "network error"


# --- API + async service ------------------------------------------------------
@pytest.fixture
def client(tmp_path):
    app = create_app(data_root=str(tmp_path), start_worker=False, price_source_mode="manual")
    app.config.update(TESTING=True)
    lead_in = [_bar(d, 100, 101, 99, 100) for d in _days(5)]
    spike = [_bar(date(2026, 1, 5), 100, 119.5, 99, 115)]
    day2 = [_bar(date(2026, 1, 6), 115, 116, 108, 109)]
    app.config["BACKTEST_DATA"] = FakeOhlcData({"XSTOCK": lead_in + spike + day2})
    c = app.test_client()
    r = c.post("/auth/register", json={"username": "rishi", "password": "supersecret1"})
    c.environ_base["HTTP_AUTHORIZATION"] = f"Bearer {r.get_json()['token']}"
    import os
    with open(os.path.join(str(tmp_path), "nse_universe.csv"), "w", encoding="utf-8") as f:
        f.write("symbol,name,series\nXSTOCK,X Stock Ltd,EQ\n")
    return c


def test_band_study_empty_before_any_run(client):
    assert client.get("/band-study/runs").get_json()["runs"] == []


def test_band_study_run_flow(client):
    r = client.post(
        "/band-study/run",
        json={"start_date": "2026-01-01", "end_date": "2026-01-31", "source": "yahoo"},
    )
    assert r.status_code == 202

    for _ in range(50):
        st = client.get("/band-study/status").get_json()
        if st["status"] in ("done", "error"):
            break
        time.sleep(0.05)
    assert st["status"] == "done", st
    run_id = st["run_id"]

    detail = client.get(f"/band-study/runs/{run_id}").get_json()
    assert detail["run"]["total_touches"] == 1
    assert detail["touches"][0]["symbol"] == "XSTOCK"
    assert detail["touches"][0]["day2_pct_change"] is not None

    runs = client.get("/band-study/runs").get_json()["runs"]
    assert len(runs) == 1


def test_band_study_rejects_end_before_start(client):
    r = client.post("/band-study/run", json={"start_date": "2026-01-31", "end_date": "2026-01-01"})
    assert r.status_code == 400


def test_band_study_rejects_future_end_date(client):
    r = client.post("/band-study/run", json={"start_date": "2026-01-01", "end_date": "2099-01-01"})
    assert r.status_code == 400


def test_band_study_rejects_bad_band(client):
    r = client.post(
        "/band-study/run",
        json={"start_date": "2026-01-01", "end_date": "2026-01-31", "band_low": 20, "band_high": 19},
    )
    assert r.status_code == 400


def test_band_study_unknown_run_404(client):
    assert client.get("/band-study/runs/999").status_code == 404


def test_band_study_yahoo_source_disabled_in_manual_mode(tmp_path):
    app = create_app(data_root=str(tmp_path), start_worker=False, price_source_mode="manual")
    c = app.test_client()
    r = c.post("/auth/register", json={"username": "bob", "password": "supersecret1"})
    c.environ_base["HTTP_AUTHORIZATION"] = f"Bearer {r.get_json()['token']}"
    resp = c.post(
        "/band-study/run",
        json={"start_date": "2026-01-01", "end_date": "2026-01-31", "source": "yahoo"},
    )
    assert resp.status_code == 503


def test_band_study_requires_auth(tmp_path):
    app = create_app(data_root=str(tmp_path), start_worker=False, price_source_mode="manual")
    assert app.test_client().get("/band-study/runs").status_code == 401
