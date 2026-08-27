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


def format_signal(signal, lot_size: float) -> str:
    arrow = "🟢 شراء" if signal.direction == "BUY" else "🔴 بيع"
    return (
        f"🔔 <b>إشارة VWAP — {signal.symbol}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"الاتجاه : <b>{arrow}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"الدخول  : <code>{signal.entry}</code>\n"
        f"SL      : <code>{signal.sl}</code>\n"
        f"TP      : <code>{signal.tp}</code>  (RR {signal.rr}:1)\n"
        f"الحجم   : <code>{lot_size}</code> لوت\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"VWAP    : <code>{signal.vwap}</code>\n"
        f"EMA 9/21: {signal.ema_fast} / {signal.ema_slow}\n"
        f"RSI     : {signal.rsi}\n"
        f"ATR     : {signal.atr}\n"
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
            from tracker import build_report
            send_message(build_report(days=1))
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
