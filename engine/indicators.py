"""Pure technical-indicator functions, unit-tested offline.

Split out of the (now-removed) daily-movers screener because the backtest
engine's spike-RSI needs `rsi()` on its own, with no dependency on anything
screener-shaped.
"""

from __future__ import annotations

from typing import Optional


def rsi(closes: list[float], period: int = 14) -> Optional[float]:
    """Wilder's Relative Strength Index over `closes` (oldest first).

    RSI measures momentum on a 0–100 scale: >70 is conventionally "overbought"
    (stretched after a run-up — a short-seller's cue), <30 "oversold" (beaten
    down — a bounce candidate). Returns None if there isn't enough history.
    """
    if len(closes) < period + 1:
        return None
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(d, 0.0) for d in deltas]
    losses = [max(-d, 0.0) for d in deltas]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    # Wilder smoothing across the remaining deltas.
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - 100 / (1 + rs), 1)
