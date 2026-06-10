"""
Orchestrator — coordinates all agents and produces a single TradeDecision.

Pipeline (cheapest/fastest checks first):
  1. MarketAgent  — technical analysis (no network)
  2. NewsAgent    — calendar + RSS headlines (cached network)
  3. RiskAgent    — lot sizing (pure math, no I/O)

A single HOLD or BLOCK from any agent skips the trade.
All three must return GO for a signal to fire.
"""
import logging

from agents.base import AgentVerdict, TradeDecision
from agents.market_agent import MarketAgent
from agents.news_agent import NewsAgent
from agents.risk_agent import RiskAgent
from agents.sentiment_agent import SentimentAgent
from strategy.base import TF_H1
from strategy.indicators import atr as _atr

logger = logging.getLogger(__name__)

_TP_ATR_MULT = 3.0
_SL_ATR_MULT = 1.5


class Orchestrator:
    def __init__(
        self,
        market_agent: MarketAgent | None = None,
        news_agent: NewsAgent | None = None,
        sentiment_agent: SentimentAgent | None = None,
        risk_agent: RiskAgent | None = None,
    ):
        self._market    = market_agent    or MarketAgent()
        self._news      = news_agent      or NewsAgent()
        self._sentiment = sentiment_agent or SentimentAgent()
        self._risk      = risk_agent      or RiskAgent()

    def decide(self, epic: str, candles: dict) -> TradeDecision:
        """
        Evaluate all agents for the given instrument and candle set.
        Returns a TradeDecision with action="buy"/"sell" or action="skip".
        """
        # ── Step 1: market analysis ───────────────────────────────────────────
        market_v = self._market.evaluate(epic, candles)
        logger.info("Orchestrator [%s] market: %s — %s", epic, market_v.verdict, market_v.reason)

        if market_v.verdict != "GO":
            return self._skip(market_v.reason, [market_v])

        direction = market_v.direction   # guaranteed non-None when verdict=GO

        # ── Step 2: news clearance ────────────────────────────────────────────
        news_v = self._news.evaluate(epic)
        logger.info("Orchestrator [%s] news:   %s — %s", epic, news_v.verdict, news_v.reason)

        if news_v.verdict != "GO":
            return self._skip(news_v.reason, [market_v, news_v])

        # ── Step 3: sentiment validation ──────────────────────────────────────
        sentiment_v = self._sentiment.evaluate(epic, direction)
        logger.info("Orchestrator [%s] sentiment: %s — %s",
                    epic, sentiment_v.verdict, sentiment_v.reason)

        if sentiment_v.verdict != "GO":
            return self._skip(sentiment_v.reason, [market_v, news_v, sentiment_v])

        # ── Step 4: risk / lot sizing ─────────────────────────────────────────
        h1         = candles.get(TF_H1, [])
        atr_series = _atr(h1, period=14)
        valid_atr  = [v for v in atr_series if v == v]

        if not valid_atr or not h1:
            return self._skip("ATR unavailable — cannot size position",
                              [market_v, news_v])

        current_atr = valid_atr[-1]
        entry       = h1[-1].close

        risk_v = self._risk.evaluate(epic, entry, current_atr, direction)
        logger.info("Orchestrator [%s] risk:   %s — %s", epic, risk_v.verdict, risk_v.reason)

        if risk_v.verdict != "GO":
            return self._skip(risk_v.reason, [market_v, news_v, sentiment_v, risk_v])

        # ── All GO ────────────────────────────────────────────────────────────
        verdicts   = [market_v, news_v, sentiment_v, risk_v]
        confidence = min(v.confidence for v in verdicts)
        lots       = risk_v.lots or 0.01

        logger.info(
            "Orchestrator [%s] FIRE %s  lots=%.2f  entry=%.2f  conf=%.0f%%",
            epic, direction.upper(), lots, entry, confidence * 100,
        )
        return TradeDecision(
            action=direction,
            lots=lots,
            reason="all agents agree",
            confidence=round(confidence, 2),
            verdicts=verdicts,
        )

    @staticmethod
    def _skip(reason: str, verdicts: list[AgentVerdict]) -> TradeDecision:
        return TradeDecision(
            action="skip", lots=0.0,
            reason=reason, confidence=0.0,
            verdicts=verdicts,
        )
