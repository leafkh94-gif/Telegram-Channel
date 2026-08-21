"""
telegram_bot.py — واجهة تيليغرام: إرسال الإشارات وأوامر التحكم
"""

import logging
import asyncio
import requests
from config import TELEGRAM_TOKEN, TELEGRAM_CHAT_ID

logger = logging.getLogger(__name__)

BASE_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"


# ─── إرسال رسالة ─────────────────────────────────────────────────────────────
def send_message(text: str, parse_mode: str = "HTML") -> bool:
    try:
        r = requests.post(
            f"{BASE_URL}/sendMessage",
            json={
                "chat_id":    TELEGRAM_CHAT_ID,
                "text":       text,
                "parse_mode": parse_mode,
            },
            timeout=10
        )
        r.raise_for_status()
        return True
    except Exception as e:
        logger.error(f"❌ خطأ إرسال تيليغرام: {e}")
        return False


# ─── صيغة رسالة الإشارة ──────────────────────────────────────────────────────
def format_setup1_message(signal, lot_size: float, trend: str) -> str:
    arrow = "🟢 شراء" if signal.direction == "BUY" else "🔴 بيع"
    stars = "⭐" * signal.confidence
    return (
        f"🔔 <b>إشارة سكالبينغ — {signal.symbol}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"الإعداد   : <b>Liquidity Sweep + {signal.setup_type}</b>\n"
        f"الاتجاه   : <b>{arrow}</b>\n"
        f"الثقة     : {stars}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"الدخول    : <code>{signal.entry}</code>\n"
        f"SL        : <code>{signal.sl}</code>\n"
        f"TP1       : <code>{signal.tp1}</code>  (RR {signal.rr:.1f}:1)\n"
        f"TP2       : <code>{signal.tp2}</code>\n"
        f"الحجم     : <code>{lot_size}</code> لوت\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"الاتجاه 1H : {trend.upper()}\n"
        f"مستوى Sweep: <code>{signal.swept_level}</code>\n"
        f"FVG       : {'✅' if signal.fvg else '❌'}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"⏰ الدخول صالح لـ 30 دقيقة"
    )


def format_setup2_message(signal, lot_size: float, trend: str) -> str:
    arrow = "🟢 شراء" if signal.direction == "BUY" else "🔴 بيع"
    return (
        f"📰 <b>إشارة News Retest — {signal.symbol}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"الإعداد   : <b>News Retest</b>\n"
        f"الخبر     : {signal.news_title}\n"
        f"الاتجاه   : <b>{arrow}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"الدخول    : <code>{signal.entry}</code>\n"
        f"SL        : <code>{signal.sl}</code>\n"
        f"TP1       : <code>{signal.tp1}</code>  (RR {signal.rr:.1f}:1)\n"
        f"TP2       : <code>{signal.tp2}</code>\n"
        f"الحجم     : <code>{lot_size}</code> لوت\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"منطقة الخبر: <code>{signal.zone_bottom} — {signal.zone_top}</code>\n"
        f"الاتجاه 1H : {trend.upper()}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"⚡ انتهز الفرصة — نافذة قصيرة"
    )


def format_setup3_message(signal, lot_size: float, trend: str) -> str:
    arrow = "🟢 شراء" if signal.direction == "BUY" else "🔴 بيع"
    zone_label = "طلب (Demand)" if signal.zone_type == "demand" else "عرض (Supply)"
    return (
        f"📊 <b>إشارة S&D Rejection — {signal.symbol}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"الإعداد   : <b>Supply & Demand Rejection</b>\n"
        f"المنطقة   : {zone_label}\n"
        f"الاتجاه   : <b>{arrow}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"الدخول    : <code>{signal.entry}</code>\n"
        f"SL        : <code>{signal.sl}</code>\n"
        f"TP1       : <code>{signal.tp1}</code>  (RR {signal.rr:.1f}:1)\n"
        f"TP2       : <code>{signal.tp2}</code>\n"
        f"الحجم     : <code>{lot_size}</code> لوت\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"حدود المنطقة: <code>{signal.zone_bottom} — {signal.zone_top}</code>\n"
        f"لمسات سابقة : {signal.touches}\n"
        f"الاتجاه 1H  : {trend.upper()}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"⏰ الدخول صالح لـ 30 دقيقة"
    )


def send_halt_alert(reason: str):
    send_message(
        f"⛔ <b>البوت متوقف</b>\n"
        f"السبب: {reason}\n"
        f"أرسل /resume لاستئناف التداول"
    )


def send_news_block_alert(news_title: str):
    send_message(
        f"📰 <b>حجب الأخبار مفعّل</b>\n"
        f"الخبر: {news_title}\n"
        f"التداول متوقف مؤقتاً"
    )


def send_daily_stats(stats: dict):
    status = "🟢 نشط" if not stats["halted"] else "🔴 متوقف"
    send_message(
        f"📈 <b>إحصائيات اليوم</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"الحالة         : {status}\n"
        f"صفقات اليوم    : {stats['trades_today']}\n"
        f"P&L اليوم      : {stats['daily_pnl']:.2f}\n"
        f"خسائر متتالية  : {stats['consec_losses']}\n"
        f"━━━━━━━━━━━━━━━━━━━━"
    )


# ─── معالج الأوامر ────────────────────────────────────────────────────────────
def get_updates(offset: int = 0) -> list[dict]:
    try:
        r = requests.get(
            f"{BASE_URL}/getUpdates",
            params={"offset": offset, "timeout": 5},
            timeout=10
        )
        r.raise_for_status()
        return r.json().get("result", [])
    except Exception:
        return []


def process_commands(bot_state: dict) -> dict:
    """
    يعالج الأوامر الواردة ويُعيد الحالة المحدثة.
    bot_state: {"mode": str, "paused": bool, "offset": int}
    """
    from risk_manager import resume_trading

    updates = get_updates(bot_state.get("offset", 0))
    for update in updates:
        bot_state["offset"] = update["update_id"] + 1
        msg = update.get("message", {})
        text = msg.get("text", "").strip().lower()

        if text == "/status":
            from risk_manager import get_daily_stats
            stats = get_daily_stats()
            mode  = bot_state.get("mode", "alert_only")
            send_daily_stats(stats)
            send_message(f"وضع التشغيل: <b>{mode}</b>")

        elif text == "/pause":
            bot_state["paused"] = True
            send_message("⏸ البوت متوقف مؤقتاً. أرسل /resume للاستئناف.")

        elif text == "/resume":
            bot_state["paused"] = False
            resume_trading()
            send_message("▶️ استُؤنف التداول.")

        elif text == "/mode alert":
            bot_state["mode"] = "alert_only"
            send_message("✅ تم التبديل لوضع: التنبيه فقط")

        elif text == "/mode semi":
            bot_state["mode"] = "semi_auto"
            send_message("✅ تم التبديل لوضع: شبه تلقائي")

        elif text == "/mode auto":
            bot_state["mode"] = "full_auto"
            send_message("⚠️ تم التبديل لوضع: تلقائي كامل. تأكدي من 30 إعداد ورقي أولاً.")

        elif text == "/stats":
            from risk_manager import get_daily_stats
            send_daily_stats(get_daily_stats())

    return bot_state
