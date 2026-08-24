"""
strategy_2_news.py — الإعداد الثاني (v2 — إصلاح timezone الأخبار)

FIX 3: Forex Factory يعطي الأوقات بـ EST/EDT وليس UTC.
الكود القديم كان يستخدم .replace(tzinfo=UTC) = خطأ فادح.
الإصلاح: .astimezone(UTC) = تحويل صحيح من EST لـ UTC.
"""

import logging
import requests
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from typing import Optional
from config import (
    NEWS_BLOCK_MINUTES, NEWS_RETEST_MAX_MINUTES,
    NEWS_MAX_RETRACEMENT_PCT, NEWS_MIN_REJECTION_WICK,
    SL_BUFFER_POINTS, MIN_RR_SETUP_2
)

logger = logging.getLogger(__name__)

# Forex Factory يستخدم US Eastern — EST = UTC-5، EDT = UTC-4
# نستخدم UTC-5 كـ fallback محافظ
FF_EST_OFFSET = timezone(timedelta(hours=-5))


@dataclass
class NewsEvent:
    title:    str
    currency: str
    impact:   str
    time_utc: datetime


@dataclass
class NewsZone:
    top:          float
    bottom:       float
    direction:    str
    news_title:   str
    impulse_size: float
    origin_candle_idx: int


@dataclass
class Setup2Signal:
    symbol:      str
    direction:   str
    entry:       float
    sl:          float
    tp1:         float
    tp2:         float
    rr:          float
    news_title:  str
    zone_top:    float
    zone_bottom: float


# ─── FIX 3: جلب الأخبار مع تحويل التوقيت الصحيح ────────────────────────────
def fetch_news_events() -> list[NewsEvent]:
    try:
        r = requests.get(
            "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
            timeout=8
        )
        r.raise_for_status()
        all_events = r.json()

        today_utc = datetime.now(timezone.utc).date()
        events    = []

        for e in all_events:
            if e.get("impact", "").lower() != "high":
                continue

            date_str = e.get("date", "")
            if not date_str:
                continue

            try:
                parsed = datetime.fromisoformat(date_str)

                if parsed.tzinfo is not None:
                    # ✅ التوقيت موجود في الـ string — حوّله لـ UTC بشكل صحيح
                    # .replace() خطأ | .astimezone() صحيح
                    event_utc = parsed.astimezone(timezone.utc)
                else:
                    # لا يوجد timezone → افترض EST (UTC-5)
                    event_utc = parsed.replace(tzinfo=FF_EST_OFFSET).astimezone(timezone.utc)

                if event_utc.date() != today_utc:
                    continue

                events.append(NewsEvent(
                    title=e.get("title", ""),
                    currency=e.get("currency", "USD"),
                    impact="high",
                    time_utc=event_utc
                ))

            except Exception as parse_err:
                logger.debug(f"⚠️ خطأ تحليل تاريخ الخبر: {parse_err}")
                continue

        logger.info(f"📰 أخبار اليوم عالية التأثير: {len(events)}")
        if events:
            for ev in events:
                logger.info(f"  ↳ {ev.title} @ {ev.time_utc.strftime('%H:%M')} UTC")

        return events

    except Exception as ex:
        logger.error(f"❌ خطأ جلب الأخبار: {ex}")
        return []


def is_news_block_active(events: list[NewsEvent]) -> tuple[bool, str]:
    now = datetime.now(timezone.utc)
    for e in events:
        delta_min = (e.time_utc - now).total_seconds() / 60
        if -5 <= delta_min <= NEWS_BLOCK_MINUTES:
            return True, e.title
    return False, ""


def get_recent_high_impact_news(
    events: list[NewsEvent],
    within_minutes: int = None
) -> Optional[NewsEvent]:
    if within_minutes is None:
        within_minutes = NEWS_RETEST_MAX_MINUTES
    now = datetime.now(timezone.utc)
    for e in sorted(events, key=lambda x: x.time_utc, reverse=True):
        elapsed = (now - e.time_utc).total_seconds() / 60
        if 0 <= elapsed <= within_minutes:
            return e
    return None


# ─── بناء منطقة الخبر ─────────────────────────────────────────────────────────
def identify_news_zone(
    candles_5m: list[dict],
    news_event: NewsEvent
) -> Optional[NewsZone]:

    news_time = news_event.time_utc
    post_news = []

    for i, c in enumerate(candles_5m):
        try:
            c_time = datetime.fromisoformat(
                c["time"].replace("Z", "+00:00")
            ).astimezone(timezone.utc)
        except Exception:
            continue
        diff_min = (c_time - news_time).total_seconds() / 60
        if 0 <= diff_min <= 30:
            post_news.append((i, c))

    if not post_news:
        logger.info(f"⚠️ لا شموع 5M بعد خبر: {news_event.title}")
        return None

    news_candle_idx, news_candle = max(
        post_news[:6],
        key=lambda x: x[1]["high"] - x[1]["low"]
    )

    body_size = abs(news_candle["close"] - news_candle["open"])
    if body_size < (news_candle["high"] - news_candle["low"]) * 0.25:
        return None

    direction = "bullish" if news_candle["close"] > news_candle["open"] else "bearish"

    if direction == "bullish":
        zone_top    = news_candle["open"] + body_size * 0.50
        zone_bottom = news_candle["open"]
    else:
        zone_top    = news_candle["open"]
        zone_bottom = news_candle["open"] - body_size * 0.50

    logger.info(
        f"📰 منطقة الخبر: {zone_bottom:.2f}—{zone_top:.2f} | "
        f"{direction} | {news_event.title}"
    )
    return NewsZone(
        top=zone_top, bottom=zone_bottom,
        direction=direction, news_title=news_event.title,
        impulse_size=body_size, origin_candle_idx=news_candle_idx
    )


# ─── كشف الـ Retest والرفض ───────────────────────────────────────────────────
def detect_retest_rejection(
    candles_5m: list[dict],
    zone: NewsZone,
    from_idx: int
) -> Optional[dict]:

    search = candles_5m[from_idx: from_idx + 6]

    for i, c in enumerate(search):
        price_in_zone = c["low"] <= zone.top and c["high"] >= zone.bottom
        if not price_in_zone:
            continue

        candle_range = c["high"] - c["low"]
        if candle_range == 0:
            continue

        if zone.direction == "bullish":
            lower_wick = min(c["open"], c["close"]) - c["low"]
            wick_ratio = lower_wick / candle_range
            is_rejection = wick_ratio >= NEWS_MIN_REJECTION_WICK and c["close"] > c["open"]
        else:
            upper_wick = c["high"] - max(c["open"], c["close"])
            wick_ratio = upper_wick / candle_range
            is_rejection = wick_ratio >= NEWS_MIN_REJECTION_WICK and c["close"] < c["open"]

        if not is_rejection:
            continue

        if i + 1 >= len(search):
            continue
        confirm = search[i + 1]
        if zone.direction == "bullish" and confirm["close"] <= confirm["open"]:
            continue
        if zone.direction == "bearish" and confirm["close"] >= confirm["open"]:
            continue

        if zone.impulse_size > 0:
            retracement = abs(
                (zone.bottom - c["low"]) if zone.direction == "bullish"
                else (c["high"] - zone.top)
            ) / zone.impulse_size
            if retracement > NEWS_MAX_RETRACEMENT_PCT:
                continue

        logger.info(f"✅ News Retest Rejection | wick={wick_ratio:.1%}")
        return {"rejection_candle": c, "confirm_candle": confirm, "wick_ratio": wick_ratio}

    return None


# ─── حساب معاملات الدخول ─────────────────────────────────────────────────────
def calculate_setup2_levels(
    symbol: str, zone: NewsZone,
    rejection: dict
) -> Optional[Setup2Signal]:

    c         = rejection["rejection_candle"]
    direction = "BUY" if zone.direction == "bullish" else "SELL"
    entry     = c["close"]

    if direction == "BUY":
        sl  = c["low"] - SL_BUFFER_POINTS
        tp1 = zone.top + zone.impulse_size
        tp2 = zone.top + zone.impulse_size * 1.5
    else:
        sl  = c["high"] + SL_BUFFER_POINTS
        tp1 = zone.bottom - zone.impulse_size
        tp2 = zone.bottom - zone.impulse_size * 1.5

    sl_dist  = abs(entry - sl)
    tp1_dist = abs(tp1 - entry)
    if sl_dist == 0:
        return None
    rr = tp1_dist / sl_dist

    if rr < MIN_RR_SETUP_2:
        return None

    return Setup2Signal(
        symbol=symbol, direction=direction,
        entry=round(entry, 2), sl=round(sl, 2),
        tp1=round(tp1, 2), tp2=round(tp2, 2),
        rr=round(rr, 2), news_title=zone.news_title,
        zone_top=round(zone.top, 2), zone_bottom=round(zone.bottom, 2),
    )


# ─── الدالة الرئيسية ──────────────────────────────────────────────────────────
def scan_setup_2(
    symbol: str, trend: str,
    candles_5m: list[dict],
    news_events: list[NewsEvent]
) -> Optional[Setup2Signal]:

    if trend == "neutral":
        return None

    recent_news = get_recent_high_impact_news(news_events)
    if recent_news is None:
        return None

    zone = identify_news_zone(candles_5m, recent_news)
    if zone is None:
        return None

    if zone.direction == "bullish" and trend != "bullish":
        return None
    if zone.direction == "bearish" and trend != "bearish":
        return None

    start_idx = zone.origin_candle_idx + 1
    if start_idx >= len(candles_5m):
        return None

    rejection = detect_retest_rejection(candles_5m, zone, start_idx)
    if rejection is None:
        return None

    return calculate_setup2_levels(symbol, zone, rejection)
