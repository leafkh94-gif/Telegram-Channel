"""
قاعدة الدخول في bot/strategy.py

تحرس ثلاثة أشياء انكسرت فعلاً في نسخ سابقة:
  • القناة تُبنى من الشموع السابقة للحالية — إدراج الحالية يجعل الاختراق
    مستحيلاً لأن إغلاقها لا يتجاوز قمّتها أبداً.
  • الاتجاه اليومي يحكم جهة الصفقة، فلا شراء في اتجاه هابط ولا العكس.
  • الشموع المُمرَّرة مغلقة؛ الدالة لا تُقيّم شمعة قيد التكوّن.
"""

import importlib
import os
import sys

import pytest


def _load_bot(*names):
    path   = os.path.join(os.path.dirname(__file__), "..", "bot")
    before = set(sys.modules)
    sys.path.insert(0, path)
    try:
        return [importlib.import_module(n) for n in names]
    finally:
        sys.path.remove(path)
        for name in set(sys.modules) - before:
            del sys.modules[name]


_config, _strategy = _load_bot("config", "strategy")
scan       = _strategy.scan
CHANNEL    = _config.ENTRY_CHANNEL_H1
DAILY_EMA  = _config.DAILY_EMA
STOP_ATR   = _config.STOP_ATR


def candles(closes, spread=2.0):
    """شموع بسيطة: القمة والقاع على بُعد نصف spread من الإغلاق."""
    return [{"time": f"2026-01-{1 + i // 24:02d}T{i % 24:02d}:00:00",
             "open": c, "close": c,
             "high": c + spread / 2, "low": c - spread / 2}
            for i, c in enumerate(closes)]


def rising_daily(n=40, start=100.0, step=1.0):
    return candles([start + i * step for i in range(n)])


def falling_daily(n=40, start=140.0, step=1.0):
    return candles([start - i * step for i in range(n)])


def flat_h1(n=60, level=100.0):
    return candles([level] * n)


def test_no_signal_without_breakout():
    """سوق صاعد يومياً لكن ساعة مسطّحة — لا اختراق فلا إشارة."""
    assert scan("T", rising_daily(), flat_h1()) is None


def test_buy_on_upside_breakout_in_uptrend():
    h1 = flat_h1()
    h1[-1] = dict(h1[-1], close=110.0, high=110.5)      # اختراق واضح
    sig = scan("T", rising_daily(), h1)
    assert sig is not None and sig.direction == "BUY"
    assert sig.entry == 110.0
    assert sig.sl < sig.entry
    assert sig.sl == pytest.approx(round(110.0 - STOP_ATR * sig.atr, 2), abs=0.02)


def test_sell_on_downside_breakout_in_downtrend():
    h1 = flat_h1()
    h1[-1] = dict(h1[-1], close=90.0, low=89.5)
    sig = scan("T", falling_daily(), h1)
    assert sig is not None and sig.direction == "SELL"
    assert sig.sl > sig.entry


def test_uptrend_never_produces_a_sell():
    """الاتجاه اليومي يحكم الجهة — اختراق هابط في اتجاه صاعد لا يُشترى ولا يُباع."""
    h1 = flat_h1()
    h1[-1] = dict(h1[-1], close=90.0, low=89.5)
    assert scan("T", rising_daily(), h1) is None


def test_downtrend_never_produces_a_buy():
    h1 = flat_h1()
    h1[-1] = dict(h1[-1], close=110.0, high=110.5)
    assert scan("T", falling_daily(), h1) is None


def channel_high(h1):
    """المستوى الذي تراه الاستراتيجية: أعلى قمة في الشموع السابقة للحالية."""
    return max(c["high"] for c in h1[-(CHANNEL + 1):-1])


def test_channel_excludes_the_current_candle():
    """
    لو دخلت الشمعة الحالية في القناة لصار الاختراق مستحيلاً: إغلاقها لا
    يتجاوز قمّتها أبداً. هذه الحالة كانت تُسكت الاستراتيجية تماماً.
    """
    h1    = flat_h1()
    level = channel_high(h1)
    # إغلاق يتجاوز القناة بالكاد، وقمّته هي نفسها — لو أُدرجت الشمعة الحالية
    # في القناة لصار المستوى مساوياً لهذا الإغلاق ولما تحقّق الاختراق.
    h1[-1] = dict(h1[-1], close=level + 0.1, high=level + 0.1)
    assert scan("T", rising_daily(), h1) is not None


def test_insufficient_history_returns_none():
    assert scan("T", rising_daily(), flat_h1(n=10)) is None
    assert scan("T", rising_daily(n=5), flat_h1()) is None


def test_breakout_must_exceed_channel_not_merely_touch_it():
    h1     = flat_h1()
    level  = channel_high(h1)
    h1[-1] = dict(h1[-1], close=level, high=level)   # يساوي القناة تماماً
    assert scan("T", rising_daily(), h1) is None
