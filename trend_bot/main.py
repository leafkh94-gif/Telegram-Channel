"""
main.py — حلقة التشغيل: يومي ← 4 ساعات ← ساعة، شراء فقط

قرارا تصميم يستحقّان الشرح:

1. التقييم على إغلاق شمعة الساعة فقط.
   الفحص كل دقيقة على شمعة قيد التكوّن يُنتج إشارات تظهر ثم تختفي، وهو ما
   أغرقنا سابقاً. هنا نتصرّف فقط حين تُغلق شمعة ساعة جديدة، فيصير سلوك البوت
   مطابقاً تماماً لما قِيس في الباك-تست.

2. الصفقات المفتوحة تُعاد بناؤها من التاريخ لا تُحفظ.
   المستودع عليه ruleset يمنع أي كتابة آلية، فلا يمكن حفظ الحالة بين
   التشغيلات (تحقّقنا: كل رفع يُرفض). لكن الاستراتيجية حتميّة: نفس الشموع
   تعطي نفس القرارات. فعند الإقلاع نُعيد تشغيل المنطق على آخر الشموع
   ونستنتج الصفقة القائمة. لا حالة تُفقد بإعادة التشغيل، ولا حاجة لتخزين.
"""

import logging
import time
from datetime import datetime, timezone

from config import (
    SYMBOLS, BOT_MODE, SCAN_INTERVAL_SECONDS,
    TIMEFRAME_DAILY, TIMEFRAME_H4, TIMEFRAME_H1, TIMEFRAME_M5,
    CANDLES_DAILY, CANDLES_H4, CANDLES_H1, CANDLES_M5,
    STOP_ATR, TRAIL_ATR,
)
from capital_client import CapitalClient
import strategy
from position import Position, open_position, update, risk_unit
from risk_manager import can_trade, calculate_lot_size
from telegram_bot import (
    send_message, format_entry, format_trail, format_exit, process_commands,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler("bot.log", encoding="utf-8")],
)
logger = logging.getLogger("MAIN")

# آخر شمعة ساعة عولجت لكل أداة — يمنع تكرار المعالجة داخل الساعة الواحدة
_last_bar: dict[str, str] = {}
# الصفقات القائمة، مُعاد بناؤها عند الإقلاع
_positions: dict[str, Position] = {}
# آخر وقف أُبلغ عنه، حتى لا نُرسل تحديثاً على كل ارتفاع تافه
_last_trail: dict[str, float] = {}

TRAIL_ALERT_PCT = 0.25   # نُبلغ حين يرتفع الوقف ≥ ربع المخاطرة الأولية


def is_weekend() -> bool:
    return datetime.now(timezone.utc).weekday() >= 5


def _closed(candles: list[dict]) -> list[dict]:
    """يُسقط الشمعة الجارية. التقييم على شمعة غير مكتملة يُنتج إشارات زائلة."""
    return candles[:-1] if len(candles) > 1 else candles


def _fetch(client: CapitalClient, epic: str) -> tuple | None:
    d  = _closed(client.get_candles(epic, TIMEFRAME_DAILY, CANDLES_DAILY))
    h4 = _closed(client.get_candles(epic, TIMEFRAME_H4,    CANDLES_H4))
    h1 = _closed(client.get_candles(epic, TIMEFRAME_H1,    CANDLES_H1))
    m5 = client.get_candles(epic, TIMEFRAME_M5, CANDLES_M5)   # السعر الحيّ مطلوب هنا
    if not d or not h4 or not h1:
        return None
    return d, h4, h1, m5


def _coarse_index(fine: list[dict], coarse: list[dict]) -> list[int]:
    out, j = [], -1
    ct = [c["time"] for c in coarse]
    for c in fine:
        while j + 1 < len(ct) and ct[j + 1] < c["time"]:
            j += 1
        out.append(j - 1)
    return out


def rebuild_position(symbol: str, d: list[dict], h4: list[dict],
                     h1: list[dict]) -> Position | None:
    """
    يُعيد اشتقاق الصفقة القائمة بتشغيل نفس المنطق على التاريخ المتاح.
    يستدعي strategy.scan و position.update — أي نفس دوال الباك-تست والتشغيل.
    """
    i4, idd = _coarse_index(h1, h4), _coarse_index(h1, d)
    pos = None
    for i in range(80, len(h1)):
        bar = h1[i]
        if pos is not None:
            pos, ex = update(pos, bar)
            if ex:
                pos = None
            continue
        di, fi = idd[i], i4[i]
        if di < 25 or fi < 25:
            continue
        sig = strategy.scan(symbol, d[:di + 1], h4[:fi + 1], h1[:i + 1], [bar])
        if sig:
            pos = open_position(symbol, sig.entry, sig.atr, bar["time"])
    return pos


def scan_symbol(symbol: str, cfg: dict, client: CapitalClient, mode: str) -> None:
    data = _fetch(client, cfg["epic"])
    if not data:
        logger.warning(f"⚠️ بيانات ناقصة: {symbol}")
        return
    d, h4, h1, m5 = data

    bar = h1[-1]
    if _last_bar.get(symbol) == bar["time"]:
        return                                   # لا شمعة ساعة جديدة بعد
    _last_bar[symbol] = bar["time"]
    logger.info(f"🕐 {symbol}: شمعة ساعة جديدة {bar['time']} | إغلاق {bar['close']:.2f}")

    pos = _positions.get(symbol)

    # ── إدارة صفقة قائمة
    if pos is not None:
        before = pos.stop
        pos, ex = update(pos, bar)
        if ex:
            _positions.pop(symbol, None)
            _last_trail.pop(symbol, None)
            send_message(format_exit(symbol, pos, ex))
            logger.info(f"🚪 {symbol} خروج {ex.reason} @ {ex.price:.2f} | {ex.r:+.2f}R")
            return
        _positions[symbol] = pos
        moved = pos.stop - _last_trail.get(symbol, before)
        if moved >= TRAIL_ALERT_PCT * risk_unit(pos):
            _last_trail[symbol] = pos.stop
            send_message(format_trail(symbol, pos))
            logger.info(f"🔒 {symbol} الوقف ارتفع إلى {pos.stop:.2f}")
        return

    # ── البحث عن دخول جديد
    sig = strategy.scan(symbol, d, h4, h1, m5)
    if not sig:
        return

    balance  = client.get_account_balance()
    open_pos = client.get_open_positions()
    allowed, reason = can_trade(balance, len(open_pos))
    if not allowed:
        logger.info(f"🚫 {reason}")
        return

    lot = calculate_lot_size(symbol, balance, sig.entry, sig.sl)
    _positions[symbol]  = open_position(symbol, sig.entry, sig.atr, bar["time"])
    _last_trail[symbol] = _positions[symbol].stop
    send_message(format_entry(sig, lot))
    logger.info(f"📤 إشارة دخول: {symbol} @ {sig.entry:.2f} وقف {sig.sl:.2f}")


def main() -> None:
    logger.info("🚀 بوت الاتجاه — يومي/4س/ساعة، شراء فقط")
    client = CapitalClient()

    # إعادة بناء الصفقات القائمة قبل أي قرار جديد
    restored = 0
    for symbol, cfg in SYMBOLS.items():
        try:
            data = _fetch(client, cfg["epic"])
            if not data:
                continue
            d, h4, h1, _ = data
            _last_bar[symbol] = h1[-1]["time"]
            p = rebuild_position(symbol, d, h4, h1)
            if p:
                _positions[symbol]  = p
                _last_trail[symbol] = p.stop
                restored += 1
                logger.info(f"♻️ {symbol}: صفقة قائمة دخول {p.entry:.2f} وقف {p.stop:.2f}")
        except Exception as e:
            logger.error(f"❌ إعادة بناء {symbol}: {e}")

    send_message(
        f"🤖 <b>بوت الاتجاه — يعمل</b>\n"
        f"الوضع: {BOT_MODE}\n"
        f"المنطق: يومي ← 4 ساعات ← ساعة | شراء فقط\n"
        f"الوقف: {STOP_ATR}×ATR | المتحرّك: {TRAIL_ATR}×ATR\n"
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
                    scan_symbol(symbol, cfg, client, state["mode"])
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
