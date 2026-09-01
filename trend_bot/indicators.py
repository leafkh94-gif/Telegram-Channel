"""
indicators.py — EMA و ATR
الاستراتيجية الاتجاهية لا تستعمل غيرهما؛ أُزيلت VWAP و RSI مع استبدالها.
"""

import logging
from datetime import datetime, timezone
from config import ATR_PERIOD

logger = logging.getLogger(__name__)


# ─── EMA ──────────────────────────────────────────────────────────────────────
def calculate_ema(values: list[float], period: int) -> list[float]:
    if len(values) < period:
        return [None] * len(values)
    k = 2 / (period + 1)
    ema = [sum(values[:period]) / period]
    for price in values[period:]:
        ema.append(price * k + ema[-1] * (1 - k))
    return [None] * (period - 1) + ema


# ─── ATR ──────────────────────────────────────────────────────────────────────
def calculate_atr(candles: list[dict], period: int = ATR_PERIOD) -> float:
    if len(candles) < period + 1:
        return 0.0

    true_ranges = []
    for i in range(1, len(candles)):
        high  = candles[i]["high"]
        low   = candles[i]["low"]
        prev_close = candles[i - 1]["close"]
        tr = max(
            high - low,
            abs(high - prev_close),
            abs(low - prev_close)
        )
        true_ranges.append(tr)

    return sum(true_ranges[-period:]) / period


