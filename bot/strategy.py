"""
strategy.py — الدخول: اتجاه يومي + اختراق قناة 24 ساعة، شراءً وبيعاً

    يومي  : ما اتجاه السوق؟   السعر مقابل EMA20
    ساعة  : متى ندخل؟         اختراق أعلى/أدنى 24 شمعة سابقة

طبقتان فقط. النسخ السابقة كان فيها مرشّح 4 ساعات وحارس ملاحقة على فريم 5
دقائق، وكلاهما محذوف هنا لا لأنه سيّئ بل لأنه غائب عن القياس الذي أنتج
الأرقام في config.py. كل شرط يُضاف بلا قياس يجعل البوت يتصرّف بغير ما قِيس.

لماذا هذه الأرقام: مسح 36 تركيبة على سنتين من شموع الساعة (قناة 8/12/24/48 ×
مرشّحات × اتجاه × ثلاثة أشكال خروج)، بخصم السبريد، بفترة تصميم 16 شهراً
وفترة عمياء 8 شهور. المطلوب كان 2-3 صفقات لكل أداة أسبوعياً، وهذه التركيبة
هي الوحيدة التي بلغت التكرار وبقيت موجبة خارج عيّنتها وبعد حذف أكبر صفقة.

توزيع الأفضلية غير متساوٍ ويجب ألا يُنسى: الذهب ‎+0.230R (t=3.08)، أما
US100 (t=0.60) و US30 (t=0.52) فموجبتان وغير مميّزتين عن الصفر إحصائياً.
"""

import logging
from dataclasses import dataclass
from typing import Optional

from indicators import calculate_ema, calculate_atr
from config import (
    DAILY_EMA, ENTRY_CHANNEL_H1, ATR_PERIOD, STOP_ATR, TRAIL_ATR, ALLOW_SHORT,
)

logger = logging.getLogger(__name__)


@dataclass
class Signal:
    symbol:      str
    direction:   str          # "BUY" أو "SELL"
    entry:       float
    sl:          float
    atr:         float        # ATR الساعة — وحدة الوقف والهدف والتتبّع
    channel:     float        # المستوى الذي اخترقناه
    daily_close: float
    daily_ema:   float


def _bias(daily: list[dict]) -> Optional[str]:
    """
    "BUY" فوق EMA20، "SELL" تحته، None إذا لم تكفِ البيانات.

    يُحسب على آخر شمعة يومية **مكتملة**؛ المتصل مسؤول عن إسقاط الجارية.
    """
    if len(daily) < DAILY_EMA + 5:
        return None
    ema = calculate_ema([c["close"] for c in daily], DAILY_EMA)
    if ema[-1] is None:
        return None
    return "BUY" if daily[-1]["close"] > ema[-1] else "SELL"


def scan(symbol: str, daily: list[dict], h1: list[dict]) -> Optional[Signal]:
    """
    تُستدعى بشموع **مغلقة** فقط.

    التقييم على شمعة قيد التكوّن يُنتج إشارات تظهر ثم تختفي — وهو ما أغرق
    المستخدم برسائل متناقضة في نسخة سابقة.
    """
    if len(h1) < ENTRY_CHANNEL_H1 + ATR_PERIOD + 5:
        return None

    side = _bias(daily)
    if side is None:
        logger.info(f"↔️ {symbol}: بيانات يومية غير كافية")
        return None
    if side == "SELL" and not ALLOW_SHORT:
        return None

    atr = calculate_atr(h1[-(ATR_PERIOD + 2):], ATR_PERIOD)
    if not atr or atr <= 0:
        return None

    # القناة من الشموع **السابقة** للحالية — إدراج الحالية يجعل الاختراق مستحيلاً
    window = h1[-(ENTRY_CHANNEL_H1 + 1):-1]
    close  = h1[-1]["close"]

    if side == "BUY":
        level = max(c["high"] for c in window)
        if close <= level:
            logger.info(f"↔️ {symbol}: صاعد، لا اختراق | {close:.2f} ≤ {level:.2f}")
            return None
        sl = close - STOP_ATR * atr
    else:
        level = min(c["low"] for c in window)
        if close >= level:
            logger.info(f"↔️ {symbol}: هابط، لا اختراق | {close:.2f} ≥ {level:.2f}")
            return None
        sl = close + STOP_ATR * atr

    ema_val = calculate_ema([c["close"] for c in daily], DAILY_EMA)[-1]
    logger.info(
        f"🎯 {symbol}: {side} | دخول {close:.2f} | وقف {sl:.2f} | "
        f"ATR {atr:.2f} | القناة {level:.2f}"
    )
    return Signal(
        symbol=symbol, direction=side,
        entry=round(close, 2), sl=round(sl, 2), atr=round(atr, 2),
        channel=round(level, 2),
        daily_close=round(daily[-1]["close"], 2),
        daily_ema=round(ema_val, 2),
    )
