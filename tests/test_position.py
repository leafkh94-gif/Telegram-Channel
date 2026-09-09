"""
دورة حياة الصفقة في bot/position.py

يحرس التفاصيل التي أعطت سابقاً رقمين متناقضين لنفس الاستراتيجية على نفس
البيانات (‎+0.105R و ‎-0.021R)، وكلها تدور حول سؤال واحد: ماذا نفترض عن ترتيب
الأحداث داخل الشمعة الواحدة؟ الشمعة تعطي قمة وقاعاً بلا ترتيب زمني بينهما،
والافتراض المتفائل يجمّل النتيجة بلا سند.

كل حالة تُختبر شراءً وبيعاً، لأن الاتجاهين يمرّان في نفس الكود وأي انحراف
بينهما يجب أن يظهر هنا لا في التداول.
"""

import importlib
import os
import sys

import pytest


def _load_bot(*names):
    """
    يستورد وحدات bot دون أن يترك أثراً في حالة الاستيراد العامة.

    أسماء وحدات البوت (config مثلاً) قد تصطدم بأسماء في جذر المستودع، فإبقاء
    المسار أو مدخل sys.modules بعد الاستيراد يجعل ملفات اختبار أخرى تستورد
    الملف الخطأ.
    """
    path   = os.path.join(os.path.dirname(__file__), "..", "bot")
    before = set(sys.modules)
    sys.path.insert(0, path)
    try:
        return [importlib.import_module(n) for n in names]
    finally:
        sys.path.remove(path)
        for name in set(sys.modules) - before:
            del sys.modules[name]


_config, _position = _load_bot("config", "position")

STOP_ATR, TRAIL_ATR = _config.STOP_ATR, _config.TRAIL_ATR
PARTIAL_TARGET_ATR  = _config.PARTIAL_TARGET_ATR
PARTIAL_FRACTION    = _config.PARTIAL_FRACTION
open_position, update = _position.open_position, _position.update
risk_unit, stop_price, sign = (_position.risk_unit, _position.stop_price,
                               _position.sign)

BOTH = pytest.mark.parametrize("side", ["BUY", "SELL"])


def bar(fav, adv, entry=100.0, s=1):
    """
    شمعة معبَّر عنها بمسافتَي الربح والخسارة، فتقرأ الحالة نفسها للاتجاهين.

    fav موجب = تحرّك لصالحنا، adv سالب = تحرّك ضدّنا.
    """
    if s > 0:
        hi, lo = entry + fav, entry + adv
    else:
        hi, lo = entry - adv, entry - fav
    return {"high": hi, "low": lo, "close": (hi + lo) / 2,
            "open": lo, "time": "2026-01-01T00:00:00"}


@pytest.fixture
def pos_for():
    """دخول 100 و ATR=1 ⇒ مخاطرة 1.5، هدف جزئي عند 1.0، وقف عند ‎-1.5."""
    def make(side):
        return open_position("TEST", side, 100.0, 1.0)
    return make


@BOTH
def test_initial_stop_is_stop_atr_away(pos_for, side):
    p = pos_for(side)
    assert p.stop_d == -STOP_ATR
    assert risk_unit(p) == STOP_ATR
    assert stop_price(p) == pytest.approx(100.0 - sign(p) * STOP_ATR)
    assert p.size == 1.0 and p.booked == 0.0


@BOTH
def test_stop_checked_before_trail_rises(pos_for, side):
    """شمعة تركض لصالحنا ثم تخترق الوقف تُقرأ خاسرة — لا نفترض أيّهما أولاً."""
    p = pos_for(side)
    _, ex = update(p, bar(fav=20.0, adv=-3.0, s=sign(p)))
    assert ex is not None and ex.reason == "SL"
    assert ex.r == pytest.approx(-1.0)


@BOTH
def test_candle_touching_both_target_and_stop_is_a_loss(pos_for, side):
    p = pos_for(side)
    p, ex = update(p, bar(fav=1.5, adv=-1.6, s=sign(p)))
    assert ex is not None and ex.reason == "SL"
    assert p.partial_price is None          # لا جني جزئي على شمعة خاسرة


@BOTH
def test_partial_books_half_and_moves_stop_to_entry(pos_for, side):
    p = pos_for(side)
    p, ex = update(p, bar(fav=1.2, adv=-0.5, s=sign(p)))
    assert ex is None
    assert p.partial_price == pytest.approx(100.0 + sign(p) * PARTIAL_TARGET_ATR)
    assert p.size == pytest.approx(1.0 - PARTIAL_FRACTION)
    assert stop_price(p) == pytest.approx(100.0)          # الباقي بلا مخاطرة
    assert p.booked == pytest.approx(PARTIAL_FRACTION * PARTIAL_TARGET_ATR / STOP_ATR)


@BOTH
def test_partial_then_back_to_entry_is_a_net_win(pos_for, side):
    """سبب وجود الجني الجزئي: صفقة كانت ستخرج بـ‎-1R تخرج موجبة."""
    p = pos_for(side)
    p, _  = update(p, bar(fav=1.2, adv=-0.5, s=sign(p)))
    p, ex = update(p, bar(fav=1.3, adv=-0.01, s=sign(p)))
    assert ex is not None and ex.r > 0
    assert ex.r == pytest.approx(PARTIAL_FRACTION * PARTIAL_TARGET_ATR / STOP_ATR)


@BOTH
def test_partial_taken_only_once(pos_for, side):
    p = pos_for(side)
    p, _ = update(p, bar(fav=1.2, adv=-0.5, s=sign(p)))
    booked = p.booked
    p, _ = update(p, bar(fav=5.0, adv=0.5, s=sign(p)))
    assert p.booked == booked
    assert p.size == pytest.approx(1.0 - PARTIAL_FRACTION)


@BOTH
def test_trailing_stop_never_retreats(pos_for, side):
    p = pos_for(side)
    p, _ = update(p, bar(fav=10.0, adv=-0.5, s=sign(p)))
    raised = p.stop_d
    assert raised == pytest.approx(10.0 - TRAIL_ATR)
    p, _ = update(p, bar(fav=4.0, adv=3.0, s=sign(p)))   # قمة أدنى
    assert p.stop_d == raised


@BOTH
def test_runner_keeps_upside_after_partial(pos_for, side):
    """النصف الباقي يتبع بلا سقف — هو ما يدفع ثمن الصفقات الخاسرة."""
    p = pos_for(side)
    for step in (1.2, 6.0, 11.0, 16.0):
        p, ex = update(p, bar(fav=step, adv=p.stop_d + 0.01, s=sign(p)))
        assert ex is None
    p, ex = update(p, bar(fav=16.0, adv=p.stop_d - 0.01, s=sign(p)))
    assert ex.r > p.booked            # الباقي أضاف فوق المحقّق


@BOTH
def test_long_and_short_are_symmetric(pos_for, side):
    """نفس المسار بوحدات الربح يجب أن يعطي نفس R للشراء والبيع."""
    results = []
    for s in ("BUY", "SELL"):
        p = open_position("TEST", s, 100.0, 1.0)
        for fav, adv in ((1.4, -0.6), (3.0, 0.2), (3.1, -5.0)):
            p, ex = update(p, bar(fav=fav, adv=adv, s=sign(p)))
            if ex:
                results.append(round(ex.r, 9))
                break
    assert len(results) == 2 and results[0] == results[1]
