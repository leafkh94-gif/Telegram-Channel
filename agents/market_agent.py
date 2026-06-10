"""
MarketAgent — technical signal analysis.

Runs the full strategy pipeline and returns a structured verdict:
  GO   — sweep detected, ADX trending, entry near S/R
  HOLD — one of the technical gates failed (not an error, just no setup)
"""
import logging
import math

from agents.base import AgentVerdict
from strategy.base import TF_H1, TF_H4
from strategy.gold_strategy import GoldStrategy
from strategy.indicators import adx as _adx, atr as _atr
from strategy.sr_levels import key_levels, near_key_level

logger = logging.getLogger(__name__)

_ADX_THRESHOLD = 25
_SR_ATR_MULT   = 1.0


class MarketAgent:
    def __init__(
        self,
        strategy: GoldStrategy | None = None,
        adx_threshold: int = _ADX_THRESHOLD,
        sr_atr_mult: float = _SR_ATR_MULT,
    ):
        self._strategy      = strategy or GoldStrategy()
        self._adx_threshold = adx_threshold
        self._sr_atr_mult   = sr_atr_mult

    def evaluate(self, epic: str, candles: dict) -> AgentVerdict:
        h4 = candles.get(TF_H4, [])
        h1 = candles.get(TF_H1, [])

        if not h1:
            return AgentVerdict(agent="market", verdict="HOLD",
                                confidence=1.0, reason="no H1 candles available")

        # ── ADX: confirm trending conditions ─────────────────────────────────
        last_adx: float | None = None
        if h4:
            adx_vals = _adx(h4, period=14)
            valid = [v for v in adx_vals if not math.isnan(v)]
            if valid:
                last_adx = valid[-1]
                if last_adx < self._adx_threshold:
                    return AgentVerdict(
                        agent="market", verdict="HOLD",
                        confidence=0.9,
                        reason=f"ADX {last_adx:.1f} < {_ADX_THRESHOLD} — market is choppy",
                    )

        # ── Strategy pipeline (regime → sweep → signal filter) ────────────────
        sig = self._strategy.evaluate(candles)
        if sig is None:
            return AgentVerdict(agent="market", verdict="HOLD",
                                confidence=0.85, reason="no liquidity sweep setup")

        # ── S/R confluence ────────────────────────────────────────────────────
        if h4 and h1:
            atr_series = _atr(h1, period=14)
            valid_atr  = [v for v in atr_series if v == v]
            if valid_atr:
                current_atr = valid_atr[-1]
                entry       = h1[-1].close
                levels      = key_levels(h4, h1)
                if levels and not near_key_level(entry, levels, current_atr, self._sr_atr_mult):
                    nearest = min(levels, key=lambda lv: abs(entry - lv))
                    return AgentVerdict(
                        agent="market", verdict="HOLD",
                        confidence=0.75,
                        reason=(f"entry {entry:.2f} not near S/R "
                                f"(nearest {nearest:.2f}, "
                                f"dist {abs(entry - nearest):.1f} > {_SR_ATR_MULT * current_atr:.1f})"),
                    )

        # ── Confidence: scales with ADX strength ──────────────────────────────
        if last_adx is not None:
            confidence = min(0.95, 0.70 + (last_adx - _ADX_THRESHOLD) / 100)
        else:
            confidence = 0.70

        adx_str = f"ADX {last_adx:.1f}" if last_adx is not None else "ADX n/a"
        return AgentVerdict(
            agent="market",
            verdict="GO",
            confidence=round(confidence, 2),
            reason=f"H1 {sig.direction} sweep, {adx_str}",
            direction=sig.direction,
        )
