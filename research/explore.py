"""
explore.py — أين توجد أفضلية عند تكرار 2-3 صفقات لكل أداة أسبوعياً؟

المطلوب تكرار أعلى بثلاث إلى أربع مرات مما كان (3.3 صفقة/أداة/شهر ← 10-13).
رفع التكرار سهل: يكفي تخفيف أي شرط. السؤال الحقيقي هو هل تبقى أفضلية بعده،
ولذلك يقيس هذا الملف الاثنين معاً ولا يقبل أحدهما بلا الآخر.

المنهج مفروض بما كلّفنا سابقاً:

  • فترة تصميم وفترة عمياء. الحكم من العمياء وحدها. هدف ثابت بدا ‎+31.3
    نقطة/صفقة داخل عيّنته وأعطى ‎+0.5 خارجها.

  • داخل الشمعة يُفحص الوقف أولاً. الشمعة لا تخبرنا أيّ مستوى لُمس أولاً،
    والقراءة المتفائلة تجمّل النتيجة بلا سند.

  • كل تركيبة تُختبر بحذف أكبر صفقة رابحة. أفضلية قائمة على ثلاث صفقات من
    225 ليست أفضلية.

  • الشموع المُمرَّرة مغلقة دائماً؛ الشمعة الجارية تُسقط. التقييم على شمعة
    قيد التكوّن يُنتج إشارات تظهر ثم تختفي.

لا يُقترح شيء من هنا قبل أن يمرّ الشروط الأربعة.
"""

import os
import statistics
import sys
from dataclasses import dataclass

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bot"))

from indicators import calculate_ema, calculate_atr    # noqa: E402

SPLIT       = "2026-01-01"     # ~16 شهراً تصميم، ~8 شهور عمياء
ATR_PERIOD  = 14
WEEKS_TOTAL = 104              # سنتان — لحساب الصفقات في الأسبوع


@dataclass
class Rule:
    """قاعدة دخول واحدة. كل الحقول تُطبع مع النتيجة حتى تبقى قابلة للمراجعة."""
    name:        str
    channel:     int            # طول قناة دونشيان بشموع الساعة
    use_daily:   bool           # اشتراط الاتجاه اليومي (السعر فوق EMA20)
    use_h4:      bool           # اشتراط تأكيد 4 ساعات (EMA9 فوق EMA21)
    direction:   str = "long"   # long | short | both


def ema_series(candles, period):
    return calculate_ema([c["close"] for c in candles], period)


def precompute(h1, h4, daily):
    """
    يحسب المؤشرات مرة واحدة بدل إعادة حسابها لكل شمعة.

    الفهرسة تربط كل شمعة ساعة بآخر شمعة خشنة **مكتملة** — الـ-1 مقصود، لأن
    الشمعة التي بدأت ولم تُغلق لا يعرف البوت الحيّ نتيجتها.
    """
    d_ema  = ema_series(daily, 20)
    f4     = ema_series(h4, 9)
    s4     = ema_series(h4, 21)

    def coarse_index(fine, coarse):
        out, j = [], -1
        ct = [c["time"] for c in coarse]
        for c in fine:
            while j + 1 < len(ct) and ct[j + 1] < c["time"]:
                j += 1
            out.append(j - 1)
        return out

    return d_ema, f4, s4, coarse_index(h1, daily), coarse_index(h1, h4)


def signals(h1, h4, daily, rule: Rule):
    """يُرجع [(الفهرس، الاتجاه، ATR)] لكل لحظة تتحقق فيها القاعدة."""
    d_ema, f4, s4, idd, id4 = precompute(h1, h4, daily)
    out   = []
    start = max(rule.channel + ATR_PERIOD + 5, 60)

    for i in range(start, len(h1)):
        di, fi = idd[i], id4[i]
        if di < 25 or fi < 25:
            continue

        atr = calculate_atr(h1[i - ATR_PERIOD - 1: i + 1], ATR_PERIOD)
        if not atr or atr <= 0:
            continue

        up_d = daily[di]["close"] > d_ema[di] if d_ema[di] is not None else None
        up_4 = f4[fi] > s4[fi] if (f4[fi] is not None and s4[fi] is not None) else None
        if (rule.use_daily and up_d is None) or (rule.use_h4 and up_4 is None):
            continue

        window = h1[i - rule.channel: i]          # يستثني الشمعة الحالية
        hi     = max(c["high"] for c in window)
        lo     = min(c["low"] for c in window)
        close  = h1[i]["close"]

        if rule.direction in ("long", "both") and close > hi:
            if (not rule.use_daily or up_d) and (not rule.use_h4 or up_4):
                out.append((i, "long", atr))
        if rule.direction in ("short", "both") and close < lo:
            if (not rule.use_daily or not up_d) and (not rule.use_h4 or not up_4):
                out.append((i, "short", atr))
    return out


def walk(h1, entries, stop_atr=2.0, trail_atr=4.0, partial_atr=None):
    """
    يمشي بكل صفقة حتى خروجها، بصفقة واحدة مفتوحة في كل لحظة.

    الترتيب داخل الشمعة: الوقف، ثم الجني الجزئي، ثم رفع التتبّع — وهو نفس
    الترتيب الذي أثبت القياس أنه الوحيد الذي لا يفترض ما لا نعرفه.
    """
    trades, i_next = [], -1
    for i, side, atr in entries:
        if i <= i_next:
            continue                       # صفقة قائمة — لا نفتح ثانية

        long_ = side == "long"
        entry = h1[i]["close"]
        risk  = stop_atr * atr

        # كل المسافات بوحدات الربح: موجبة لصالحنا مهما كان الاتجاه. هذا يجعل
        # الشراء والبيع يمرّان في نفس الكود بدل فرعين يفترق سلوكهما بصمت.
        stop_d = -risk                     # مسافة الوقف من الدخول
        peak   = 0.0
        booked, size, last = 0.0, 1.0, 0.0

        for j in range(i + 1, len(h1)):
            if long_:
                fav, adv = h1[j]["high"] - entry, h1[j]["low"] - entry
            else:
                fav, adv = entry - h1[j]["low"], entry - h1[j]["high"]
            last = fav

            if adv <= stop_d:                                   # 1) الوقف أولاً
                trades.append((h1[i]["time"], booked + size * stop_d / risk))
                i_next = j
                break

            if partial_atr is not None and size == 1.0 and fav >= partial_atr * atr:
                booked += 0.5 * (partial_atr * atr) / risk      # 2) جني جزئي
                size    = 0.5
                stop_d  = max(stop_d, 0.0)                      # الباقي بلا مخاطرة

            peak   = max(peak, fav)                             # 3) ثم التتبّع
            stop_d = max(stop_d, peak - trail_atr * atr)
        else:
            # نفدت البيانات والصفقة مفتوحة — نُغلق على آخر سعر معروف بدل
            # تجاهلها، فتجاهل الصفقات المفتوحة يحذف الخاسرات الطويلة وحدها.
            trades.append((h1[i]["time"], booked + size * last / risk))
            i_next = len(h1)
    return trades


def score(trades, label):
    """يُرجع سطر نتيجة، أو None إذا لم تبلغ التركيبة التكرار المطلوب."""
    if len(trades) < 20:
        return None
    ins = [r for t, r in trades if t <  SPLIT]
    oos = [r for t, r in trades if t >= SPLIT]
    if not ins or not oos:
        return None
    rs   = [r for _, r in trades]
    srt  = sorted(rs)
    return {
        "label":  label,
        "n":      len(rs),
        "per_wk": len(rs) / WEEKS_TOTAL,
        "wr":     sum(1 for r in rs if r > 0) / len(rs) * 100,
        "R":      statistics.mean(rs),
        "ins":    statistics.mean(ins),
        "oos":    statistics.mean(oos),
        "no_best": statistics.mean(srt[:-1]),
    }
