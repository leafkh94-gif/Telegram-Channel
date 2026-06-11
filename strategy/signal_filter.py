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

        # 1. RSI: reversal sweep should arrive from an extreme.
        # For BUY sweeps: RSI below 50 (came from oversold territory).
        # For SELL sweeps: RSI above 50 (came from overbought territory).
        rsi_vals = rsi(closes, 14)
        last_rsi = rsi_vals[-1]
        if not math.isnan(last_rsi):
            if direction == "buy" and last_rsi < 55.0:
                score += 1
                reasons.append(f"rsi={last_rsi:.1f} below 55 (reversal buy zone)")
            elif direction == "sell" and last_rsi > 45.0:
                score += 1
                reasons.append(f"rsi={last_rsi:.1f} above 45 (reversal sell zone)")
            else:
                reasons.append(f"rsi={last_rsi:.1f} not in reversal zone for {direction}")

        # 2. MACD histogram momentum — check histogram is turning, not just polarity.
        # For a reversal BUY sweep: histogram rising (less negative or positive).
        # For a reversal SELL sweep: histogram falling (less positive or negative).
        _, _, hist = macd(closes, 12, 26, 9)
        last_hist = hist[-1]
        prev_hist = next((v for v in reversed(hist[:-1]) if not math.isnan(v)), float("nan"))
        if not (math.isnan(last_hist) or math.isnan(prev_hist)):
            if direction == "buy" and last_hist >= prev_hist:
                score += 1
                reasons.append(f"macd_hist turning up ({prev_hist:.4f}→{last_hist:.4f})")
            elif direction == "sell" and last_hist <= prev_hist:
                score += 1
                reasons.append(f"macd_hist turning down ({prev_hist:.4f}→{last_hist:.4f})")
            else:
                reasons.append(f"macd_hist still moving AGAINST {direction}")

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

        # 4. Candle direction confirms sweep (close vs open)
        # Liquidity sweeps are reversals — EMA alignment is deliberately wrong.
        # Instead: the sweep candle itself must close in the signal direction.
        last_bar = candles[-1]
        if hasattr(last_bar, "open"):
            if direction == "buy" and last_bar.close >= last_bar.open:
                score += 1
                reasons.append(f"close={last_bar.close:.2f} >= open={last_bar.open:.2f} bullish candle")
            elif direction == "sell" and last_bar.close <= last_bar.open:
                score += 1
                reasons.append(f"close={last_bar.close:.2f} <= open={last_bar.open:.2f} bearish candle")
            else:
                reasons.append(f"candle direction AGAINST {direction}")

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

        # 6. Wick confirms rejection (sweep candle has significant wick in sweep direction)
        # A buy sweep pierces a low and rejects → lower wick should be prominent.
        # A sell sweep pierces a high and rejects → upper wick should be prominent.
        last_bar = candles[-1]
        bar_range = last_bar.high - last_bar.low
        if bar_range > 0 and hasattr(last_bar, "open"):
            candle_low  = min(last_bar.open, last_bar.close)
            candle_high = max(last_bar.open, last_bar.close)
            lower_wick = candle_low  - last_bar.low
            upper_wick = last_bar.high - candle_high
            if direction == "buy":
                wick_ratio = lower_wick / bar_range
            else:
                wick_ratio = upper_wick / bar_range
            if wick_ratio >= 0.20:
                score += 1
                reasons.append(f"wick_ratio={wick_ratio:.2f} confirms rejection")
            else:
                reasons.append(f"wick_ratio={wick_ratio:.2f} weak rejection wick")

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
