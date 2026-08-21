"""
strategy_2_news.py — الإعداد الثاني: News Retest
المنطق: الخبر يصنع اندفاعاً → السعر يتراجع لمنطقة الخبر → نبحث عن رفض → دخول.
"""

import logging
import requests
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from typing import Optional
from config import (
    NEWS_BLOCK_MINUTES, NEWS_RETEST_MAX_MINUTES,
    NEWS_MAX_RETRACEMENT_PCT, NEWS_MIN_REJECTION_WICK,
    SL_BUFFER_POINTS, MIN_RR_SETUP_2, TP1_CLOSE_PCT_S2
)

logger = logging.getLogger(__name__)


@dataclass
class NewsEvent:
    title:    str
    currency: str
    impact:   str
    time_utc: datetime


@dataclass
class NewsZone:
    top:         float
    bottom:      float
    direction:   str    # "bullish" (خبر دفع للأعلى) أو "bearish"
    news_title:  str
    impulse_size: float
    origin_candle_idx: int


@dataclass
class Setup2Signal:
    symbol:     str
    direction:  str
    entry:      float
    sl:         float
    tp1:        float
    tp2:        float
    rr:         float
    news_title: str
    zone_top:   float
    zone_bottom: float


# ─── جلب الأخبار من Forex Factory ────────────────────────────────────────────
def fetch_news_events() -> list[NewsEvent]:
    """
    يجلب أحداث اليوم عالية التأثير.
    يستخدم Forex Factory JSON API.
    """
    try:
        today = datetime.now(timezone.utc).strftime("%b%d.%Y")  # مثال: Jan01.2026
        url   = f"https://nfs.faireconomy.media/ff_calendar_thisweek.json"
        r     = requests.get(url, timeout=8)
        r.raise_for_status()
        all_events = r.json()

        events = []
        today_date = datetime.now(timezone.utc).date()

        for e in all_events:
            if e.get("impact", "").lower() not in ("high", "holiday"):
                continue
            try:
                event_dt = datetime.fromisoformat(e["date"]).replace(tzinfo=timezone.utc)
                if event_dt.date() != today_date:
                    continue
                events.append(NewsEvent(
                    title=e.get("title", ""),
                    currency=e.get("currency", ""),
                    impact=e.get("impact", ""),
                    time_utc=event_dt
                ))
            except Exception:
                continue

        logger.info(f"📰 أخبار اليوم عالية التأثير: {len(events)}")
        return events

    except Exception as ex:
        logger.error(f"❌ خطأ جلب الأخبار: {ex}")
        return []


def is_news_block_active(events: list[NewsEvent]) -> tuple[bool, str]:
    """
    يُعيد (True, اسم_الخبر) إذا كان هناك خبر خلال NEWS_BLOCK_MINUTES دقيقة.
    """
    now = datetime.now(timezone.utc)
    for e in events:
        delta_minutes = (e.time_utc - now).total_seconds() / 60
        if -5 <= delta_minutes <= NEWS_BLOCK_MINUTES:
            return True, e.title
    return False, ""


def get_recent_high_impact_news(events: list[NewsEvent],
                                 within_minutes: int = 20) -> Optional[NewsEvent]:
    """
    يُعيد أحدث خبر عالي التأثير وقع خلال (within_minutes) الماضية.
    """
    now = datetime.now(timezone.utc)
    for e in sorted(events, key=lambda x: x.time_utc, reverse=True):
        elapsed = (now - e.time_utc).total_seconds() / 60
        if 0 <= elapsed <= within_minutes:
            return e
    return None


# ─── كشف شمعة الخبر وتعريف المنطقة ──────────────────────────────────────────
def identify_news_zone(candles_5m: list[dict], news_event: NewsEvent) -> Optional[NewsZone]:
    """
    يحدد شمعة الخبر ويبني منطقة الـ Retest.
    شمعة الخبر: أكبر شمعة من حيث المدى خلال أول 6 شموع بعد وقت الخبر.
    """
    news_time = news_event.time_utc

    # ابحث عن الشموع بعد الخبر مباشرة
    post_news = []
    for i, c in enumerate(candles_5m):
        try:
            c_time = datetime.fromisoformat(c["time"].replace("Z", "+00:00"))
        except Exception:
            continue
        diff = (c_time - news_time).total_seconds() / 60
        if 0 <= diff <= 30:  # أول 30 دقيقة بعد الخبر
            post_news.append((i, c))

    if not post_news:
        return None

    # شمعة الخبر = الأكبر مدىً
    news_candle_idx, news_candle = max(
        post_news[:6],
        key=lambda x: x[1]["high"] - x[1]["low"]
    )

    range_size = news_candle["high"] - news_candle["low"]
    body_size  = abs(news_candle["close"] - news_candle["open"])
    if body_size < range_size * 0.3:
        logger.info("❌ شمعة الخبر صغيرة الجسم — لا إعداد")
        return None

    direction = "bullish" if news_candle["close"] > news_candle["open"] else "bearish"

    # منطقة الـ Retest: الثلث الأقرب لبداية الخبر
    if direction == "bullish":
        zone_top    = news_candle["open"] + body_size * 0.50
        zone_bottom = news_candle["open"]
    else:
        zone_top    = news_candle["open"]
        zone_bottom = news_candle["open"] - body_size * 0.50

    logger.info(f"📰 منطقة الخبر: {zone_bottom:.2f} — {zone_top:.2f} | {direction}")
    return NewsZone(
        top=zone_top,
        bottom=zone_bottom,
        direction=direction,
        news_title=news_event.title,
        impulse_size=body_size,
        origin_candle_idx=news_candle_idx
    )


# ─── كشف الـ Retest والرفض على 5M ────────────────────────────────────────────
def detect_retest_rejection(candles_5m: list[dict],
                             zone: NewsZone,
                             from_idx: int) -> Optional[dict]:
    """
    يفحص الشموع من (from_idx) بحثاً عن:
    1. السعر يدخل المنطقة (Retest)
    2. ظهور شمعة رفض (Pin Bar أو Engulfing)
    3. شمعة تأكيد
    يُعيد dict بمعاملات الدخول أو None.
    """
    search_window = candles_5m[from_idx: from_idx + 6]  # أقصى 6 شموع (30 دقيقة)

    for i, c in enumerate(search_window):
        # هل السعر دخل المنطقة؟
        price_in_zone = c["low"] <= zone.top and c["high"] >= zone.bottom
        if not price_in_zone:
            continue

        # فحص الرفض
        candle_range = c["high"] - c["low"]
        if candle_range == 0:
            continue

        if zone.direction == "bullish":
            # رفض من الأسفل (Pin Bar سفلي أو Bullish Engulfing)
            lower_wick = c["open"] - c["low"] if c["close"] > c["open"] else c["close"] - c["low"]
            wick_ratio = lower_wick / candle_range
            is_rejection = wick_ratio >= NEWS_MIN_REJECTION_WICK and c["close"] > c["open"]

        else:
            # رفض من الأعلى
            upper_wick = c["high"] - c["open"] if c["close"] < c["open"] else c["high"] - c["close"]
            wick_ratio = upper_wick / candle_range
            is_rejection = wick_ratio >= NEWS_MIN_REJECTION_WICK and c["close"] < c["open"]

        if not is_rejection:
            continue

        # شمعة التأكيد
        if i + 1 >= len(search_window):
            continue
        confirm = search_window[i + 1]
        if zone.direction == "bullish" and confirm["close"] <= confirm["open"]:
            continue
        if zone.direction == "bearish" and confirm["close"] >= confirm["open"]:
            continue

        # تأكد أن التراجع لم يتجاوز الحد الأقصى
        if zone.direction == "bullish":
            retracement = (zone.bottom - c["low"]) / zone.impulse_size if zone.impulse_size > 0 else 1
        else:
            retracement = (c["high"] - zone.top) / zone.impulse_size if zone.impulse_size > 0 else 1

        if retracement > NEWS_MAX_RETRACEMENT_PCT:
            logger.info(f"❌ التراجع {retracement:.1%} يتجاوز الحد — إلغاء")
            return None

        logger.info(f"✅ رفض Retest مؤكد @ {c['close']:.2f} | wick_ratio={wick_ratio:.1%}")
        return {
            "rejection_candle": c,
            "confirm_candle":   confirm,
            "wick_ratio":       wick_ratio,
        }

    return None


# ─── حساب معاملات الدخول ─────────────────────────────────────────────────────
def calculate_setup2_levels(symbol: str, zone: NewsZone,
                             rejection: dict) -> Optional[Setup2Signal]:
    c   = rejection["rejection_candle"]
    direction = "BUY" if zone.direction == "bullish" else "SELL"
    entry = c["close"]

    if direction == "BUY":
        sl   = c["low"] - SL_BUFFER_POINTS
        tp1  = zone.top + zone.impulse_size        # قمة شمعة الخبر
        tp2  = zone.top + zone.impulse_size * 1.5  # امتداد 1.5x
    else:
        sl   = c["high"] + SL_BUFFER_POINTS
        tp1  = zone.bottom - zone.impulse_size
        tp2  = zone.bottom - zone.impulse_size * 1.5

    sl_dist  = abs(entry - sl)
    tp1_dist = abs(tp1 - entry)
    if sl_dist == 0:
        return None
    rr = tp1_dist / sl_dist

    if rr < MIN_RR_SETUP_2:
        logger.info(f"❌ RR {rr:.2f} < {MIN_RR_SETUP_2} — إلغاء إعداد الخبر")
        return None

    return Setup2Signal(
        symbol=symbol,
        direction=direction,
        entry=round(entry, 2),
        sl=round(sl, 2),
        tp1=round(tp1, 2),
        tp2=round(tp2, 2),
        rr=round(rr, 2),
        news_title=zone.news_title,
        zone_top=round(zone.top, 2),
        zone_bottom=round(zone.bottom, 2),
    )


# ─── الدالة الرئيسية ──────────────────────────────────────────────────────────
def scan_setup_2(symbol: str,
                 trend: str,
                 candles_5m: list[dict],
                 news_events: list[NewsEvent]) -> Optional[Setup2Signal]:
    if trend == "neutral":
        return None

    # هل يوجد خبر حصل مؤخراً؟
    recent_news = get_recent_high_impact_news(news_events, within_minutes=NEWS_RETEST_MAX_MINUTES)
    if recent_news is None:
        return None

    # بناء منطقة الخبر
    zone = identify_news_zone(candles_5m, recent_news)
    if zone is None:
        return None

    # الخبر يجب أن يكون في نفس اتجاه الـ trend
    if zone.direction == "bullish" and trend != "bullish":
        return None
    if zone.direction == "bearish" and trend != "bearish":
        return None

    # كشف الـ Retest والرفض
    # ابدأ من الشمعة التالية لشمعة الخبر
    start_idx = zone.origin_candle_idx + 1
    if start_idx >= len(candles_5m):
        return None

    rejection = detect_retest_rejection(candles_5m, zone, start_idx)
    if rejection is None:
        return None

    return calculate_setup2_levels(symbol, zone, rejection)
