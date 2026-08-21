"""
strategy_3_sd.py — الإعداد الثالث: Supply & Demand Rejection
المنطق: رسم مناطق S&D على 1H → انتظار وصول السعر → رفض على 5M → دخول.
"""

import logging
from dataclasses import dataclass
from typing import Optional
from indicators import (
    find_supply_zones, find_demand_zones,
    count_zone_touches, SDZone
)
from config import (
    SD_ZONE_MAX_TOUCHES, SD_REJECTION_MIN_WICK,
    SL_BUFFER_POINTS, MIN_RR_SETUP_3, TP1_CLOSE_PCT_S3
)

logger = logging.getLogger(__name__)

# كم نقطة قبل الوصول للمنطقة نبدأ المراقبة على 5M
ZONE_PROXIMITY_POINTS = 10


@dataclass
class Setup3Signal:
    symbol:     str
    direction:  str
    entry:      float
    sl:         float
    tp1:        float
    tp2:        float
    rr:         float
    zone_type:  str    # "demand" أو "supply"
    zone_top:   float
    zone_bottom: float
    touches:    int


# ─── تصفية المناطق الصالحة ───────────────────────────────────────────────────
def get_valid_zones(candles_1h: list[dict], kind: str) -> list[SDZone]:
    """
    يُعيد المناطق الصالحة (لم تُخترق + لمسات ≤ الحد الأقصى).
    kind: "supply" أو "demand"
    """
    if kind == "supply":
        zones = find_supply_zones(candles_1h)
    else:
        zones = find_demand_zones(candles_1h)

    valid = []
    current_price = candles_1h[-1]["close"]

    for z in zones:
        # فحص عدم الاختراق
        if kind == "supply" and current_price >= z.top:
            continue  # السعر اخترق منطقة العرض للأعلى → ملغاة
        if kind == "demand" and current_price <= z.bottom:
            continue  # السعر اخترق منطقة الطلب للأسفل → ملغاة

        # فحص عدد اللمسات
        touches = count_zone_touches(candles_1h[z.candle_index:], z)
        if touches > SD_ZONE_MAX_TOUCHES:
            continue

        z.touches = touches
        z.valid   = True
        valid.append(z)

    # ترتيب حسب الأقرب للسعر الحالي
    valid.sort(key=lambda z: abs(
        (z.top + z.bottom) / 2 - current_price
    ))
    return valid


# ─── هل السعر قريب من المنطقة؟ ───────────────────────────────────────────────
def price_approaching_zone(current_price: float, zone: SDZone,
                            proximity: float = ZONE_PROXIMITY_POINTS) -> bool:
    if zone.kind == "demand":
        return current_price <= zone.top + proximity
    else:
        return current_price >= zone.bottom - proximity


# ─── كشف الرفض على 5M ────────────────────────────────────────────────────────
def detect_sd_rejection(candles_5m: list[dict], zone: SDZone) -> Optional[dict]:
    """
    يفحص آخر 6 شموع على 5M بحثاً عن رفض داخل المنطقة.

    للطلب (demand): Pin Bar سفلي أو Bullish Engulfing داخل المنطقة
    للعرض (supply): Pin Bar علوي أو Bearish Engulfing داخل المنطقة
    """
    last_candles = candles_5m[-6:]

    for i, c in enumerate(last_candles):
        # هل الشمعة داخل المنطقة؟
        touches_zone = c["low"] <= zone.top and c["high"] >= zone.bottom
        if not touches_zone:
            continue

        candle_range = c["high"] - c["low"]
        if candle_range == 0:
            continue

        body_size = abs(c["close"] - c["open"])

        # ─ Demand Rejection (شراء)
        if zone.kind == "demand":
            # Pin Bar سفلي
            lower_wick = min(c["open"], c["close"]) - c["low"]
            wick_ratio = lower_wick / candle_range
            is_pin_bar = wick_ratio >= SD_REJECTION_MIN_WICK and c["close"] > c["open"]

            # Bullish Engulfing
            is_engulfing = False
            if i > 0:
                prev = last_candles[i - 1]
                is_engulfing = (
                    c["close"] > c["open"] and
                    c["open"] < prev["close"] and
                    c["close"] > prev["open"]
                )

            is_rejection = is_pin_bar or is_engulfing

        # ─ Supply Rejection (بيع)
        else:
            # Pin Bar علوي
            upper_wick = c["high"] - max(c["open"], c["close"])
            wick_ratio = upper_wick / candle_range
            is_pin_bar = wick_ratio >= SD_REJECTION_MIN_WICK and c["close"] < c["open"]

            # Bearish Engulfing
            is_engulfing = False
            if i > 0:
                prev = last_candles[i - 1]
                is_engulfing = (
                    c["close"] < c["open"] and
                    c["open"] > prev["close"] and
                    c["close"] < prev["open"]
                )

            is_rejection = is_pin_bar or is_engulfing

        if not is_rejection:
            continue

        # شمعة تأكيد
        if i + 1 >= len(last_candles):
            continue
        confirm = last_candles[i + 1]
        if zone.kind == "demand" and confirm["close"] <= confirm["open"]:
            continue
        if zone.kind == "supply" and confirm["close"] >= confirm["open"]:
            continue

        logger.info(
            f"✅ S&D Rejection | zone={zone.kind} | "
            f"wick_ratio={wick_ratio:.1%} | pin={is_pin_bar} | engulf={is_engulfing}"
        )
        return {
            "rejection_candle": c,
            "confirm_candle":   confirm,
            "wick_ratio":       wick_ratio,
            "is_pin_bar":       is_pin_bar,
            "is_engulfing":     is_engulfing,
        }

    return None


# ─── حساب معاملات الدخول ─────────────────────────────────────────────────────
def calculate_setup3_levels(symbol: str, zone: SDZone,
                             rejection: dict,
                             candles_1h: list[dict]) -> Optional[Setup3Signal]:
    c = rejection["rejection_candle"]
    direction = "BUY" if zone.kind == "demand" else "SELL"
    entry = c["close"]

    # الهدف: أقرب منطقة معاكسة
    current_price = candles_1h[-1]["close"]

    if direction == "BUY":
        sl  = c["low"] - SL_BUFFER_POINTS
        # TP1: أعلى المنطقة الأقرب للعرض (أو 2R)
        supply_zones = find_supply_zones(candles_1h)
        supply_above = [z for z in supply_zones if z.bottom > entry]
        supply_above.sort(key=lambda z: z.bottom)
        tp1 = supply_above[0].bottom if supply_above else entry + abs(entry - sl) * 2
        tp2 = supply_above[1].bottom if len(supply_above) > 1 else entry + abs(entry - sl) * 3.5
    else:
        sl  = c["high"] + SL_BUFFER_POINTS
        demand_zones = find_demand_zones(candles_1h)
        demand_below = [z for z in demand_zones if z.top < entry]
        demand_below.sort(key=lambda z: z.top, reverse=True)
        tp1 = demand_below[0].top if demand_below else entry - abs(sl - entry) * 2
        tp2 = demand_below[1].top if len(demand_below) > 1 else entry - abs(sl - entry) * 3.5

    sl_dist  = abs(entry - sl)
    tp1_dist = abs(tp1 - entry)
    if sl_dist == 0:
        return None
    rr = tp1_dist / sl_dist

    if rr < MIN_RR_SETUP_3:
        logger.info(f"❌ RR {rr:.2f} < {MIN_RR_SETUP_3} — إلغاء إعداد S&D")
        return None

    return Setup3Signal(
        symbol=symbol,
        direction=direction,
        entry=round(entry, 2),
        sl=round(sl, 2),
        tp1=round(tp1, 2),
        tp2=round(tp2, 2),
        rr=round(rr, 2),
        zone_type=zone.kind,
        zone_top=round(zone.top, 2),
        zone_bottom=round(zone.bottom, 2),
        touches=zone.touches,
    )


# ─── الدالة الرئيسية ──────────────────────────────────────────────────────────
def scan_setup_3(symbol: str,
                 trend: str,
                 candles_1h:  list[dict],
                 candles_5m:  list[dict],
                 current_price: float) -> Optional[Setup3Signal]:
    if trend == "neutral":
        return None

    # المناطق حسب الاتجاه
    if trend == "bullish":
        zones = get_valid_zones(candles_1h, "demand")
    else:
        zones = get_valid_zones(candles_1h, "supply")

    if not zones:
        return None

    # هل السعر قريب من أي منطقة؟
    for zone in zones:
        if not price_approaching_zone(current_price, zone):
            continue

        logger.info(f"🎯 السعر قريب من منطقة {zone.kind} | {zone.bottom:.2f} — {zone.top:.2f}")

        # كشف الرفض على 5M
        rejection = detect_sd_rejection(candles_5m, zone)
        if rejection is None:
            continue

        signal = calculate_setup3_levels(symbol, zone, rejection, candles_1h)
        if signal:
            return signal

    return None
