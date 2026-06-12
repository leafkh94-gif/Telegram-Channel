"""
ConfluenceScorer — 5-condition scoring system for trade quality.

Each condition is worth 1 point. Condition 1 (price confirmation) is
mandatory: if it fails the scorer returns immediately with total=0.
The firing threshold is set by GoldStrategy.min_confluence (default 2 —
condition 1 + at least 1 of conditions 2-5). Because condition 1 is
mandatory, counter-trend setups stay blocked even at the lower threshold.

Conditions:
  1. Price confirmation   — H1 close breaks above swing high (buy) or below swing low (sell)
  2. Indicators aligned   — RSI, MACD, EMA-50 on H1 (need ≥ 2/3)
  3. Multi-TF alignment   — H4 and D1 bias both match direction
  4. Session              — London (07-12 UTC) or New York (13-17 UTC)
  5. Risk:Reward          — R:R ≥ 1.5 using nearest swing TP and ATR-based SL

All conditions catch their own exceptions and mark as failed rather than raise.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Sequence

from strategy.base import Candle, TF_D1, TF_H1, TF_H4
from strategy.indicators import atr, ema, macd, rsi
from strategy.session_filter import is_london_or_ny_session, session_label

logger = logging.getLogger(__name__)

_MIN_CANDLES = 55   # minimum H1 bars needed for indicator warmup


@dataclass
class ConditionResult:
    name: str
    passed: bool
    detail: str


@dataclass
class ConfluenceResult:
    total: int
    max_score: int = 5
    conditions: list[ConditionResult] = field(default_factory=list)

    def summary(self) -> str:
        return f"{self.total}/{self.max_score}"

    def percentage(self) -> int:
        """Confirmation strength as a rounded percentage (0-100)."""
        if self.max_score <= 0:
            return 0
        return round(self.total / self.max_score * 100)


def _safe_last(vals: list[float]) -> float:
    """Return last non-NaN value or NaN."""
    for v in reversed(vals):
        if not math.isnan(v):
            return v
    return float("nan")


def _swing_high(candles: Sequence[Candle], lookback: int) -> float:
    """Highest high in the last `lookback` bars."""
    window = list(candles)[-lookback:]
    return max((c.high for c in window), default=float("nan"))


def _swing_low(candles: Sequence[Candle], lookback: int) -> float:
    """Lowest low in the last `lookback` bars."""
    window = list(candles)[-lookback:]
    return min((c.low for c in window), default=float("nan"))


class ConfluenceScorer:

    def score(self, candles: dict, direction: str) -> ConfluenceResult:
        h1 = candles.get(TF_H1, [])
        h4 = candles.get(TF_H4, [])
        d1 = candles.get(TF_D1, [])
        conditions: list[ConditionResult] = []

        # ── Condition 1: Price confirmation (MANDATORY) ───────────────────────
        c1 = self._check_price_confirmation(h1, direction)
        conditions.append(c1)
        if not c1.passed:
            return ConfluenceResult(total=0, conditions=conditions)

        # ── Conditions 2-5 ────────────────────────────────────────────────────
        conditions.append(self._check_indicators(h1, direction))
        conditions.append(self._check_mtf_alignment(h4, d1, direction))
        conditions.append(self._check_session())
        conditions.append(self._check_risk_reward(h1, direction))

        total = sum(1 for c in conditions if c.passed)
        return ConfluenceResult(total=total, conditions=conditions)

    # ── Individual condition checks ───────────────────────────────────────────

    def _check_price_confirmation(self, h1: list[Candle], direction: str) -> ConditionResult:
        name = "Price Confirmation"
        try:
            if len(h1) < 10:
                return ConditionResult(name, False, "Not enough H1 candles")
            last_close = h1[-1].close
            if direction == "buy":
                level = _swing_high(h1[:-1], lookback=10)
                if math.isnan(level):
                    return ConditionResult(name, False, "No swing high found")
                passed = last_close > level
                detail = (f"Closed above {level:,.2f}" if passed
                          else f"No breakout — close {last_close:,.2f} < high {level:,.2f}")
            else:
                level = _swing_low(h1[:-1], lookback=10)
                if math.isnan(level):
                    return ConditionResult(name, False, "No swing low found")
                passed = last_close < level
                detail = (f"Closed below {level:,.2f}" if passed
                          else f"No breakdown — close {last_close:,.2f} > low {level:,.2f}")
            return ConditionResult(name, passed, detail)
        except Exception as exc:
            logger.debug("confluence c1 error: %s", exc)
            return ConditionResult(name, False, "error checking condition")

    def _check_indicators(self, h1: list[Candle], direction: str) -> ConditionResult:
        name = "Indicators"
        try:
            if len(h1) < _MIN_CANDLES:
                return ConditionResult(name, False, f"Need {_MIN_CANDLES} H1 bars, have {len(h1)}")
            closes = [c.close for c in h1]
            parts: list[str] = []
            passing = 0

            # a. RSI
            rsi_vals = rsi(closes, 14)
            last_rsi  = _safe_last(rsi_vals)
            prev_rsi  = _safe_last(rsi_vals[:-1])
            if not (math.isnan(last_rsi) or math.isnan(prev_rsi)):
                rising = last_rsi > prev_rsi
                if direction == "buy" and last_rsi > 50 and rising:
                    parts.append(f"RSI {last_rsi:.0f}↑")
                    passing += 1
                elif direction == "sell" and last_rsi < 50 and not rising:
                    parts.append(f"RSI {last_rsi:.0f}↓")
                    passing += 1
                else:
                    arrow = "↑" if rising else "↓"
                    parts.append(f"RSI {last_rsi:.0f}{arrow} (miss)")

            # b. MACD histogram
            _, _, hist = macd(closes, 12, 26, 9)
            last_h = _safe_last(hist)
            prev_h = _safe_last(hist[:-1])
            if not (math.isnan(last_h) or math.isnan(prev_h)):
                if direction == "buy" and last_h > 0 and last_h >= prev_h:
                    parts.append("MACD bullish")
                    passing += 1
                elif direction == "sell" and last_h < 0 and last_h <= prev_h:
                    parts.append("MACD bearish")
                    passing += 1
                else:
                    parts.append("MACD (miss)")

            # c. Price vs EMA-50
            ema50 = ema(closes, 50)
            last_e50 = _safe_last(ema50)
            last_close = closes[-1]
            if not math.isnan(last_e50):
                if direction == "buy" and last_close > last_e50:
                    parts.append("above EMA50")
                    passing += 1
                elif direction == "sell" and last_close < last_e50:
                    parts.append("below EMA50")
                    passing += 1
                else:
                    parts.append("EMA50 (miss)")

            passed = passing >= 2
            detail = f"{', '.join(parts)} ({passing}/3)" if parts else "no indicator data"
            return ConditionResult(name, passed, detail)
        except Exception as exc:
            logger.debug("confluence c2 error: %s", exc)
            return ConditionResult(name, False, "error checking condition")

    def _check_mtf_alignment(self, h4: list[Candle], d1: list[Candle],
                              direction: str) -> ConditionResult:
        name = "Multi-TF Alignment"
        try:
            parts: list[str] = []
            h4_ok = False
            d1_ok = False
            d1_available = len(d1) > 5

            if len(h4) > 20:
                h4_bias = "bullish" if h4[-1].close > h4[-20].close else "bearish"
                h4_ok   = (direction == "buy" and h4_bias == "bullish") or \
                           (direction == "sell" and h4_bias == "bearish")
                parts.append(f"H4 {h4_bias}")
            else:
                parts.append("H4 n/a")

            if d1_available:
                d1_bias = "bullish" if d1[-1].close > d1[-5].close else "bearish"
                d1_ok   = (direction == "buy" and d1_bias == "bullish") or \
                           (direction == "sell" and d1_bias == "bearish")
                parts.append(f"D1 {d1_bias}")
            else:
                parts.append("D1 not available")
                d1_ok = True   # don't penalise if D1 data absent

            passed = h4_ok and d1_ok
            detail = ", ".join(parts)
            return ConditionResult(name, passed, detail)
        except Exception as exc:
            logger.debug("confluence c3 error: %s", exc)
            return ConditionResult(name, False, "error checking condition")

    def _check_session(self) -> ConditionResult:
        name = "Session"
        try:
            passed = is_london_or_ny_session()
            detail = session_label()
            return ConditionResult(name, passed, detail)
        except Exception as exc:
            logger.debug("confluence c4 error: %s", exc)
            return ConditionResult(name, False, "error checking condition")

    def _check_risk_reward(self, h1: list[Candle], direction: str) -> ConditionResult:
        name = "Risk:Reward"
        try:
            if len(h1) < 20:
                return ConditionResult(name, False, "Not enough H1 candles for R:R")
            entry = h1[-1].close
            atr_vals = atr(h1, 14)
            last_atr = _safe_last(atr_vals)
            if math.isnan(last_atr) or last_atr <= 0:
                return ConditionResult(name, False, "ATR unavailable")

            sl_dist = 1.5 * last_atr

            # TP = nearest swing high (buy) or swing low (sell) beyond entry
            lookback = min(30, len(h1) - 1)
            if direction == "buy":
                tp_level = _swing_high(h1[:-1], lookback)
                if math.isnan(tp_level) or tp_level <= entry:
                    tp_level = entry + 3.0 * last_atr   # fallback
            else:
                tp_level = _swing_low(h1[:-1], lookback)
                if math.isnan(tp_level) or tp_level >= entry:
                    tp_level = entry - 3.0 * last_atr

            reward = abs(tp_level - entry)
            rr = reward / sl_dist if sl_dist > 0 else 0.0
            passed = rr >= 1.5
            marginal = passed and rr < 2.0
            marker = "⚠️" if marginal else ("✅" if passed else "❌")
            detail = f"R:R 1:{rr:.1f} {marker}"
            if not passed:
                detail += " — below minimum"
            return ConditionResult(name, passed, detail)
        except Exception as exc:
            logger.debug("confluence c5 error: %s", exc)
            return ConditionResult(name, False, "error checking condition")
