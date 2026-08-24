"""
main.py — حلقة التشغيل الرئيسية للبوت
"""

import time
import logging
from datetime import datetime, timezone

from config import (
    SYMBOLS, TIMEFRAME_1H, TIMEFRAME_15M, TIMEFRAME_5M,
    CANDLES_1H_COUNT, CANDLES_15M_COUNT, CANDLES_5M_COUNT,
    SCAN_INTERVAL_SECONDS, BOT_MODE, SESSIONS
)
from capital_client  import CapitalClient
from indicators      import detect_trend_1h
import setup_tracker   # الإعداد الأول أصبح متتبّعاً بحالة مستمرة (Sweep→BOS→دخول)
from strategy_2_news  import scan_setup_2, fetch_news_events, is_news_block_active
from strategy_3_sd    import scan_setup_3
from risk_manager     import can_trade, calculate_lot_size, record_trade_open, get_daily_stats
from telegram_bot     import (
    send_message, format_setup1_message, format_setup2_message,
    format_setup3_message, send_halt_alert, send_news_block_alert,
    process_commands
)

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("bot.log", encoding="utf-8"),
    ]
)
logger = logging.getLogger("MAIN")


# ─── فحص الجلسة ──────────────────────────────────────────────────────────────
def is_session_active() -> bool:
    now = datetime.now(timezone.utc)
    # تحويل لـ UTC+4 (دبي)
    hour_dubai   = (now.hour + 4) % 24
    minute_dubai = now.minute

    current_minutes = hour_dubai * 60 + minute_dubai

    for session in SESSIONS:
        start = session["start"][0] * 60 + session["start"][1]
        end   = session["end"][0]   * 60 + session["end"][1]
        if start <= current_minutes <= end:
            return True
    return False


# ─── حلقة الفحص لكل أداة ─────────────────────────────────────────────────────
def scan_symbol(symbol: str, config: dict, client: CapitalClient,
                news_events: list, bot_mode: str) -> None:
    epic = config["epic"]
    logger.info(f"🔍 فحص {symbol}")

    # جلب الشموع
    candles_1h  = client.get_candles(epic, TIMEFRAME_1H,  CANDLES_1H_COUNT)
    candles_15m = client.get_candles(epic, TIMEFRAME_15M, CANDLES_15M_COUNT)
    candles_5m  = client.get_candles(epic, TIMEFRAME_5M,  CANDLES_5M_COUNT)

    if not candles_1h or not candles_15m or not candles_5m:
        logger.warning(f"⚠️ بيانات ناقصة للأداة {symbol}")
        return

    current_price_data = client.get_current_price(epic)
    if not current_price_data:
        return
    current_price = current_price_data["mid"]

    # تحديد الاتجاه
    trend = detect_trend_1h(candles_1h)
    logger.info(f"📈 {symbol} | اتجاه 1H: {trend} | سعر: {current_price:.2f}")

    if trend == "neutral":
        logger.info(f"↔️ {symbol} محايد — تخطي")
        return

    # فحص الرصيد والحدود
    balance  = client.get_account_balance()
    open_pos = client.get_open_positions()
    allowed, reason = can_trade(balance, len(open_pos))
    if not allowed:
        logger.info(f"🚫 {reason}")
        return

    signal_found = None
    setup_num    = None

    # ─ الإعداد الأول: Sweep → BOS (متتبّع بحالة مستمرة يتذكّر الـ Sweep عبر الفحوصات)
    s1, s1_reason = setup_tracker.process(symbol, trend, candles_15m, candles_5m, current_price)
    logger.info(f"🧭 {symbol} إعداد1: {s1_reason}")
    if s1:
        logger.info(f"🎯 إعداد 1 مؤكَّد: {symbol} {s1.direction}")
        signal_found = s1
        setup_num    = 1

    # ─ الإعداد الثاني: News Retest (فقط إذا لا يوجد إعداد 1)
    if not signal_found:
        s2 = scan_setup_2(symbol, trend, candles_5m, news_events)
        if s2:
            logger.info(f"🎯 إعداد 2 مكتشف: {symbol} {s2.direction}")
            signal_found = s2
            setup_num    = 2

    # ─ الإعداد الثالث: S&D Rejection
    if not signal_found:
        s3 = scan_setup_3(symbol, trend, candles_1h, candles_5m, current_price)
        if s3:
            logger.info(f"🎯 إعداد 3 مكتشف: {symbol} {s3.direction}")
            signal_found = s3
            setup_num    = 3

    if not signal_found:
        # تشخيص: لماذا لا توجد إشارة رغم وجود اتجاه — نُظهر أين توقّف كل إعداد
        logger.info(f"❌ {symbol} لا إشارة (اتجاه={trend}) | إعداد1={s1_reason}")
        return

    # حساب حجم اللوت
    lot_size = calculate_lot_size(symbol, balance, signal_found.entry, signal_found.sl)

    # تنسيق الرسالة
    if setup_num == 1:
        msg = format_setup1_message(signal_found, lot_size, trend)
    elif setup_num == 2:
        msg = format_setup2_message(signal_found, lot_size, trend)
    else:
        msg = format_setup3_message(signal_found, lot_size, trend)

    # ─ الإرسال / التنفيذ حسب الوضع
    if bot_mode == "alert_only":
        send_message(msg)
        logger.info(f"📤 إشارة أُرسلت لتيليغرام: {symbol} {signal_found.direction}")

    elif bot_mode == "semi_auto":
        send_message(msg + "\n\n⚡ أرسل /confirm للتنفيذ")

    elif bot_mode == "full_auto":
        send_message(msg)
        result = client.place_order(
            epic=epic,
            direction=signal_found.direction,
            size=lot_size,
            stop_level=signal_found.sl,
            profit_level=signal_found.tp1,
        )
        if result:
            record_trade_open()
            logger.info(f"✅ صفقة مفتوحة تلقائياً: {symbol}")
        else:
            send_message(f"❌ فشل فتح صفقة {symbol} — راجعي الحساب")


# ─── الحلقة الرئيسية ─────────────────────────────────────────────────────────
def main():
    logger.info("🚀 بدء تشغيل البوت")
    send_message("🤖 <b>بوت السكالبينغ بدأ التشغيل</b>\nالوضع: " + BOT_MODE)

    client    = CapitalClient()
    bot_state = {"mode": BOT_MODE, "paused": False, "offset": 0}

    # جلب الأخبار مرة عند البداية
    news_events      = fetch_news_events()
    news_fetch_hour  = datetime.now(timezone.utc).hour

    while True:
        try:
            # معالجة أوامر تيليغرام
            bot_state = process_commands(bot_state)

            if bot_state["paused"]:
                time.sleep(SCAN_INTERVAL_SECONDS)
                continue

            # تحديث الأخبار كل ساعة
            current_hour = datetime.now(timezone.utc).hour
            if current_hour != news_fetch_hour:
                news_events     = fetch_news_events()
                news_fetch_hour = current_hour

            # فحص حجب الأخبار
            blocked, news_title = is_news_block_active(news_events)
            if blocked:
                logger.info(f"📰 حجب الأخبار: {news_title}")
                send_news_block_alert(news_title)
                time.sleep(60)
                continue

            # فحص الجلسة
            if not is_session_active():
                logger.info("💤 خارج ساعات التداول")
                time.sleep(60)
                continue

            # فحص كل أداة
            current_mode = bot_state["mode"]
            for symbol, config in SYMBOLS.items():
                try:
                    scan_symbol(symbol, config, client, news_events, current_mode)
                except Exception as e:
                    logger.error(f"❌ خطأ فحص {symbol}: {e}")

            logger.info(f"✅ دورة فحص مكتملة — انتظار {SCAN_INTERVAL_SECONDS} ثانية")
            time.sleep(SCAN_INTERVAL_SECONDS)

        except KeyboardInterrupt:
            logger.info("🛑 إيقاف يدوي")
            send_message("🛑 البوت أُوقف يدوياً")
            break

        except Exception as e:
            # Log only — do NOT Telegram on every loop error, or a persistent fault
            # turns into a message every 30s. Errors are visible in the run logs.
            logger.error(f"❌ خطأ رئيسي: {e}")
            time.sleep(30)


if __name__ == "__main__":
    main()
