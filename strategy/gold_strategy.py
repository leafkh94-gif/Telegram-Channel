"""
GoldStrategy — top-level liquidity-sweep strategy.

Despite the name (kept for backward compatibility), this strategy is
instrument-agnostic and is applied to every market in the watchlist:
Gold (XAU/USD), S&P 500, Nasdaq 100, and Dow Jones.

Chains: H4 regime filter → H1 liquidity sweep → signal filter → position sizer.
Outputs a Signal or None. Never touches the broker or any core module.
"""
import logging
import math
from typing import Optional

from execution.models import Signal
from strategy.base import MarketRegime, MultiTimeframeCandles, StrategyBase, TF_H1, TF_H4
from strategy.indicators import atr
from strategy.liquidity_sweep import LiquiditySweepDetector
from strategy.position_sizer import PositionSizer
from strategy.regime_filter import RegimeFilter
from strategy.signal_filter import MLSignalFilter, SignalFilter

logger = logging.getLogger(__name__)


class GoldStrategy(StrategyBase):
    def __init__(
        self,
        lots: float = 0.05,                     # fallback when sizer has no ATR
        regime_filter: RegimeFilter | None = None,
        sweep_detector: LiquiditySweepDetector | None = None,
        signal_filter: SignalFilter | None = None,
        position_sizer: PositionSizer | None = None,
    ):
        self.lots = lots
        self.regime_filter = regime_filter or RegimeFilter()
        self.sweep_detector = sweep_detector or LiquiditySweepDetector()
        self.signal_filter = signal_filter or MLSignalFilter()
        self.position_sizer = position_sizer or PositionSizer(fallback_lots=lots)

    def evaluate(self, candles: MultiTimeframeCandles) -> Optional[Signal]:
        h4 = candles.get(TF_H4, [])
        h1 = candles.get(TF_H1, [])

        # ── Gate 1: enough data ───────────────────────────────────────────────
        if len(h4) < self.regime_filter.min_candles:
            logger.info("gate1 SKIP: not enough H4 candles (%d < %d)", len(h4), self.regime_filter.min_candles)
            return None
        if len(h1) < self.sweep_detector.min_candles:
            logger.info("gate1 SKIP: not enough H1 candles (%d < %d)", len(h1), self.sweep_detector.min_candles)
            return None

        # ── Gate 2: regime filter (H4) ────────────────────────────────────────
        regime = self.regime_filter.classify(h4)
        logger.info("gate2: regime=%s", regime.value)
        if regime == MarketRegime.VOLATILE:
            logger.info("gate2 SKIP: regime VOLATILE")
            return None

        # ── Gate 3: liquidity sweep (H1) ─────────────────────────────────────
        direction = self.sweep_detector.detect(h1)
        if direction is None:
            logger.info("gate3 SKIP: no liquidity sweep detected")
            return None
        logger.info("gate3 PASS: sweep direction=%s", direction)

        # ── Gate 4: signal filter ─────────────────────────────────────────────
        # Compute lot size before building the candidate so the filter can see it
        atr_vals = atr(h1, 14)
        last_atr = atr_vals[-1] if atr_vals else float("nan")
        last_close = h1[-1].close if h1 else 0.0
        computed_lots = self.position_sizer.compute(last_atr, last_close)

        candidate = Signal(direction=direction, lots=computed_lots)
        if not self.signal_filter.accept(candidate, h1):
            logger.info("gate4 SKIP: signal filter rejected")
            return None

        logger.info(
            "signal generated: %s %.2f lots (regime=%s atr=%.4f)",
            direction, computed_lots, regime.value, last_atr if not math.isnan(last_atr) else -1,
        )
        return candidate
