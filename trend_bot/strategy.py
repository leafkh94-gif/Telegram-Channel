"""
strategy.py — استراتيجية اتجاهية متعددة الأطر (شراء فقط)

    يومي     : ما اتجاه السوق؟        السعر مقابل EMA20
    4 ساعات  : هل يؤكّد؟               EMA9 فوق EMA21
    ساعة     : متى ندخل؟               اختراق أعلى قناة 48 ساعة
    5 دقائق  : هل السعر ما زال قريباً؟ حارس ضد ملاحقة الشمعة
    الخروج   : وقف متحرّك 4×ATR ساعة، ووقف مبدئي 2×ATR

لماذا هذه الأرقام وليس غيرها — كلها من قياس على سنتين (~12,600 شمعة ساعة لكل
أداة)، والاختيار تمّ على أول 16 شهراً فقط ثم اختُبر على 10 شهور لم يرها التصميم:

  • أفضل 5 تركيبات داخل العيّنة بقيت كلها موجبة خارجها، وبعضها أقوى — أي أن
    النتيجة ليست ملاءمة. (قناة 48 / وقف 2 / تتبّع 4: ‎+0.056R داخل، ‎+0.105R خارج)

  • الشراء فقط. البيع خسر في الفترتين وفي كل تركيبة جُرّبت (40 تركيبة بوقف
    متحرّك وبهدف ثابت). السبب بنيوي لا عَرَضي: موجات الهبوط في هذه الأدوات
    تدوم 4.5-5.5 يوم مقابل 8.5-12 للصعود، وارتداداتها عنيفة، فلا يجد الوقف
    المتحرّك مدى يعمل فيه. وخلال أيام الهبوط اليومي نفسها كان السوق يرتفع
    تراكمياً (US100 ‎+35%) — أي أن "الهبوط" هنا تذبذب عنيف لا نزول متصل.

  • تأكيد الـ5 دقائق ليس لتحسين الدخول بل لمنع تسوئه. القياس غير متماثل بشدّة:
    دخول أفضل بـ0.10 ATR يضيف ‎+0.02R فقط، بينما دخول أسوأ بنفس المقدار يهبط
    بالنتيجة من ‎+0.077R إلى ‎-0.021R — أي يمحو الأفضلية كاملة. الدخول على إغلاق
    شمعة الساعة يعني الشراء بعد أن ركضت الشمعة، وهذا ما يحرسه هذا الشرط.

تحفّظ يجب أن يبقى ظاهراً: السنتان المتاحتان كانتا سوقاً صاعدة. الأفضلية المثبتة
هي "الشراء في اتجاه صاعد"، ولم تُختبر في سوق هابطة ممتدة.
"""

import logging
from dataclasses import dataclass
from typing import Optional

from indicators import calculate_ema, calculate_atr
from config import (
    DAILY_EMA, H4_FAST, H4_SLOW, ENTRY_CHANNEL_H1,
    STOP_ATR, TRAIL_ATR, MAX_CHASE_ATR, ATR_PERIOD,
)

logger = logging.getLogger(__name__)


@dataclass
class Signal:
    symbol:      str
    direction:   str          # دائماً "BUY" — انظر تعليل الشراء فقط أعلاه
    entry:       float
    sl:          float
    atr:         float        # ATR الساعة، وحدة الوقف والتتبّع
    channel_high: float
    daily_close: float
    daily_ema:   float
    trail_atr:   float


def _bias_daily(daily: list[dict]) -> Optional[bool]:
    """صاعد إذا أغلق اليوم الأخير المكتمل فوق EMA20. None إذا لا تكفي البيانات."""
    if len(daily) < DAILY_EMA + 5:
        return None
    ema = calculate_ema([c["close"] for c in daily], DAILY_EMA)
    if ema[-1] is None:
        return None
    return daily[-1]["close"] > ema[-1]


def _confirm_h4(h4: list[dict]) -> Optional[bool]:
    if len(h4) < H4_SLOW + 5:
        return None
    cl = [c["close"] for c in h4]
    f, s = calculate_ema(cl, H4_FAST), calculate_ema(cl, H4_SLOW)
    if f[-1] is None or s[-1] is None:
        return None
    return f[-1] > s[-1]


def scan(symbol: str, daily: list[dict], h4: list[dict],
         h1: list[dict], m5: list[dict]) -> Optional[Signal]:
    """
    تُستدعى بشموع **مغلقة** فقط. المتصل مسؤول عن إسقاط الشمعة الجارية —
    التقييم على شمعة قيد التكوّن يُنتج إشارات تظهر ثم تختفي.
    """
    if len(h1) < ENTRY_CHANNEL_H1 + ATR_PERIOD + 5:
        logger.info(f"↔️ {symbol}: شموع ساعة غير كافية ({len(h1)})")
        return None

    up = _bias_daily(daily)
    if up is None:
        logger.info(f"↔️ {symbol}: بيانات يومية غير كافية")
        return None
    if not up:
        logger.info(f"↔️ {symbol}: الاتجاه اليومي هابط — لا شراء")
        return None

    conf = _confirm_h4(h4)
    if conf is None:
        logger.info(f"↔️ {symbol}: بيانات 4 ساعات غير كافية")
        return None
    if not conf:
        logger.info(f"↔️ {symbol}: 4 ساعات لا تؤكّد الاتجاه اليومي")
        return None

    atr = calculate_atr(h1[-(ATR_PERIOD + 2):], ATR_PERIOD)
    if not atr or atr <= 0:
        return None

    # القناة من الشموع **السابقة** للشمعة الحالية — إدراجها يجعل الاختراق مستحيلاً
    channel = h1[-(ENTRY_CHANNEL_H1 + 1):-1]
    ch_high = max(c["high"] for c in channel)
    last    = h1[-1]

    if last["close"] <= ch_high:
        logger.info(
            f"↔️ {symbol}: لا اختراق | إغلاق {last['close']:.2f} ≤ "
            f"قناة {ch_high:.2f} (ينقص {ch_high - last['close']:.2f})"
        )
        return None

    # حارس الملاحقة: السعر الحيّ يجب ألا يكون قد ابتعد عن مستوى الاختراق.
    # هذا هو دور إطار الـ5 دقائق — القياس أظهر أن دخولاً أسوأ بـ0.10 ATR يمحو
    # الأفضلية بالكامل، فالحماية من الملاحقة أهم من تحسين الدخول.
    live = m5[-1]["close"] if m5 else last["close"]
    chase = (live - ch_high) / atr
    if chase > MAX_CHASE_ATR:
        logger.info(
            f"⏭️ {symbol}: اختراق ✓ لكن السعر ابتعد {chase:.2f}×ATR "
            f"(الحد {MAX_CHASE_ATR}) — لا نلاحق"
        )
        return None

    entry = live
    sl    = entry - STOP_ATR * atr
    logger.info(
        f"🎯 {symbol}: شراء | دخول {entry:.2f} | وقف {sl:.2f} | "
        f"ATR {atr:.2f} | قناة {ch_high:.2f} | ابتعاد {chase:+.2f}×ATR"
    )
    return Signal(
        symbol=symbol, direction="BUY",
        entry=round(entry, 2), sl=round(sl, 2), atr=round(atr, 2),
        channel_high=round(ch_high, 2),
        daily_close=round(daily[-1]["close"], 2),
        daily_ema=round(calculate_ema([c["close"] for c in daily], DAILY_EMA)[-1], 2),
        trail_atr=TRAIL_ATR,
    )


def trail_stop(entry: float, current_stop: float, highest: float,
               atr: float) -> float:
    """
    الوقف المتحرّك: يرتفع فقط، ولا ينزل أبداً.
    هذا ما يجعل الاستراتيجية تربح بنسبة نجاح دون 40% — الخاسر يُقطع عند
    2×ATR بينما الرابح يُترك يركض خلف 4×ATR.
    """
    return max(current_stop, highest - TRAIL_ATR * atr)
