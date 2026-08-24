"""
indicators.py — دوال التحليل الفني (v2 — إصلاح EMA والـ Trend)
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
    kind:  str
    time:  str


@dataclass
class FVG:
    top:       float
    bottom:    float
    direction: str
    mid:       float
    candle_index: int


@dataclass
class SDZone:
    top:       float
    bottom:    float
    kind:      str
    touches:   int
    valid:     bool
    candle_index: int


@dataclass
class BOSEvent:
    direction: str
    level:     float
    kind:      str
    candle_index: int


# ─── EMA ──────────────────────────────────────────────────────────────────────
def calculate_ema(candles: list[dict], period: int) -> list[float]:
    closes = [c["close"] for c in candles]
    if len(closes) < period:
        return [None] * len(closes)
    k = 2 / (period + 1)
    ema = [sum(closes[:period]) / period]
    for price in closes[period:]:
        ema.append(price * k + ema[-1] * (1 - k))
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
                index=i, price=candles[i]["high"],
                kind="high", time=candles[i]["time"]
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
                index=i, price=candles[i]["low"],
                kind="low", time=candles[i]["time"]
            ))
    return lows


# ─── FIX: detect_trend_1h المُصلحة ────────────────────────────────────────────
def detect_trend_1h(candles: list[dict]) -> str:
    """
    يُعيد: "bullish" | "bearish" | "neutral"

    الإصلاح v2:
    - EMA period أصبح 20 بدل 50 (في config.py)
    - CANDLES_1H_COUNT أصبح 120 بدل 50 — يعطي EMA تاريخاً حقيقياً
    - المنطق: يكفي أحد المؤشرين (EMA أو Swings) بدون تعارض من الآخر
    """
    if len(candles) < TREND_EMA_PERIOD + 5:
        logger.warning(f"TREND: شموع غير كافية {len(candles)} < {TREND_EMA_PERIOD + 5}")
        return "neutral"

    ema_values    = calculate_ema(candles, TREND_EMA_PERIOD)
    current_price = candles[-1]["close"]
    current_ema   = ema_values[-1]

    if current_ema is None:
        return "neutral"

    # ─ تحيز EMA (0.05% كحد أدنى لتجنب الـ noise)
    ema_threshold = current_ema * 0.0005
    ema_diff      = current_price - current_ema

    if ema_diff > ema_threshold:
        ema_bias = "bullish"
    elif ema_diff < -ema_threshold:
        ema_bias = "bearish"
    else:
        ema_bias = "neutral"

    # ─ تحيز الـ Swings (آخر 15 شمعة)
    recent      = candles[-15:]
    swing_highs = find_swing_highs(recent, lookback=2)
    swing_lows  = find_swing_lows(recent,  lookback=2)
    swing_bias  = "neutral"

    if len(swing_highs) >= 2 and len(swing_lows) >= 2:
        hh = swing_highs[-1].price > swing_highs[-2].price
        hl = swing_lows[-1].price  > swing_lows[-2].price
        lh = swing_highs[-1].price < swing_highs[-2].price
        ll = swing_lows[-1].price  < swing_lows[-2].price
        if hh and hl:
            swing_bias = "bullish"
        elif lh and ll:
            swing_bias = "bearish"

    # ─ تسجيل تشخيصي
    logger.info(
        f"TREND_DETAIL | price={current_price:.2f} | ema={current_ema:.2f} | "
        f"diff={ema_diff:+.2f} | ema_bias={ema_bias} | swing_bias={swing_bias}"
    )

    # ─ القرار: يكفي واحد بدون تعارض من الآخر
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
    swings = find_swing_highs(candles)
    prices = [s.price for s in swings]
    groups, used = [], set()
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


def find_equal_lows(candles: list[dict], tolerance: float = EQUAL_LEVEL_TOLERANCE) -> list[float]:
    swings = find_swing_lows(candles)
    prices = [s.price for s in swings]
    groups, used = [], set()
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
    fvgs   = []
    recent = candles[-lookback:] if len(candles) > lookback else candles
    base   = len(candles) - len(recent)

    for i in range(1, len(recent) - 1):
        c1, c3 = recent[i - 1], recent[i + 1]
        if c1["high"] < c3["low"]:
            fvgs.append(FVG(
                top=c3["low"], bottom=c1["high"],
                direction="bullish",
                mid=(c3["low"] + c1["high"]) / 2,
                candle_index=base + i
            ))
        elif c1["low"] > c3["high"]:
            fvgs.append(FVG(
                top=c1["low"], bottom=c3["high"],
                direction="bearish",
                mid=(c1["low"] + c3["high"]) / 2,
                candle_index=base + i
            ))
    return fvgs


# ─── BOS / CHOCH ──────────────────────────────────────────────────────────────
def detect_bos_choch(candles: list[dict], from_index: int = 0) -> Optional[BOSEvent]:
    window = candles[from_index: from_index + 8]
    if len(window) < 3:
        return None

    swing_highs = find_swing_highs(window, lookback=1)
    swing_lows  = find_swing_lows(window,  lookback=1)

    if not swing_highs and not swing_lows:
        # Fallback: استخدم أعلى وأدنى الـ window
        last_high = max(c["high"] for c in window[:-1])
        last_low  = min(c["low"]  for c in window[:-1])
    else:
        last_high = max((s.price for s in swing_highs), default=max(c["high"] for c in window[:-1]))
        last_low  = min((s.price for s in swing_lows),  default=min(c["low"]  for c in window[:-1]))

    last_close = window[-1]["close"]

    if last_close > last_high:
        return BOSEvent(
            direction="bullish", level=last_high,
            kind="BOS", candle_index=from_index + len(window) - 1
        )
    if last_close < last_low:
        return BOSEvent(
            direction="bearish", level=last_low,
            kind="BOS", candle_index=from_index + len(window) - 1
        )
    return None


# ─── مناطق Supply & Demand ────────────────────────────────────────────────────
def find_demand_zones(candles_1h: list[dict]) -> list[SDZone]:
    zones = []
    n = len(candles_1h)
    for i in range(n - SD_IMPULSE_MIN_CANDLES - 1):
        c = candles_1h[i]
        if c["close"] >= c["open"]:
            continue
        impulse = candles_1h[i + 1: i + 1 + SD_IMPULSE_MIN_CANDLES]
        if sum(1 for ic in impulse if ic["close"] > ic["open"]) >= SD_IMPULSE_MIN_CANDLES:
            zones.append(SDZone(
                top=max(c["open"], c["close"]),
                bottom=min(c["open"], c["close"]),
                kind="demand", touches=0, valid=True, candle_index=i
            ))
    return zones


def find_supply_zones(candles_1h: list[dict]) -> list[SDZone]:
    zones = []
    n = len(candles_1h)
    for i in range(n - SD_IMPULSE_MIN_CANDLES - 1):
        c = candles_1h[i]
        if c["close"] <= c["open"]:
            continue
        impulse = candles_1h[i + 1: i + 1 + SD_IMPULSE_MIN_CANDLES]
        if sum(1 for ic in impulse if ic["close"] < ic["open"]) >= SD_IMPULSE_MIN_CANDLES:
            zones.append(SDZone(
                top=max(c["open"], c["close"]),
                bottom=min(c["open"], c["close"]),
                kind="supply", touches=0, valid=True, candle_index=i
            ))
    return zones


def count_zone_touches(candles: list[dict], zone: SDZone) -> int:
    return sum(1 for c in candles if c["low"] <= zone.top and c["high"] >= zone.bottom)
