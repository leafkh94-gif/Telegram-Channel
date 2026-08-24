"""
strategy_3_sd.py — الإعداد الثالث (v2 — إصلاح ZONE_PROXIMITY_POINTS)

FIX 4: ZONE_PROXIMITY_POINTS كانت 10 نقاط — لا تكفي للمؤشرات.
US100 يتحرك 300-400 نقطة يومياً. الآن 80 نقطة (في config.py).
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
    SL_BUFFER_POINTS, MIN_RR_SETUP_3,
    ZONE_PROXIMITY_POINTS
)

logger = logging.getLogger(__name__)


@dataclass
class Setup3Signal:
    symbol:      str
    direction:   str
    entry:       float
    sl:          float
    tp1:         float
    tp2:         float
    rr:          float
    zone_type:   str
    zone_top:    float
    zone_bottom: float
    touches:     int


# ─── تصفية المناطق الصالحة ───────────────────────────────────────────────────
def get_valid_zones(candles_1h: list[dict], kind: str) -> list[SDZone]:
    if kind == "supply":
        zones = find_supply_zones(candles_1h)
    else:
        zones = find_demand_zones(candles_1h)

    current_price = candles_1h[-1]["close"]
    valid = []

    for z in zones:
        if kind == "supply" and current_price >= z.top:
            continue
        if kind == "demand" and current_price <= z.bottom:
            continue

        touches = count_zone_touches(candles_1h[z.candle_index:], z)
        if touches > SD_ZONE_MAX_TOUCHES:
            continue

        z.touches = touches
        z.valid   = True
        valid.append(z)

    valid.sort(key=lambda z: abs((z.top + z.bottom) / 2 - current_price))
    return valid


# ─── FIX 4: القرب بـ 80 نقطة بدل 10 ─────────────────────────────────────────
def price_approaching_zone(
    current_price: float,
    zone: SDZone,
    proximity: float = ZONE_PROXIMITY_POINTS
) -> bool:
    if zone.kind == "demand":
        # السعر يهبط نحو منطقة الطلب
        return current_price <= zone.top + proximity
    else:
        # السعر يصعد نحو منطقة العرض
        return current_price >= zone.bottom - proximity


# ─── كشف الرفض على 5M ────────────────────────────────────────────────────────
def detect_sd_rejection(candles_5m: list[dict], zone: SDZone) -> Optional[dict]:
    last_candles = candles_5m[-8:]  # زدنا من 6 لـ 8

    for i, c in enumerate(last_candles):
        touches_zone = c["low"] <= zone.top and c["high"] >= zone.bottom
        if not touches_zone:
            continue

        candle_range = c["high"] - c["low"]
        if candle_range == 0:
            continue

        if zone.kind == "demand":
            lower_wick = min(c["open"], c["close"]) - c["low"]
            wick_ratio = lower_wick / candle_range
            is_pin    = wick_ratio >= SD_REJECTION_MIN_WICK and c["close"] > c["open"]
            is_engulf = False
            if i > 0:
                prev = last_candles[i - 1]
                is_engulf = (
                    c["close"] > c["open"] and
                    c["open"]  < prev["close"] and
                    c["close"] > prev["open"]
                )
            is_rejection = is_pin or is_engulf
        else:
            upper_wick = c["high"] - max(c["open"], c["close"])
            wick_ratio = upper_wick / candle_range
            is_pin    = wick_ratio >= SD_REJECTION_MIN_WICK and c["close"] < c["open"]
            is_engulf = False
            if i > 0:
                prev = last_candles[i - 1]
                is_engulf = (
                    c["close"] < c["open"] and
                    c["open"]  > prev["close"] and
                    c["close"] < prev["open"]
                )
            is_rejection = is_pin or is_engulf

        if not is_rejection:
            continue

        if i + 1 >= len(last_candles):
            continue
        confirm = last_candles[i + 1]
        if zone.kind == "demand" and confirm["close"] <= confirm["open"]:
            continue
        if zone.kind == "supply" and confirm["close"] >= confirm["open"]:
            continue

        logger.info(
            f"✅ S&D Rejection @ {zone.kind} | "
            f"wick={wick_ratio:.1%} | pin={is_pin} | engulf={is_engulf}"
        )
        return {
            "rejection_candle": c,
            "confirm_candle":   confirm,
            "wick_ratio":       wick_ratio,
        }

    return None


# ─── حساب معاملات الدخول ─────────────────────────────────────────────────────
def calculate_setup3_levels(
    symbol: str, zone: SDZone,
    rejection: dict,
    candles_1h: list[dict]
) -> Optional[Setup3Signal]:

    c         = rejection["rejection_candle"]
    direction = "BUY" if zone.kind == "demand" else "SELL"
    entry     = c["close"]

    if direction == "BUY":
        sl  = c["low"] - SL_BUFFER_POINTS
        supply_above = sorted(
            [z for z in find_supply_zones(candles_1h) if z.bottom > entry],
            key=lambda z: z.bottom
        )
        tp1 = supply_above[0].bottom if supply_above else entry + abs(entry - sl) * 2
        tp2 = supply_above[1].bottom if len(supply_above) > 1 else entry + abs(entry - sl) * 3.5
    else:
        sl  = c["high"] + SL_BUFFER_POINTS
        demand_below = sorted(
            [z for z in find_demand_zones(candles_1h) if z.top < entry],
            key=lambda z: z.top, reverse=True
        )
        tp1 = demand_below[0].top if demand_below else entry - abs(sl - entry) * 2
        tp2 = demand_below[1].top if len(demand_below) > 1 else entry - abs(sl - entry) * 3.5

    sl_dist  = abs(entry - sl)
    tp1_dist = abs(tp1 - entry)
    if sl_dist == 0:
        return None
    rr = tp1_dist / sl_dist

    if rr < MIN_RR_SETUP_3:
        return None

    return Setup3Signal(
        symbol=symbol, direction=direction,
        entry=round(entry, 2), sl=round(sl, 2),
        tp1=round(tp1, 2), tp2=round(tp2, 2),
        rr=round(rr, 2), zone_type=zone.kind,
        zone_top=round(zone.top, 2), zone_bottom=round(zone.bottom, 2),
        touches=zone.touches,
    )


# ─── الدالة الرئيسية ──────────────────────────────────────────────────────────
def scan_setup_3(
    symbol: str, trend: str,
    candles_1h: list[dict],
    candles_5m: list[dict],
    current_price: float
) -> Optional[Setup3Signal]:

    if trend == "neutral":
        return None

    kind  = "demand" if trend == "bullish" else "supply"
    zones = get_valid_zones(candles_1h, kind)

    if not zones:
        return None

    for zone in zones[:3]:  # فقط أقرب 3 مناطق
        if not price_approaching_zone(current_price, zone):
            logger.debug(
                f"S&D: بُعد {abs((zone.top+zone.bottom)/2 - current_price):.1f} "
                f"> {ZONE_PROXIMITY_POINTS} — تخطي"
            )
            continue

        logger.info(
            f"🎯 {symbol}: السعر قريب من منطقة {kind} | "
            f"{zone.bottom:.2f}—{zone.top:.2f}"
        )

        rejection = detect_sd_rejection(candles_5m, zone)
        if rejection is None:
            continue

        signal = calculate_setup3_levels(symbol, zone, rejection, candles_1h)
        if signal:
            return signal

    return None
