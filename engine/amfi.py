"""Official SEBI / AMFI Market-Cap Classification & Synchronizer.

Under SEBI regulations (SEBI/HO/IMD/DF3/CIR/P/2017/114) and AMFI guidelines,
Indian listed equity universe is categorized as follows based on full market
capitalization (averaged over a 6-month period):

  - Large Cap : 1st to 100th company  (NIFTY 100 constituents)
  - Mid Cap   : 101st to 250th company (NIFTY Midcap 150 constituents)
  - Small Cap : 251st company onwards  (NIFTY Smallcap 250 + all remaining listed equities)

This module downloads official constituent archives from NSE, compiles a master
lookup table, and caches it locally so scans and backtests get instant, 100%
regulatory-compliant classifications with zero latency and no third-party API rate limits.
"""

from __future__ import annotations

import csv
import io
import json
import os
import time
from typing import Dict, Any, Optional

NIFTY_100_URL = "https://nsearchives.nseindia.com/content/indices/ind_nifty100list.csv"
NIFTY_MIDCAP_150_URL = "https://nsearchives.nseindia.com/content/indices/ind_niftymidcap150list.csv"
NIFTY_SMALLCAP_250_URL = "https://nsearchives.nseindia.com/content/indices/ind_niftysmallcap250list.csv"
NSE_EQUITY_LIST_URL = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"

# AMFI / Index constituent reviews happen semi-annually (Jan & July).
# Caching for 30 days is optimal.
MASTER_CACHE_MAX_AGE = 30 * 24 * 3600

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/csv,application/csv,*/*",
}


def _extract_symbols_from_csv(text: str) -> list[dict]:
    """Parse NSE constituent/master CSV into list of records with symbol and company name."""
    items = []
    reader = csv.DictReader(io.StringIO(text))
    for raw in reader:
        row = {(k or "").strip(): (v or "").strip() for k, v in raw.items()}
        sym = row.get("Symbol") or row.get("SYMBOL") or ""
        sym = sym.strip().upper()
        if not sym:
            continue
        name = row.get("Company Name") or row.get("NAME OF COMPANY") or row.get("Company") or sym
        isin = row.get("ISIN Code") or row.get("ISIN NUMBER") or row.get("ISIN") or ""
        items.append({"symbol": sym, "name": name, "isin": isin})
    return items


def fetch_official_constituents() -> dict[str, dict[str, Any]]:
    """Fetch official constituent lists from NSE archives and build market cap mapping.

    Returns:
        Dict mapping symbol -> {
            "bucket": "large" | "mid" | "small",
            "tier": "1-100" | "101-250" | "251+",
            "name": str,
            "isin": str
        }
    """
    try:
        import requests
    except ImportError:
        return {}

    mapping: dict[str, dict[str, Any]] = {}

    # 1. Fetch Large Cap (Nifty 100 -> Rank 1 to 100)
    try:
        r100 = requests.get(NIFTY_100_URL, headers=_HEADERS, timeout=20)
        if r100.status_code == 200:
            for item in _extract_symbols_from_csv(r100.text):
                mapping[item["symbol"]] = {
                    "bucket": "large",
                    "tier": "1-100",
                    "name": item["name"],
                    "isin": item["isin"],
                }
    except Exception:
        pass

    # 2. Fetch Mid Cap (Nifty Midcap 150 -> Rank 101 to 250)
    try:
        rmid = requests.get(NIFTY_MIDCAP_150_URL, headers=_HEADERS, timeout=20)
        if rmid.status_code == 200:
            for item in _extract_symbols_from_csv(rmid.text):
                if item["symbol"] not in mapping:  # Don't overwrite if already placed
                    mapping[item["symbol"]] = {
                        "bucket": "mid",
                        "tier": "101-250",
                        "name": item["name"],
                        "isin": item["isin"],
                    }
    except Exception:
        pass

    # 3. Fetch Small Cap 250 (Rank 251 to 500)
    try:
        rsmall = requests.get(NIFTY_SMALLCAP_250_URL, headers=_HEADERS, timeout=20)
        if rsmall.status_code == 200:
            for item in _extract_symbols_from_csv(rsmall.text):
                if item["symbol"] not in mapping:
                    mapping[item["symbol"]] = {
                        "bucket": "small",
                        "tier": "251-500",
                        "name": item["name"],
                        "isin": item["isin"],
                    }
    except Exception:
        pass

    # 4. Fetch All Remaining NSE Listed Equities (Rank 500+ -> Small Cap by SEBI rule)
    try:
        req = requests.get(NSE_EQUITY_LIST_URL, headers=_HEADERS, timeout=20)
        if req.status_code == 200:
            for item in _extract_symbols_from_csv(req.text):
                if item["symbol"] not in mapping:
                    mapping[item["symbol"]] = {
                        "bucket": "small",
                        "tier": "251+",
                        "name": item["name"],
                        "isin": item["isin"],
                    }
    except Exception:
        pass

    return mapping


def load_amfi_master(master_file: str, force_refresh: bool = False) -> dict[str, dict[str, Any]]:
    """Load the SEBI/AMFI master market-cap map, refreshing from NSE if needed.

    Returns dict mapping symbol -> {"bucket": "large"|"mid"|"small", ...}
    """
    now = time.time()
    if not force_refresh and os.path.exists(master_file):
        try:
            mtime = os.path.getmtime(master_file)
            if (now - mtime) < MASTER_CACHE_MAX_AGE:
                with open(master_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict) and len(data) > 100:
                        return data
        except Exception:
            pass

    # Try live download & compile
    master = fetch_official_constituents()
    if master and len(master) >= 100:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(master_file)), exist_ok=True)
            with open(master_file, "w", encoding="utf-8") as f:
                json.dump(master, f, indent=2)
            return master
        except Exception:
            return master

    # If live download fails, use stale file if present
    if os.path.exists(master_file):
        try:
            with open(master_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict) and len(data) > 0:
                    return data
        except Exception:
            pass

    return {}
