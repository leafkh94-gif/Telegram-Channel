"""
MarketAgent — multi-factor signal scoring.

Decision is no longer binary (sweep = GO). Instead, 8 factors are scored:

  Mandatory  : liquidity sweep detected             (blocks if absent)
  Mandatory  : ADX >= threshold (market trending)   (blocks if absent)
  Scored     : price confirmation   2 pts  (breaks the swing level)
  Scored     : indicators aligned   1 pt   (RSI/MACD/EMA-50, 2/3)
  Scored     : multi-TF alignment   1 pt   (H4 + D1 bias)
  Scored     : session              1 pt   (London / New York)
  Scored     : risk:reward >= 1.5   1 pt
  Scored     : near S/R level       1 pt
  Scored     : volume above avg     1 pt

GO fires when scored total >= min_score (default 3 / 8).
Confidence scales with the scored total and ADX strength.
"""
import logging
import math
from typing import Sequence

from agents.base import AgentVerdict
from strategy.base import Candle, TF_H1, TF_H4
from strategy.confluence_scorer import ConfluenceScorer
from strategy.gold_strategy import GoldStrategy
from strategy.indicators import adx as _adx, atr as _atr
from strategy.sr_levels import key_levels, near_key_level

logger = logging.getLogger(__name__)

_ADX_THRESHOLD    = 20
_SR_ATR_MULT      = 1.0
_VOL_LOOKBACK     = 20
_VOL_STRONG_RATIO = 1.5
_MIN_SCORE        = 3    # minimum scored points (out of 8) to fire GO


def _volume_above_avg(h1: Sequence[Candle]) -> bool | None:
    """True = above average, False = below, None = unknown (no volume data)."""
    vols = [c.volume for c in h1 if c.volume > 0]
    if len(vols) < _VOL_LOOKBACK + 1:
        return None
    avg  = sum(vols[-(_VOL_LOOKBACK + 1):-1]) / _VOL_LOOKBACK
    return vols[-1] >= avg if avg > 0 else None


class MarketAgent:
    def __init__(
        self,
        strategy: GoldStrategy | None = None,
        adx_threshold: int = _ADX_THRESHOLD,
        sr_atr_mult: float = _SR_ATR_MULT,
        min_score: int = _MIN_SCORE,
    ):
        self._strategy      = strategy or GoldStrategy()
        self._adx_threshold = adx_threshold
        self._sr_atr_mult   = sr_atr_mult
        self._min_score     = min_score
        self._scorer        = ConfluenceScorer()

    def evaluate(self, epic: str, candles: dict) -> AgentVerdict:
        h4 = candles.get(TF_H4, [])
        h1 = candles.get(TF_H1, [])

        if not h1:
            return AgentVerdict(agent="market", verdict="HOLD",
                                confidence=1.0, reason="no H1 candles available")

        # ── Mandatory 1: ADX — market must be moving ──────────────────────────
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

        # ── Mandatory 2: liquidity sweep + regime + signal filter ─────────────
        sig = self._strategy.evaluate(candles)
        if sig is None:
            return AgentVerdict(agent="market", verdict="HOLD",
                                confidence=0.85, reason="no liquidity sweep setup")

        direction = sig.direction

        # ── Scored factors ────────────────────────────────────────────────────
        score = 0
        factors: list[str] = []

        # Run full confluence scorer for the 5 technical conditions
        conf = self._scorer.score(candles, direction)
        sig.confluence = conf

        # Price confirmation (2 pts — most important technical confirmation)
        price_conf = next((c for c in conf.conditions if c.name == "Price Confirmation"), None)
        if price_conf and price_conf.passed:
            score += 2
            factors.append(f"✅ price confirmed ({price_conf.detail})")
        else:
            detail = price_conf.detail if price_conf else "n/a"
            factors.append(f"❌ price not confirmed ({detail})")

        # Indicators (RSI/MACD/EMA-50)
        ind = next((c for c in conf.conditions if c.name == "Indicators"), None)
        if ind and ind.passed:
            score += 1
            factors.append(f"✅ indicators ({ind.detail})")
        elif ind:
            factors.append(f"❌ indicators ({ind.detail})")

        # Multi-TF alignment
        mtf = next((c for c in conf.conditions if c.name == "Multi-TF Alignment"), None)
        if mtf and mtf.passed:
            score += 1
            factors.append(f"✅ MTF aligned ({mtf.detail})")
        elif mtf:
            factors.append(f"❌ MTF ({mtf.detail})")

        # Session
        sess = next((c for c in conf.conditions if c.name == "Session"), None)
        if sess and sess.passed:
            score += 1
            factors.append(f"✅ {sess.detail}")
        elif sess:
            factors.append(f"❌ {sess.detail}")

        # Risk:Reward
        rr = next((c for c in conf.conditions if c.name == "Risk:Reward"), None)
        if rr and rr.passed:
            score += 1
            factors.append(f"✅ {rr.detail}")
        elif rr:
            factors.append(f"❌ {rr.detail}")

        # Near S/R level
        sr_near = False
        if h4 and h1:
            atr_series = _atr(h1, period=14)
            valid_atr  = [v for v in atr_series if not math.isnan(v)]
            if valid_atr:
                entry  = h1[-1].close
                levels = key_levels(h4, h1)
                if levels:
                    sr_near = near_key_level(entry, levels, valid_atr[-1], self._sr_atr_mult)
        if sr_near:
            score += 1
            factors.append("✅ near S/R")
        else:
            factors.append("❌ no nearby S/R")

        # Volume
        vol_above = _volume_above_avg(h1)
        if vol_above is True:
            score += 1
            factors.append("✅ volume above avg")
        elif vol_above is False:
            factors.append("❌ volume below avg")
        else:
            factors.append("— volume unknown")

        # ── Decision ──────────────────────────────────────────────────────────
        adx_str = f"ADX {last_adx:.1f}" if last_adx is not None else "ADX n/a"
        logger.info(
            "market_agent [%s]: score=%d/%d (%s), %s",
            epic, score, 8, adx_str,
            "; ".join(f.replace("✅ ", "").replace("❌ ", "✗ ").replace("— ", "") for f in factors),
        )

        if score < self._min_score:
            return AgentVerdict(
                agent="market", verdict="HOLD",
                confidence=0.80,
                reason=f"score {score}/8 below threshold — {adx_str}, "
                       + ", ".join(f for f in factors if f.startswith("❌")),
            )

        # Confidence scales with score and ADX strength
        base = 0.60 + (score / 8) * 0.30        # 0.60–0.90 based on score
        if last_adx is not None:
            base = min(0.97, base + (last_adx - self._adx_threshold) / 200)
        confidence = round(min(0.97, base), 2)

        reason = (f"H1 {direction} sweep, {adx_str}, score {score}/8 — "
                  + ", ".join(f for f in factors if f.startswith("✅")))

        return AgentVerdict(
            agent="market",
            verdict="GO",
            confidence=confidence,
            reason=reason,
            direction=direction,
        )
