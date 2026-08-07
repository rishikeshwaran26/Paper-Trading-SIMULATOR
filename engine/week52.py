"""Where a touch sat against its own 52-week range, as of the day it happened.

A naive "52-week high as of today" would be wrong for a backtest, where the
only honest question is what the 52-week high was on the touch date itself,
using nothing that had not happened yet.

Rebuilt from the bhavcopy files already on disk, so no re-scan and no network:
the whole exchange's daily high/low is cached there anyway, which is exactly
what a 52-week extreme is made of.
"""

from __future__ import annotations

import csv
import json
import os
from datetime import date, timedelta
from typing import Iterable, Optional, Sequence

# Within this much of the extreme counts as "near" it.
NEAR_EDGE = 0.03

# A 52-week level built from a handful of bars is not a 52-week level. Below
# this many prior trading days the touch is reported as unknown rather than
# given a figure the data cannot support — early dates in a short cache would
# otherwise every one of them look like a "new 52-week high".
MIN_BARS = 60

# Closed at (within a whisker of) the extreme it touched — the main app's own
# circuit-lock tolerance, reused so "frozen" means one thing across the app.
FROZEN_TOLERANCE = 0.15

WINDOW_DAYS = 365


def _read_day(path: str) -> Iterable[tuple[str, float, float]]:
    """(symbol, high, low) for one cached bhavcopy day, EQ series only —
    matching what the band study itself scanned."""
    try:
        with open(path, newline="", encoding="utf-8") as fh:
            for raw in csv.DictReader(fh):
                row = {(k or "").strip(): (v or "").strip() for k, v in raw.items()}
                if row.get("SERIES", "").upper() != "EQ":
                    continue
                try:
                    yield row["SYMBOL"].upper(), float(row["HIGH_PRICE"]), float(row["LOW_PRICE"])
                except (KeyError, ValueError):
                    continue
    except OSError:
        return


def _load_series(cache_dir: str, symbols: set[str], start: date, end: date) -> dict[str, list[tuple[str, float, float]]]:
    """Per-symbol [(date, high, low)] over [start, end], oldest first.

    Restricted to the symbols actually asked about: a run touches a few hundred
    names, the exchange lists a few thousand, and carrying the rest would be
    most of the work for none of the answer.
    """
    series: dict[str, list[tuple[str, float, float]]] = {s: [] for s in symbols}
    d = start
    while d <= end:
        iso = d.isoformat()
        path = os.path.join(cache_dir, f"{iso}.csv")
        if os.path.exists(path):
            for sym, high, low in _read_day(path):
                if sym in series:
                    series[sym].append((iso, high, low))
        d += timedelta(days=1)
    return series


def classify(
    touches: Sequence[dict],
    cache_dir: str,
    cache_file: Optional[str] = None,
) -> dict[str, dict]:
    """Label every touch by where it sat in its own 52-week range.

    `touches` need {symbol, date, high, low, touch_pct, close_pct}. Returns
    {"SYMBOL|DATE": {bucket, w52_high, w52_low, bars}} where bucket is one of:

        new_high / froze_high / near_high
        new_low  / froze_low  / near_low
        none      — inside its range on both sides
        unknown   — not enough prior history to judge

    "new" and "froze" are exclusive: a touch that made a new high AND closed
    locked there is froze_high only, so the two can be counted separately.
    """
    if not touches:
        return {}

    cached: dict[str, dict] = {}
    if cache_file and os.path.exists(cache_file):
        try:
            with open(cache_file, encoding="utf-8") as fh:
                cached = json.load(fh)
        except (OSError, ValueError):
            cached = {}

    todo = [t for t in touches if f"{t['symbol']}|{t['date']}" not in cached]
    out = {k: v for k, v in cached.items()
           if k in {f"{t['symbol']}|{t['date']}" for t in touches}}

    if todo:
        symbols = {t["symbol"].upper() for t in todo}
        dates = [date.fromisoformat(t["date"]) for t in todo]
        series = _load_series(cache_dir, symbols,
                              min(dates) - timedelta(days=WINDOW_DAYS), max(dates))

        for t in todo:
            key = f"{t['symbol']}|{t['date']}"
            bars = series.get(t["symbol"].upper(), [])
            on = t["date"]
            floor = (date.fromisoformat(on) - timedelta(days=WINDOW_DAYS)).isoformat()
            # Strictly BEFORE the touch day: including it would make every new
            # high equal its own 52-week high by construction.
            window = [b for b in bars if floor <= b[0] < on]
            if len(window) < MIN_BARS:
                out[key] = {"bucket": "unknown", "w52_high": None, "w52_low": None, "bars": len(window)}
                continue
            hi = max(b[1] for b in window)
            lo = min(b[2] for b in window)
            frozen = (t.get("touch_pct") is not None and t.get("close_pct") is not None
                      and abs(t["touch_pct"] - t["close_pct"]) <= FROZEN_TOLERANCE)

            bucket = "none"
            if t.get("high") is not None and t["high"] >= hi:
                bucket = "froze_high" if frozen else "new_high"
            elif t.get("low") is not None and t["low"] <= lo:
                bucket = "froze_low" if frozen else "new_low"
            elif t.get("high") is not None and t["high"] >= hi * (1 - NEAR_EDGE):
                bucket = "near_high"
            elif t.get("low") is not None and t["low"] <= lo * (1 + NEAR_EDGE):
                bucket = "near_low"

            out[key] = {"bucket": bucket, "w52_high": round(hi, 2),
                        "w52_low": round(lo, 2), "bars": len(window)}

        if cache_file:
            merged = {**cached, **out}
            try:
                os.makedirs(os.path.dirname(cache_file), exist_ok=True)
                with open(cache_file, "w", encoding="utf-8") as fh:
                    json.dump(merged, fh)
            except OSError:
                pass  # a cache that cannot be written is slow, not broken

    return out
