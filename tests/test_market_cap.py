"""Tests for SEBI / AMFI market-cap classification and caching."""

import os
import tempfile
import time
import pytest

from engine.amfi import load_amfi_master, _extract_symbols_from_csv
from engine.market_cap import cap_buckets_for, get_cap_detail, _bucket_from_cap_cr


SAMPLE_NIFTY100_CSV = """Company Name,Industry,Symbol,Series,ISIN Code
Reliance Industries Ltd.,Oil Gas & Consumable Fuels,RELIANCE,EQ,INE002A01018
Tata Consultancy Services Ltd.,Information Technology,TCS,EQ,INE467B01029
HDFC Bank Ltd.,Financial Services,HDFCBANK,EQ,INE040A01034
Infosys Ltd.,Information Technology,INFY,EQ,INE009A01021
"""

SAMPLE_MIDCAP150_CSV = """Company Name,Industry,Symbol,Series,ISIN Code
The Federal Bank Ltd.,Financial Services,FEDERALBNK,EQ,INE171A01029
Dixon Technologies (India) Ltd.,Consumer Durables,DIXON,EQ,INE935N01020
Polycab India Ltd.,Capital Goods,POLYCAB,EQ,INE455K01017
"""

SAMPLE_SMALLCAP250_CSV = """Company Name,Industry,Symbol,Series,ISIN Code
20 Microns Ltd.,Commodities,20MICRONS,EQ,INE144J01027
AAA Technologies Ltd.,Information Technology,AAATECH,EQ,INE0D0U01013
"""


def test_extract_symbols_from_csv():
    items = _extract_symbols_from_csv(SAMPLE_NIFTY100_CSV)
    assert len(items) == 4
    assert items[0]["symbol"] == "RELIANCE"
    assert items[0]["name"] == "Reliance Industries Ltd."
    assert items[0]["isin"] == "INE002A01018"


def test_bucket_from_cap_cr():
    assert _bucket_from_cap_cr(25_000) == "large"
    assert _bucket_from_cap_cr(20_000) == "large"
    assert _bucket_from_cap_cr(19_999) == "mid"
    assert _bucket_from_cap_cr(5_000) == "mid"
    assert _bucket_from_cap_cr(4_999) == "small"
    assert _bucket_from_cap_cr(500) == "small"


def test_amfi_master_loading_and_categories():
    master_path = os.path.join("data", "amfi_market_caps.json")
    master = load_amfi_master(master_path)
    assert isinstance(master, dict)
    assert len(master) >= 100

    # Test Large Caps (Nifty 100)
    for sym in ["RELIANCE", "TCS", "INFY", "HDFCBANK"]:
        if sym in master:
            assert master[sym]["bucket"] == "large"
            assert master[sym]["tier"] == "1-100"

    # Test Mid Caps (Nifty Midcap 150)
    for sym in ["FEDERALBNK", "DIXON", "POLYCAB"]:
        if sym in master:
            assert master[sym]["bucket"] == "mid"
            assert master[sym]["tier"] == "101-250"


def test_cap_buckets_for_lookups():
    cache_file = os.path.join("data", "market_cap_cache.json")
    syms = ["RELIANCE", "TCS", "INFY", "FEDERALBNK", "DIXON", "20MICRONS", "AAATECH"]
    res = cap_buckets_for(syms, cache_file)

    assert res.get("RELIANCE") == "large"
    assert res.get("TCS") == "large"
    assert res.get("INFY") == "large"
    assert res.get("FEDERALBNK") == "mid"
    assert res.get("DIXON") == "mid"
    assert res.get("20MICRONS") == "small"
    assert res.get("AAATECH") == "small"


def test_get_cap_detail():
    cache_file = os.path.join("data", "market_cap_cache.json")
    detail = get_cap_detail("RELIANCE", cache_file)
    assert detail["bucket"] == "large"
    assert detail["tier"] == "1-100"
    assert "Reliance" in detail["name"]


def test_cap_buckets_speed():
    cache_file = os.path.join("data", "market_cap_cache.json")
    # Batch lookup of 50 symbols should be instant (< 100ms)
    syms = ["RELIANCE", "TCS", "INFY", "FEDERALBNK", "DIXON", "20MICRONS"] * 10
    start = time.perf_counter()
    res = cap_buckets_for(syms, cache_file)
    elapsed_ms = (time.perf_counter() - start) * 1000

    assert len(res) == 6  # unique keys
    assert elapsed_ms < 100, f"Lookup too slow: {elapsed_ms:.2f}ms"
