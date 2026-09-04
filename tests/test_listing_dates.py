"""Tests for NSE listing date parsing, universe cache integration, and API route."""

import os
import pytest
from datetime import date
from engine import universe
from engine.backtest import BandTouch, BandStudyResult
from engine.repository import BandStudyRepository
from api import create_app


def test_parse_listing_date_formats():
    assert universe.parse_listing_date("27-NOV-1995") == "1995-11-27"
    assert universe.parse_listing_date("05-JAN-2021") == "2021-01-05"
    assert universe.parse_listing_date("15-Jan-2024") == "2024-01-15"
    assert universe.parse_listing_date("2023-08-10") == "2023-08-10"
    assert universe.parse_listing_date("12/04/2019") == "2019-04-12"
    assert universe.parse_listing_date("") is None
    assert universe.parse_listing_date(None) is None
    assert universe.parse_listing_date("not-a-date") is None


def test_parse_csv_extracts_listing_date():
    csv_text = (
        "SYMBOL,NAME OF COMPANY,SERIES,DATE OF LISTING,PAID UP VALUE,MARKET LOT,ISIN NUMBER,FACE VALUE\n"
        "RELIANCE,Reliance Industries Limited,EQ,29-NOV-1995,10,1,INE002A01018,10\n"
        "TATAMOTORS,Tata Motors Limited,EQ,22-JUL-1998,2,1,INE155A01022,2\n"
        "JIOFIN,Jio Financial Services Limited,EQ,21-AUG-2023,10,1,INE758E01017,10\n"
        "OLDDEBT,Some Debt Instrument,BZ,10-JAN-2015,100,1,INE000000000,100\n"
    )
    rows = universe._parse_csv(csv_text)
    assert len(rows) == 4
    rel = next(r for r in rows if r["symbol"] == "RELIANCE")
    assert rel["listing_date"] == "1995-11-29"
    assert rel["name"] == "Reliance Industries Limited"
    assert rel["series"] == "EQ"

    jio = next(r for r in rows if r["symbol"] == "JIOFIN")
    assert jio["listing_date"] == "2023-08-21"

    debt = next(r for r in rows if r["symbol"] == "OLDDEBT")
    assert debt["listing_date"] == "2015-01-10"
    assert debt["series"] == "BZ"


def test_universe_cache_roundtrip(tmp_path):
    cache_file = str(tmp_path / "nse_universe.csv")
    sample_rows = [
        {"symbol": "INFY", "name": "Infosys Limited", "series": "EQ", "listing_date": "1994-06-14"},
        {"symbol": "TCS", "name": "Tata Consultancy Services Limited", "series": "EQ", "listing_date": "2004-08-25"},
    ]
    universe._write_cache(cache_file, sample_rows)
    loaded, src = universe.load_universe(cache_file)
    assert src == "cache"
    assert len(loaded) == 2
    assert loaded[0]["symbol"] == "INFY"
    assert loaded[0]["listing_date"] == "1994-06-14"

    dates = universe.listing_dates_for(["INFY", "TCS", "UNKNOWN"], cache_file)
    assert dates == {
        "INFY": "1994-06-14",
        "TCS": "2004-08-25",
    }


def test_api_listing_dates_route(tmp_path):
    app = create_app(data_root=str(tmp_path), start_worker=False, price_source_mode="manual")
    app.config.update(TESTING=True)
    anon = app.test_client()

    r = anon.post("/auth/register", json={"username": "rishi", "password": "supersecret1"})
    token = r.get_json()["token"]
    anon.environ_base["HTTP_AUTHORIZATION"] = f"Bearer {token}"

    db_path = str(tmp_path / "papertrading.db")
    repo = BandStudyRepository(db_path)
    run_id = repo.create_run("2024-01-01", "2024-01-31", 19.0, 20.0, "up", source="bhavcopy")

    t = BandTouch(
        symbol="TFCILTD", name="Tourism Finance Corporation", date="2024-01-15",
        direction="up", prev_close=100.0, high=120.0, low=99.0, touch_pct=20.0,
        close=118.0, close_pct=18.0, volume=500000,
    )
    result = BandStudyResult(
        start_date="2024-01-01", end_date="2024-01-31", band_low=19.0, band_high=20.0,
        direction="up", universe_count=1, touches=[t],
    )
    repo.finish_run(run_id, result)

    universe_file = str(tmp_path / "nse_universe.csv")
    universe._write_cache(universe_file, [
        {"symbol": "TFCILTD", "name": "Tourism Finance Corporation", "series": "EQ", "listing_date": "1995-11-27"},
    ])

    res = anon.get(f"/band-study/runs/{run_id}/listing-dates")
    assert res.status_code == 200
    data = res.get_json()
    assert "listing_dates" in data
    assert data["listing_dates"]["TFCILTD"] == "1995-11-27"
