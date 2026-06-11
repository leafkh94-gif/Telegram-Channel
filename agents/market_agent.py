"""
MarketAgent — technical signal analysis including volume confirmation.

Runs the full strategy pipeline and returns a structured verdict:
  GO   — sweep detected, ADX trending, entry near S/R, adequate volume
  HOLD — one of the technical gates failed (not an error, just no setup)

Volume confirmation implements the "trading volumes" pillar of big-data
analysis: a sweep on above-average volume is a stronger reversal signal
because it shows institutional participation, not just retail stop-hunting.
"""
import logging
import math
from typing import Sequence

from agents.base import AgentVerdict
from strategy.base import Candle, TF_H1, TF_H4
from strategy.gold_strategy import GoldStrategy
from strategy.indicators import adx as _adx, atr as _atr
from strategy.sr_levels import key_levels, near_key_level

logger = logging.getLogger(__name__)

_ADX_THRESHOLD    = 20    # 20 = medium selectivity; 25 = stricter (fewer signals)
_SR_ATR_MULT      = 1.0
_VOL_LOOKBACK     = 20    # bars used to compute average volume
_VOL_WEAK_RATIO   = 0.5   # sweep volume < 50% of avg → weak (HOLD)
_VOL_STRONG_RATIO = 1.5   # sweep volume > 150% of avg → strong (confidence boost)


def _volume_signal(h1: Sequence[Candle]) -> tuple[str, float]:
    """
    Compare the last bar's volume against the 20-bar rolling average.
    Returns (label, ratio) where label is 'strong'|'normal'|'weak'|'unknown'.
    Volume = 0 on most Yahoo Finance index candles; skip check in that case.
    """
    vols = [c.volume for c in h1 if c.volume > 0]
    if len(vols) < _VOL_LOOKBACK + 1:
        return "unknown", 1.0
    avg  = sum(vols[-(_VOL_LOOKBACK + 1):-1]) / _VOL_LOOKBACK
    last = vols[-1]
    if avg <= 0:
        return "unknown", 1.0
    ratio = last / avg
    if ratio >= _VOL_STRONG_RATIO:
        return "strong", ratio
    if ratio < _VOL_WEAK_RATIO:
        return "weak", ratio
    return "normal", ratio


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
                        reason=f"ADX {last_adx:.1f} < {self._adx_threshold} — market is choppy",
                    )

        # ── Strategy pipeline (regime → sweep → signal filter) ────────────────
        sig = self._strategy.evaluate(candles)
        if sig is None:
            return AgentVerdict(agent="market", verdict="HOLD",
                                confidence=0.85, reason="no liquidity sweep setup")

        # ── S/R confluence (confidence modifier, not a hard block) ───────────
        sr_near = False
        if h4 and h1:
            atr_series = _atr(h1, period=14)
            valid_atr  = [v for v in atr_series if v == v]
            if valid_atr:
                current_atr = valid_atr[-1]
                entry       = h1[-1].close
                levels      = key_levels(h4, h1)
                if levels:
                    sr_near = near_key_level(entry, levels, current_atr, self._sr_atr_mult)

        # ── Volume confirmation ───────────────────────────────────────────────
        vol_label, vol_ratio = _volume_signal(h1)
        if vol_label == "weak":
            return AgentVerdict(
                agent="market", verdict="HOLD",
                confidence=0.80,
                reason=f"weak volume on sweep ({vol_ratio:.1f}× avg) — low conviction",
            )

        # ── Confidence: ADX strength + S/R confluence + volume boost ─────────
        if last_adx is not None:
            confidence = min(0.90, 0.70 + (last_adx - self._adx_threshold) / 100)
        else:
            confidence = 0.70

        if sr_near:
            confidence = min(0.97, confidence + 0.05)   # +5% for S/R alignment
        if vol_label == "strong":
            confidence = min(0.97, confidence + 0.07)

        adx_str = f"ADX {last_adx:.1f}" if last_adx is not None else "ADX n/a"
        sr_str  = ", near S/R" if sr_near else ""
        vol_str = (f", vol {vol_ratio:.1f}× avg" if vol_label != "unknown" else "")
        return AgentVerdict(
            agent="market",
            verdict="GO",
            confidence=round(confidence, 2),
            reason=f"H1 {sig.direction} sweep, {adx_str}{sr_str}{vol_str}",
            direction=sig.direction,
        )
