"""
config.py — الاتصال والأدوات فقط

لا معاملات استراتيجية هنا بعد الآن. حُذفت كلها مع الاستراتيجية القديمة،
وتُضاف الجديدة حين تُقاس لا حين تُقترح.
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ─── Capital.com API ───────────────────────────────────────────────────────────
# .strip() ضروري وليس تجميلاً: أسرار GitHub غالباً فيها سطر جديد زائد، و requests
# يرفض قيمة هيدر تحتوي "\n" برمي ValueError عند الإقلاع. هذا بالضبط ما سبّب
# انهياراً متكرراً سابقاً — كل تشغيل يموت خلال ثوانٍ ويُعيد إطلاق نفسه، فوصلتك
# رسالة "البوت يعمل" كل ثانية تقريباً.
CAPITAL_API_KEY    = os.getenv("CAPITAL_API_KEY", "").strip()
CAPITAL_PASSWORD   = os.getenv("CAPITAL_PASSWORD", "").strip()
CAPITAL_IDENTIFIER = os.getenv("CAPITAL_IDENTIFIER", "").strip()
CAPITAL_DEMO       = os.getenv("CAPITAL_DEMO", "true").strip().lower() == "true"
# الرابط يتحدد تلقائياً حسب demo/live
if CAPITAL_DEMO:
    CAPITAL_BASE_URL = "https://demo-api-capital.backend-capital.com/api/v1"
else:
    CAPITAL_BASE_URL = "https://api-capital.backend-capital.com/api/v1"

# ─── Telegram ─────────────────────────────────────────────────────────────────
# اسم السرّ في هذا المستودع هو TELEGRAM_BOT_TOKEN (وهو ما يمرّره الـ workflow).
# قراءة TELEGRAM_TOKEN وحده كانت ستُرجع نصاً فارغاً فيفشل الإرسال بصمت.
TELEGRAM_TOKEN   = (os.getenv("TELEGRAM_BOT_TOKEN", "") or os.getenv("TELEGRAM_TOKEN", "")).strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# ─── الأدوات ──────────────────────────────────────────────────────────────────
# الـ epics مطابقة لما يعمل فعلاً على هذا الحساب
# (NASDAQ/DOW/SPX500 كانت تُرجع 404 — نستخدم US100/US30/US500)
# US500 أُعيدت: فحص الدلالة الإحصائية على بيانات الجمعة أظهر أن الفروق
# بين الأدوات كلها ضمن الضجيج (احتمال الصدفة 0.17-1.00)، فحذفها كان
# قراراً على عيّنة يوم واحد لا تدعمه. نجمع أسبوعاً ثم نقرر.
SYMBOLS = {
    "US100":  {"epic": "US100",  "pip_size": 1.0,  "min_atr": 5.0},
    "US30":   {"epic": "US30",   "pip_size": 1.0,  "min_atr": 8.0},
    "US500":  {"epic": "US500",  "pip_size": 0.25, "min_atr": 1.5},
    "XAUUSD": {"epic": "GOLD",   "pip_size": 0.1,  "min_atr": 1.0},  # الذهب
}

# الإطار الأساسي للدخول
TIMEFRAME_ENTRY = "MINUTE_5"
CANDLES_COUNT   = 100   # آخر 100 شمعة 5M — كافية لـ VWAP و EMA و RSI و ATR

# ─── الأطر الزمنية ────────────────────────────────────────────────────────────
TIMEFRAME_DAILY = "DAY"
TIMEFRAME_H4    = "HOUR_4"
TIMEFRAME_H1    = "HOUR"
TIMEFRAME_M5    = "MINUTE_5"

CANDLES_DAILY = 120     # يكفي EMA20 بهامش واسع
CANDLES_H4    = 120
CANDLES_H1    = 200     # قناة 48 + ATR 14 + هامش
CANDLES_M5    = 30      # لحارس الملاحقة فقط
