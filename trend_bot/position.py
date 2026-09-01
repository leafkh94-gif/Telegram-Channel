"""
position.py — دورة حياة الصفقة، مصدر وحيد للحقيقة

سبب وجود هذا الملف:
كان الباك-تست يدير الصفقة بكود، والبوت الحيّ بكود آخر. الاثنان يفترض أن يفعلا
الشيء نفسه، لكنهما اختلفا في تفصيلتين، فأعطيا نتيجتين متناقضتين على نفس البيانات
(‎+0.105R مقابل ‎-0.021R). أي رقم يُقاس بكود غير الذي سيعمل فعلاً هو رقم عن شيء
آخر. من الآن: الباك-تست والبوت يستدعيان الدوال هنا نفسها.

التفصيلتان اللتان تسبّبتا في الاختلاف، ومُثبّتتان هنا صراحةً:

1. ترتيب تحديث الوقف مقابل فحص الخروج داخل الشمعة الواحدة.
   الشمعة تعطينا قمة وقاعاً بلا ترتيب زمني بينهما. إن حدّثنا الوقف المتحرّك
   بقمة هذه الشمعة ثم فحصنا القاع، نكون افترضنا أن القمة جاءت أولاً — وهذا
   نظر إلى المستقبل يجمّل النتيجة. هنا نفحص الخروج أولاً مقابل وقف الشمعة
   السابقة، ثم نحدّث التتبّع. القراءة المتحفّظة هي الصادقة.

2. مدة الاحتفاظ القصوى. كان أحد التنفيذين يغلق بعد 240 شمعة والآخر لا يغلق
   أبداً. الآن سياسة واحدة صريحة قابلة للضبط (None = نحتفظ حتى يُضرب الوقف).
"""

from dataclasses import dataclass
from typing import Optional

from config import STOP_ATR, TRAIL_ATR, MAX_HOLD_BARS


@dataclass
class Position:
    symbol:  str
    entry:   float
    stop:    float
    highest: float          # أعلى قمة منذ الدخول — أساس الوقف المتحرّك
    atr:     float          # ATR لحظة الدخول؛ ثابت طوال الصفقة
    bars:    int = 0
    opened_at: str = ""


@dataclass
class Exit:
    reason: str             # "SL" عند ضرب الوقف، "MAXHOLD" عند انتهاء المدة
    price:  float
    r:      float           # النتيجة بوحدات المخاطرة الأولية


def open_position(symbol: str, fill: float, atr: float,
                  opened_at: str = "") -> Position:
    """المخاطرة الأولية = STOP_ATR × ATR، وهي وحدة قياس R لهذه الصفقة."""
    return Position(symbol=symbol, entry=fill, stop=fill - STOP_ATR * atr,
                    highest=fill, atr=atr, opened_at=opened_at)


def risk_unit(pos: Position) -> float:
    return STOP_ATR * pos.atr


def update(pos: Position, candle: dict, spread: float = 0.0
           ) -> tuple[Position, Optional[Exit]]:
    """
    يتقدّم بالصفقة شمعةً واحدة.

    الترتيب مقصود: الخروج يُفحص مقابل الوقف **الحالي** (المحسوب من الشموع
    السابقة) قبل أن يرى الوقف قمة هذه الشمعة. عكسه يفترض أن القمة سبقت القاع.
    """
    pos.bars += 1

    # 1) الخروج أولاً، بالوقف الموروث من الشمعة السابقة
    if candle["low"] - spread / 2 <= pos.stop:
        r = ((pos.stop - pos.entry) - spread) / risk_unit(pos)
        return pos, Exit("SL", pos.stop, r)

    # 2) ثم يرتفع الوقف المتحرّك — ولا ينزل أبداً
    pos.highest = max(pos.highest, candle["high"])
    pos.stop    = max(pos.stop, pos.highest - TRAIL_ATR * pos.atr)

    # 3) مدة الاحتفاظ القصوى، إن وُجدت
    if MAX_HOLD_BARS is not None and pos.bars >= MAX_HOLD_BARS:
        px = candle["close"]
        r  = ((px - pos.entry) - spread) / risk_unit(pos)
        return pos, Exit("MAXHOLD", px, r)

    return pos, None
