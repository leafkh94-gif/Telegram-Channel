"""
═══════════════════════════════════════════════════════════════════════════════
                    بوت VWAP Scalping — النسخة النهائية v3
═══════════════════════════════════════════════════════════════════════════════

⚠️⚠️⚠️  تعليمات للمبرمج — اقرأها أولاً قبل التشغيل  ⚠️⚠️⚠️

هذه النسخة تستبدل كل الاستراتيجيات القديمة بالكامل.
يجب حذف الملفات التالية من المشروع قبل التشغيل:

    ❌ احذف: strategy_1_sweep.py
    ❌ احذف: strategy_2_news.py
    ❌ احذف: strategy_3_sd.py
    ❌ احذف: أي ملف قديم باسم indicators.py (استبدله بالجديد)
    ❌ احذف: أي ملف قديم باسم config.py (استبدله بالجديد)

الملفات الجديدة المطلوبة (فقط هذه):
    ✅ config.py
    ✅ indicators.py
    ✅ strategy.py          ← الاستراتيجية الوحيدة الآن
    ✅ capital_client.py
    ✅ risk_manager.py
    ✅ telegram_bot.py
    ✅ main.py              ← هذا الملف

أمر الحذف السريع (نفّذه في مجلد المشروع):
    rm -f strategy_1_sweep.py strategy_2_news.py strategy_3_sd.py

═══════════════════════════════════════════════════════════════════════════════

منطق الاستراتيجية (مبني على بحث حقيقي — إجماع المصادر المحترفة):
    1. VWAP:  السعر فوق = شراء فقط | تحت = بيع فقط
    2. EMA:   السعر فوق EMA9 و EMA9 > EMA21 (شراء) | العكس (بيع)
    3. RSI:   RSI(3) ضمن النطاق الصحي (زخم بلا تشبع)
    4. ATR:   Stop = 1× ATR | Target = 2× ATR (RR 1:2)
    5. الوقت: 24 ساعة الاثنين-الجمعة (فلتر ATR يتكفّل بالساعات الميتة)

يعمل على: US100 | US30 | XAUUSD (الذهب)
24 ساعة من الاثنين للجمعة | يتوقف السبت والأحد (لا قيد جلسة)
═══════════════════════════════════════════════════════════════════════════════
"""

import time
import logging
from datetime import datetime, timezone, timedelta

from config import (
    SYMBOLS, TIMEFRAME_ENTRY, CANDLES_COUNT,
    SCAN_INTERVAL_SECONDS, BOT_MODE, SYMBOL_COOLDOWN_MIN
)
from capital_client import CapitalClient
from strategy       import scan
from risk_manager   import can_trade, calculate_lot_size, record_trade_open, get_daily_stats
from telegram_bot   import send_message, format_signal, process_commands
from tracker        import log_signal, update_open_signals

# استيراد محمي: لو تعذّر تحميل التقرير لأي سبب، يستمر البوت في عمله الأساسي
try:
    from reporter import send_daily_report
    REPORTER_AVAILABLE = True
except ImportError as e:
    REPORTER_AVAILABLE = False
    logging.getLogger("MAIN").error(f"⚠️ التقرير اليومي غير متاح: {e}")
    def send_daily_report(*a, **k): pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("bot.log", encoding="utf-8"),
    ]
)
logger = logging.getLogger("MAIN")


def is_weekend() -> bool:
    return datetime.now(timezone.utc).weekday() >= 5


# ─── منع تكرار الإشارات ──────────────────────────────────────────────────────
# شروط هذه الاستراتيجية حالة مستمرة، لا حدثاً لحظياً: طالما السعر فوق VWAP و
# الـ EMA مرتّبة و RSI فوق 50، تبقى الشروط صحيحة عشرات الشموع. مع فحص كل 60
# ثانية وشمعة 5 دقائق، تُرسل نفس الإشارة ~145 مرة للحركة الواحدة (قياس فعلي).
# هذا الحارس يرسل الإشارة مرة ثم يكتمها مدة تهدئة.
# التهدئة لكل **أداة**، لا لكل (أداة+اتجاه). البصمة السابقة symbol:direction
# كانت تجعل BUY و SELL بصمتين منفصلتين، فيمرّان معاً — وهكذا وصلت إشارتان
# متعاكستان على نفس السوق خلال 8-13 دقيقة. الآن أي إشارة تكتم الأداة كلها.
_recent_alerts: dict = {}

def _is_duplicate(symbol: str) -> bool:
    t = _recent_alerts.get(symbol)
    return t is not None and (datetime.now(timezone.utc) - t) < timedelta(minutes=SYMBOL_COOLDOWN_MIN)


def scan_symbol(symbol: str, config: dict, client: CapitalClient, bot_mode: str,
                candles_out: dict | None = None):
    epic = config["epic"]

    candles = client.get_candles(epic, TIMEFRAME_ENTRY, CANDLES_COUNT)
    if not candles:
        logger.warning(f"⚠️ {symbol}: لا بيانات")
        return

    if candles_out is not None:
        candles_out[symbol] = candles

    signal = scan(symbol, candles)
    if signal is None:
        return

    balance  = client.get_account_balance()
    open_pos = client.get_open_positions()
    allowed, reason = can_trade(balance, len(open_pos))
    if not allowed:
        logger.info(f"🚫 {reason}")
        return

    if _is_duplicate(symbol):
        last = _recent_alerts[symbol]
        mins = (datetime.now(timezone.utc) - last).total_seconds() / 60
        logger.info(
            f"🔁 {symbol} {signal.direction} — مكتومة "
            f"(آخر إشارة قبل {mins:.0f}د، التهدئة {SYMBOL_COOLDOWN_MIN}د)"
        )
        return
    _recent_alerts[symbol] = datetime.now(timezone.utc)

    lot_size = calculate_lot_size(symbol, balance, signal.entry, signal.sl)
    msg = format_signal(signal, lot_size)
    log_signal(signal, symbol, lot_size)

    if bot_mode == "alert_only":
        send_message(msg)
        logger.info(f"📤 إشارة أُرسلت: {symbol} {signal.direction}")
    elif bot_mode == "full_auto":
        send_message(msg)
        result = client.place_order(
            epic=epic, direction=signal.direction, size=lot_size,
            stop_level=signal.sl, profit_level=signal.tp
        )
        if result:
            record_trade_open()


def main():
    logger.info("🚀 بوت VWAP Scalping v3 — بدء")
    send_message(
        f"🤖 <b>بوت VWAP Scalping — يعمل</b>\n"
        f"الوضع: {BOT_MODE}\n"
        f"الأدوات: US100 · US30 · XAUUSD\n"
        f"المنطق: VWAP + EMA + RSI + ATR\n"
        f"الجدول: الاثنين—الجمعة | 24 ساعة"
    )

    client    = CapitalClient()
    bot_state = {"mode": BOT_MODE, "paused": False, "offset": 0}
    last_report_date = None

    while True:
        try:
            bot_state = process_commands(bot_state)

            # التقرير اليومي 21:00 UTC — مرة واحدة في اليوم.
            # محاط بحماية: فشل التقرير يجب ألا يوقف البوت عن إرسال الإشارات.
            now_utc = datetime.now(timezone.utc)
            if now_utc.hour == 21 and last_report_date != now_utc.date():
                try:
                    send_daily_report()
                except Exception as e:
                    logger.error(f"❌ خطأ التقرير (لا يؤثر على البوت): {e}")
                last_report_date = now_utc.date()

            if bot_state["paused"]:
                time.sleep(SCAN_INTERVAL_SECONDS)
                continue

            if is_weekend():
                logger.info("🔴 عطلة نهاية الأسبوع")
                time.sleep(300)
                continue

            candles_all: dict = {}
            for symbol, config in SYMBOLS.items():
                try:
                    scan_symbol(symbol, config, client, bot_state["mode"], candles_all)
                except Exception as e:
                    logger.error(f"❌ خطأ {symbol}: {e}")

            try:
                update_open_signals(candles_all)
            except Exception as e:
                logger.error(f"❌ خطأ متابعة الإشارات: {e}")

            logger.info(f"✅ دورة مكتملة — انتظار {SCAN_INTERVAL_SECONDS}s")
            time.sleep(SCAN_INTERVAL_SECONDS)

        except KeyboardInterrupt:
            send_message("🛑 البوت أُوقف يدوياً")
            break
        except Exception as e:
            logger.error(f"❌ خطأ رئيسي: {e}")
            time.sleep(30)


if __name__ == "__main__":
    main()
