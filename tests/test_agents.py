"""
Tests for the multi-agent trading system:
  AgentVerdict, TradeDecision, MarketAgent, NewsAgent, RiskAgent, Orchestrator.
"""
import math
import pytest
from unittest.mock import patch, MagicMock

from agents.base import AgentVerdict, TradeDecision
from agents.market_agent import MarketAgent
from agents.news_agent import NewsAgent
from agents.risk_agent import RiskAgent
from agents.sentiment_agent import SentimentAgent, _score_post
from agents.orchestrator import Orchestrator
from strategy.base import Candle, TF_H1, TF_H4
from strategy.gold_strategy import GoldStrategy
from strategy.trend_momentum import TrendMomentumDetector
from strategy.regime_filter import RegimeFilter


# ── Helpers ───────────────────────────────────────────────────────────────────

def flat_candles(n: int, price: float = 2300.0) -> list[Candle]:
    return [
        Candle(timestamp=f"2024-01-01T{i % 24:02d}:00:00Z",
               open=price, high=price + 1, low=price - 1, close=price)
        for i in range(n)
    ]


def trending_candles(n: int, start: float = 2200.0, step: float = 2.0) -> list[Candle]:
    """
    Realistic trending candles for TrendMomentumDetector tests.

    Structure:
      - First 75 % of bars: wide ATR "warmup" with slight trend drift + occasional pullback.
        These establish RSI/MACD direction and provide a high ATR baseline.
      - Last 25 % of bars: narrow ATR "signal zone" with continued trend closes.
        Current ATR ends up below the 80th-percentile threshold → no spike block.
        RSI stays above 52 and rising; MACD histogram stays positive.
    """
    candles, p = [], start
    direction = 1 if step >= 0 else -1
    warmup_end = int(n * 0.75)
    for i in range(n):
        if i < warmup_end:
            # Wide candles, modest trend + occasional pullback so RSI isn't pegged
            move = direction * abs(step) * (0.8 if i % 4 != 3 else -0.3)
            bar_extra = 4.0          # wide range → high ATR in warmup window
        else:
            # Narrow candles, *accelerating* trend → MACD histogram grows, ATR stays low
            accel = 1.0 + (i - warmup_end) * 0.1
            move = direction * abs(step) * accel
            bar_extra = 0.5          # tight range → low current ATR
        high = p + abs(move) + bar_extra
        low  = p - bar_extra
        candles.append(Candle(
            timestamp=f"2024-01-{(i // 24) + 1:02d}T{i % 24:02d}:00:00Z",
            open=p, high=high, low=low, close=p + move,
        ))
        p += move
    return candles


def _build_sweep_candles(direction: str, lookback: int = 20,
                         sweep_lookback: int = 5) -> list[Candle]:
    base = 2300.0
    n = lookback + sweep_lookback * 3 + 5
    candles = []
    pivot_idx = n - sweep_lookback - 2
    for i in range(n - 1):
        if direction == "buy" and i == pivot_idx:
            c = Candle(f"t{i}", base, base + 2, base - 30, base)
        elif direction == "sell" and i == pivot_idx:
            c = Candle(f"t{i}", base, base + 30, base - 2, base)
        else:
            c = Candle(f"t{i}", base, base + 5, base - 5, base)
        candles.append(c)
    if direction == "buy":
        candles.append(Candle(f"t{n-1}", base, base + 2, base - 40, base + 1))
    else:
        candles.append(Candle(f"t{n-1}", base, base + 40, base - 2, base - 1))
    return candles


def _candles_with_momentum(direction: str) -> dict:
    """250 trending H4 + 100 trending H1 candles to trigger TrendMomentumDetector."""
    step = 2.0 if direction == "buy" else -2.0
    return {
        TF_H4: trending_candles(250, start=2200.0, step=step),
        TF_H1: trending_candles(100, start=2200.0, step=step),
    }


def _candles_with_sweep(direction: str) -> dict:
    """Alias kept so existing callers compile; now uses momentum candles."""
    return _candles_with_momentum(direction)


# ── AgentVerdict ──────────────────────────────────────────────────────────────

def test_verdict_valid():
    v = AgentVerdict(agent="market", verdict="GO", confidence=0.8, reason="ok")
    assert v.emoji() == "✅"


def test_verdict_invalid_verdict():
    with pytest.raises(ValueError):
        AgentVerdict(agent="market", verdict="YES", confidence=0.8, reason="ok")


def test_verdict_invalid_confidence():
    with pytest.raises(ValueError):
        AgentVerdict(agent="market", verdict="GO", confidence=1.5, reason="ok")


def test_verdict_hold_emoji():
    v = AgentVerdict(agent="news", verdict="HOLD", confidence=0.9, reason="event")
    assert v.emoji() == "⏸"


def test_verdict_block_emoji():
    v = AgentVerdict(agent="news", verdict="BLOCK", confidence=0.9, reason="crash")
    assert v.emoji() == "🚫"


# ── MarketAgent ───────────────────────────────────────────────────────────────

def test_market_agent_hold_on_no_candles():
    agent = MarketAgent()
    result = agent.evaluate("GOLD", {TF_H1: [], TF_H4: []})
    assert result.verdict == "HOLD"


def test_market_agent_hold_on_no_signal():
    agent = MarketAgent()
    candles = {TF_H4: flat_candles(250), TF_H1: flat_candles(100)}
    result = agent.evaluate("GOLD", candles)
    assert result.verdict == "HOLD"


def test_market_agent_go_on_sweep():
    rf    = RegimeFilter(volatile_atr_pct=0.05)
    det   = TrendMomentumDetector()
    strat = GoldStrategy(regime_filter=rf, sweep_detector=det)
    # adx_threshold=0 skips ADX gate; sr_atr_mult=999 ensures S/R always passes
    # min_score=0 bypasses multi-factor threshold so synthetic candles pass
    agent   = MarketAgent(strategy=strat, adx_threshold=0, sr_atr_mult=999, min_score=0)
    candles = _candles_with_momentum("buy")
    result  = agent.evaluate("GOLD", candles)
    assert result.verdict == "GO"
    assert result.direction == "buy"


def test_market_agent_go_direction_sell():
    rf    = RegimeFilter(volatile_atr_pct=0.05)
    det   = TrendMomentumDetector()
    strat = GoldStrategy(regime_filter=rf, sweep_detector=det)
    # adx_threshold=0 skips ADX gate; sr_atr_mult=999 ensures S/R always passes
    # min_score=0 bypasses multi-factor threshold so synthetic candles pass
    agent   = MarketAgent(strategy=strat, adx_threshold=0, sr_atr_mult=999, min_score=0)
    candles = _candles_with_momentum("sell")
    result  = agent.evaluate("GOLD", candles)
    assert result.verdict == "GO"
    assert result.direction == "sell"


def test_market_agent_confidence_in_range():
    rf    = RegimeFilter(volatile_atr_pct=0.05)
    det   = TrendMomentumDetector()
    strat = GoldStrategy(regime_filter=rf, sweep_detector=det)
    # adx_threshold=0 skips ADX gate; sr_atr_mult=999 ensures S/R always passes
    agent   = MarketAgent(strategy=strat, adx_threshold=0, sr_atr_mult=999)
    candles = _candles_with_momentum("buy")
    result  = agent.evaluate("GOLD", candles)
    if result.verdict == "GO":
        assert 0.0 <= result.confidence <= 1.0


# ── NewsAgent ─────────────────────────────────────────────────────────────────

def test_news_agent_go_when_no_events():
    agent = NewsAgent()
    with patch("agents.news_agent.high_impact_news_within", return_value=False), \
         patch("agents.news_agent._get_rss", return_value=[]):
        result = agent.evaluate("GOLD")
    assert result.verdict == "GO"
    assert result.agent == "news"


def test_news_agent_hold_on_calendar_event():
    agent = NewsAgent()
    with patch("agents.news_agent.high_impact_news_within", return_value=True):
        result = agent.evaluate("GOLD")
    assert result.verdict == "HOLD"


def test_news_agent_block_on_crash_headline():
    import time as _time
    agent = NewsAgent()
    fresh = [{"title": "global market crash sends stocks plunging",
              "pub_ts": _time.time() - 60}]   # 1 min old
    with patch("agents.news_agent.high_impact_news_within", return_value=False), \
         patch("agents.news_agent._get_rss", return_value=fresh):
        result = agent.evaluate("GOLD")
    assert result.verdict == "BLOCK"


def test_news_agent_hold_on_relevant_recent_headline():
    import time as _time
    agent = NewsAgent()
    fresh = [{"title": "gold price surges on safe haven demand",
              "pub_ts": _time.time() - 300}]  # 5 min old
    with patch("agents.news_agent.high_impact_news_within", return_value=False), \
         patch("agents.news_agent._get_rss", return_value=fresh):
        result = agent.evaluate("GOLD")
    assert result.verdict == "HOLD"


def test_news_agent_go_on_old_relevant_headline():
    import time as _time
    agent = NewsAgent()
    old = [{"title": "gold price surges on safe haven demand",
            "pub_ts": _time.time() - 3600}]   # 60 min old → not recent
    with patch("agents.news_agent.high_impact_news_within", return_value=False), \
         patch("agents.news_agent._get_rss", return_value=old):
        result = agent.evaluate("GOLD")
    assert result.verdict == "GO"


# ── RiskAgent ─────────────────────────────────────────────────────────────────

def test_risk_agent_go_normal_conditions():
    # ATR=8 on GOLD: lots = (2000×0.01) / (1.5×8×100) = 20/1200 = 0.0167 → valid
    agent = RiskAgent(account_size_usd=2000, risk_per_trade_pct=0.01)
    result = agent.evaluate("GOLD", entry=2300.0, atr=8.0, direction="buy")
    assert result.verdict == "GO"
    assert result.lots is not None
    assert 0.01 <= result.lots <= 0.10


def test_risk_agent_lots_formula():
    # ATR=8: lots = (2000×0.01) / (1.5×8×100) = 0.01667
    agent = RiskAgent(account_size_usd=2000, risk_per_trade_pct=0.01)
    result = agent.evaluate("GOLD", entry=2300.0, atr=8.0, direction="buy")
    expected_raw  = (2000 * 0.01) / (1.5 * 8.0 * 100)
    expected_lots = max(0.01, min(expected_raw, 0.10))
    assert result.lots == pytest.approx(expected_lots, rel=1e-3)


def test_risk_agent_uses_min_lot_when_atr_large():
    # ATR=15: ideal lots = 0.0089 < MIN_LOTS=0.01 → trade the 0.01 minimum, GO
    agent = RiskAgent(account_size_usd=2000, risk_per_trade_pct=0.01)
    result = agent.evaluate("GOLD", entry=2300.0, atr=15.0, direction="buy")
    assert result.verdict == "GO"
    assert result.lots == pytest.approx(0.01)


def test_risk_agent_block_on_zero_atr():
    agent = RiskAgent(account_size_usd=2000, risk_per_trade_pct=0.01)
    result = agent.evaluate("GOLD", entry=2300.0, atr=0.0, direction="buy")
    assert result.verdict == "BLOCK"


def test_risk_agent_block_on_zero_entry():
    agent = RiskAgent(account_size_usd=2000, risk_per_trade_pct=0.01)
    result = agent.evaluate("GOLD", entry=0.0, atr=15.0, direction="buy")
    assert result.verdict == "BLOCK"


def test_risk_agent_index_contract():
    # US index: $1/point/lot → bigger lot sizes for same risk
    agent = RiskAgent(account_size_usd=2000, risk_per_trade_pct=0.01)
    result = agent.evaluate("US500", entry=5000.0, atr=20.0, direction="buy")
    assert result.verdict == "GO"
    # lots = (2000 × 0.01) / (1.5 × 20.0 × 1) = 20/30 ≈ 0.67 → capped at 0.10
    assert result.lots == pytest.approx(0.10, rel=1e-3)


# ── SentimentAgent ───────────────────────────────────────────────────────────

def test_score_post_bullish():
    assert _score_post("gold rally, bulls and bull market signal buy signal") == 1


def test_score_post_bearish():
    assert _score_post("gold crash, bears and bear market signal sell signal") == -1


def test_score_post_neutral():
    assert _score_post("gold trading sideways ahead of fed decision") == 0


def test_sentiment_go_when_no_posts():
    agent = SentimentAgent()
    with patch("agents.sentiment_agent._get_posts", return_value=[]):
        result = agent.evaluate("GOLD", "buy")
    assert result.verdict == "GO"
    assert result.agent == "sentiment"


def test_sentiment_go_when_aligned():
    import time as _t
    agent = SentimentAgent(bull_threshold=1)
    posts = [
        {"title": "gold bull market breakout, buy signal confirmed",   "pub_ts": _t.time() - 300, "source": "reddit"},
        {"title": "gold bullish surge, bulls targeting new highs",     "pub_ts": _t.time() - 600, "source": "news"},
    ]
    with patch("agents.sentiment_agent._get_posts", return_value=posts):
        result = agent.evaluate("GOLD", "buy")
    assert result.verdict == "GO"


def test_sentiment_hold_when_contradicts():
    import time as _t
    agent = SentimentAgent(bear_threshold=-1)
    posts = [
        {"title": "gold crash, bears and bear market send sell signal", "pub_ts": _t.time() - 300, "source": "reddit"},
        {"title": "gold bearish breakdown on precious metal",           "pub_ts": _t.time() - 600, "source": "news"},
    ]
    with patch("agents.sentiment_agent._get_posts", return_value=posts):
        result = agent.evaluate("GOLD", "buy")   # BUY vs bearish sentiment
    assert result.verdict == "HOLD"


def test_sentiment_go_on_old_posts():
    import time as _t
    agent = SentimentAgent(bear_threshold=-1)
    # Posts older than SENTIMENT_WINDOW_H (3h) are ignored
    old_posts = [
        {"title": "gold crash, bears and bear market signal",           "pub_ts": _t.time() - 4 * 3600, "source": "reddit"},
    ]
    with patch("agents.sentiment_agent._get_posts", return_value=old_posts):
        result = agent.evaluate("GOLD", "buy")
    assert result.verdict == "GO"


def test_sentiment_ignores_irrelevant_posts():
    import time as _t
    agent = SentimentAgent(bear_threshold=-1)
    posts = [
        # Bearish but mentions oil, not gold
        {"title": "oil crash, energy bears take control",               "pub_ts": _t.time() - 300, "source": "reddit"},
    ]
    with patch("agents.sentiment_agent._get_posts", return_value=posts):
        result = agent.evaluate("GOLD", "buy")
    assert result.verdict == "GO"


# ── Orchestrator ──────────────────────────────────────────────────────────────

def _mock_market(direction: str | None):
    agent = MagicMock()
    if direction:
        agent.evaluate.return_value = AgentVerdict(
            agent="market", verdict="GO", confidence=0.80,
            reason="sweep detected", direction=direction,
        )
    else:
        agent.evaluate.return_value = AgentVerdict(
            agent="market", verdict="HOLD", confidence=0.90,
            reason="no sweep",
        )
    return agent


def _mock_news(verdict: str):
    agent = MagicMock()
    agent.evaluate.return_value = AgentVerdict(
        agent="news", verdict=verdict, confidence=0.95,
        reason="test",
    )
    return agent


def _mock_sentiment(verdict: str = "GO"):
    agent = MagicMock()
    agent.evaluate.return_value = AgentVerdict(
        agent="sentiment", verdict=verdict, confidence=0.70,
        reason="test",
    )
    return agent


def _mock_risk(lots: float = 0.07):
    agent = MagicMock()
    agent.evaluate.return_value = AgentVerdict(
        agent="risk", verdict="GO", confidence=1.0,
        reason=f"{lots:.2f} lots", lots=lots,
    )
    return agent


def _candles_with_atr() -> dict:
    """Minimal candle set with enough bars for ATR to be computable."""
    return {TF_H1: flat_candles(50), TF_H4: flat_candles(250)}


def test_orchestrator_skip_when_market_hold():
    orch = Orchestrator(
        market_agent=_mock_market(None),
        news_agent=_mock_news("GO"),
        sentiment_agent=_mock_sentiment("GO"),
        risk_agent=_mock_risk(),
    )
    decision = orch.decide("GOLD", _candles_with_atr())
    assert decision.action == "skip"


def test_orchestrator_skip_when_news_hold():
    orch = Orchestrator(
        market_agent=_mock_market("buy"),
        news_agent=_mock_news("HOLD"),
        sentiment_agent=_mock_sentiment("GO"),
        risk_agent=_mock_risk(),
    )
    decision = orch.decide("GOLD", _candles_with_atr())
    assert decision.action == "skip"


def test_orchestrator_skip_when_news_block():
    orch = Orchestrator(
        market_agent=_mock_market("buy"),
        news_agent=_mock_news("BLOCK"),
        sentiment_agent=_mock_sentiment("GO"),
        risk_agent=_mock_risk(),
    )
    decision = orch.decide("GOLD", _candles_with_atr())
    assert decision.action == "skip"


def test_orchestrator_skip_when_sentiment_hold():
    orch = Orchestrator(
        market_agent=_mock_market("buy"),
        news_agent=_mock_news("GO"),
        sentiment_agent=_mock_sentiment("HOLD"),
        risk_agent=_mock_risk(),
    )
    decision = orch.decide("GOLD", _candles_with_atr())
    assert decision.action == "skip"


def test_orchestrator_fires_when_all_go():
    orch = Orchestrator(
        market_agent=_mock_market("buy"),
        news_agent=_mock_news("GO"),
        sentiment_agent=_mock_sentiment("GO"),
        risk_agent=_mock_risk(lots=0.07),
    )
    decision = orch.decide("GOLD", _candles_with_atr())
    assert decision.action == "buy"
    assert decision.lots == pytest.approx(0.07)
    assert decision.confidence > 0


def test_orchestrator_sell_signal():
    orch = Orchestrator(
        market_agent=_mock_market("sell"),
        news_agent=_mock_news("GO"),
        sentiment_agent=_mock_sentiment("GO"),
        risk_agent=_mock_risk(lots=0.05),
    )
    decision = orch.decide("GOLD", _candles_with_atr())
    assert decision.action == "sell"


def test_orchestrator_confidence_is_minimum():
    orch = Orchestrator(
        market_agent=_mock_market("buy"),    # 0.80
        news_agent=_mock_news("GO"),         # 0.95
        sentiment_agent=_mock_sentiment("GO"),  # 0.70
        risk_agent=_mock_risk(lots=0.07),    # 1.00
    )
    # min(0.80, 0.95, 0.70, 1.00) = 0.70
    decision = orch.decide("GOLD", _candles_with_atr())
    assert decision.confidence == pytest.approx(0.70)


def test_orchestrator_verdicts_attached():
    orch = Orchestrator(
        market_agent=_mock_market("buy"),
        news_agent=_mock_news("GO"),
        sentiment_agent=_mock_sentiment("GO"),
        risk_agent=_mock_risk(),
    )
    decision = orch.decide("GOLD", _candles_with_atr())
    assert len(decision.verdicts) == 4
    agents = {v.agent for v in decision.verdicts}
    assert agents == {"market", "news", "sentiment", "risk"}


def test_orchestrator_news_not_called_when_market_hold():
    news = _mock_news("GO")
    orch = Orchestrator(
        market_agent=_mock_market(None),
        news_agent=news,
        sentiment_agent=_mock_sentiment("GO"),
        risk_agent=_mock_risk(),
    )
    orch.decide("GOLD", _candles_with_atr())
    news.evaluate.assert_not_called()


def test_orchestrator_sentiment_not_called_when_news_blocks():
    sentiment = _mock_sentiment("GO")
    orch = Orchestrator(
        market_agent=_mock_market("buy"),
        news_agent=_mock_news("BLOCK"),
        sentiment_agent=sentiment,
        risk_agent=_mock_risk(),
    )
    orch.decide("GOLD", _candles_with_atr())
    sentiment.evaluate.assert_not_called()
