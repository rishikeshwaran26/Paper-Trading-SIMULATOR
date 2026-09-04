"""Official SEBI / AMFI Market-Cap Classification for NSE symbols.

Under SEBI regulations (SEBI/HO/IMD/DF3/CIR/P/2017/114) and AMFI guidelines,
equities are categorized by full market capitalization ranking:

  large   — 1st to 100th company   (NIFTY 100 / AMFI Large Cap)
  mid     — 101st to 250th company (NIFTY Midcap 150 / AMFI Mid Cap)
  small   — 251st company onwards  (NIFTY Smallcap 250 + all remaining listed equities)
  unknown — Unlisted / ETF / unrecognized symbol with no market data

This module uses the official SEBI/AMFI constituent master dataset for instant O(1)
lookups, falling back to Yahoo Finance only for newly listed IPOs or unmapped symbols.
"""

from __future__ import annotations

import json
import os
import time
from typing import Sequence, Dict, Any, Optional

from .amfi import load_amfi_master

_TTL = 7 * 24 * 3600   # seconds for fallback cache
_LARGE_CR = 20_000     # ₹20,000 cr threshold for yfinance fallback
_MID_CR = 5_000        # ₹5,000 cr threshold for yfinance fallback

# In-memory cached master map
_IN_MEM_MASTER: dict[str, dict[str, Any]] = {}
_LAST_MASTER_PATH: str = ""


def _bucket_from_cap_cr(cap_cr: float) -> str:
    """Fallback classification based on market cap in ₹ Crores."""
    if cap_cr >= _LARGE_CR:
        return "large"
    if cap_cr >= _MID_CR:
        return "mid"
    return "small"


def _get_master(cache_file: str) -> dict[str, dict[str, Any]]:
    """Resolve and load the AMFI master mapping."""
    global _IN_MEM_MASTER, _LAST_MASTER_PATH
    base_dir = os.path.dirname(os.path.abspath(cache_file)) if cache_file else "data"
    master_file = os.path.join(base_dir, "amfi_market_caps.json")
    
    if _IN_MEM_MASTER and _LAST_MASTER_PATH == master_file:
        return _IN_MEM_MASTER

    master = load_amfi_master(master_file)
    if master:
        _IN_MEM_MASTER = master
        _LAST_MASTER_PATH = master_file
    return master or _IN_MEM_MASTER


def cap_buckets_for(symbols: Sequence[str], cache_file: str) -> dict[str, str]:
    """Return {symbol: bucket} for every symbol in *symbols*.

    Classification priority:
      1. Official SEBI / AMFI regulatory constituent list (Instant O(1), no network).
      2. Fresh entry in *cache_file* (7-day TTL).
      3. Yahoo Finance fast_info lookup for new IPOs / unmapped tickers.

    Always returns an entry for every requested symbol.
    """
    now = time.time()
    cache: dict = {}
    if os.path.exists(cache_file):
        try:
            with open(cache_file, encoding="utf-8") as fh:
                cache = json.load(fh)
        except Exception:
            cache = {}

    master = _get_master(cache_file)

    result: dict[str, str] = {}
    to_fetch: list[str] = []

    for sym in symbols:
        sym_clean = sym.strip().upper()
        
        # 1. Primary check: Official AMFI / NSE master dataset
        if sym_clean in master:
            result[sym] = master[sym_clean].get("bucket", "small")
            continue

        # 2. Secondary check: Recent cache file
        entry = cache.get(sym_clean)
        if entry and now - entry.get("ts", 0) < _TTL:
            result[sym] = entry["bucket"]
        else:
            to_fetch.append(sym_clean)

    # 3. Fallback: Network fetch via yfinance for missing/unmapped symbols
    if to_fetch:
        try:
            import yfinance as yf
        except ImportError:
            yf = None

        for sym_clean in to_fetch:
            bucket = "unknown"
            if yf is not None:
                try:
                    info = yf.Ticker(f"{sym_clean}.NS").fast_info
                    cap = getattr(info, "market_cap", None) or getattr(info, "marketCap", None)
                    if cap and cap > 0:
                        bucket = _bucket_from_cap_cr(cap / 1e7)
                except Exception:
                    pass
            result[sym_clean] = bucket
            cache[sym_clean] = {"bucket": bucket, "ts": now}

        try:
            os.makedirs(os.path.dirname(cache_file) or ".", exist_ok=True)
            with open(cache_file, "w", encoding="utf-8") as fh:
                json.dump(cache, fh)
        except Exception:
            pass

    return result


def get_cap_detail(symbol: str, cache_file: str) -> dict[str, Any]:
    """Return detailed SEBI/AMFI categorization info for a symbol."""
    sym_clean = symbol.strip().upper()
    master = _get_master(cache_file)
    if sym_clean in master:
        return master[sym_clean]
    
    buckets = cap_buckets_for([sym_clean], cache_file)
    return {
        "bucket": buckets.get(sym_clean, "unknown"),
        "tier": "unknown",
        "name": sym_clean,
        "isin": ""
    }
