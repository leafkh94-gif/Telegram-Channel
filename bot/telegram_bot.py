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


def format_entry(sig) -> str:
    arrow = "🟢 شراء" if sig.direction == "BUY" else "🔴 بيع"
    trend = "فوق" if sig.direction == "BUY" else "تحت"
    return (
        f"{arrow} <b>{sig.symbol}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"الدخول : <code>{sig.entry}</code>\n"
        f"الوقف  : <code>{sig.sl}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"اليومي : {sig.daily_close} {trend} EMA {sig.daily_ema} ✓\n"
        f"الاختراق: قناة 24 ساعة عند {sig.channel}\n"
        f"ATR ساعة: {sig.atr}"
    )


def format_partial(symbol: str, pos) -> str:
    from position import sign, stop_price
    gained = (pos.partial_price - pos.entry) * sign(pos)
    return (
        f"💰 <b>جني جزئي — {symbol}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"أغلقنا {int((1 - pos.size) * 100)}% عند <code>{pos.partial_price:.2f}</code>\n"
        f"الربح المحقّق: <b>{gained:+.2f}</b> نقطة ({pos.booked:+.2f}R)\n"
        f"الباقي يتبع، ووقفه الآن عند الدخول <code>{pos.entry:.2f}</code>\n"
        f"لا خسارة ممكنة على هذه الصفقة بعد الآن"
    )


def format_trail(symbol: str, pos) -> str:
    from position import sign, stop_price
    sp     = stop_price(pos)
    locked = (sp - pos.entry) * sign(pos)
    state  = ("🔒 الوقف تجاوز الدخول — الصفقة مؤمّنة" if locked > 0
              else "الوقف ما زال دون الدخول")
    return (
        f"🔺 <b>تحديث وقف — {symbol}</b>\n"
        f"الوقف الجديد: <code>{sp:.2f}</code>\n"
        f"الدخول كان : <code>{pos.entry:.2f}</code>\n"
        f"{state}"
    )


def format_exit(symbol: str, pos, ex) -> str:
    # ex.r يجمع المحقّق سابقاً مع المتبقّي، فنُظهر التفصيل لا رقماً غامضاً
    detail = ""
    if pos.partial_price is not None:
        detail = (f"جني سابق: <code>{pos.partial_price:.2f}</code> "
                  f"({pos.booked:+.2f}R)\n"
                  f"الباقي  : {int(pos.size * 100)}% خرج الآن\n")
    side = "شراء" if pos.side == "BUY" else "بيع"
    return (
        f"{'✅' if ex.r > 0 else '❌'} <b>خروج {side} — {symbol}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"الدخول : <code>{pos.entry:.2f}</code>\n"
        f"{detail}"
        f"الخروج : <code>{ex.price:.2f}</code>\n"
        f"النتيجة: <b>{ex.r:+.2f}R</b>"
    )


def get_updates(offset: int = 0) -> list[dict]:
    try:
        r = requests.get(f"{BASE_URL}/getUpdates",
                         params={"offset": offset, "timeout": 5}, timeout=10)
        r.raise_for_status()
        return r.json().get("result", [])
    except Exception:
        return []


def process_commands(bot_state: dict, status_fn=None) -> dict:
    """
    يعالج أوامر تيليغرام.

    لا استيراد لوحدات غير موجودة هنا. النسخة السابقة كانت تستورد risk_manager
    و reporter و tracker — وكلها حُذفت مع الاستراتيجية القديمة — فكانت ترمي
    ImportError في أول سطر من كل دورة، فلا يُنفَّذ الفحص إطلاقاً. البوت بقي
    كذلك 23 ساعة وكل المؤشرات الخارجية تقول إنه يعمل.

    status_fn تُمرَّر من main.py وتُرجع سطور حالة الصفقات القائمة، فلا تحتاج
    هذه الوحدة أن تعرف شيئاً عن الصفقات.
    """
    for update in get_updates(bot_state.get("offset", 0)):
        bot_state["offset"] = update["update_id"] + 1
        text = update.get("message", {}).get("text", "").strip().lower()

        if text in ("/status", "/حالة"):
            lines = [f"الوضع: <b>{bot_state.get('mode')}</b>",
                     "⏸ متوقف مؤقتاً" if bot_state.get("paused") else "▶️ يعمل"]
            if status_fn:
                lines += status_fn()
            send_message("\n".join(lines))
        elif text in ("/pause", "/ايقاف"):
            bot_state["paused"] = True
            send_message("⏸ متوقف مؤقتاً — أرسل /resume للاستئناف")
        elif text in ("/resume", "/استئناف"):
            bot_state["paused"] = False
            send_message("▶️ استُؤنف")
    return bot_state
