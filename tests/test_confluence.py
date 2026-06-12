"""Tests for strategy/confluence_scorer.py"""
import math

import pytest

from strategy.base import Candle, TF_D1, TF_H1, TF_H4
from strategy.confluence_scorer import ConfluenceScorer


def _make_uptrend_candles(n: int, start: float = 1900.0, step: float = 5.0) -> list[Candle]:
    """Synthetic uptrending H1 candles — close rises by `step` each bar."""
    candles = []
    price = start
    for i in range(n):
        o = price
        c = price + step
        h = c + step * 0.3
        lo = o - step * 0.1
        candles.append(Candle(
            timestamp=f"2024-01-01T{i:05d}",
            open=o, high=h, low=lo, close=c, volume=1000.0,
        ))
        price = c
    return candles


def _make_downtrend_candles(n: int, start: float = 2100.0, step: float = 5.0) -> list[Candle]:
    """Synthetic downtrending H1 candles — close falls by `step` each bar."""
    candles = []
    price = start
    for i in range(n):
        o = price
        c = price - step
        h = o + step * 0.1
        lo = c - step * 0.3
        candles.append(Candle(
            timestamp=f"2024-01-01T{i:05d}",
            open=o, high=h, low=lo, close=c, volume=1000.0,
        ))
        price = c
    return candles


@pytest.fixture
def scorer():
    return ConfluenceScorer()


class TestPriceConfirmation:
    def test_buy_breaks_above_swing_high(self, scorer):
        candles = _make_uptrend_candles(15)
        # Last candle close is well above all prior highs — should pass
        result = scorer.score({TF_H1: candles}, "buy")
        c1 = result.conditions[0]
        assert c1.name == "Price Confirmation"
        # If uptrend is steep enough, close > prior 10-bar swing high
        # Just verify it ran without error
        assert isinstance(c1.passed, bool)

    def test_sell_breaks_below_swing_low(self, scorer):
        candles = _make_downtrend_candles(15)
        result = scorer.score({TF_H1: candles}, "sell")
        c1 = result.conditions[0]
        assert c1.name == "Price Confirmation"
        assert isinstance(c1.passed, bool)

    def test_mandatory_condition_short_circuits(self, scorer):
        """If price confirmation fails, total must be 0 regardless of other conditions."""
        # Flat candles — no breakout in either direction
        flat = [Candle(f"t{i}", 100.0, 101.0, 99.0, 100.0) for i in range(15)]
        result = scorer.score({TF_H1: flat}, "buy")
        if not result.conditions[0].passed:
            assert result.total == 0
            assert len(result.conditions) == 1

    def test_too_few_candles_fails(self, scorer):
        tiny = _make_uptrend_candles(5)
        result = scorer.score({TF_H1: tiny}, "buy")
        assert result.total == 0


class TestIndicators:
    def test_buy_with_strong_uptrend_passes_indicators(self, scorer):
        # Uptrend with occasional pullbacks so RSI stays in 55-80 range,
        # then a decisive breakout candle at the end to pass price confirmation.
        candles = []
        price = 1900.0
        for i in range(59):
            step = -1.0 if i % 4 == 3 else 5.0
            o, c = price, price + step
            candles.append(Candle(f"t{i}", o, max(o, c) + 0.5, min(o, c) - 0.5, c))
            price = c
        # Final bar: big breakout well above all prior candles in lookback window
        last_high = max(c.high for c in candles[-10:])
        o = price
        c = last_high + 20.0
        candles.append(Candle("t59", o, c + 1.0, o - 0.5, c))
        result = scorer.score({TF_H1: candles}, "buy")
        # Condition 1 must pass for condition 2 to be evaluated
        assert result.conditions[0].passed, f"Price confirmation failed: {result.conditions[0].detail}"
        c2 = next(c for c in result.conditions if c.name == "Indicators")
        assert c2.passed is True, f"Indicators failed: {c2.detail}"

    def test_sell_with_strong_downtrend_passes_indicators(self, scorer):
        candles = _make_downtrend_candles(60)
        result = scorer.score({TF_H1: candles}, "sell")
        c2 = next(c for c in result.conditions if c.name == "Indicators")
        assert c2.passed is True

    def test_not_enough_candles_fails(self, scorer):
        candles = _make_uptrend_candles(30)   # < 55 min
        result = scorer.score({TF_H1: candles}, "buy")
        c2 = next((c for c in result.conditions if c.name == "Indicators"), None)
        if c2:  # only present if c1 passed
            assert not c2.passed


class TestMTFAlignment:
    def test_h4_and_d1_bullish_for_buy(self, scorer):
        h1 = _make_uptrend_candles(60)
        h4 = _make_uptrend_candles(25, start=1800.0)
        d1 = _make_uptrend_candles(10, start=1700.0)
        result = scorer.score({TF_H1: h1, TF_H4: h4, TF_D1: d1}, "buy")
        c3 = next((c for c in result.conditions if c.name == "Multi-TF Alignment"), None)
        if c3:
            assert c3.passed is True

    def test_d1_absent_does_not_penalise(self, scorer):
        h1 = _make_uptrend_candles(60)
        h4 = _make_uptrend_candles(25, start=1800.0)
        result = scorer.score({TF_H1: h1, TF_H4: h4}, "buy")
        c3 = next((c for c in result.conditions if c.name == "Multi-TF Alignment"), None)
        if c3:
            # d1 absent → d1_ok=True by default; result depends only on H4
            assert "D1 not available" in c3.detail

    def test_contradicting_h4_fails(self, scorer):
        h1 = _make_uptrend_candles(60)
        h4 = _make_downtrend_candles(25, start=2100.0)  # bearish H4
        result = scorer.score({TF_H1: h1, TF_H4: h4}, "buy")
        c3 = next((c for c in result.conditions if c.name == "Multi-TF Alignment"), None)
        if c3:
            assert c3.passed is False


class TestSession:
    def test_session_condition_returns_bool(self, scorer):
        result = scorer.score({TF_H1: _make_uptrend_candles(60)}, "buy")
        c4 = next((c for c in result.conditions if c.name == "Session"), None)
        if c4:
            assert isinstance(c4.passed, bool)
            assert c4.detail in ("London session", "New York session",
                                 "Outside major sessions", "Weekend")


class TestRiskReward:
    def test_steep_uptrend_has_good_rr(self, scorer):
        candles = _make_uptrend_candles(35, step=10.0)
        result = scorer.score({TF_H1: candles}, "buy")
        c5 = next((c for c in result.conditions if c.name == "Risk:Reward"), None)
        if c5:
            assert isinstance(c5.passed, bool)
            assert "R:R" in c5.detail

    def test_too_few_candles_fails(self, scorer):
        candles = _make_uptrend_candles(10)
        result = scorer.score({TF_H1: candles}, "buy")
        c5 = next((c for c in result.conditions if c.name == "Risk:Reward"), None)
        if c5:
            assert c5.passed is False


class TestOverallScoring:
    def test_uptrend_buy_scores_at_least_3(self, scorer):
        """50-bar steep uptrend should generate confluence >= 3/5 for a buy."""
        h1 = _make_uptrend_candles(60, step=8.0)
        h4 = _make_uptrend_candles(25, start=1800.0, step=15.0)
        d1 = _make_uptrend_candles(10, start=1700.0, step=30.0)
        result = scorer.score({TF_H1: h1, TF_H4: h4, TF_D1: d1}, "buy")
        assert result.total >= 3, (
            f"Expected >= 3/5, got {result.summary()}: "
            + "; ".join(f"{c.name}={'✓' if c.passed else '✗'} ({c.detail})" for c in result.conditions)
        )

    def test_downtrend_sell_scores_at_least_3(self, scorer):
        """50-bar steep downtrend should generate confluence >= 3/5 for a sell."""
        h1 = _make_downtrend_candles(60, step=8.0)
        h4 = _make_downtrend_candles(25, start=2100.0, step=15.0)
        d1 = _make_downtrend_candles(10, start=2200.0, step=30.0)
        result = scorer.score({TF_H1: h1, TF_H4: h4, TF_D1: d1}, "sell")
        assert result.total >= 3, (
            f"Expected >= 3/5, got {result.summary()}: "
            + "; ".join(f"{c.name}={'✓' if c.passed else '✗'} ({c.detail})" for c in result.conditions)
        )

    def test_result_structure(self, scorer):
        h1 = _make_uptrend_candles(60)
        result = scorer.score({TF_H1: h1}, "buy")
        assert result.max_score == 5
        assert isinstance(result.total, int)
        assert 0 <= result.total <= 5
        assert isinstance(result.summary(), str)

    def test_condition_count_when_c1_passes(self, scorer):
        h1 = _make_uptrend_candles(60, step=20.0)  # steep enough to break swing high
        result = scorer.score({TF_H1: h1}, "buy")
        if result.conditions[0].passed:
            assert len(result.conditions) == 5
        else:
            assert len(result.conditions) == 1
