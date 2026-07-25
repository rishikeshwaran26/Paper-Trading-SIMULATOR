"""Tests for the NSE bhavcopy parser: EQ-series filtering, whitespace
handling, and — the one that matters most — correctly detecting a
holiday/weekend instead of silently accepting NSE's stale-file fallback."""

from datetime import date

from engine.bhavcopy import NSEBhavcopyData, _parse_bhavcopy_csv, fetch_day


def _csv(rows):
    header = "SYMBOL, SERIES, DATE1, PREV_CLOSE, OPEN_PRICE, HIGH_PRICE, LOW_PRICE, LAST_PRICE, CLOSE_PRICE, AVG_PRICE, TTL_TRD_QNTY, TURNOVER_LACS, NO_OF_TRADES, DELIV_QTY, DELIV_PER\n"
    return header + "\n".join(rows)


def _row(symbol, series, date_str, prev_close, open_p, high, low, close, volume=100000):
    return f"{symbol}, {series}, {date_str}, {prev_close}, {open_p}, {high}, {low}, {close}, {close}, {close}, {volume}, 50.0, 100, 50000, 50.00"


def test_parses_eq_rows_and_skips_others():
    text = _csv([
        _row("RELIANCE", "EQ", "16-Jan-2026", 1300, 1310, 1330, 1305, 1320),
        _row("1018GS2026", "GS", "16-Jan-2026", 106, 106, 106, 105, 105),  # government security, not EQ
        _row("SOMEBE", "BE", "16-Jan-2026", 50, 51, 52, 49, 50),           # trade-to-trade, not EQ
    ])
    rows = _parse_bhavcopy_csv(text, date(2026, 1, 16))
    assert len(rows) == 1
    assert rows[0]["SYMBOL"] == "RELIANCE"


def test_whitespace_in_header_and_values_stripped():
    text = _csv([_row(" RELIANCE ", " EQ ", "16-Jan-2026", 1300, 1310, 1330, 1305, 1320)])
    rows = _parse_bhavcopy_csv(text, date(2026, 1, 16))
    assert rows[0]["SYMBOL"] == "RELIANCE"  # no stray whitespace
    assert rows[0]["SERIES"] == "EQ"


def test_holiday_stale_fallback_detected_and_rejected():
    """The core correctness guard: NSE serves the PREVIOUS trading day's file
    (200 OK, not 404) for a holiday. The DATE1 inside must not match what we
    asked for, so we must reject it rather than silently accept stale data
    under the wrong date."""
    # We ask for Jan 26 (a holiday), but the server serves Jan 23's file.
    text = _csv([_row("RELIANCE", "EQ", "23-Jan-2026", 1300, 1310, 1330, 1305, 1320)])
    result = _parse_bhavcopy_csv(text, date(2026, 1, 26))
    assert result is None


def test_matching_date_accepted():
    text = _csv([_row("RELIANCE", "EQ", "16-Jan-2026", 1300, 1310, 1330, 1305, 1320)])
    result = _parse_bhavcopy_csv(text, date(2026, 1, 16))
    assert result is not None
    assert len(result) == 1


def test_empty_file_returns_none():
    assert _parse_bhavcopy_csv("SYMBOL, SERIES, DATE1\n", date(2026, 1, 16)) is None


# --- fetch_day: caching behaviour (no real network — pre-seed the cache) -----
def test_fetch_day_reads_from_cache_without_network(tmp_path, monkeypatch):
    cache = str(tmp_path)
    # Pre-seed a cached file exactly as fetch_day would have written it.
    import csv
    path = tmp_path / "2026-01-16.csv"
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["SYMBOL", "SERIES", "DATE1", "PREV_CLOSE", "OPEN_PRICE", "HIGH_PRICE", "LOW_PRICE", "CLOSE_PRICE", "TTL_TRD_QNTY"])
        w.writeheader()
        w.writerow({"SYMBOL": "RELIANCE", "SERIES": "EQ", "DATE1": "16-Jan-2026", "PREV_CLOSE": "1300", "OPEN_PRICE": "1310", "HIGH_PRICE": "1330", "LOW_PRICE": "1305", "CLOSE_PRICE": "1320", "TTL_TRD_QNTY": "100000"})

    # Make any network attempt fail loudly so we know the cache path was used.
    monkeypatch.setattr("requests.get", lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not hit network")))

    rows = fetch_day(cache, date(2026, 1, 16))
    assert rows is not None
    assert rows[0]["SYMBOL"] == "RELIANCE"


def test_fetch_day_caches_empty_marker_for_non_trading_day(tmp_path, monkeypatch):
    """Once we've determined a date has no market (holiday), a re-run must not
    hit the network again for it."""
    cache = str(tmp_path)
    marker = tmp_path / "2026-01-26.csv.empty"
    marker.write_text("")

    monkeypatch.setattr("requests.get", lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not hit network")))
    rows = fetch_day(cache, date(2026, 1, 26))
    assert rows is None


# --- NSEBhavcopyData.history() ------------------------------------------------
def test_history_filters_to_requested_tickers(tmp_path):
    import csv

    # One combined file for the day, both symbols — matches the real shape
    # (a bhavcopy file always holds the whole market, never one symbol).
    path = tmp_path / "2026-01-16.csv"
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["SYMBOL", "SERIES", "DATE1", "PREV_CLOSE", "OPEN_PRICE", "HIGH_PRICE", "LOW_PRICE", "CLOSE_PRICE", "TTL_TRD_QNTY"])
        w.writeheader()
        w.writerow({"SYMBOL": "RELIANCE", "SERIES": "EQ", "DATE1": "16-Jan-2026", "PREV_CLOSE": "1300", "OPEN_PRICE": "1310", "HIGH_PRICE": "1330", "LOW_PRICE": "1305", "CLOSE_PRICE": "1320", "TTL_TRD_QNTY": "100000"})
        w.writerow({"SYMBOL": "TCS", "SERIES": "EQ", "DATE1": "16-Jan-2026", "PREV_CLOSE": "3300", "OPEN_PRICE": "3350", "HIGH_PRICE": "3420", "LOW_PRICE": "3340", "CLOSE_PRICE": "3400", "TTL_TRD_QNTY": "50000"})

    data = NSEBhavcopyData(str(tmp_path))
    bars = data.history(["RELIANCE"], date(2026, 1, 16), date(2026, 1, 16))
    assert list(bars.keys()) == ["RELIANCE"]
    assert bars["RELIANCE"][0]["close"] == 1320.0
