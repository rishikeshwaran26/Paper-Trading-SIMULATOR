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

import csv
import os
import re
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

# A standing copy of whichever bhavcopy file(s) the LAST scan actually used —
# separate from CACHE_DIR (which every scan ever run keeps piling into) so
# there's always exactly one place that shows "what this scan just touched",
# cleared and refilled on every scan rather than accumulating forever.
EXPORT_DIR = os.path.join(_ROOT, "scan_exports")
os.makedirs(EXPORT_DIR, exist_ok=True)
_EXPORT_NAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.csv$")

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


def _write_annotated_csv(src: str, dest: str) -> None:
    """Copies one bhavcopy day into `dest`, adding UP_PCT and DOWN_PCT columns
    — the same move the scan itself computes (high/low vs previous close,
    rounded before comparison, same as the band test above), worked out for
    every row, not just the ones that matched a band. So opening the exported
    file shows the number you came here for already sitting in it, instead of
    the raw exchange file you'd have to do the maths on yourself.

    Sorted by UP_PCT, biggest first — a raw bhavcopy is alphabetical by
    symbol, which buries the whole point of opening this file. Rows with no
    computable % (a bad PREV_CLOSE) sink to the bottom rather than breaking
    the sort or landing at the top by accident.
    """
    with open(src, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        return
    fieldnames = list(rows[0].keys()) + ["UP_PCT", "DOWN_PCT"]
    for row in rows:
        try:
            prev_close = float(row["PREV_CLOSE"])
            high = float(row["HIGH_PRICE"])
            low = float(row["LOW_PRICE"])
        except (KeyError, ValueError):
            prev_close = 0
        if prev_close > 0:
            row["UP_PCT"] = round((high - prev_close) / prev_close * 100, 2)
            row["DOWN_PCT"] = round((low - prev_close) / prev_close * 100, 2)
        else:
            row["UP_PCT"] = ""
            row["DOWN_PCT"] = ""
    rows.sort(key=lambda r: r["UP_PCT"] if r["UP_PCT"] != "" else float("-inf"), reverse=True)
    with open(dest, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


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

    before_days = int(body.get("before_days", 0))
    follow_up_days = int(body.get("follow_up_days", 0))

    margin_before = before_days * 3 + 10 if before_days > 0 else 0
    margin_after = follow_up_days * 3 + 10 if follow_up_days > 0 else 0
    ext_start = start - timedelta(days=margin_before)
    ext_end = end + timedelta(days=margin_after)

    names = symbol_names()
    up_hits = []
    down_hits = []
    missing_days = []
    all_trading_days = []
    day_cache = {}

    curr = ext_start
    while curr <= ext_end:
        rows = fetch_day(CACHE_DIR, curr)
        if rows is None:
            if start <= curr <= end:
                missing_days.append(curr.isoformat())
            curr += timedelta(days=1)
            continue
        
        date_str = curr.isoformat()
        all_trading_days.append(date_str)
        day_cache[date_str] = {}
        for row in rows:
            try:
                sym = row["SYMBOL"].strip().upper()
                day_cache[date_str][sym] = {
                    "close": float(row["CLOSE_PRICE"]),
                    "prev_close": float(row["PREV_CLOSE"]),
                    "high": float(row["HIGH_PRICE"]),
                    "low": float(row["LOW_PRICE"]),
                }
            except (KeyError, ValueError):
                continue
        curr += timedelta(days=1)

    scan_trading_days = [d for d in all_trading_days if start.isoformat() <= d <= end.isoformat()]
    trading_days = len(scan_trading_days)
    traded_dates = scan_trading_days

    for date_str in scan_trading_days:
        symbols_data = day_cache[date_str]
        for sym, row in symbols_data.items():
            high = row["high"]
            low = row["low"]
            close = row["close"]
            prev_close = row["prev_close"]
            if prev_close <= 0:
                continue

            hit = {"symbol": sym, "name": names.get(sym, ""), "date": date_str}
            close_pct = round((close - prev_close) / prev_close * 100, 2)

            up_pct = round((high - prev_close) / prev_close * 100, 2)
            if band_low <= up_pct <= band_high:
                frozen = abs(up_pct - close_pct) <= 0.15
                
                before_perf = []
                follow_up_perf = []
                idx = all_trading_days.index(date_str)
                
                if before_days > 0:
                    start_idx = max(0, idx - before_days)
                    for b_date in all_trading_days[start_idx:idx]:
                        b_row = day_cache[b_date].get(sym)
                        if b_row and b_row["prev_close"] > 0:
                            b_pct = round((b_row["close"] - b_row["prev_close"]) / b_row["prev_close"] * 100, 2)
                            before_perf.append({"date": b_date, "pct": b_pct})
                        else:
                            before_perf.append({"date": b_date, "pct": None})
                
                if follow_up_days > 0:
                    end_idx = min(len(all_trading_days), idx + 1 + follow_up_days)
                    for f_date in all_trading_days[idx + 1:end_idx]:
                        f_row = day_cache[f_date].get(sym)
                        if f_row and f_row["prev_close"] > 0:
                            f_pct = round((f_row["close"] - f_row["prev_close"]) / f_row["prev_close"] * 100, 2)
                            follow_up_perf.append({"date": f_date, "pct": f_pct})
                        else:
                            follow_up_perf.append({"date": f_date, "pct": None})

                up_hits.append({
                    **hit,
                    "pct": up_pct,
                    "close_pct": close_pct,
                    "frozen": frozen,
                    "before_perf": before_perf,
                    "follow_up_perf": follow_up_perf
                })

            down_pct = round((low - prev_close) / prev_close * 100, 2)
            if -band_high <= down_pct <= -band_low:
                frozen = abs(down_pct - close_pct) <= 0.15
                
                before_perf = []
                follow_up_perf = []
                idx = all_trading_days.index(date_str)
                
                if before_days > 0:
                    start_idx = max(0, idx - before_days)
                    for b_date in all_trading_days[start_idx:idx]:
                        b_row = day_cache[b_date].get(sym)
                        if b_row and b_row["prev_close"] > 0:
                            b_pct = round((b_row["close"] - b_row["prev_close"]) / b_row["prev_close"] * 100, 2)
                            before_perf.append({"date": b_date, "pct": b_pct})
                        else:
                            before_perf.append({"date": b_date, "pct": None})
                
                if follow_up_days > 0:
                    end_idx = min(len(all_trading_days), idx + 1 + follow_up_days)
                    for f_date in all_trading_days[idx + 1:end_idx]:
                        f_row = day_cache[f_date].get(sym)
                        if f_row and f_row["prev_close"] > 0:
                            f_pct = round((f_row["close"] - f_row["prev_close"]) / f_row["prev_close"] * 100, 2)
                            follow_up_perf.append({"date": f_date, "pct": f_pct})
                        else:
                            follow_up_perf.append({"date": f_date, "pct": None})

                down_hits.append({
                    **hit,
                    "pct": down_pct,
                    "close_pct": close_pct,
                    "frozen": frozen,
                    "before_perf": before_perf,
                    "follow_up_perf": follow_up_perf
                })

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

    # Wipe last scan's copies and drop in this scan's, in date order — the
    # folder always mirrors exactly the scan that just ran, never a history of
    # every scan ever made. Copied rather than moved/symlinked: CACHE_DIR is
    # shared with the main app, and this folder is meant to survive even if
    # something there later gets cleaned up.
    for old in os.listdir(EXPORT_DIR):
        if _EXPORT_NAME_RE.match(old):
            try:
                os.remove(os.path.join(EXPORT_DIR, old))
            except OSError:
                # Windows locks a file while Explorer/another process has it
                # open (e.g. you had it open from the last scan) — that's not
                # a reason to fail the whole scan. Writing the new file below
                # can still overwrite an in-place file it couldn't delete; only a
                # genuinely stale file from a date no longer in this scan
                # would survive as clutter, which the next successful scan
                # will clear.
                pass
    exported_files = []
    for iso in traded_dates:
        src = os.path.join(CACHE_DIR, f"{iso}.csv")
        if os.path.exists(src):
            try:
                _write_annotated_csv(src, os.path.join(EXPORT_DIR, f"{iso}.csv"))
            except OSError:
                continue  # same lock story as above — skip, don't fail the scan
            exported_files.append(f"{iso}.csv")

    return jsonify(
        up_count=len(up_hits),
        down_count=len(down_hits),
        up_hits=up_hits,
        down_hits=down_hits,
        trading_days=trading_days,
        missing_days=len(missing_days),
        exported_files=exported_files,
    )


@app.get("/scan-exports/<fname>")
def scan_export_file(fname: str):
    """Serve one exported CSV inline, so "open" shows it in a new tab rather
    than forcing a download. `fname` is checked against a strict YYYY-MM-DD.csv
    pattern before touching the filesystem — it comes from the URL, and
    send_from_directory only guards path traversal, not an arbitrary name."""
    if not _EXPORT_NAME_RE.match(fname):
        return jsonify(error="not found"), 404
    return send_from_directory(EXPORT_DIR, fname, mimetype="text/csv")


@app.post("/scan-exports/open-folder")
def open_export_folder():
    """Pop the export folder open in Windows Explorer. Only makes sense
    because this app runs on the same machine as the person clicking the
    button — it would be meaningless (and wrong) for a hosted deployment."""
    try:
        os.startfile(EXPORT_DIR)  # noqa: S606 — local-only tool, user's own click
    except (AttributeError, OSError) as e:
        return jsonify(error=f"could not open the folder: {e}"), 500
    return jsonify(ok=True)


if __name__ == "__main__":
    app.run(port=5050, debug=True)
