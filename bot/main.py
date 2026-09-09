"""
main.py — حلقة التشغيل: اتجاه يومي ← اختراق قناة 24 ساعة، شراءً وبيعاً

قرارا تصميم يستحقّان الشرح:

1. التقييم عند إغلاق شمعة الساعة فقط.
   الفحص كل دقيقة على شمعة قيد التكوّن يُنتج إشارات تظهر ثم تختفي، وهو ما
   أغرق المستخدم برسائل متناقضة سابقاً (بيع وشراء على نفس الأداة خلال عشر
   دقائق). هنا نتصرّف فقط حين تُغلق شمعة ساعة جديدة، فيصير سلوك البوت
   مطابقاً لما قِيس في الباك-تست.

2. الصفقات المفتوحة تُعاد بناؤها من التاريخ لا تُحفظ.
   على المستودع ruleset يمنع أي كتابة آلية، فلا يمكن حفظ الحالة بين
   التشغيلات (تحقّقنا: كل رفع يُرفض، وسجلّ الإشارات لم يُحفظ قط). لكن
   الاستراتيجية حتميّة: نفس الشموع تعطي نفس القرارات. فعند الإقلاع نُعيد
   تشغيل المنطق على آخر الشموع ونستنتج الصفقة القائمة.
"""

import logging
import time
from datetime import datetime, timezone

from config import (
    SYMBOLS, BOT_MODE, SCAN_INTERVAL_SECONDS,
    TIMEFRAME_DAILY, TIMEFRAME_H1, CANDLES_DAILY, CANDLES_H1,
    STOP_ATR, TRAIL_ATR, PARTIAL_TARGET_ATR, ENTRY_CHANNEL_H1,
)
from capital_client import CapitalClient
import strategy
from position import Position, open_position, update, risk_unit, stop_price
from telegram_bot import (
    send_message, format_entry, format_partial, format_trail, format_exit,
    process_commands,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler("bot.log", encoding="utf-8")],
)
logger = logging.getLogger("MAIN")

_last_bar:   dict[str, str]      = {}   # آخر شمعة ساعة عولجت لكل أداة
_positions:  dict[str, Position] = {}   # الصفقات القائمة
_last_trail: dict[str, float]    = {}   # آخر وقف أُبلغ عنه

TRAIL_ALERT_PCT = 0.25   # نُبلغ حين يتقدّم الوقف ≥ ربع المخاطرة الأولية


def is_weekend() -> bool:
    return datetime.now(timezone.utc).weekday() >= 5


def _closed(candles: list[dict]) -> list[dict]:
    """يُسقط الشمعة الجارية. التقييم على شمعة غير مكتملة يُنتج إشارات زائلة."""
    return candles[:-1] if len(candles) > 1 else candles


def _fetch(client: CapitalClient, epic: str):
    d  = _closed(client.get_candles(epic, TIMEFRAME_DAILY, CANDLES_DAILY))
    h1 = _closed(client.get_candles(epic, TIMEFRAME_H1, CANDLES_H1))
    return (d, h1) if d and h1 else None


def _daily_index(h1: list[dict], daily: list[dict]) -> list[int]:
    out, j = [], -1
    dt = [c["time"] for c in daily]
    for c in h1:
        while j + 1 < len(dt) and dt[j + 1] < c["time"]:
            j += 1
        out.append(j - 1)
    return out


def rebuild_position(symbol: str, d: list[dict], h1: list[dict]) -> Position | None:
    """
    يُعيد اشتقاق الصفقة القائمة بتشغيل نفس المنطق على التاريخ المتاح.
    يستدعي strategy.scan و position.update — أي نفس دوال الباك-تست والتشغيل.
    """
    idd = _daily_index(h1, d)
    pos = None
    for i in range(60, len(h1)):
        bar = h1[i]
        if pos is not None:
            pos, ex = update(pos, bar)
            if ex:
                pos = None
            continue
        di = idd[i]
        if di < 25:
            continue
        sig = strategy.scan(symbol, d[:di + 1], h1[:i + 1])
        if sig:
            pos = open_position(symbol, sig.direction, sig.entry, sig.atr, bar["time"])
    return pos


def scan_symbol(symbol: str, cfg: dict, client: CapitalClient) -> None:
    data = _fetch(client, cfg["epic"])
    if not data:
        logger.warning(f"⚠️ بيانات ناقصة: {symbol}")
        return
    d, h1 = data

    bar = h1[-1]
    if _last_bar.get(symbol) == bar["time"]:
        return                                   # لا شمعة ساعة جديدة بعد
    _last_bar[symbol] = bar["time"]
    logger.info(f"🕐 {symbol}: شمعة ساعة {bar['time']} | إغلاق {bar['close']:.2f}")

    pos = _positions.get(symbol)

    # ── إدارة صفقة قائمة
    if pos is not None:
        had_partial = pos.partial_price is not None
        pos, ex = update(pos, bar)
        if ex:
            _positions.pop(symbol, None)
            _last_trail.pop(symbol, None)
            send_message(format_exit(symbol, pos, ex))
            logger.info(f"🚪 {symbol} خروج @ {ex.price:.2f} | {ex.r:+.2f}R")
            return
        _positions[symbol] = pos

        # الجني الجزئي يرفع الوقف في نفس اللحظة — رسالة واحدة عن حدث واحد
        if not had_partial and pos.partial_price is not None:
            _last_trail[symbol] = stop_price(pos)
            send_message(format_partial(symbol, pos))
            logger.info(f"💰 {symbol} جني جزئي @ {pos.partial_price:.2f} | "
                        f"محقّق {pos.booked:+.2f}R")
            return

        now = stop_price(pos)
        if abs(now - _last_trail.get(symbol, now)) >= TRAIL_ALERT_PCT * risk_unit(pos):
            _last_trail[symbol] = now
            send_message(format_trail(symbol, pos))
            logger.info(f"🔒 {symbol} الوقف تقدّم إلى {now:.2f}")
        return

    # ── البحث عن دخول جديد
    sig = strategy.scan(symbol, d, h1)
    if not sig:
        return

    _positions[symbol]  = open_position(symbol, sig.direction, sig.entry,
                                        sig.atr, bar["time"])
    _last_trail[symbol] = stop_price(_positions[symbol])
    send_message(format_entry(sig))
    logger.info(f"📤 إشارة: {symbol} {sig.direction} @ {sig.entry:.2f}")


def main() -> None:
    logger.info("🚀 بوت الاتجاه — يومي/ساعة، شراء وبيع")
    client = CapitalClient()

    restored = 0
    for symbol, cfg in SYMBOLS.items():
        try:
            data = _fetch(client, cfg["epic"])
            if not data:
                continue
            d, h1 = data
            _last_bar[symbol] = h1[-1]["time"]
            p = rebuild_position(symbol, d, h1)
            if p:
                _positions[symbol]  = p
                _last_trail[symbol] = stop_price(p)
                restored += 1
                logger.info(f"♻️ {symbol}: صفقة قائمة {p.side} "
                            f"دخول {p.entry:.2f} وقف {stop_price(p):.2f}")
        except Exception as e:
            logger.error(f"❌ إعادة بناء {symbol}: {e}")

    send_message(
        f"🤖 <b>البوت يعمل</b>\n"
        f"الوضع: {BOT_MODE}\n"
        f"الأدوات: {' · '.join(SYMBOLS)}\n"
        f"المنطق: اتجاه يومي ← اختراق قناة {ENTRY_CHANNEL_H1} ساعة | شراء وبيع\n"
        f"الوقف {STOP_ATR}×ATR | جني نصف عند {PARTIAL_TARGET_ATR}×ATR | "
        f"تتبّع {TRAIL_ATR}×ATR\n"
        f"صفقات قائمة: {restored}"
    )

    state = {"mode": BOT_MODE, "paused": False, "offset": 0}
    while True:
        try:
            state = process_commands(state)
            if state["paused"] or is_weekend():
                time.sleep(300 if is_weekend() else SCAN_INTERVAL_SECONDS)
                continue
            for symbol, cfg in SYMBOLS.items():
                try:
                    scan_symbol(symbol, cfg, client)
                except Exception as e:
                    logger.error(f"❌ {symbol}: {e}")
            time.sleep(SCAN_INTERVAL_SECONDS)
        except KeyboardInterrupt:
            send_message("🛑 البوت أُوقف يدوياً")
            break
        except Exception as e:
            logger.error(f"❌ خطأ رئيسي: {e}")
            time.sleep(30)


if __name__ == "__main__":
    main()
