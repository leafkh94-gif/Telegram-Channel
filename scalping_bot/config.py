"""
config.py — إعدادات البوت المركزية (v2 — إصلاح شامل)
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ─── Capital.com API ───────────────────────────────────────────────────────────
# أمان: الرابط يجب أن يكون دومين Capital.com الرسمي. النسخة المرفوعة كانت تشير إلى
# "api-capital.backend.capsule.everi.one" — وهو ليس Capital.com، وكان سيرسل
# المُعرّف وكلمة المرور ومفتاح الـ API لطرف ثالث. صُحّح للمضيف الرسمي (حسب CAPITAL_DEMO).
# كذلك .strip() لكل بيانات الدخول: أسرار GitHub غالباً فيها سطر جديد زائد، و requests
# يرفض قيمة هيدر فيها "\n" فينهار البوت عند الإقلاع.
CAPITAL_API_KEY    = os.getenv("CAPITAL_API_KEY", "").strip()
CAPITAL_PASSWORD   = os.getenv("CAPITAL_PASSWORD", "").strip()
CAPITAL_IDENTIFIER = os.getenv("CAPITAL_IDENTIFIER", "").strip()
CAPITAL_DEMO       = os.getenv("CAPITAL_DEMO", "true").strip().lower() == "true"
CAPITAL_BASE_URL   = (
    "https://demo-api-capital.backend-capital.com/api/v1" if CAPITAL_DEMO
    else "https://api-capital.backend-capital.com/api/v1"
)

# ─── Telegram ─────────────────────────────────────────────────────────────────
# يقرأ TELEGRAM_BOT_TOKEN (اسم السرّ في هذا المستودع) مع رجوع لـ TELEGRAM_TOKEN.
TELEGRAM_TOKEN   = (os.getenv("TELEGRAM_BOT_TOKEN", "") or os.getenv("TELEGRAM_TOKEN", "")).strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# ─── الأدوات ──────────────────────────────────────────────────────────────────
# الـ epics مطابقة لما يعمل فعلاً على هذا الحساب (US100/US30/US500) — قيم
# NASDAQ/DOW/SPX500 كانت تُرجع 404 هنا.
SYMBOLS = {
    "US100": {"epic": "US100", "pip_size": 1.0,  "pip_value_per_lot": 1.0},
    "US30":  {"epic": "US30",  "pip_size": 1.0,  "pip_value_per_lot": 1.0},
    "US500": {"epic": "US500", "pip_size": 0.25, "pip_value_per_lot": 0.25},
}

TIMEFRAME_1H  = "HOUR"
TIMEFRAME_15M = "MINUTE_15"
TIMEFRAME_5M  = "MINUTE_5"

# ─── FIX 1: الشموع والـ EMA ────────────────────────────────────────────────────
# الخطأ السابق: 50 شمعة + EMA-50 = EMA ساكن لا يتحرك
# الإصلاح: 120 شمعة + EMA-20 يعطي تاريخاً كافياً واستجابة يومية
CANDLES_1H_COUNT  = 120  # كان 50 — يجب أن يكون أكبر بكثير من EMA_PERIOD
CANDLES_15M_COUNT = 80
CANDLES_5M_COUNT  = 100

# ─── إدارة المخاطرة ───────────────────────────────────────────────────────────
RISK_PER_TRADE_PCT   = 0.01
MAX_OPEN_TRADES      = 2
MAX_TRADES_PER_DAY   = 4
MAX_DAILY_LOSS_PCT   = 0.02
MAX_CONSEC_LOSSES    = 3
MIN_RR_SETUP_1       = 2.0
MIN_RR_SETUP_2       = 2.5
MIN_RR_SETUP_3       = 2.0
SL_BUFFER_POINTS     = 5

# ─── معاملات الاستراتيجيات ───────────────────────────────────────────────────
# FIX 1: EMA-20 أكثر استجابة للاتجاه اليومي — كان 50
TREND_EMA_PERIOD         = 20
SWING_LOOKBACK           = 3

# الإعداد الأول — Sweep + BOS
EQUAL_LEVEL_TOLERANCE    = 5       # نقاط — رفعناها من 3 لـ 5 للمؤشرات ذات النطاقات الواسعة
SWEEP_LOOKBACK_CANDLES   = 20      # للسيولة على 15M
SWEEP_DETECTION_CANDLES  = 8       # FIX 2: كان 3 — الآن 8 شموع (120 دقيقة)
MAX_CANDLES_AFTER_SWEEP  = 6       # شموع 5M للبحث عن BOS بعد الـ Sweep
# FIX 4: نسبة الارتداد للدخول حين لا يوجد FVG.
# الخطأ السابق: الدخول عند مستوى الـ BOS نفسه (قمة الاندفاع) بينما الوقف عند قاع
# الاصطياد — فتصير المخاطرة = الساق كاملة و RR أقل من 1 دائماً (مثال حقيقي من
# اللوق: دخول 29296 / وقف 29215 / هدف 29329 → RR 0.41 مرفوض).
# الصحيح في SMC: ننتظر ارتداد السعر داخل ساق (الاصطياد → BOS) ثم ندخل بخصم.
# 0.618 = منطقة OTE القياسية. نفس المثال يصير RR 2.36 ✅
ENTRY_RETRACE_PCT        = 0.618
TP1_CLOSE_PCT_S1         = 0.60

# ─── (أ) الاصطياد المعاكس للاتجاه ────────────────────────────────────────────
# كان الفلتر يرفض كل اصطياد لا يوافق اتجاه 1H. في سوق هابط، الاصطيادات التي تقع
# فعلاً هي صاعدة (السعر ينزل تحت قاع ثم يرتد فوقه) — فكانت تُرمى كلها. تعداد على
# هبوط 443 نقطة: 6 من أصل 7 اصطيادات رُفضت لهذا السبب وحده.
# الآن نسمح بها بشرطين إضافيين لأنها أخطر: وجود FVG (دليل اندفاع حقيقي) وعتبة
# RR أعلى. أي إعداد معاكس يُوسم في الرسالة حتى تعرفي أنك تتداولين ضد الاتجاه.
ALLOW_COUNTER_TREND      = True
MIN_RR_COUNTER_TREND     = 2.5     # أعلى من MIN_RR_SETUP_1 = 2.0
COUNTER_TREND_NEEDS_FVG  = True

# ─── (ب) الإعداد الرابع — استمرارية الاتجاه ──────────────────────────────────
# الثغرة الحقيقية: الإعدادات 1-3 كلها تنتظر انعكاساً بعد اصطياد سيولة، فالبوت
# أعمى تماماً عن الحركة المتجهة نفسها. هبوط 400-500 نقطة على كل المؤشرات مرّ
# بصفر إشارات. هذا الإعداد يدخل مع الاتجاه على ارتداد، بلا اشتراط اصطياد.
CONT_LEG_LOOKBACK_15M    = 30      # شموع 15M للبحث عن ساق الاندفاع
CONT_MIN_LEG_POINTS      = 40      # أدنى حجم ساق تُعتبر اندفاعاً حقيقياً
CONT_MIN_RETRACE         = 0.236   # أدنى ارتداد مقبول للدخول
CONT_MAX_RETRACE         = 0.786   # أقصى ارتداد — أبعد منه يعني الاتجاه انكسر
MIN_RR_SETUP_4           = 2.0

# الإعداد الثاني — News Retest
NEWS_BLOCK_MINUTES       = 10
NEWS_RETEST_MAX_MINUTES  = 20
NEWS_MAX_RETRACEMENT_PCT = 0.50
NEWS_MIN_REJECTION_WICK  = 0.60
TP1_CLOSE_PCT_S2         = 0.50

# الإعداد الثالث — S&D Rejection
SD_IMPULSE_MIN_CANDLES   = 3
SD_ZONE_MAX_TOUCHES      = 2
SD_REJECTION_MIN_WICK    = 0.60
TP1_CLOSE_PCT_S3         = 0.50
# FIX 3: كانت 10 — رفعناها لـ 80 للمؤشرات ذات الحركة الواسعة
ZONE_PROXIMITY_POINTS    = 80

# ─── تشغيل 24/7 الاثنين-الجمعة ───────────────────────────────────────────────
# لا توجد قيود على الجلسات — البوت يشتغل طوال أيام الأسبوع
# يتوقف تلقائياً السبت والأحد (السوق مغلق)

# ─── وضع التشغيل ──────────────────────────────────────────────────────────────
BOT_MODE = os.getenv("BOT_MODE", "alert_only")

# ─── الفحص كل كم ثانية ────────────────────────────────────────────────────────
SCAN_INTERVAL_SECONDS = 60
