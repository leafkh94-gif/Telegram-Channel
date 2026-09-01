"""
telegram_bot.py — إرسال الإشارات وأوامر التحكم
"""

import logging
import requests
from config import TELEGRAM_TOKEN, TELEGRAM_CHAT_ID

logger = logging.getLogger(__name__)
BASE_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"


def send_message(text: str) -> bool:
    try:
        r = requests.post(
            f"{BASE_URL}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=10
        )
        r.raise_for_status()
        return True
    except Exception as e:
        logger.error(f"❌ خطأ تيليغرام: {e}")
        return False


def format_entry(sig, lot: float) -> str:
    risk = sig.entry - sig.sl
    return (
        f"🟢 <b>دخول شراء — {sig.symbol}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"الدخول    : <code>{sig.entry}</code>\n"
        f"الوقف     : <code>{sig.sl}</code>  ({risk:.1f} نقطة)\n"
        f"الحجم     : <code>{lot}</code> لوت\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>لا يوجد هدف ثابت.</b>\n"
        f"الوقف يتحرّك خلف السعر بـ {sig.trail_atr}×ATR،\n"
        f"والصفقة تُغلق حين يُضرب — وسأخبرك بذلك.\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"اليومي  : {sig.daily_close} فوق EMA {sig.daily_ema} ✓\n"
        f"الاختراق: قناة {sig.channel_high}\n"
        f"ATR ساعة: {sig.atr}"
    )


def format_trail(symbol: str, pos) -> str:
    locked = pos.stop - pos.entry
    state = ("🔒 <b>الوقف فوق الدخول — الصفقة مؤمّنة</b>" if locked > 0
             else "الوقف ما زال تحت الدخول")
    return (
        f"🔺 <b>تحديث وقف — {symbol}</b>\n"
        f"الوقف الجديد: <code>{pos.stop:.2f}</code>\n"
        f"الدخول كان : <code>{pos.entry:.2f}</code>\n"
        f"{state}"
    )


def format_exit(symbol: str, pos, ex) -> str:
    return (
        f"{'✅' if ex.r > 0 else '❌'} <b>خروج — {symbol}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"الدخول : <code>{pos.entry:.2f}</code>\n"
        f"الخروج : <code>{ex.price:.2f}</code>\n"
        f"النتيجة: <b>{ex.r:+.2f}R</b>\n"
        f"السبب  : {'الوقف المتحرّك' if ex.reason == 'SL' else 'انتهاء المدة'}"
    )


def send_daily_stats(stats: dict):
    status = "🟢 نشط" if not stats["halted"] else "🔴 متوقف"
    send_message(
        f"📈 <b>إحصائيات اليوم</b>\n"
        f"الحالة        : {status}\n"
        f"صفقات اليوم   : {stats['trades_today']}\n"
        f"P&L اليوم     : {stats['daily_pnl']:.2f}\n"
        f"خسائر متتالية : {stats['consec_losses']}"
    )


def get_updates(offset: int = 0) -> list[dict]:
    try:
        r = requests.get(f"{BASE_URL}/getUpdates",
                         params={"offset": offset, "timeout": 5}, timeout=10)
        r.raise_for_status()
        return r.json().get("result", [])
    except Exception:
        return []


def process_commands(bot_state: dict) -> dict:
    from risk_manager import resume_trading, get_daily_stats
    for update in get_updates(bot_state.get("offset", 0)):
        bot_state["offset"] = update["update_id"] + 1
        text = update.get("message", {}).get("text", "").strip().lower()
        if text in ("/report", "/تقرير"):
            try:
                from reporter import send_daily_report
                send_daily_report()
            except Exception:
                send_message("⚠️ التقرير غير متاح حالياً")
            continue

        if text in ("/report7", "/week"):
            from tracker import build_report
            send_message(build_report(days=7))
            continue

        if text == "/status":
            send_daily_stats(get_daily_stats())
            send_message(f"الوضع: <b>{bot_state.get('mode')}</b>")
        elif text == "/pause":
            bot_state["paused"] = True
            send_message("⏸ متوقف مؤقتاً")
        elif text == "/resume":
            bot_state["paused"] = False
            resume_trading()
            send_message("▶️ استُؤنف")
        elif text == "/stats":
            send_daily_stats(get_daily_stats())
    return bot_state
