"""
config.py — إعدادات البوت المركزية
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ─── Capital.com API ───────────────────────────────────────────────────────────
# SECURITY: the base URL must be Capital.com's real host. The uploaded bundle
# pointed at "api-capital.backend.capsule.everi.one" — NOT Capital.com — which
# would have sent the login identifier, password and API key to a third party.
# Corrected to the official live/demo hosts, selected by CAPITAL_DEMO.
CAPITAL_API_KEY    = os.getenv("CAPITAL_API_KEY", "")
CAPITAL_PASSWORD   = os.getenv("CAPITAL_PASSWORD", "")
CAPITAL_IDENTIFIER = os.getenv("CAPITAL_IDENTIFIER", "")
CAPITAL_DEMO       = os.getenv("CAPITAL_DEMO", "true").lower() == "true"
CAPITAL_BASE_URL   = (
    "https://demo-api-capital.backend-capital.com/api/v1" if CAPITAL_DEMO
    else "https://api-capital.backend-capital.com/api/v1"
)

# ─── Telegram ─────────────────────────────────────────────────────────────────
# Accept TELEGRAM_BOT_TOKEN (the name used by this repo's deployment secrets),
# falling back to TELEGRAM_TOKEN for the bundle's original .env naming.
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "") or os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# ─── الأدوات والإطارات الزمنية ─────────────────────────────────────────────────
# Epics aligned to the ones proven against this Capital.com account (US100/US30/
# US500) — the bundle's original NASDAQ/DOW/SPX500 epics would 404 here.
SYMBOLS = {
    "US100": {"epic": "US100", "pip_size": 1.0,  "pip_value_per_lot": 1.0},
    "US30":  {"epic": "US30",  "pip_size": 1.0,  "pip_value_per_lot": 1.0},
    "US500": {"epic": "US500", "pip_size": 0.25, "pip_value_per_lot": 0.25},
}

TIMEFRAME_1H  = "HOUR"
TIMEFRAME_15M = "MINUTE_15"
TIMEFRAME_5M  = "MINUTE_5"

CANDLES_1H_COUNT  = 50   # آخر 50 شمعة ساعة
CANDLES_15M_COUNT = 80   # آخر 80 شمعة 15 دقيقة
CANDLES_5M_COUNT  = 100  # آخر 100 شمعة 5 دقيقة

# ─── إدارة المخاطرة ───────────────────────────────────────────────────────────
RISK_PER_TRADE_PCT   = 0.01   # 1% من الرصيد لكل صفقة
MAX_OPEN_TRADES      = 2      # أقصى صفقات مفتوحة في نفس الوقت
MAX_TRADES_PER_DAY   = 4      # أقصى صفقات يومياً
MAX_DAILY_LOSS_PCT   = 0.02   # 2% خسارة يومية → إيقاف تام
MAX_CONSEC_LOSSES    = 3      # خسائر متتالية → توقف 24 ساعة
MIN_RR_SETUP_1       = 2.0    # Sweep + BOS
MIN_RR_SETUP_2       = 2.5    # News Retest
MIN_RR_SETUP_3       = 2.0    # S&D Rejection
SL_BUFFER_POINTS     = 3      # نقاط إضافية خلف السعر الأقصى

# ─── معاملات الاستراتيجيات ───────────────────────────────────────────────────
# الاتجاه (1H)
TREND_EMA_PERIOD         = 50
SWING_LOOKBACK           = 3      # عدد الشموع يميناً ويساراً لتأكيد القمة/القاع

# الإعداد الأول — Sweep + BOS
EQUAL_LEVEL_TOLERANCE    = 3      # نقاط — فرق مقبول بين Equal Highs/Lows
SWEEP_LOOKBACK_CANDLES   = 20     # آخر 20 شمعة لرسم السيولة على 15M
MAX_CANDLES_AFTER_SWEEP  = 6      # أقصى شموع للانتظار بعد Sweep على 5M
TP1_CLOSE_PCT_S1         = 0.60   # إغلاق 60% عند TP1

# الإعداد الثاني — News Retest
NEWS_BLOCK_MINUTES       = 10     # توقف قبل الخبر بـ 10 دقائق
NEWS_RETEST_MAX_MINUTES  = 15     # أقصى وقت للانتظار بعد الخبر
NEWS_MAX_RETRACEMENT_PCT = 0.50   # أقصى تراجع من حركة الخبر
NEWS_MIN_REJECTION_WICK  = 0.60   # 60% فتيل من الشمعة الكاملة
TP1_CLOSE_PCT_S2         = 0.50

# الإعداد الثالث — S&D Rejection
SD_IMPULSE_MIN_CANDLES   = 3      # أقل اندفاع لتأكيد منطقة S&D على 1H
SD_ZONE_MAX_TOUCHES      = 2      # أقصى عدد لمسات للمنطقة قبل رفضها
SD_REJECTION_MIN_WICK    = 0.60   # 60% فتيل من الشمعة الكاملة
TP1_CLOSE_PCT_S3         = 0.50

# ─── جلسات التداول (UTC+4 دبي) ────────────────────────────────────────────────
# (ساعة, دقيقة)
SESSIONS = [
    {"name": "فتح أمريكا",    "start": (16, 30), "end": (18, 30), "priority": "high"},
    {"name": "منتصف أمريكا",  "start": (18, 30), "end": (21, 00), "priority": "high"},
    {"name": "قبل أمريكا",    "start": (14, 00), "end": (16, 30), "priority": "medium"},
]

# ─── وضع التشغيل ──────────────────────────────────────────────────────────────
# "alert_only" | "semi_auto" | "full_auto"
BOT_MODE = os.getenv("BOT_MODE", "alert_only")

# ─── الفحص كل كم ثانية ────────────────────────────────────────────────────────
SCAN_INTERVAL_SECONDS = 60  # كل دقيقة
