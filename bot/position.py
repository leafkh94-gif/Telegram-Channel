"""
position.py — دورة حياة الصفقة، مصدر وحيد للحقيقة

سبب وجود هذا الملف في مكان واحد:
في نسخة سابقة كانت إدارة الصفقة مكتوبة في الباك-تست وفي البوت الحيّ. الاثنان
يُفترض أن يفعلا الشيء نفسه، لكنهما اختلفا في تفصيلتين فأعطيا نتيجتين
متناقضتين على نفس البيانات (‎+0.105R و ‎-0.021R). أي رقم يُقاس بكود غير الذي
سيعمل فعلاً هو رقم عن شيء آخر.

ثلاثة قرارات مُثبَّتة هنا صراحةً:

1. الترتيب داخل الشمعة: الوقف، ثم الجني الجزئي، ثم رفع التتبّع.
   الشمعة تعطينا قمة وقاعاً بلا ترتيب زمني بينهما. إن رفعنا التتبّع بقمة هذه
   الشمعة ثم فحصنا قاعها، نكون افترضنا أن القمة جاءت أولاً — وهذا نظر إلى
   المستقبل يجمّل النتيجة. شمعة تلمس الهدف والوقف معاً تُقرأ خاسرة.

2. الشراء والبيع يمرّان في نفس الكود.
   كل المسافات بوحدات الربح: موجبة لصالحنا مهما كان الاتجاه. فرعان منفصلان
   يفترق سلوكهما بصمت بعد أول تعديل يُطبَّق على أحدهما فقط.

3. الجني الجزئي جزء من الاستراتيجية لا زينة فوقها.
   نصف الكمية عند 1×ATR، ووقف الباقي إلى الدخول. هذا ما يرفع نسبة الإصابة
   إلى 62.7%، وهو مقيس ضمن الأرقام في config.py لا مضاف بعدها.
"""

from dataclasses import dataclass
from typing import Optional

from config import (
    STOP_ATR, TRAIL_ATR, PARTIAL_TARGET_ATR, PARTIAL_FRACTION,
)


@dataclass
class Position:
    symbol:  str
    side:    str            # "BUY" أو "SELL"
    entry:   float
    atr:     float          # ATR لحظة الدخول؛ ثابت طوال الصفقة
    stop_d:  float          # مسافة الوقف بوحدات الربح (سالبة = خسارة)
    peak:    float = 0.0    # أقصى ربح بلغته الصفقة، بوحدات السعر
    bars:    int   = 0
    size:    float = 1.0    # ما تبقّى من الكمية بعد الجني الجزئي
    booked:  float = 0.0    # R محقّقة فعلاً من الجني الجزئي
    partial_price: Optional[float] = None
    opened_at: str = ""


@dataclass
class Exit:
    reason: str             # "SL" — الوقف، ثابتاً كان أو متحرّكاً
    price:  float
    r:      float           # النتيجة الكاملة بوحدات المخاطرة الأولية


def sign(pos: Position) -> int:
    return 1 if pos.side == "BUY" else -1


def risk_unit(pos: Position) -> float:
    return STOP_ATR * pos.atr


def stop_price(pos: Position) -> float:
    """الوقف كسعر معروض للمستخدم. داخلياً نحتفظ به كمسافة لتوحيد الاتجاهين."""
    return pos.entry + sign(pos) * pos.stop_d


def open_position(symbol: str, side: str, fill: float, atr: float,
                  opened_at: str = "") -> Position:
    return Position(symbol=symbol, side=side, entry=fill, atr=atr,
                    stop_d=-STOP_ATR * atr, opened_at=opened_at)


def update(pos: Position, candle: dict, spread: float = 0.0
           ) -> tuple[Position, Optional[Exit]]:
    """يتقدّم بالصفقة شمعةً واحدة. انظر الترتيب المشروح في رأس الملف."""
    pos.bars += 1

    if sign(pos) > 0:
        fav, adv = candle["high"] - pos.entry, candle["low"] - pos.entry
    else:
        fav, adv = pos.entry - candle["low"], pos.entry - candle["high"]

    # 1) الخروج أولاً، بالوقف الموروث من الشمعة السابقة
    if adv - spread / 2 <= pos.stop_d:
        r = pos.booked + pos.size * (pos.stop_d - spread) / risk_unit(pos)
        return pos, Exit("SL", stop_price(pos), r)

    # 2) الجني الجزئي، ووقف الباقي إلى الدخول
    if PARTIAL_TARGET_ATR is not None and pos.partial_price is None:
        target = PARTIAL_TARGET_ATR * pos.atr
        if fav >= target:
            pos.booked += PARTIAL_FRACTION * (target - spread) / risk_unit(pos)
            pos.size   -= PARTIAL_FRACTION
            pos.stop_d  = max(pos.stop_d, 0.0)
            pos.partial_price = pos.entry + sign(pos) * target

    # 3) ثم يتقدّم الوقف المتحرّك — ولا يتراجع أبداً
    pos.peak   = max(pos.peak, fav)
    pos.stop_d = max(pos.stop_d, pos.peak - TRAIL_ATR * pos.atr)

    return pos, None
