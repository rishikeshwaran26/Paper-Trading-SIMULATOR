"""The NSE stock universe — the full list of tradable symbols a whole-exchange
scan (band study, backtest) needs to cover.

Why this is separate from symbols.py
-------------------------------------
symbols.py is a *curated* ~165-name list for search/autocomplete: hand-picked,
bundled, always available offline. A whole-exchange scan needs the OPPOSITE —
the whole exchange (~1,900 equities), because a big intraday mover is often a
small/mid-cap you'd never think to type. So this module goes and gets the
real, complete list.

Where the list comes from
-------------------------
NSE publishes the official equity master as a plain CSV:

    https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv

Unlike NSE's quote API (aggressively IP-blocked, see engine/prices.py), this is a
static file on an archive host and usually downloads fine — especially from an
ordinary Indian residential connection, which is where you'll run the scan.

We cache it to disk so every scan doesn't re-download it, refresh weekly, and if
the download ever fails we fall back to the bundled symbols.py names so a scan
still runs (just over fewer stocks) instead of breaking.
"""

from __future__ import annotations

from datetime import datetime, date
import csv
import io
import os
import time

from . import symbols as symbol_directory

NSE_EQUITY_LIST_URL = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"

# A week is plenty: the equity list changes only when stocks list/delist, which
# is a handful of names per week at most.
CACHE_MAX_AGE_SECONDS = 7 * 24 * 3600

# Browser-ish headers. The archive host is lenient, but a real User-Agent avoids
# the occasional bot filter.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
    ),
    "Accept": "text/csv,application/csv,*/*",
}


def parse_listing_date(raw: str) -> str | None:
    """Normalize a listing date string from NSE or elsewhere to ISO YYYY-MM-DD."""
    if not raw or not raw.strip():
        return None
    s = raw.strip()
    for fmt in ("%d-%b-%Y", "%d-%B-%Y", "%d-%m-%Y", "%Y-%m-%d", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _bundled_fallback() -> list[dict]:
    """The curated symbols.py list, shaped like the CSV rows. Used when the live
    download fails so a scan still has *something* to cover."""
    return [
        {"symbol": sym, "name": name, "series": "EQ", "listing_date": ""}
        for sym, name, _sector in symbol_directory.SYMBOLS
    ]


def _parse_csv(text: str) -> list[dict]:
    """Parse EQUITY_L.csv into [{symbol, name, series, listing_date}].

    Columns (as published): SYMBOL, NAME OF COMPANY, SERIES, DATE OF LISTING,
    PAID UP VALUE, MARKET LOT, ISIN NUMBER, FACE VALUE.
    """
    rows: list[dict] = []
    reader = csv.DictReader(io.StringIO(text))
    for raw in reader:
        # Keys can carry stray whitespace in NSE's CSV — normalize them.
        row = {(k or "").strip(): (v or "").strip() for k, v in raw.items()}
        sym = row.get("SYMBOL", "").upper()
        if not sym:
            continue
        series = row.get("SERIES", "").upper() or "EQ"
        listing_date = parse_listing_date(row.get("DATE OF LISTING", "")) or ""
        rows.append({
            "symbol": sym,
            "name": row.get("NAME OF COMPANY", sym),
            "series": series,
            "listing_date": listing_date,
        })
    return rows


def _download() -> list[dict]:
    """Fetch and parse the live equity list. Returns [] on any failure so the
    caller can fall back — scanning the curated list beats a crash."""
    try:
        import requests  # yfinance already depends on it; lazy import keeps startup light
    except ImportError:
        return []
    try:
        resp = requests.get(NSE_EQUITY_LIST_URL, headers=_HEADERS, timeout=20)
        resp.raise_for_status()
    except Exception:
        return []
    rows = _parse_csv(resp.text)
    # Sanity check: the real file has ~1,900 rows. If we got a handful, we
    # probably hit an error page masquerading as 200 — treat it as a failure.
    return rows if len(rows) > 100 else []


def _write_cache(cache_path: str, rows: list[dict]) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(cache_path)), exist_ok=True)
    with open(cache_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["symbol", "name", "series", "listing_date"], extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _read_cache(cache_path: str) -> list[dict]:
    with open(cache_path, "r", newline="", encoding="utf-8") as f:
        rows = []
        for r in csv.DictReader(f):
            d = dict(r)
            d.setdefault("listing_date", "")
            rows.append(d)
        return rows


def _has_listing_dates(rows: list[dict]) -> bool:
    return any(bool(r.get("listing_date")) for r in rows)


def load_universe(cache_path: str, force_refresh: bool = False) -> tuple[list[dict], str]:
    """Return (rows, source) where rows is [{symbol, name, series, listing_date}] and source is
    one of "cache" | "download" | "bundled" so the caller can tell the user how
    complete the scan universe is.

    Order of preference:
      1. Fresh cache on disk (fast, no network).
      2. Live download from NSE (and cache it for next time).
      3. Stale cache, if a download wasn't possible.
      4. The bundled curated list, as a last resort.
    """
    fresh_cache = (
        not force_refresh
        and os.path.exists(cache_path)
        and (time.time() - os.path.getmtime(cache_path)) < CACHE_MAX_AGE_SECONDS
    )
    if fresh_cache:
        try:
            return _read_cache(cache_path), "cache"
        except Exception:
            pass  # corrupt cache -> fall through to re-download

    rows = _download()
    if rows:
        try:
            _write_cache(cache_path, rows)
        except Exception:
            pass  # a failed cache write shouldn't fail the scan
        return rows, "download"

    # Download failed — use a stale cache if we have one before giving up.
    if os.path.exists(cache_path):
        try:
            return _read_cache(cache_path), "cache"
        except Exception:
            pass

    return _bundled_fallback(), "bundled"


def symbols_only(cache_path: str, force_refresh: bool = False) -> list[str]:
    """Just the ticker list, for callers that don't need names."""
    rows, _source = load_universe(cache_path, force_refresh)
    return [r["symbol"] for r in rows]


def listing_dates_for(symbols: list[str] | set[str], cache_path: str) -> dict[str, str]:
    """Return {symbol: "YYYY-MM-DD"} for requested symbols from the cached universe."""
    wanted = {s.strip().upper() for s in symbols if s}
    if not wanted:
        return {}
    rows, _ = load_universe(cache_path)
    out: dict[str, str] = {}
    for r in rows:
        sym = r.get("symbol", "").upper()
        if sym in wanted:
            ld = (r.get("listing_date") or "").strip()
            if ld:
                out[sym] = ld
    return out

