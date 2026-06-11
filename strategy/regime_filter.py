"""
Classifies the current market regime from H4 candles.

Upgraded from a 3-state EMA-cross + fixed-ATR filter to:

  Volatile gate  : ATR/close > volatile_atr_pct  (original, backward-compatible)
  Trend axis     : ADX > adx_trend_threshold  (replaces EMA-slope heuristic)
  Tie-breaker    : Hurst exponent when ADX is transitional (20-25)
  Direction bias : EMA-50 vs EMA-200 cross (unchanged)

4-state output: VOLATILE | TRENDING_UP | TRENDING_DOWN | RANGING
"""
from __future__ import annotations

import logging
import math
from typing import Sequence

from strategy.base import Candle, MarketRegime
from strategy.indicators import adx, atr, ema, hurst_exponent

logger = logging.getLogger(__name__)


class RegimeFilter:
    def __init__(
        self,
        atr_period: int = 14,
        ema_fast: int = 50,
        ema_slow: int = 200,
        volatile_atr_pct: float = 0.018,        # original param, still primary gate
        adx_period: int = 14,
        adx_trend_threshold: float = 25.0,
        hurst_trend_min: float = 0.55,           # tie-breaker when ADX is 20-25
    ):
        self.atr_period = atr_period
        self.ema_fast = ema_fast
        self.ema_slow = ema_slow
        self.volatile_atr_pct = volatile_atr_pct
        self.adx_period = adx_period
        self.adx_trend_threshold = adx_trend_threshold
        self.hurst_trend_min = hurst_trend_min

    @property
    def min_candles(self) -> int:
        return self.ema_slow + self.atr_period + 1

    def classify(self, candles: Sequence[Candle]) -> MarketRegime:
        if len(candles) < self.min_candles:
            return MarketRegime.RANGING

        closes = [c.close for c in candles]
        atr_vals = atr(candles, self.atr_period)
        last_close = closes[-1]
        last_atr = atr_vals[-1]

        # ── Volatile gate (original behaviour preserved) ──────────────────────
        if not math.isnan(last_atr) and last_close > 0:
            if last_atr / last_close > self.volatile_atr_pct:
                logger.debug(
                    "regime VOLATILE: atr/close=%.4f > threshold=%.4f",
                    last_atr / last_close, self.volatile_atr_pct,
                )
                return MarketRegime.VOLATILE

        # ── Trend axis: ADX ───────────────────────────────────────────────────
        adx_vals = adx(candles, self.adx_period)
        last_adx = adx_vals[-1]

        ema_fast_vals = ema(closes, self.ema_fast)
        ema_slow_vals = ema(closes, self.ema_slow)
        last_fast = ema_fast_vals[-1]
        last_slow = ema_slow_vals[-1]

        if math.isnan(last_fast) or math.isnan(last_slow):
            return MarketRegime.RANGING

        up_bias = last_fast > last_slow

        # Clear trend
        if not math.isnan(last_adx) and last_adx >= self.adx_trend_threshold:
            regime = MarketRegime.TRENDING_UP if up_bias else MarketRegime.TRENDING_DOWN
            logger.debug("regime %s: adx=%.1f", regime.value, last_adx)
            return regime

        # Transitional zone (ADX 20-25) — use Hurst exponent as tie-breaker
        if not math.isnan(last_adx) and last_adx >= 20.0:
            h = hurst_exponent(closes)
            if not math.isnan(h) and h >= self.hurst_trend_min:
                regime = MarketRegime.TRENDING_UP if up_bias else MarketRegime.TRENDING_DOWN
                logger.debug("regime %s via Hurst=%.2f", regime.value, h)
                return regime

        logger.debug("regime RANGING: adx=%.1f", last_adx if not math.isnan(last_adx) else -1)
        return MarketRegime.RANGING
