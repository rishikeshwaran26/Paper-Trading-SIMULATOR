"""Market-cap classification for NSE symbols.

Fetches market cap from Yahoo Finance for touched symbols only (never the
whole exchange), caches results for 7 days in a shared JSON file, and maps
each symbol to a SEBI-style bucket:

  large  — ₹20,000 cr+ (roughly top-100 listed companies)
  mid    — ₹5,000–20,000 cr (roughly rank 101–250)
  small  — below ₹5,000 cr (rank 251+)
  unknown — Yahoo returned nothing / error

The cache is shared between the main app and the simple scanner so a symbol
looked up by either never triggers a second network hit within 7 days.
"""

from __future__ import annotations

import json
import os
import time
from typing import Sequence

import yfinance as yf

_TTL = 7 * 24 * 3600   # seconds
_LARGE_CR = 20_000
_MID_CR = 5_000


def _bucket(cap_cr: float) -> str:
    if cap_cr >= _LARGE_CR:
        return "large"
    if cap_cr >= _MID_CR:
        return "mid"
    return "small"


def cap_buckets_for(symbols: Sequence[str], cache_file: str) -> dict[str, str]:
    """Return {symbol: bucket} for every symbol in *symbols*.

    Results are read from *cache_file* when fresh; misses are fetched from
    Yahoo Finance and written back.  The function always returns an entry for
    every requested symbol — callers never have to handle missing keys.
    """
    now = time.time()
    cache: dict = {}
    if os.path.exists(cache_file):
        try:
            with open(cache_file, encoding="utf-8") as fh:
                cache = json.load(fh)
        except Exception:
            cache = {}

    result: dict[str, str] = {}
    to_fetch: list[str] = []

    for sym in symbols:
        entry = cache.get(sym)
        if entry and now - entry.get("ts", 0) < _TTL:
            result[sym] = entry["bucket"]
        else:
            to_fetch.append(sym)

    for sym in to_fetch:
        bucket = "unknown"
        try:
            info = yf.Ticker(f"{sym}.NS").fast_info
            cap = getattr(info, "market_cap", None) or getattr(info, "marketCap", None)
            if cap and cap > 0:
                bucket = _bucket(cap / 1e7)
        except Exception:
            pass
        result[sym] = bucket
        cache[sym] = {"bucket": bucket, "ts": now}

    if to_fetch:
        try:
            os.makedirs(os.path.dirname(cache_file) or ".", exist_ok=True)
            with open(cache_file, "w", encoding="utf-8") as fh:
                json.dump(cache, fh)
        except Exception:
            pass

    return result
