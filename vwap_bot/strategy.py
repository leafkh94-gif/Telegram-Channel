"""
strategy.py — استراتيجية VWAP Scalping (الوحيدة)

المنطق (مبني على إجماع البحث):
1. VWAP:  السعر فوق VWAP = شراء فقط | تحت = بيع فقط
2. EMA:   السعر فوق EMA 9 و EMA 9 > EMA 21 (شراء) | العكس (بيع)
3. RSI:   RSI(3) خرج من تشبع (ارتد من oversold للشراء / overbought للبيع)
4. ATR:   Stop = 1× ATR | Target = 2× ATR
"""

import logging
from dataclasses import dataclass
from typing import Optional
from indicators import compute_all
from config import (
    RSI_OVERSOLD, RSI_OVERBOUGHT,
    ATR_SL_MULT, ATR_TP_MULT, SYMBOLS,
    VWAP_BUFFER_ATR, REQUIRE_CLOSED_CANDLE
)

logger = logging.getLogger(__name__)


@dataclass
class Signal:
    symbol:    str
    direction: str      # "BUY" أو "SELL"
    entry:     float
    sl:        float
    tp:        float
    rr:        float
    vwap:      float
    ema_fast:  float
    ema_slow:  float
    rsi:       float
    atr:       float


def scan(symbol: str, candles: list[dict]) -> Optional[Signal]:
    """
    يفحص أداة واحدة ويُعيد إشارة أو None.
    """
    # نقيّم آخر شمعة **مغلقة**: الفحص كل 60 ثانية يقع غالباً داخل شمعة 5M قيد
    # التكوّن، وإغلاقها يتغيّر حتى تكتمل — فتظهر إشارة ثم تختفي، وهو أحد سببَي
    # تناوب BUY/SELL خلال دقائق على نفس الأداة.
    if REQUIRE_CLOSED_CANDLE and len(candles) > 1:
        candles = candles[:-1]

    if len(candles) < 30:
        logger.warning(f"⚠️ {symbol}: شموع غير كافية ({len(candles)})")
        return None

    ind = compute_all(candles)

    price    = ind["price"]
    vwap     = ind["vwap"]
    ema_fast = ind["ema_fast"]
    ema_slow = ind["ema_slow"]
    rsi      = ind["rsi"]
    atr      = ind["atr"]

    if None in (ema_fast, ema_slow) or atr == 0:
        logger.info(f"↔️ {symbol}: مؤشرات غير مكتملة")
        return None

    # ─── تسجيل تشخيصي كامل (يظهر في كل دورة) ─────────────────────────────────
    logger.info(
        f"📊 {symbol} | price={price:.2f} | vwap={vwap:.2f} | "
        f"ema9={ema_fast:.2f} | ema21={ema_slow:.2f} | "
        f"rsi={rsi:.1f} | atr={atr:.2f} | "
        f"هامش VWAP=±{atr * VWAP_BUFFER_ATR:.2f}"
    )

    # ─── فلتر ATR (سوق ميت = تجاهل) ──────────────────────────────────────────
    min_atr = SYMBOLS.get(symbol, {}).get("min_atr", 0)
    if atr < min_atr:
        logger.info(f"↔️ {symbol}: ATR {atr:.2f} < {min_atr} — سوق هادئ، تخطي")
        return None

    # هامش حول VWAP: بلا هامش يكفي عبور نقطة واحدة لقلب الإشارة، فيتناوب
    # BUY/SELL كلما تأرجح السعر حول الخط. المطلوب ابتعاد حقيقي بمقياس تقلّب
    # الأداة نفسها، لا رقم ثابت.
    band = atr * VWAP_BUFFER_ATR

    # ─── شروط الشراء ─────────────────────────────────────────────────────────
    # RSI: نريد زخماً صاعداً (فوق 50) لكن ليس تشبعاً متطرفاً جداً (تحت 85)
    # هذا يركب الاتجاه القوي بدل رفضه
    buy_conditions = (
        price > vwap + band and             # فوق VWAP بهامش
        price > ema_fast and                # فوق EMA 9
        ema_fast > ema_slow and             # EMA 9 فوق EMA 21
        rsi > 50 and                        # زخم صاعد
        rsi < 90                            # ليس تشبعاً متطرفاً مطلقاً
    )

    # ─── شروط البيع ──────────────────────────────────────────────────────────
    sell_conditions = (
        price < vwap - band and             # تحت VWAP بهامش
        price < ema_fast and                # تحت EMA 9
        ema_fast < ema_slow and             # EMA 9 تحت EMA 21
        rsi < 50 and                        # زخم هابط
        rsi > 10                            # ليس تشبعاً متطرفاً مطلقاً
    )

    if buy_conditions:
        entry = price
        sl    = entry - (atr * ATR_SL_MULT)
        tp    = entry + (atr * ATR_TP_MULT)
        logger.info(f"🎯 {symbol}: إشارة شراء!")
        return Signal(
            symbol=symbol, direction="BUY",
            entry=round(entry, 2), sl=round(sl, 2), tp=round(tp, 2),
            rr=round(ATR_TP_MULT / ATR_SL_MULT, 1),
            vwap=round(vwap, 2), ema_fast=round(ema_fast, 2),
            ema_slow=round(ema_slow, 2), rsi=round(rsi, 1), atr=round(atr, 2)
        )

    if sell_conditions:
        entry = price
        sl    = entry + (atr * ATR_SL_MULT)
        tp    = entry - (atr * ATR_TP_MULT)
        logger.info(f"🎯 {symbol}: إشارة بيع!")
        return Signal(
            symbol=symbol, direction="SELL",
            entry=round(entry, 2), sl=round(sl, 2), tp=round(tp, 2),
            rr=round(ATR_TP_MULT / ATR_SL_MULT, 1),
            vwap=round(vwap, 2), ema_fast=round(ema_fast, 2),
            ema_slow=round(ema_slow, 2), rsi=round(rsi, 1), atr=round(atr, 2)
        )

    return None
