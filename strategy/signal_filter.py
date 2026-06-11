"""
Signal filter — Gate 4 of the strategy pipeline.

RuleBasedSignalFilter scores each candidate signal against 6 technical
conditions derived from the feature-engineering and signal-classification
skills. A signal needs a majority of conditions in its favour to pass.

MLSignalFilter is kept as a thin wrapper so existing code that instantiates
it still works; it now delegates to RuleBasedSignalFilter.
"""
from __future__ import annotations

import logging
import math
from abc import ABC, abstractmethod
from typing import Sequence

from execution.models import Signal
from strategy.base import Candle
from strategy.indicators import atr, bollinger_bands, ema, macd, rsi

logger = logging.getLogger(__name__)


class SignalFilter(ABC):
    @abstractmethod
    def accept(self, signal: Signal, candles: Sequence[Candle]) -> bool: ...


class RuleBasedSignalFilter(SignalFilter):
    """
    Scores a candidate signal on 6 independent conditions.
    Each condition that aligns with signal direction adds 1 point.
    Signal is accepted when score >= min_score (default 4 / 6).

    Conditions
    ----------
    1. RSI not at extreme against direction  (overbought on buy = bad)
    2. MACD histogram agrees with direction
    3. Price not beyond Bollinger Band (avoid chasing)
    4. EMA-20 / EMA-50 short-term trend alignment
    5. ATR not in top-10 % of recent range   (avoid entering in spikes)
    6. Body-to-range ratio >= 0.3             (candle has conviction)
    """

    def __init__(
        self,
        min_score: int = 4,
        rsi_overbought: float = 70.0,
        rsi_oversold: float = 30.0,
        atr_spike_pct: float = 90.0,
        min_body_ratio: float = 0.30,
    ):
        self.min_score = min_score
        self.rsi_overbought = rsi_overbought
        self.rsi_oversold = rsi_oversold
        self.atr_spike_pct = atr_spike_pct
        self.min_body_ratio = min_body_ratio

    def accept(self, signal: Signal, candles: Sequence[Candle]) -> bool:
        if len(candles) < 55:
            logger.info("signal_filter: not enough candles (%d), accepting by default", len(candles))
            return True

        direction = signal.direction
        closes = [c.close for c in candles]
        score = 0
        reasons: list[str] = []

        # 1. RSI
        rsi_vals = rsi(closes, 14)
        last_rsi = rsi_vals[-1]
        if not math.isnan(last_rsi):
            if direction == "buy" and last_rsi < self.rsi_overbought:
                score += 1
                reasons.append(f"rsi={last_rsi:.1f} ok for buy")
            elif direction == "sell" and last_rsi > self.rsi_oversold:
                score += 1
                reasons.append(f"rsi={last_rsi:.1f} ok for sell")
            else:
                reasons.append(f"rsi={last_rsi:.1f} AGAINST {direction}")

        # 2. MACD histogram
        _, _, hist = macd(closes, 12, 26, 9)
        last_hist = hist[-1]
        if not math.isnan(last_hist):
            if direction == "buy" and last_hist > 0:
                score += 1
                reasons.append(f"macd_hist={last_hist:.4f} bullish")
            elif direction == "sell" and last_hist < 0:
                score += 1
                reasons.append(f"macd_hist={last_hist:.4f} bearish")
            else:
                reasons.append(f"macd_hist={last_hist:.4f} AGAINST {direction}")

        # 3. Bollinger Band — not over-extended
        upper, _, lower = bollinger_bands(closes, 20, 2.0)
        last_close = closes[-1]
        last_upper, last_lower = upper[-1], lower[-1]
        if not (math.isnan(last_upper) or math.isnan(last_lower)):
            if direction == "buy" and last_close < last_upper:
                score += 1
                reasons.append("price below BB upper (not over-extended for buy)")
            elif direction == "sell" and last_close > last_lower:
                score += 1
                reasons.append("price above BB lower (not over-extended for sell)")
            else:
                reasons.append(f"price outside BB for {direction}")

        # 4. EMA-20 / EMA-50 short-term alignment
        ema20 = ema(closes, 20)
        ema50 = ema(closes, 50)
        e20, e50 = ema20[-1], ema50[-1]
        if not (math.isnan(e20) or math.isnan(e50)):
            if direction == "buy" and e20 >= e50:
                score += 1
                reasons.append(f"ema20={e20:.2f} >= ema50={e50:.2f} aligned buy")
            elif direction == "sell" and e20 <= e50:
                score += 1
                reasons.append(f"ema20={e20:.2f} <= ema50={e50:.2f} aligned sell")
            else:
                reasons.append(f"ema short-term AGAINST {direction}")

        # 5. ATR not spiking
        atr_vals = [v for v in atr(candles, 14) if not math.isnan(v)]
        if len(atr_vals) >= 20:
            window = atr_vals[-100:]
            current_atr = atr_vals[-1]
            pct = 100.0 * sum(1 for v in window if v <= current_atr) / len(window)
            if pct < self.atr_spike_pct:
                score += 1
                reasons.append(f"atr_pct={pct:.0f} (no spike)")
            else:
                reasons.append(f"atr_pct={pct:.0f} SPIKE — deduct")

        # 6. Body-to-range ratio on last bar
        last_bar = candles[-1]
        bar_range = last_bar.high - last_bar.low
        body = abs(last_bar.close - last_bar.open) if hasattr(last_bar, "open") else bar_range * 0.5
        ratio = body / bar_range if bar_range > 0 else 0.0
        if ratio >= self.min_body_ratio:
            score += 1
            reasons.append(f"body_ratio={ratio:.2f} has conviction")
        else:
            reasons.append(f"body_ratio={ratio:.2f} weak bar")

        accepted = score >= self.min_score
        logger.info(
            "signal_filter: direction=%s score=%d/%d %s — %s",
            direction, score, 6,
            "ACCEPT" if accepted else "REJECT",
            "; ".join(reasons),
        )
        return accepted

    def _features(self, candles: Sequence[Candle]) -> list[float]:
        """Full feature vector for future ML model training."""
        if len(candles) < 55:
            return []
        closes = [c.close for c in candles]

        rsi_vals = rsi(closes, 14)
        _, _, hist = macd(closes, 12, 26, 9)
        upper, mid, lower = bollinger_bands(closes, 20, 2.0)
        ema20 = ema(closes, 20)
        ema50 = ema(closes, 50)
        atr_vals = atr(candles, 14)

        last = candles[-1]
        bar_range = last.high - last.low
        body = abs(last.close - last.open) if hasattr(last, "open") else bar_range * 0.5

        def _safe(v: float) -> float:
            return v if not math.isnan(v) else 0.0

        return [
            _safe(rsi_vals[-1]),
            _safe(hist[-1]),
            _safe((closes[-1] - mid[-1]) / mid[-1]) if not math.isnan(mid[-1]) and mid[-1] else 0.0,
            _safe(ema20[-1] - ema50[-1]),
            _safe(atr_vals[-1] / closes[-1]) if closes[-1] else 0.0,
            body / bar_range if bar_range > 0 else 0.0,
            (closes[-1] - closes[-5]) / closes[-5] if len(closes) >= 6 and closes[-5] else 0.0,
            (closes[-1] - closes[-20]) / closes[-20] if len(closes) >= 21 and closes[-20] else 0.0,
        ]


class MLSignalFilter(RuleBasedSignalFilter):
    """Kept for backward compatibility — now delegates to RuleBasedSignalFilter."""
    pass
