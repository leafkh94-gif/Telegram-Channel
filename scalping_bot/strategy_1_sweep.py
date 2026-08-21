"""
strategy_1_sweep.py — الإعداد الأول: Liquidity Sweep + كسر البنية
المنطق: السوق يصطاد السيولة فوق القمم أو تحت القيعان ثم ينعكس.
الدخول بعد الاصطياد + تأكيد BOS أو CHOCH على 5M.
"""

import logging
from dataclasses import dataclass
from typing import Optional
from indicators import (
    find_swing_highs, find_swing_lows,
    find_equal_highs, find_equal_lows,
    detect_bos_choch, find_fvg,
    SwingPoint, BOSEvent, FVG
)
from config import (
    SWEEP_LOOKBACK_CANDLES, MAX_CANDLES_AFTER_SWEEP,
    SL_BUFFER_POINTS, TP1_CLOSE_PCT_S1, MIN_RR_SETUP_1,
    EQUAL_LEVEL_TOLERANCE
)

logger = logging.getLogger(__name__)


@dataclass
class SweepEvent:
    direction:    str    # "bullish" (اصطياد sell-side) أو "bearish" (اصطياد buy-side)
    swept_level:  float
    sweep_low:    float  # أدنى نقطة وصلها الاصطياد
    sweep_high:   float  # أعلى نقطة وصلها الاصطياد
    candle_index: int


@dataclass
class Setup1Signal:
    symbol:      str
    direction:   str    # "BUY" أو "SELL"
    entry:       float
    sl:          float
    tp1:         float
    tp2:         float
    rr:          float
    setup_type:  str    # "BOS" أو "CHOCH"
    swept_level: float
    fvg:         Optional[FVG]
    confidence:  int    # 1-3


# ─── تجميع مستويات السيولة على 15M ────────────────────────────────────────────
def build_liquidity_map(candles_15m: list[dict]) -> dict:
    recent = candles_15m[-SWEEP_LOOKBACK_CANDLES:]

    swing_highs  = find_swing_highs(recent)
    swing_lows   = find_swing_lows(recent)
    equal_highs  = find_equal_highs(recent, EQUAL_LEVEL_TOLERANCE)
    equal_lows   = find_equal_lows(recent, EQUAL_LEVEL_TOLERANCE)

    # ترتيب تنازلي للقمم وتصاعدي للقيعان
    buy_side_levels  = sorted(
        [s.price for s in swing_highs] + equal_highs,
        reverse=True
    )
    sell_side_levels = sorted(
        [s.price for s in swing_lows] + equal_lows
    )

    current_price = candles_15m[-1]["close"]

    # المستويات فوق وتحت السعر الحالي فقط
    bsl = [l for l in buy_side_levels  if l > current_price]  # Buy-side liquidity فوق
    ssl = [l for l in sell_side_levels if l < current_price]  # Sell-side liquidity تحت

    return {
        "bsl": bsl,  # أهداف للبيع (اصطياد ثم هبوط)
        "ssl": ssl,  # أهداف للشراء (اصطياد ثم صعود)
        "nearest_bsl": bsl[0] if bsl else None,
        "nearest_ssl": ssl[-1] if ssl else None,
    }


# ─── كشف الـ Sweep على 15M ────────────────────────────────────────────────────
def detect_sweep_15m(candles_15m: list[dict], liquidity_map: dict) -> Optional[SweepEvent]:
    """
    يفحص آخر 3 شموع على 15M.
    Sweep صالح: الفتيل يخترق المستوى والإغلاق يعود داخله.
    """
    if len(candles_15m) < 3:
        return None

    last3 = candles_15m[-3:]
    current_price = candles_15m[-1]["close"]

    # ─ Bullish Sweep (اصطياد sell-side تحت) → نتوقع صعوداً
    nearest_ssl = liquidity_map.get("nearest_ssl")
    if nearest_ssl is not None:
        for i, c in enumerate(last3):
            # الفتيل السفلي اخترق المستوى
            if c["low"] < nearest_ssl:
                # الإغلاق عاد فوق المستوى
                if c["close"] > nearest_ssl:
                    logger.info(f"✅ Bullish Sweep @ {nearest_ssl:.2f}")
                    return SweepEvent(
                        direction="bullish",
                        swept_level=nearest_ssl,
                        sweep_low=c["low"],
                        sweep_high=c["high"],
                        candle_index=len(candles_15m) - 3 + i
                    )

    # ─ Bearish Sweep (اصطياد buy-side فوق) → نتوقع هبوطاً
    nearest_bsl = liquidity_map.get("nearest_bsl")
    if nearest_bsl is not None:
        for i, c in enumerate(last3):
            if c["high"] > nearest_bsl:
                if c["close"] < nearest_bsl:
                    logger.info(f"✅ Bearish Sweep @ {nearest_bsl:.2f}")
                    return SweepEvent(
                        direction="bearish",
                        swept_level=nearest_bsl,
                        sweep_low=c["low"],
                        sweep_high=c["high"],
                        candle_index=len(candles_15m) - 3 + i
                    )

    return None


# ─── كشف BOS / CHOCH على 5M بعد الـ Sweep ────────────────────────────────────
def detect_entry_after_sweep(candles_5m: list[dict],
                              sweep: SweepEvent,
                              trend: str) -> Optional[tuple[BOSEvent, Optional[FVG]]]:
    """
    يفحص آخر MAX_CANDLES_AFTER_SWEEP شمعة على 5M.
    يبحث عن BOS أو CHOCH يتوافق مع اتجاه الـ Sweep والـ Trend.
    يُعيد (BOSEvent, FVG أو None).
    """
    # الـ sweep direction يجب أن يتوافق مع trend
    if sweep.direction == "bullish" and trend != "bullish":
        return None
    if sweep.direction == "bearish" and trend != "bearish":
        return None

    window = candles_5m[-MAX_CANDLES_AFTER_SWEEP:]
    bos = detect_bos_choch(window, from_index=0)

    if bos is None:
        return None

    # تأكد أن BOS في نفس اتجاه الـ Sweep
    if bos.direction != sweep.direction:
        return None

    # ابحث عن FVG داخل نافذة الـ BOS
    fvgs = find_fvg(window, lookback=len(window))
    matching_fvgs = [f for f in fvgs if f.direction == sweep.direction]
    best_fvg = matching_fvgs[-1] if matching_fvgs else None

    logger.info(f"✅ {bos.kind} {bos.direction} @ {bos.level:.2f} | FVG: {best_fvg is not None}")
    return (bos, best_fvg)


# ─── حساب معاملات الدخول ─────────────────────────────────────────────────────
def calculate_setup1_levels(symbol: str, trend: str,
                             sweep: SweepEvent, bos: BOSEvent,
                             fvg: Optional[FVG],
                             liquidity_map: dict,
                             current_price: float) -> Optional[Setup1Signal]:
    direction = "BUY" if sweep.direction == "bullish" else "SELL"

    # ─ نقطة الدخول
    if fvg:
        entry = fvg.mid
    else:
        # الدخول عند مستوى BOS نفسه
        entry = bos.level

    # ─ Stop Loss
    if direction == "BUY":
        sl = sweep.sweep_low - SL_BUFFER_POINTS
        # TP1: أقرب BSL فوق
        bsl_targets = [l for l in liquidity_map["bsl"] if l > entry]
        tp1 = bsl_targets[0] if bsl_targets else entry + (entry - sl) * 2
        # TP2: ثاني BSL أو امتداد
        tp2 = bsl_targets[1] if len(bsl_targets) > 1 else entry + (entry - sl) * 3.5
    else:
        sl = sweep.sweep_high + SL_BUFFER_POINTS
        ssl_targets = [l for l in liquidity_map["ssl"] if l < entry]
        ssl_targets.sort(reverse=True)
        tp1 = ssl_targets[0] if ssl_targets else entry - (sl - entry) * 2
        tp2 = ssl_targets[1] if len(ssl_targets) > 1 else entry - (sl - entry) * 3.5

    # ─ RR
    sl_dist  = abs(entry - sl)
    tp1_dist = abs(tp1 - entry)
    if sl_dist == 0:
        return None
    rr = tp1_dist / sl_dist

    if rr < MIN_RR_SETUP_1:
        logger.info(f"❌ RR {rr:.2f} < الحد الأدنى {MIN_RR_SETUP_1} — إلغاء")
        return None

    # ─ مستوى الثقة
    confidence = 1
    if fvg:
        confidence += 1
    if bos.kind == "CHOCH":
        confidence += 1

    return Setup1Signal(
        symbol=symbol,
        direction=direction,
        entry=round(entry, 2),
        sl=round(sl, 2),
        tp1=round(tp1, 2),
        tp2=round(tp2, 2),
        rr=round(rr, 2),
        setup_type=bos.kind,
        swept_level=sweep.swept_level,
        fvg=fvg,
        confidence=confidence,
    )


# ─── الدالة الرئيسية ──────────────────────────────────────────────────────────
def scan_setup_1(symbol: str,
                 trend: str,
                 candles_15m: list[dict],
                 candles_5m:  list[dict],
                 current_price: float) -> Optional[Setup1Signal]:
    """
    نقطة الدخول الرئيسية للإعداد الأول.
    يُعيد Setup1Signal أو None.
    """
    if trend == "neutral":
        return None

    liquidity_map = build_liquidity_map(candles_15m)

    sweep = detect_sweep_15m(candles_15m, liquidity_map)
    if sweep is None:
        return None

    result = detect_entry_after_sweep(candles_5m, sweep, trend)
    if result is None:
        return None

    bos, fvg = result

    signal = calculate_setup1_levels(
        symbol, trend, sweep, bos, fvg, liquidity_map, current_price
    )
    return signal
