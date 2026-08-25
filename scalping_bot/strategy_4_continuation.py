"""
strategy_4_continuation.py — الإعداد الرابع: استمرارية الاتجاه

الثغرة التي يسدّها:
الإعدادات 1-3 كلها تنتظر انعكاساً بعد اصطياد سيولة. هذا يجعل البوت أعمى تماماً
عن الحركة المتجهة نفسها — هبوط 400-500 نقطة على US100/US30/US500 مرّ بصفر
إشارات، لأن الاصطياد الهابط يتطلب أن يرتد السعر **فوق قمة سابقة** ثم يغلق
تحتها، وفي سوق نازل السعر يصنع قمماً أوطأ فلا يتجاوز القمة السابقة أبداً.

المنطق هنا مختلف: لا ننتظر اصطياداً. ننتظر:
  1. ساق اندفاع حقيقية في اتجاه 1H (حجمها ≥ CONT_MIN_LEG_POINTS)
  2. ارتداداً ضدها ضمن نطاق صحّي (23.6% - 78.6%)
  3. تأكيداً على 5M بأن الاتجاه استأنف (BOS في اتجاه الاندفاع)

الوقف خلف قمة/قاع الارتداد (قريب) والهدف عند نهاية الساق ثم امتداد — وهذا ما
يعطي RR جيداً: المخاطرة صغيرة لأن الوقف قريب، والمكافأة كامل ما تبقّى من الساق.
"""

import logging
from dataclasses import dataclass
from typing import Optional

from indicators import (
    detect_bos_choch, find_fvg,
    find_swing_highs, find_swing_lows, FVG
)
from config import (
    CONT_LEG_LOOKBACK_15M, CONT_MIN_LEG_POINTS,
    CONT_MIN_RETRACE, CONT_MAX_RETRACE,
    SL_BUFFER_POINTS, MIN_RR_SETUP_4,
)

logger = logging.getLogger(__name__)


@dataclass
class Setup4Signal:
    symbol:      str
    direction:   str
    entry:       float
    sl:          float
    tp1:         float
    tp2:         float
    rr:          float
    setup_type:  str
    leg_high:    float
    leg_low:     float
    retrace_pct: float
    fvg:         Optional[FVG]
    confidence:  int


def _find_impulse_leg(candles_15m: list[dict], trend: str) -> Optional[tuple]:
    """
    يحدّد آخر ساق اندفاع في اتجاه 1H.

    هابط : أعلى قمة في النافذة، ثم أدنى قاع **بعدها**
    صاعد : أدنى قاع في النافذة، ثم أعلى قمة **بعدها**

    الترتيب الزمني مهم — الساق يجب أن تتحرك في اتجاه الاتجاه، لا عكسه.
    """
    window = candles_15m[-CONT_LEG_LOOKBACK_15M:]
    if len(window) < 6:
        return None

    # نهاية الساق يجب أن تكون قمة/قاع **مؤكّداً** (fractal)، لا الحد الجاري.
    # لو أخذنا أدنى قاع في النافذة مباشرة، فهو في سوق هابط يكون الشمعة الأخيرة
    # نفسها — فتصير نسبة الارتداد صفراً دائماً ولا يمرّ أي إعداد (قياس فعلي:
    # 55 من 56 فحصاً رُفضت بسبب "ارتداد ضعيف" بوسيط 6.2%). القاع المؤكّد يضمن
    # وجود شموع بعده، أي ارتداداً حقيقياً يمكن قياسه.
    if trend == "bearish":
        # قاع الاندفاع = أدنى قاع فعلي (لا قاعاً مؤكّداً — في هبوط قوي يكون
        # القاع المؤكّد متأخراً وقد نزل السعر تحته، فتصير نسبة الارتداد سالبة).
        i_low   = min(range(len(window)), key=lambda i: window[i]["low"])
        leg_low = window[i_low]["low"]
        # قمة الساق = **آخر** قمة مؤكّدة قبل القاع، لا أعلى قمة في النافذة كلها.
        # أعلى قمة تجعل الساق كامل حركة الـ 30 شمعة (400+ نقطة) فيصير أي ارتداد
        # طبيعي (40-60 نقطة) نسبته 10% ويُرفض دائماً — قياس فعلي: 55 من 56 فحصاً.
        # الصحيح في تداول الاستمرارية: نقيس الارتداد مقابل آخر تأرجح، لا الحركة كلها.
        highs_before = [s for s in find_swing_highs(window, lookback=2) if s.index < i_low]
        if not highs_before:
            return None
        i_high   = highs_before[-1].index
        leg_high = window[i_high]["high"]
        return (leg_high, leg_low, i_high, i_low, window)

    if trend == "bullish":
        i_high   = max(range(len(window)), key=lambda i: window[i]["high"])
        leg_high = window[i_high]["high"]
        lows_before = [s for s in find_swing_lows(window, lookback=2) if s.index < i_high]
        if not lows_before:
            return None
        i_low   = lows_before[-1].index
        leg_low = window[i_low]["low"]
        return (leg_high, leg_low, i_high, i_low, window)

    return None


def scan_setup_4(
    symbol: str, trend: str,
    candles_15m: list[dict],
    candles_5m:  list[dict],
    current_price: float
) -> Optional[Setup4Signal]:

    if trend not in ("bullish", "bearish"):
        return None

    leg = _find_impulse_leg(candles_15m, trend)
    if leg is None:
        return None
    leg_high, leg_low, i_high, i_low, window = leg

    leg_size = leg_high - leg_low
    if leg_size < CONT_MIN_LEG_POINTS:
        logger.info(
            f"🔎 {symbol} استمرارية: الساق {leg_size:.1f} نقطة "
            f"< {CONT_MIN_LEG_POINTS} — اندفاع ضعيف"
        )
        return None

    # ─ نسبة الارتداد الحالية داخل الساق
    if trend == "bearish":
        # الاندفاع للأسفل؛ الارتداد صعوداً من leg_low
        retrace_pct = (current_price - leg_low) / leg_size
        pullback_bars = window[i_low:]
        pullback_ext  = max(c["high"] for c in pullback_bars)   # قمة الارتداد
    else:
        # الاندفاع للأعلى؛ الارتداد هبوطاً من leg_high
        retrace_pct = (leg_high - current_price) / leg_size
        pullback_bars = window[i_high:]
        pullback_ext  = min(c["low"] for c in pullback_bars)    # قاع الارتداد

    if not (CONT_MIN_RETRACE <= retrace_pct <= CONT_MAX_RETRACE):
        logger.info(
            f"🔎 {symbol} استمرارية: ارتداد {retrace_pct:.1%} خارج النطاق "
            f"({CONT_MIN_RETRACE:.1%}-{CONT_MAX_RETRACE:.1%}) — "
            f"{'لم يرتد كفاية' if retrace_pct < CONT_MIN_RETRACE else 'ارتداد عميق، الاتجاه قد ينكسر'}"
        )
        return None

    # ─ تأكيد الاستئناف على 5M: BOS في اتجاه الاتجاه
    win5 = candles_5m[-24:] if len(candles_5m) >= 24 else candles_5m
    bos  = None
    for start in range(0, max(len(win5) - 7, 1)):
        cand = detect_bos_choch(win5, from_index=start)
        if cand is not None and cand.direction == trend:
            bos = cand
            break
    if bos is None:
        logger.info(
            f"🔎 {symbol} استمرارية: ارتداد {retrace_pct:.1%} ✓ "
            f"لكن لا تأكيد BOS {trend} على 5M"
        )
        return None

    fvgs = find_fvg(win5, lookback=len(win5))
    matching = [f for f in fvgs if f.direction == trend]
    best_fvg = matching[-1] if matching else None

    # ─ مستويات الصفقة
    direction = "SELL" if trend == "bearish" else "BUY"
    entry = current_price

    if direction == "SELL":
        sl  = pullback_ext + SL_BUFFER_POINTS      # فوق قمة الارتداد
        tp1 = leg_low                              # نهاية الساق = سيولة
        tp2 = leg_low - leg_size * 0.5             # امتداد
    else:
        sl  = pullback_ext - SL_BUFFER_POINTS      # تحت قاع الارتداد
        tp1 = leg_high
        tp2 = leg_high + leg_size * 0.5

    sl_dist  = abs(entry - sl)
    tp1_dist = abs(tp1 - entry)
    if sl_dist == 0:
        return None
    rr = tp1_dist / sl_dist

    if rr < MIN_RR_SETUP_4:
        logger.info(
            f"❌ {symbol} استمرارية RR {rr:.2f} < {MIN_RR_SETUP_4} | {direction} | "
            f"دخول={entry:.2f} وقف={sl:.2f} (مخاطرة {sl_dist:.1f}) "
            f"هدف={tp1:.2f} (ربح {tp1_dist:.1f})"
        )
        return None

    confidence = 1
    if best_fvg:
        confidence += 1
    if 0.382 <= retrace_pct <= 0.618:      # منطقة OTE — أفضل مناطق الاستئناف
        confidence += 1

    logger.info(
        f"✅ {symbol} استمرارية {trend} | ساق {leg_size:.1f} نقطة | "
        f"ارتداد {retrace_pct:.1%} | RR {rr:.2f}"
    )

    return Setup4Signal(
        symbol=symbol, direction=direction,
        entry=round(entry, 2), sl=round(sl, 2),
        tp1=round(tp1, 2), tp2=round(tp2, 2),
        rr=round(rr, 2), setup_type="Trend Continuation",
        leg_high=round(leg_high, 2), leg_low=round(leg_low, 2),
        retrace_pct=round(retrace_pct, 3),
        fvg=best_fvg, confidence=confidence,
    )
