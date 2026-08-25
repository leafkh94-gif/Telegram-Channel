"""
strategy_1_sweep.py — الإعداد الأول (v2 — إصلاح نافذة الـ Sweep والـ BOS)

الإصلاحات:
- FIX 2: توسيع نافذة الـ Sweep من 3 لـ 8 شموع (15M)
- FIX 2: تتبع فهرس شمعة الـ Sweep لربط البحث عن BOS بالوقت الصحيح على 5M
"""

import logging
from dataclasses import dataclass, field
from typing import Optional
from indicators import (
    find_swing_highs, find_swing_lows,
    find_equal_highs, find_equal_lows,
    detect_bos_choch, find_fvg,
    BOSEvent, FVG
)
from config import (
    SWEEP_LOOKBACK_CANDLES, SWEEP_DETECTION_CANDLES,
    MAX_CANDLES_AFTER_SWEEP, SL_BUFFER_POINTS, ENTRY_RETRACE_PCT,
    TP1_CLOSE_PCT_S1, MIN_RR_SETUP_1, EQUAL_LEVEL_TOLERANCE
)

logger = logging.getLogger(__name__)


@dataclass
class SweepEvent:
    direction:    str
    swept_level:  float
    sweep_low:    float
    sweep_high:   float
    candle_index: int
    candles_ago:  int = 0   # كم شمعة 15M مضت منذ الـ Sweep


@dataclass
class Setup1Signal:
    symbol:      str
    direction:   str
    entry:       float
    sl:          float
    tp1:         float
    tp2:         float
    rr:          float
    setup_type:  str
    swept_level: float
    fvg:         Optional[FVG]
    confidence:  int


# ─── خريطة السيولة ────────────────────────────────────────────────────────────
def build_liquidity_map(candles_15m: list[dict]) -> dict:
    recent = candles_15m[-SWEEP_LOOKBACK_CANDLES:]

    swing_highs = find_swing_highs(recent)
    swing_lows  = find_swing_lows(recent)
    eq_highs    = find_equal_highs(recent, EQUAL_LEVEL_TOLERANCE)
    eq_lows     = find_equal_lows(recent,  EQUAL_LEVEL_TOLERANCE)

    all_highs = set([s.price for s in swing_highs] + eq_highs)
    all_lows  = set([s.price for s in swing_lows]  + eq_lows)

    current_price = candles_15m[-1]["close"]

    # FIX 8 — ترتيب مستويات السيولة حسب القرب من السعر (الأقرب أولاً).
    # الخطأ السابق: all_highs كانت مرتّبة تنازلياً (reverse=True) ثم يؤخذ bsl[0]
    # كـ "nearest_bsl" — وهو في الحقيقة **أعلى** قمة في النافذة، أي الأبعد عن
    # السعر. في سوق هابط كل القمم فوق السعر، فكان البوت ينتظر ارتداداً حتى أعلى
    # قمة في النافذة كلها ثم إغلاقاً تحتها ليعتبرها اصطياداً — وهذا لا يحدث أبداً
    # على إطار سكالبينغ. النتيجة: صفر إعدادات هابطة في سوق هابط.
    # كذلك كان اختيار الأهداف للشراء يأخذ bsl[0] كـ TP1 و bsl[1] كـ TP2 من قائمة
    # تنازلية، فيصير TP2 **أقرب** من TP1 — أي معكوسين.
    # الآن: القائمتان مرتّبتان بالقرب من السعر، فـ [0] هو الأقرب و [1] الذي يليه.
    bsl = sorted([l for l in all_highs if l > current_price])                # تصاعدي: الأقرب فوق أولاً
    ssl = sorted([l for l in all_lows  if l < current_price], reverse=True)  # تنازلي: الأقرب تحت أولاً

    return {
        "bsl": bsl,
        "ssl": ssl,
        "nearest_bsl": bsl[0] if bsl else None,
        "nearest_ssl": ssl[0] if ssl else None,
    }


# ─── FIX 2: كشف الـ Sweep على 8 شموع بدل 3 ──────────────────────────────────
def detect_sweep_15m(candles_15m: list[dict], liquidity_map: dict) -> Optional[SweepEvent]:
    """
    يفحص آخر SWEEP_DETECTION_CANDLES شمعة (افتراضي 8 = 120 دقيقة).
    يُعيد أحدث sweep وقع ويسجل كم شمعة مضت (candles_ago).
    """
    if len(candles_15m) < SWEEP_DETECTION_CANDLES + 1:
        return None

    search = candles_15m[-SWEEP_DETECTION_CANDLES:]
    n      = len(search)

    nearest_ssl = liquidity_map.get("nearest_ssl")
    nearest_bsl = liquidity_map.get("nearest_bsl")

    def _no_sweep_reason() -> str:
        """
        تشخيص: حين لا يوجد اصطياد، نميّز بين ثلاث حالات مختلفة تماماً بدل الصمت:
          1. السعر لم يقترب من السيولة أصلاً (سوق هادئ ضمن النطاق)
          2. اخترق المستوى لكن أغلق خارجه (كسر حقيقي — ليس اصطياداً)
          3. لا مستوى في ذلك الاتجاه
        """
        lows  = [c["low"]  for c in search]
        highs = [c["high"] for c in search]
        parts = []
        if nearest_ssl is None:
            parts.append("SSL: لا مستوى تحت السعر")
        elif any(c["low"] < nearest_ssl for c in search):
            n = sum(1 for c in search if c["low"] < nearest_ssl)
            parts.append(f"SSL {nearest_ssl:.2f}: اخترق ({n} شمعة) لكن أغلق تحته = كسر لا اصطياد")
        else:
            parts.append(f"SSL {nearest_ssl:.2f}: لم يُلمس — أدنى قاع {min(lows):.2f} (يبعد {min(lows) - nearest_ssl:+.1f})")
        if nearest_bsl is None:
            parts.append("BSL: لا مستوى فوق السعر")
        elif any(c["high"] > nearest_bsl for c in search):
            n = sum(1 for c in search if c["high"] > nearest_bsl)
            parts.append(f"BSL {nearest_bsl:.2f}: اخترق ({n} شمعة) لكن أغلق فوقه = كسر لا اصطياد")
        else:
            parts.append(f"BSL {nearest_bsl:.2f}: لم يُلمس — أعلى قمة {max(highs):.2f} (يبعد {max(highs) - nearest_bsl:+.1f})")
        return " | ".join(parts)

    # نبحث من الأحدث للأقدم للحصول على آخر sweep
    for rev_i in range(n - 1, -1, -1):
        c          = search[rev_i]
        candles_ago = (n - 1) - rev_i  # 0 = الشمعة الحالية

        # Bullish sweep (SSL)
        if nearest_ssl is not None:
            if c["low"] < nearest_ssl and c["close"] > nearest_ssl:
                logger.info(
                    f"✅ Bullish Sweep @ {nearest_ssl:.2f} | "
                    f"شمعة {candles_ago} × 15M مضت"
                )
                return SweepEvent(
                    direction="bullish",
                    swept_level=nearest_ssl,
                    sweep_low=c["low"],
                    sweep_high=c["high"],
                    candle_index=len(candles_15m) - n + rev_i,
                    candles_ago=candles_ago
                )

        # Bearish sweep (BSL)
        if nearest_bsl is not None:
            if c["high"] > nearest_bsl and c["close"] < nearest_bsl:
                logger.info(
                    f"✅ Bearish Sweep @ {nearest_bsl:.2f} | "
                    f"شمعة {candles_ago} × 15M مضت"
                )
                return SweepEvent(
                    direction="bearish",
                    swept_level=nearest_bsl,
                    sweep_low=c["low"],
                    sweep_high=c["high"],
                    candle_index=len(candles_15m) - n + rev_i,
                    candles_ago=candles_ago
                )

    logger.info(f"🔎 لا اصطياد خلال آخر {n} شمعة 15M | {_no_sweep_reason()}")
    return None


# ─── FIX 2: BOS مرتبط بوقت الـ Sweep ─────────────────────────────────────────
def detect_entry_after_sweep(
    candles_5m: list[dict],
    sweep: SweepEvent,
    trend: str
) -> Optional[tuple[BOSEvent, Optional[FVG]]]:
    """
    يبحث عن BOS في الـ 5M بعد وقت الـ Sweep مباشرة.

    الإصلاح: candles_ago × 3 = عدد شموع 5M المناظرة لوقت الـ Sweep.
    نفحص من تلك النقطة للأمام بدل أن نأخذ آخر 6 شموع دائماً.
    """
    if (sweep.direction == "bullish" and trend != "bullish") or \
       (sweep.direction == "bearish" and trend != "bearish"):
        logger.info(f"🔎 اصطياد {sweep.direction} لكن اتجاه 1H = {trend} — تعارض، تخطي")
        return None

    # احسب نقطة البداية في الـ 5M
    # كل شمعة 15M ≈ 3 شموع 5M
    # نضيف MAX_CANDLES_AFTER_SWEEP كهامش أمان
    lookback_5m = min(
        (sweep.candles_ago * 3) + MAX_CANDLES_AFTER_SWEEP,
        30  # أقصى 150 دقيقة
    )
    window = candles_5m[-lookback_5m:] if len(candles_5m) >= lookback_5m else candles_5m

    # FIX 7 — نافذة الـ BOS كانت عمياء عن الشموع الحديثة.
    # detect_bos_choch يفحص شريحة ثابتة من 8 شموع تبدأ عند from_index. الاستدعاء
    # السابق كان from_index=0 دائماً، أي **أقدم** 8 شموع من النافذة فقط. مع
    # candles_ago كبيرة تصل النافذة إلى 30 شمعة 5M (150 دقيقة) وكنا نفحص أول 40
    # دقيقة منها ونتجاهل الـ 110 دقيقة التالية — فالـ BOS الذي وقع فعلاً بعد
    # الاصطياد (وهو بطبيعته متأخر عنه) لا يُرى أبداً.
    # الآن: نُمرّر شريحة الـ 8 عبر النافذة كلها ونأخذ أول BOS في اتجاه الاصطياد.
    # كذلك كان فحص الاتجاه يُلغي الإعداد كله إذا صادف أن أول BOS معاكس؛ الآن
    # نُكمل البحث بدل الانسحاب.
    bos = None
    for start in range(0, max(len(window) - 7, 1)):
        candidate = detect_bos_choch(window, from_index=start)
        if candidate is not None and candidate.direction == sweep.direction:
            bos = candidate
            break

    if bos is None:
        logger.info(
            f"🔎 اصطياد {sweep.direction} ✓ لكن لا BOS مطابق على 5M "
            f"(نافذة {len(window)} شمعة بعد الاصطياد)"
        )
        return None

    fvgs = find_fvg(window, lookback=len(window))
    matching_fvgs = [f for f in fvgs if f.direction == sweep.direction]
    best_fvg = matching_fvgs[-1] if matching_fvgs else None

    logger.info(
        f"✅ {bos.kind} {bos.direction} @ {bos.level:.2f} | "
        f"FVG: {'نعم' if best_fvg else 'لا'} | "
        f"5M lookback: {lookback_5m}"
    )
    return (bos, best_fvg)


# ─── حساب معاملات الدخول ─────────────────────────────────────────────────────
def calculate_setup1_levels(
    symbol: str, trend: str,
    sweep: SweepEvent, bos: BOSEvent,
    fvg: Optional[FVG],
    liquidity_map: dict,
    current_price: float
) -> Optional[Setup1Signal]:

    direction = "BUY" if sweep.direction == "bullish" else "SELL"

    # FIX 4: نقطة الدخول.
    # كان: entry = bos.level حين لا يوجد FVG — أي الدخول عند قمة الاندفاع بينما
    # الوقف عند قاع الاصطياد، فالمخاطرة = الساق كاملة و RR < 1 دائماً.
    # الآن: ننتظر ارتداداً داخل ساق (الاصطياد → BOS) وندخل بخصم عند ENTRY_RETRACE_PCT
    # (0.618 = منطقة OTE). أمر الدخول LIMIT ينتظر عودة السعر — لا نلاحق القمة.
    if fvg:
        entry = fvg.mid
    elif direction == "BUY":
        leg   = bos.level - sweep.sweep_low
        entry = sweep.sweep_low + leg * (1 - ENTRY_RETRACE_PCT)
    else:
        leg   = sweep.sweep_high - bos.level
        entry = sweep.sweep_high - leg * (1 - ENTRY_RETRACE_PCT)

    # القائمتان مرتّبتان بالقرب من السعر أصلاً (FIX 8)، فالفلترة تحافظ على الترتيب:
    # [0] = أقرب هدف، [1] = الذي يليه. لا حاجة لإعادة ترتيب.
    if direction == "BUY":
        sl  = sweep.sweep_low - SL_BUFFER_POINTS
        bsl = [l for l in liquidity_map["bsl"] if l > entry]
        tp1 = bsl[0] if bsl else entry + (entry - sl) * 2
        tp2 = bsl[1] if len(bsl) > 1 else entry + (entry - sl) * 3.5
    else:
        sl  = sweep.sweep_high + SL_BUFFER_POINTS
        ssl = [l for l in liquidity_map["ssl"] if l < entry]
        tp1 = ssl[0] if ssl else entry - (sl - entry) * 2
        tp2 = ssl[1] if len(ssl) > 1 else entry - (sl - entry) * 3.5

    sl_dist  = abs(entry - sl)
    tp1_dist = abs(tp1 - entry)
    if sl_dist == 0:
        return None
    rr = tp1_dist / sl_dist

    if rr < MIN_RR_SETUP_1:
        logger.info(
            f"❌ {symbol} RR {rr:.2f} < {MIN_RR_SETUP_1} | {direction} | "
            f"دخول={entry:.2f} وقف={sl:.2f} (مخاطرة {sl_dist:.1f}) "
            f"هدف={tp1:.2f} (ربح {tp1_dist:.1f}) | FVG={'نعم' if fvg else 'لا'}"
        )
        return None

    confidence = 1
    if fvg: confidence += 1
    if bos.kind == "CHOCH": confidence += 1

    return Setup1Signal(
        symbol=symbol, direction=direction,
        entry=round(entry, 2), sl=round(sl, 2),
        tp1=round(tp1, 2), tp2=round(tp2, 2),
        rr=round(rr, 2), setup_type=bos.kind,
        swept_level=sweep.swept_level,
        fvg=fvg, confidence=confidence,
    )


# ─── الدالة الرئيسية ──────────────────────────────────────────────────────────
def scan_setup_1(
    symbol: str, trend: str,
    candles_15m: list[dict],
    candles_5m:  list[dict],
    current_price: float
) -> Optional[Setup1Signal]:

    if trend == "neutral":
        return None

    liquidity_map = build_liquidity_map(candles_15m)

    # تشخيص المسار: سطر واحد لكل فحص يوضّح أين يتوقف القمع بالأرقام، بدل أن
    # يمرّ الفحص صامتاً فلا نعرف هل السبب السيولة أم الاصطياد أم الـ BOS أم RR.
    def _fmt(v) -> str:
        return f"{v:.2f}" if v is not None else "—"

    logger.info(
        f"🧭 {symbol} FUNNEL | اتجاه={trend} | "
        f"BSL={len(liquidity_map['bsl'])} SSL={len(liquidity_map['ssl'])} | "
        f"أقرب BSL={_fmt(liquidity_map['nearest_bsl'])} "
        f"أقرب SSL={_fmt(liquidity_map['nearest_ssl'])} | "
        f"إغلاق 15M={candles_15m[-1]['close']:.2f}"
    )

    if not liquidity_map["nearest_bsl"] and not liquidity_map["nearest_ssl"]:
        logger.info(f"⚠️ {symbol}: لا توجد مستويات سيولة")
        return None

    sweep = detect_sweep_15m(candles_15m, liquidity_map)
    if sweep is None:
        return None

    result = detect_entry_after_sweep(candles_5m, sweep, trend)
    if result is None:
        return None

    bos, fvg = result
    return calculate_setup1_levels(
        symbol, trend, sweep, bos, fvg, liquidity_map, current_price
    )
