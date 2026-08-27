"""
indicators.py — مؤشرات VWAP Scalping
VWAP + EMA + RSI + ATR — كلها محسوبة من الصفر بلا مكتبات خارجية.
"""

import logging
from datetime import datetime, timezone
from config import EMA_FAST, EMA_SLOW, RSI_PERIOD, ATR_PERIOD

logger = logging.getLogger(__name__)


# ─── EMA ──────────────────────────────────────────────────────────────────────
def calculate_ema(values: list[float], period: int) -> list[float]:
    if len(values) < period:
        return [None] * len(values)
    k = 2 / (period + 1)
    ema = [sum(values[:period]) / period]
    for price in values[period:]:
        ema.append(price * k + ema[-1] * (1 - k))
    return [None] * (period - 1) + ema


# ─── VWAP (يُحسب من بداية جلسة اليوم) ────────────────────────────────────────
def calculate_vwap(candles: list[dict]) -> float:
    """
    VWAP = مجموع (السعر النموذجي × الحجم) / مجموع الحجم
    يُحسب فقط لشموع جلسة اليوم الحالي (منذ منتصف الليل UTC).

    إذا الحجم غير متوفر، يستخدم الحجم = 1 لكل شمعة (يتحول لمتوسط سعري).
    """
    today = datetime.now(timezone.utc).date()

    cum_pv = 0.0  # مجموع (السعر × الحجم)
    cum_v  = 0.0  # مجموع الحجم

    for c in candles:
        # فلترة شموع اليوم فقط
        try:
            c_time = datetime.fromisoformat(
                c["time"].replace("Z", "+00:00")
            ).astimezone(timezone.utc)
            if c_time.date() != today:
                continue
        except Exception:
            pass  # لو فشل التاريخ، نضمّن الشمعة

        typical = (c["high"] + c["low"] + c["close"]) / 3
        volume  = c.get("volume", 0) or 1  # fallback = 1 لو صفر أو مفقود

        cum_pv += typical * volume
        cum_v  += volume

    if cum_v == 0:
        # fallback نهائي: متوسط الإغلاق
        return sum(c["close"] for c in candles[-20:]) / min(20, len(candles))

    return cum_pv / cum_v


# ─── RSI ──────────────────────────────────────────────────────────────────────
def calculate_rsi(candles: list[dict], period: int = RSI_PERIOD) -> float:
    if len(candles) < period + 1:
        return 50.0

    closes = [c["close"] for c in candles]
    gains, losses = [], []

    for i in range(1, len(closes)):
        change = closes[i] - closes[i - 1]
        gains.append(max(change, 0))
        losses.append(abs(min(change, 0)))

    # آخر (period) قيمة
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


# ─── ATR ──────────────────────────────────────────────────────────────────────
def calculate_atr(candles: list[dict], period: int = ATR_PERIOD) -> float:
    if len(candles) < period + 1:
        return 0.0

    true_ranges = []
    for i in range(1, len(candles)):
        high  = candles[i]["high"]
        low   = candles[i]["low"]
        prev_close = candles[i - 1]["close"]
        tr = max(
            high - low,
            abs(high - prev_close),
            abs(low - prev_close)
        )
        true_ranges.append(tr)

    return sum(true_ranges[-period:]) / period


# ─── حساب كل المؤشرات دفعة واحدة ─────────────────────────────────────────────
def compute_all(candles: list[dict]) -> dict:
    closes = [c["close"] for c in candles]

    ema_fast_series = calculate_ema(closes, EMA_FAST)
    ema_slow_series = calculate_ema(closes, EMA_SLOW)

    return {
        "vwap":     calculate_vwap(candles),
        "ema_fast": ema_fast_series[-1],
        "ema_slow": ema_slow_series[-1],
        "rsi":      calculate_rsi(candles),
        "atr":      calculate_atr(candles),
        "price":    closes[-1],
    }
