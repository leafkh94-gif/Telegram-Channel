"""
indicators.py — دوال التحليل الفني والبنية السعرية
"""

import logging
from dataclasses import dataclass
from typing import Optional
from config import (
    TREND_EMA_PERIOD, SWING_LOOKBACK,
    EQUAL_LEVEL_TOLERANCE, SD_IMPULSE_MIN_CANDLES
)

logger = logging.getLogger(__name__)


@dataclass
class SwingPoint:
    index: int
    price: float
    kind:  str   # "high" أو "low"
    time:  str


@dataclass
class FVG:
    top:       float
    bottom:    float
    direction: str   # "bullish" أو "bearish"
    mid:       float
    candle_index: int


@dataclass
class SDZone:
    top:       float
    bottom:    float
    kind:      str   # "supply" أو "demand"
    touches:   int
    valid:     bool
    candle_index: int


@dataclass
class BOSEvent:
    direction: str   # "bullish" أو "bearish"
    level:     float
    kind:      str   # "BOS" أو "CHOCH"
    candle_index: int


# ─── EMA ──────────────────────────────────────────────────────────────────────
def calculate_ema(candles: list[dict], period: int) -> list[float]:
    closes = [c["close"] for c in candles]
    if len(closes) < period:
        return []
    k = 2 / (period + 1)
    ema = [sum(closes[:period]) / period]
    for price in closes[period:]:
        ema.append(price * k + ema[-1] * (1 - k))
    # نعيد قائمة بنفس طول candles (الأوائل None)
    padding = [None] * (period - 1)
    return padding + ema


# ─── القمم والقيعان ────────────────────────────────────────────────────────────
def find_swing_highs(candles: list[dict], lookback: int = SWING_LOOKBACK) -> list[SwingPoint]:
    highs = []
    n = len(candles)
    for i in range(lookback, n - lookback):
        window_left  = [candles[i - j]["high"] for j in range(1, lookback + 1)]
        window_right = [candles[i + j]["high"] for j in range(1, lookback + 1)]
        if candles[i]["high"] > max(window_left) and candles[i]["high"] > max(window_right):
            highs.append(SwingPoint(
                index=i,
                price=candles[i]["high"],
                kind="high",
                time=candles[i]["time"]
            ))
    return highs


def find_swing_lows(candles: list[dict], lookback: int = SWING_LOOKBACK) -> list[SwingPoint]:
    lows = []
    n = len(candles)
    for i in range(lookback, n - lookback):
        window_left  = [candles[i - j]["low"] for j in range(1, lookback + 1)]
        window_right = [candles[i + j]["low"] for j in range(1, lookback + 1)]
        if candles[i]["low"] < min(window_left) and candles[i]["low"] < min(window_right):
            lows.append(SwingPoint(
                index=i,
                price=candles[i]["low"],
                kind="low",
                time=candles[i]["time"]
            ))
    return lows


# ─── الاتجاه على 1H ───────────────────────────────────────────────────────────
def detect_trend_1h(candles: list[dict]) -> str:
    """
    يُعيد: "bullish" | "bearish" | "neutral"
    المنطق: EMA 50 + تسلسل قمم وقيعان — يكفي واحد منهم مع عدم تعارض الآخر.

    التعديل v1.1: الكود القديم كان يشترط تطابق EMA والـ Swings معاً،
    مما أنتج neutral في أغلب الحالات حتى في أسواق واضحة الاتجاه.
    """
    if len(candles) < TREND_EMA_PERIOD + 5:
        return "neutral"

    ema_values    = calculate_ema(candles, TREND_EMA_PERIOD)
    current_price = candles[-1]["close"]
    current_ema   = ema_values[-1]

    if current_ema is None:
        return "neutral"

    # ─── تحيز EMA ─────────────────────────────────────────────────────────────
    # نستخدم 0.1% كحد أدنى لتجنب الـ noise في المناطق الضيقة
    ema_threshold = current_ema * 0.001
    ema_diff      = current_price - current_ema

    if ema_diff > ema_threshold:
        ema_bias = "bullish"
    elif ema_diff < -ema_threshold:
        ema_bias = "bearish"
    else:
        ema_bias = "neutral"

    # ─── تحيز الـ Swings ──────────────────────────────────────────────────────
    swing_highs = find_swing_highs(candles[-20:])
    swing_lows  = find_swing_lows(candles[-20:])
    swing_bias  = "neutral"

    if len(swing_highs) >= 2 and len(swing_lows) >= 2:
        is_hh_hl = (swing_highs[-1].price > swing_highs[-2].price and
                    swing_lows[-1].price   > swing_lows[-2].price)
        is_lh_ll = (swing_highs[-1].price < swing_highs[-2].price and
                    swing_lows[-1].price   < swing_lows[-2].price)
        if is_hh_hl:
            swing_bias = "bullish"
        elif is_lh_ll:
            swing_bias = "bearish"

    # ─── القرار النهائي ───────────────────────────────────────────────────────
    # يكفي أن يكون أحدهما في اتجاه، بشرط ألا يكون الآخر في الاتجاه المعاكس.
    # التعارض الصريح (أحدهما bullish والآخر bearish) → neutral.

    if swing_bias == "bearish" and ema_bias == "bullish":
        return "neutral"
    if swing_bias == "bullish" and ema_bias == "bearish":
        return "neutral"

    if swing_bias == "bullish" or ema_bias == "bullish":
        return "bullish"
    if swing_bias == "bearish" or ema_bias == "bearish":
        return "bearish"

    return "neutral"


# ─── Equal Highs / Lows ───────────────────────────────────────────────────────
def find_equal_highs(candles: list[dict], tolerance: float = EQUAL_LEVEL_TOLERANCE) -> list[float]:
    """يجد القمم المتساوية (Equal Highs) للسيولة"""
    swings = find_swing_highs(candles)
    prices = [s.price for s in swings]
    groups = []
    used   = set()

    for i in range(len(prices)):
        if i in used:
            continue
        group = [prices[i]]
        for j in range(i + 1, len(prices)):
            if j not in used and abs(prices[i] - prices[j]) <= tolerance:
                group.append(prices[j])
                used.add(j)
        if len(group) >= 2:
            groups.append(sum(group) / len(group))  # متوسط المستوى
        used.add(i)
    return groups


def find_equal_lows(candles: list[dict], tolerance: float = EQUAL_LEVEL_TOLERANCE) -> list[float]:
    """يجد القيعان المتساوية (Equal Lows) للسيولة"""
    swings = find_swing_lows(candles)
    prices = [s.price for s in swings]
    groups = []
    used   = set()

    for i in range(len(prices)):
        if i in used:
            continue
        group = [prices[i]]
        for j in range(i + 1, len(prices)):
            if j not in used and abs(prices[i] - prices[j]) <= tolerance:
                group.append(prices[j])
                used.add(j)
        if len(group) >= 2:
            groups.append(sum(group) / len(group))
        used.add(i)
    return groups


# ─── Fair Value Gap ────────────────────────────────────────────────────────────
def find_fvg(candles: list[dict], lookback: int = 20) -> list[FVG]:
    """
    يجد الـ FVG في آخر (lookback) شمعة.
    FVG صعودي: أعلى الشمعة 1 < أدنى الشمعة 3
    FVG هبوطي: أدنى الشمعة 1 > أعلى الشمعة 3
    """
    fvgs = []
    recent = candles[-lookback:] if len(candles) > lookback else candles
    base   = len(candles) - len(recent)

    for i in range(1, len(recent) - 1):
        c1, c2, c3 = recent[i - 1], recent[i], recent[i + 1]

        # FVG صعودي
        if c1["high"] < c3["low"]:
            fvgs.append(FVG(
                top=c3["low"],
                bottom=c1["high"],
                direction="bullish",
                mid=(c3["low"] + c1["high"]) / 2,
                candle_index=base + i
            ))

        # FVG هبوطي
        elif c1["low"] > c3["high"]:
            fvgs.append(FVG(
                top=c1["low"],
                bottom=c3["high"],
                direction="bearish",
                mid=(c1["low"] + c3["high"]) / 2,
                candle_index=base + i
            ))

    return fvgs


# ─── BOS / CHOCH ──────────────────────────────────────────────────────────────
def detect_bos_choch(candles: list[dict], from_index: int = 0) -> Optional[BOSEvent]:
    """
    يفحص الشموع من (from_index) للأمام ويكتشف أول BOS أو CHOCH.
    يُعيد None إذا لم يجد خلال 6 شموع.
    """
    window = candles[from_index: from_index + 6]
    if len(window) < 3:
        return None

    swing_highs = find_swing_highs(window, lookback=1)
    swing_lows  = find_swing_lows(window,  lookback=1)

    if not swing_highs or not swing_lows:
        return None

    last_close = window[-1]["close"]
    last_high  = max(s.price for s in swing_highs)
    last_low   = min(s.price for s in swing_lows)

    # BOS صعودي: إغلاق فوق آخر قمة
    if last_close > last_high:
        return BOSEvent(
            direction="bullish",
            level=last_high,
            kind="BOS",
            candle_index=from_index + len(window) - 1
        )

    # BOS هبوطي: إغلاق تحت آخر قاع
    if last_close < last_low:
        return BOSEvent(
            direction="bearish",
            level=last_low,
            kind="BOS",
            candle_index=from_index + len(window) - 1
        )

    return None


# ─── مناطق Supply & Demand ────────────────────────────────────────────────────
def find_demand_zones(candles_1h: list[dict]) -> list[SDZone]:
    """
    منطقة طلب: آخر شمعة هابطة قبل اندفاع صعودي قوي (≥ SD_IMPULSE_MIN_CANDLES)
    """
    zones = []
    n = len(candles_1h)

    for i in range(n - SD_IMPULSE_MIN_CANDLES - 1):
        c = candles_1h[i]
        # الشمعة الحالية هابطة
        if c["close"] >= c["open"]:
            continue

        # فحص الاندفاع الصعودي بعدها
        impulse_candles = candles_1h[i + 1: i + 1 + SD_IMPULSE_MIN_CANDLES]
        bullish_count   = sum(1 for ic in impulse_candles if ic["close"] > ic["open"])

        if bullish_count >= SD_IMPULSE_MIN_CANDLES:
            zone = SDZone(
                top=max(c["open"], c["close"]),
                bottom=min(c["open"], c["close"]),
                kind="demand",
                touches=0,
                valid=True,
                candle_index=i
            )
            zones.append(zone)

    return zones


def find_supply_zones(candles_1h: list[dict]) -> list[SDZone]:
    """
    منطقة عرض: آخر شمعة صاعدة قبل اندفاع هبوطي قوي (≥ SD_IMPULSE_MIN_CANDLES)
    """
    zones = []
    n = len(candles_1h)

    for i in range(n - SD_IMPULSE_MIN_CANDLES - 1):
        c = candles_1h[i]
        # الشمعة الحالية صاعدة
        if c["close"] <= c["open"]:
            continue

        # فحص الاندفاع الهبوطي بعدها
        impulse_candles = candles_1h[i + 1: i + 1 + SD_IMPULSE_MIN_CANDLES]
        bearish_count   = sum(1 for ic in impulse_candles if ic["close"] < ic["open"])

        if bearish_count >= SD_IMPULSE_MIN_CANDLES:
            zone = SDZone(
                top=max(c["open"], c["close"]),
                bottom=min(c["open"], c["close"]),
                kind="supply",
                touches=0,
                valid=True,
                candle_index=i
            )
            zones.append(zone)

    return zones


def count_zone_touches(candles: list[dict], zone: SDZone) -> int:
    """يحسب كم مرة وصل السعر لمنطقة العرض أو الطلب"""
    count = 0
    for c in candles:
        if c["low"] <= zone.top and c["high"] >= zone.bottom:
            count += 1
    return count
