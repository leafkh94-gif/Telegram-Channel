"""
TrendMomentumDetector — trend-following entry signal for H1 candles.

Fires in the direction of the prevailing trend when momentum is confirmed.
Replaces the counter-trend LiquiditySweepDetector.

BUY signal conditions (all must pass):
  - H1 close > EMA-20  (price above short-term average — trend is up)
  - RSI(14) > 52 AND rising  (momentum is bullish)
  - MACD histogram > 0 AND rising  (trend acceleration)
  - ATR below 80th percentile  (no volatility spike — clean entry)

SELL signal: mirror of the above.

Returns "buy", "sell", or None.
"""
from __future__ import annotations

import math
import logging
from typing import Sequence

from strategy.base import Candle
from strategy.indicators import atr, ema, macd, rsi

logger = logging.getLogger(__name__)

_MIN_CANDLES = 55   # warmup for indicators


class TrendMomentumDetector:
    """
    Detects trend-following momentum entry on H1 candles.
    Interface matches LiquiditySweepDetector for drop-in replacement.
    """

    min_candles: int = _MIN_CANDLES

    def detect(self, candles: Sequence[Candle]) -> str | None:
        if len(candles) < _MIN_CANDLES:
            return None

        closes = [c.close for c in candles]

        # ── EMA-20: price must be on the right side ───────────────────────────
        ema20_vals = ema(closes, 20)
        last_ema20 = ema20_vals[-1]
        if math.isnan(last_ema20):
            return None

        last_close = closes[-1]
        price_above = last_close > last_ema20
        price_below = last_close < last_ema20

        # ── RSI: momentum must be confirmed and rising ────────────────────────
        rsi_vals = rsi(closes, 14)
        last_rsi  = rsi_vals[-1]
        prev_rsi  = next((v for v in reversed(rsi_vals[:-1]) if not math.isnan(v)), float("nan"))
        if math.isnan(last_rsi) or math.isnan(prev_rsi):
            return None

        rsi_bullish = last_rsi > 52 and last_rsi > prev_rsi
        rsi_bearish = last_rsi < 48 and last_rsi < prev_rsi

        # ── MACD histogram: direction and acceleration ────────────────────────
        _, _, hist = macd(closes, 12, 26, 9)
        last_hist = next((v for v in reversed(hist)    if not math.isnan(v)), float("nan"))
        prev_hist = next((v for v in reversed(hist[:-1]) if not math.isnan(v)), float("nan"))
        if math.isnan(last_hist) or math.isnan(prev_hist):
            return None

        macd_bullish = last_hist > 0 and last_hist >= prev_hist
        macd_bearish = last_hist < 0 and last_hist <= prev_hist

        # ── ATR: avoid entering during volatility spikes ──────────────────────
        atr_vals = [v for v in atr(candles, 14) if not math.isnan(v)]
        if len(atr_vals) >= 20:
            window = atr_vals[-80:]
            current_atr = atr_vals[-1]
            pct = 100.0 * sum(1 for v in window if v <= current_atr) / len(window)
            if pct >= 80.0:
                logger.debug("trend_momentum: ATR spike (pct=%.0f) — no signal", pct)
                return None

        # ── Signal decision ───────────────────────────────────────────────────
        if price_above and rsi_bullish and macd_bullish:
            logger.info(
                "trend_momentum BUY: close=%.2f > ema20=%.2f, rsi=%.1f↑, macd_hist=%.4f↑",
                last_close, last_ema20, last_rsi, last_hist,
            )
            return "buy"

        if price_below and rsi_bearish and macd_bearish:
            logger.info(
                "trend_momentum SELL: close=%.2f < ema20=%.2f, rsi=%.1f↓, macd_hist=%.4f↓",
                last_close, last_ema20, last_rsi, last_hist,
            )
            return "sell"

        return None
