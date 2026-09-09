"""
دورة حياة الصفقة في trend_bot/position.py

هذا الملف يحرس التفاصيل التي أعطت سابقاً رقمين متناقضين لنفس الاستراتيجية على
نفس البيانات (‎+0.105R و ‎-0.021R). كلها تدور حول سؤال واحد: ماذا نفترض عن
ترتيب الأحداث داخل الشمعة الواحدة؟ الشمعة تعطينا قمة وقاعاً بلا ترتيب زمني
بينهما، والافتراض المتفائل يجمّل النتيجة بلا سند.
"""

import importlib
import os
import sys

import pytest


def _load_trend_bot(*names):
    """
    يستورد وحدات trend_bot دون أن يترك أثراً في حالة الاستيراد العامة.

    trend_bot/config.py يحمل اسم حزمة config الموجودة في جذر المستودع، فإبقاء
    المسار أو مدخل sys.modules بعد الاستيراد يجعل ملفات اختبار أخرى تستورد
    الملف الخطأ — وهو ما كسر test_secrets.py فعلاً عند كتابة هذا الملف.
    """
    path   = os.path.join(os.path.dirname(__file__), "..", "trend_bot")
    before = set(sys.modules)
    sys.path.insert(0, path)
    try:
        return [importlib.import_module(n) for n in names]
    finally:
        sys.path.remove(path)
        for name in set(sys.modules) - before:
            del sys.modules[name]


_config, _position = _load_trend_bot("config", "position")

STOP_ATR, TRAIL_ATR = _config.STOP_ATR, _config.TRAIL_ATR
PARTIAL_TARGET_ATR  = _config.PARTIAL_TARGET_ATR
PARTIAL_FRACTION    = _config.PARTIAL_FRACTION
open_position, update, risk_unit = (_position.open_position, _position.update,
                                    _position.risk_unit)


def candle(high, low, close=None):
    return {"high": high, "low": low, "close": close if close is not None else low,
            "open": low, "time": "2026-01-01T00:00:00"}


@pytest.fixture
def pos():
    """دخول عند 100 و ATR = 1 ⇒ وقف 98، هدف جزئي 101، وحدة مخاطرة 2."""
    return open_position("TEST", 100.0, 1.0)


def test_initial_stop_is_stop_atr_below_entry(pos):
    assert pos.stop == 100.0 - STOP_ATR
    assert risk_unit(pos) == STOP_ATR
    assert pos.size == 1.0 and pos.booked == 0.0


def test_stop_checked_before_trail_rises(pos):
    """
    شمعة ترتفع كثيراً ثم تهبط تحت الوقف يجب أن تُقرأ خاسرة.

    لو رفعنا التتبّع بقمة هذه الشمعة قبل فحص قاعها، لكنّا افترضنا أن القمة
    جاءت أولاً — وهو نظر إلى المستقبل.
    """
    _, ex = update(pos, candle(high=120.0, low=97.0))
    assert ex is not None and ex.reason == "SL"
    assert ex.r == pytest.approx(-1.0)


def test_candle_touching_both_target_and_stop_is_a_loss(pos):
    """الشمعة التي تلمس الهدف والوقف معاً تُقرأ خاسرة — القراءة المتحفّظة."""
    p, ex = update(pos, candle(high=101.5, low=97.9))
    assert ex is not None and ex.reason == "SL"
    assert p.partial_price is None          # لا جني جزئي على شمعة خاسرة


def test_partial_books_half_and_moves_stop_to_entry(pos):
    p, ex = update(pos, candle(high=101.5, low=99.5))
    assert ex is None
    assert p.partial_price == 100.0 + PARTIAL_TARGET_ATR
    assert p.size == pytest.approx(1.0 - PARTIAL_FRACTION)
    assert p.stop == 100.0                  # الباقي بلا مخاطرة
    # نصف الكمية × (1×ATR ÷ وحدة مخاطرة 2×ATR)
    assert p.booked == pytest.approx(PARTIAL_FRACTION * PARTIAL_TARGET_ATR / STOP_ATR)


def test_partial_then_back_to_entry_is_a_net_win(pos):
    """
    هذا هو سبب وجود الخروج الجزئي كله: صفقة كانت ستُخرج بـ‎-1R تخرج موجبة.
    """
    p, _ = update(pos, candle(high=101.5, low=99.5))
    p, ex = update(p, candle(high=101.6, low=99.99))
    assert ex is not None and ex.reason == "SL"
    assert ex.r == pytest.approx(0.25) and ex.r > 0


def test_partial_taken_only_once(pos):
    p, _ = update(pos, candle(high=101.5, low=99.5))
    booked_after_first = p.booked
    p, _ = update(p, candle(high=105.0, low=100.5))
    assert p.booked == booked_after_first
    assert p.size == pytest.approx(1.0 - PARTIAL_FRACTION)


def test_trailing_stop_never_falls(pos):
    p, _ = update(pos, candle(high=110.0, low=99.5))
    raised = p.stop
    assert raised == pytest.approx(110.0 - TRAIL_ATR)
    p, _ = update(p, candle(high=104.0, low=103.0))   # قمة أدنى
    assert p.stop == raised


def test_runner_keeps_full_upside_after_partial(pos):
    """النصف الباقي يتبع بلا سقف — هذا ما يدفع ثمن الصفقات الخاسرة."""
    p = pos
    for _ in range(3):
        p, ex = update(p, candle(high=p.highest + 10.0, low=p.stop + 0.01))
        assert ex is None
    p, ex = update(p, candle(high=p.highest, low=p.stop - 0.01))
    assert ex.r > p.booked          # الباقي أضاف فوق المحقّق
