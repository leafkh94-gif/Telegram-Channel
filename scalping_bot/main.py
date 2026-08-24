"""
main.py — حلقة التشغيل الرئيسية (v2)

التغييرات:
- تشغيل 24/7 من الاثنين للجمعة (إزالة قيود الجلسات)
- إيقاف تلقائي السبت والأحد
"""

import time
import logging
from datetime import datetime, timezone

from config import (
    SYMBOLS, TIMEFRAME_1H, TIMEFRAME_15M, TIMEFRAME_5M,
    CANDLES_1H_COUNT, CANDLES_15M_COUNT, CANDLES_5M_COUNT,
    SCAN_INTERVAL_SECONDS, BOT_MODE
)
from capital_client   import CapitalClient
from indicators       import detect_trend_1h
from strategy_1_sweep import scan_setup_1
from strategy_2_news  import scan_setup_2, fetch_news_events, is_news_block_active
from strategy_3_sd    import scan_setup_3
from risk_manager     import can_trade, calculate_lot_size, record_trade_open, get_daily_stats
from telegram_bot     import (
    send_message, format_setup1_message, format_setup2_message,
    format_setup3_message, send_halt_alert, send_news_block_alert,
    process_commands
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("bot.log", encoding="utf-8"),
    ]
)
logger = logging.getLogger("MAIN")


# ─── منع تكرار الإشارات ──────────────────────────────────────────────────────
# منطق الإعداد الأول في v2 عديم الحالة: طالما الـ Sweep ضمن آخر 8 شموع والـ BOS
# قائم، تُعيد نفس الإشارة كل 60 ثانية (حتى ~ساعتين) = إغراق. هذا الحارس يرسل كل
# إعداد مرة واحدة ثم يكتمه لمدة تهدئة. (في الذاكرة — قد يُعاد الإرسال مرة بعد
# إعادة التشغيل كل ~5س40د، وهذا مقبول مقابل عشرات التكرارات.)
from datetime import timedelta
_ALERT_COOLDOWN_MIN = 90
_recent_alerts: dict = {}

def _is_duplicate(fp: str) -> bool:
    t = _recent_alerts.get(fp)
    return t is not None and (datetime.now(timezone.utc) - t) < timedelta(minutes=_ALERT_COOLDOWN_MIN)

def _mark_sent(fp: str) -> None:
    _recent_alerts[fp] = datetime.now(timezone.utc)


# ─── فحص عطلة نهاية الأسبوع ──────────────────────────────────────────────────
def is_weekend() -> bool:
    """السبت = 5، الأحد = 6"""
    return datetime.now(timezone.utc).weekday() >= 5


# ─── فحص كل أداة ─────────────────────────────────────────────────────────────
def scan_symbol(
    symbol: str, config: dict,
    client: CapitalClient,
    news_events: list,
    bot_mode: str
) -> None:

    epic = config["epic"]
    logger.info(f"🔍 فحص {symbol}")

    candles_1h  = client.get_candles(epic, TIMEFRAME_1H,  CANDLES_1H_COUNT)
    candles_15m = client.get_candles(epic, TIMEFRAME_15M, CANDLES_15M_COUNT)
    candles_5m  = client.get_candles(epic, TIMEFRAME_5M,  CANDLES_5M_COUNT)

    if not candles_1h or not candles_15m or not candles_5m:
        logger.warning(f"⚠️ بيانات ناقصة: {symbol}")
        return

    price_data = client.get_current_price(epic)
    if not price_data:
        return
    current_price = price_data["mid"]

    trend = detect_trend_1h(candles_1h)
    logger.info(f"📈 {symbol} | اتجاه 1H: {trend} | سعر: {current_price:.2f}")

    if trend == "neutral":
        logger.info(f"↔️ {symbol} محايد — تخطي")
        return

    balance  = client.get_account_balance()
    open_pos = client.get_open_positions()
    allowed, reason = can_trade(balance, len(open_pos))
    if not allowed:
        logger.info(f"🚫 {reason}")
        return

    signal_found = None
    setup_num    = None

    # الإعداد الأول: Sweep + BOS
    s1 = scan_setup_1(symbol, trend, candles_15m, candles_5m, current_price)
    if s1:
        signal_found = s1
        setup_num    = 1

    # الإعداد الثاني: News Retest
    if not signal_found:
        s2 = scan_setup_2(symbol, trend, candles_5m, news_events)
        if s2:
            signal_found = s2
            setup_num    = 2

    # الإعداد الثالث: S&D Rejection
    if not signal_found:
        s3 = scan_setup_3(symbol, trend, candles_1h, candles_5m, current_price)
        if s3:
            signal_found = s3
            setup_num    = 3

    if not signal_found:
        return

    # حارس التكرار: أرسل كل إعداد مرة واحدة ثم اكتمه (يمنع إغراق نفس الإشارة كل دقيقة)
    fp = f"{symbol}:{setup_num}:{signal_found.direction}:{round(signal_found.entry, 1)}"
    if _is_duplicate(fp):
        logger.info(f"🔁 {symbol} إشارة مكررة — تخطي الإرسال ({fp})")
        return
    _mark_sent(fp)

    lot_size = calculate_lot_size(symbol, balance, signal_found.entry, signal_found.sl)

    if setup_num == 1:
        msg = format_setup1_message(signal_found, lot_size, trend)
    elif setup_num == 2:
        msg = format_setup2_message(signal_found, lot_size, trend)
    else:
        msg = format_setup3_message(signal_found, lot_size, trend)

    if bot_mode == "alert_only":
        send_message(msg)
        logger.info(f"📤 إشارة أُرسلت: {symbol} {signal_found.direction}")

    elif bot_mode == "semi_auto":
        send_message(msg + "\n\n⚡ أرسل /confirm للتنفيذ")

    elif bot_mode == "full_auto":
        send_message(msg)
        result = client.place_order(
            epic=epic, direction=signal_found.direction,
            size=lot_size, stop_level=signal_found.sl,
            profit_level=signal_found.tp1,
        )
        if result:
            record_trade_open()
        else:
            send_message(f"❌ فشل فتح صفقة {symbol}")


# ─── الحلقة الرئيسية ─────────────────────────────────────────────────────────
def main():
    logger.info("🚀 بدء تشغيل البوت v2 — 24/7 الاثنين-الجمعة")
    send_message(
        f"🤖 <b>بوت السكالبينغ v2 — يعمل</b>\n"
        f"الوضع: {BOT_MODE}\n"
        f"الجدول: الاثنين—الجمعة | 24 ساعة"
    )

    client    = CapitalClient()
    bot_state = {"mode": BOT_MODE, "paused": False, "offset": 0}

    news_events     = fetch_news_events()
    news_fetch_hour = datetime.now(timezone.utc).hour

    while True:
        try:
            bot_state = process_commands(bot_state)

            if bot_state["paused"]:
                time.sleep(SCAN_INTERVAL_SECONDS)
                continue

            # ─ إيقاف نهاية الأسبوع
            if is_weekend():
                logger.info("🔴 عطلة نهاية الأسبوع — السوق مغلق")
                time.sleep(300)
                continue

            # ─ تحديث الأخبار كل ساعة
            current_hour = datetime.now(timezone.utc).hour
            if current_hour != news_fetch_hour:
                news_events     = fetch_news_events()
                news_fetch_hour = current_hour

            # ─ فحص حجب الأخبار
            blocked, news_title = is_news_block_active(news_events)
            if blocked:
                logger.info(f"📰 حجب الأخبار: {news_title}")
                send_news_block_alert(news_title)
                time.sleep(60)
                continue

            # ─ فحص الأدوات
            current_mode = bot_state["mode"]
            for symbol, config in SYMBOLS.items():
                try:
                    scan_symbol(symbol, config, client, news_events, current_mode)
                except Exception as e:
                    logger.error(f"❌ خطأ {symbol}: {e}")

            logger.info(f"✅ دورة مكتملة — انتظار {SCAN_INTERVAL_SECONDS}s")
            time.sleep(SCAN_INTERVAL_SECONDS)

        except KeyboardInterrupt:
            logger.info("🛑 إيقاف يدوي")
            send_message("🛑 البوت أُوقف يدوياً")
            break

        except Exception as e:
            logger.error(f"❌ خطأ رئيسي: {e}")
            send_message(f"⚠️ خطأ:\n<code>{e}</code>")
            time.sleep(30)


if __name__ == "__main__":
    main()
