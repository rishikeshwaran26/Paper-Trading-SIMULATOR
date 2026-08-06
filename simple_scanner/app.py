"""Simple Scanner — a standalone, single-purpose sibling to the main
Papertrading app.

Deliberately NOT part of that app: no login, no user accounts, no database, no
build step. One page, one question: "between these two dates, how many stocks
touched this % band (or threshold) on the way up, and how many on the way
down?" Nothing else. If you want the full band-touch study (before/after
tracking, filters, bar charts, EV) that lives in the main app's Backtest page
— this exists so a quick count doesn't require any of that.

Run with:  python simple_scanner/app.py
Then open: http://localhost:5050
"""

from __future__ import annotations

import os
import sys
from datetime import date, timedelta

from flask import Flask, jsonify, request, send_from_directory

# Reuse the main app's bhavcopy fetcher and symbol->name universe — the two
# pieces of real logic here (NSE's daily file, and the equity master for
# company names), cached to disk, no reason to fork either just to avoid an
# import.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from engine.bhavcopy import fetch_day  # noqa: E402
from engine.market_cap import cap_buckets_for  # noqa: E402
from engine.universe import load_universe  # noqa: E402

app = Flask(__name__, static_folder="static", static_url_path="")

# Point at the MAIN app's caches (../data/...) rather than private ones:
# bhavcopy files are immutable once published, and the equity list changes at
# most a handful of names a week, so sharing means a date/list already fetched
# by either app never needs fetching again by the other.
_ROOT = os.path.dirname(os.path.abspath(__file__))
_DATA_DIR = os.path.join(os.path.dirname(_ROOT), "data")
CACHE_DIR = os.path.join(_DATA_DIR, "bhavcopy_cache")
UNIVERSE_CACHE = os.path.join(_DATA_DIR, "nse_universe.csv")
os.makedirs(CACHE_DIR, exist_ok=True)

MAX_RANGE_DAYS = 366  # a generous cap so a fat-fingered 10-year range can't hang the server

# symbol -> company name, loaded once per process and reused across scans. The
# list barely changes day to day, so re-reading it on every request would be
# wasted I/O for no real freshness gain.
_NAME_CACHE: dict[str, str] | None = None


def symbol_names() -> dict[str, str]:
    global _NAME_CACHE
    if _NAME_CACHE is None:
        rows, _source = load_universe(UNIVERSE_CACHE)
        _NAME_CACHE = {r["symbol"].strip().upper(): r["name"] for r in rows}
    return _NAME_CACHE


# Shared market cap cache — lives in the main app's data/ so a symbol looked
# up by either app never triggers a second Yahoo fetch within 7 days.
CAP_CACHE_FILE = os.path.join(_DATA_DIR, "market_cap_cache.json")


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.post("/scan")
def scan():
    body = request.get_json(silent=True) or {}

    try:
        start = date.fromisoformat(str(body.get("start_date", "")))
        end = date.fromisoformat(str(body.get("end_date", "")))
    except ValueError:
        return jsonify(error="start_date and end_date must be ISO dates, e.g. 2026-01-01"), 400
    if start > end:
        return jsonify(error="start_date must not be after end_date"), 400
    if (end - start).days > MAX_RANGE_DAYS:
        return jsonify(error=f"range too wide — max {MAX_RANGE_DAYS} days"), 400

    mode = body.get("mode", "band")
    try:
        if mode == "band":
            band_low = float(body.get("band_low", 19))
            band_high = float(body.get("band_high", 20))
        elif mode == "single":
            # "no range at all" — one threshold, no upper cap. Modeled as a band
            # whose top is effectively infinite, so the same comparison below
            # (band_low <= pct <= band_high) handles both modes identically.
            band_low = float(body.get("threshold", 20))
            band_high = float("inf")
        else:
            return jsonify(error="mode must be 'band' or 'single'"), 400
    except (TypeError, ValueError):
        return jsonify(error="band/threshold values must be numbers"), 400
    if band_low < 0 or band_low > band_high:
        return jsonify(error="band_low must be between 0 and band_high"), 400

    names = symbol_names()
    up_hits = []
    down_hits = []
    trading_days = 0
    missing_days = []

    d = start
    while d <= end:
        rows = fetch_day(CACHE_DIR, d)
        if rows is None:
            # Either a real non-trading day (weekend/holiday) or a fetch
            # failure — fetch_day doesn't distinguish, so we can't either.
            # Weekends are the overwhelming majority of these and are
            # expected, so this is reported as a count, not an error list.
            missing_days.append(d.isoformat())
            d += timedelta(days=1)
            continue
        trading_days += 1
        for row in rows:
            try:
                sym = row["SYMBOL"].strip().upper()
                high = float(row["HIGH_PRICE"])
                low = float(row["LOW_PRICE"])
                close = float(row["CLOSE_PRICE"])
                prev_close = float(row["PREV_CLOSE"])
            except (KeyError, ValueError):
                continue
            if prev_close <= 0:
                continue
            hit = {"symbol": sym, "name": names.get(sym, ""), "date": d.isoformat()}
            # Rounded BEFORE the band test, not after, and the rounded value is
            # what gets compared and reported. A stock locked at the +20%
            # circuit is mathematically exactly 20%, but in binary floating
            # point (732.60 - 610.50) / 610.50 * 100 lands on 20.000000000000004
            # — a hair outside a 19–20 band, so it vanished from the scan while
            # a neighbour at the same circuit stayed. That silently dropped 43%
            # of every exact-20% touch, which is most of what this tool is for.
            # The main app already compares the rounded value; this matches it.
            close_pct = round((close - prev_close) / prev_close * 100, 2)

            up_pct = round((high - prev_close) / prev_close * 100, 2)
            if band_low <= up_pct <= band_high:
                # Closed at (within a whisker of) the day's high it touched:
                # almost always means it FROZE there — locked at the upper
                # circuit for the rest of the session, only buyers left, no
                # sellers to trade into. |up_pct - close_pct| <= 0.15 is the
                # same tolerance the main app uses for this.
                frozen = abs(up_pct - close_pct) <= 0.15
                up_hits.append({**hit, "pct": up_pct, "close_pct": close_pct, "frozen": frozen})

            down_pct = round((low - prev_close) / prev_close * 100, 2)
            if -band_high <= down_pct <= -band_low:
                # Same idea on the way down — closed at its low, frozen at the
                # lower circuit.
                frozen = abs(down_pct - close_pct) <= 0.15
                down_hits.append({**hit, "pct": down_pct, "close_pct": close_pct, "frozen": frozen})
        d += timedelta(days=1)

    # Biggest move first within each list — the touches most worth a second
    # look surface without any client-side sorting.
    up_hits.sort(key=lambda h: h["pct"], reverse=True)
    down_hits.sort(key=lambda h: h["pct"])

    # Only look up caps for symbols that actually touched — never the whole
    # exchange — so this stays fast regardless of the scan's date range.
    unique_symbols = sorted({h["symbol"] for h in up_hits} | {h["symbol"] for h in down_hits})
    caps = cap_buckets_for(unique_symbols, CAP_CACHE_FILE)
    for h in up_hits:
        h["cap"] = caps.get(h["symbol"], "unknown")
    for h in down_hits:
        h["cap"] = caps.get(h["symbol"], "unknown")

    return jsonify(
        up_count=len(up_hits),
        down_count=len(down_hits),
        up_hits=up_hits,
        down_hits=down_hits,
        trading_days=trading_days,
        missing_days=len(missing_days),
    )


if __name__ == "__main__":
    app.run(port=5050, debug=True)
