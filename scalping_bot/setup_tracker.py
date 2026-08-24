"""
setup_tracker.py — متتبّع الإعداد الأول (Sweep → BOS → دخول) بحالة مستمرة

المشكلة في الكود القديم: كان يشترط أن يظهر الـ Sweep (على 15M) والـ BOS (على 5M)
في نفس لحظة الفحص، ضمن آخر 3 و6 شموع فقط — بلا ذاكرة. عملياً لا يتقاطعان أبداً،
فينتج صفر صفقات.

هذا المتتبّع يحتفظ بحالة لكل أداة عبر عمليات الفحص:
  IDLE → (اكتشاف Sweep) → AWAIT_CONFIRM → (كسر البنية BOS بعد الـ Sweep) → إشارة
         → COOLDOWN (منع التكرار على نفس المستوى) → IDLE

الحالة تُحفظ في ملف JSON (يُخزَّن في cache بين تشغيلات GitHub Actions) لتصمد أمام
إعادة التشغيل كل ~5 ساعات و40 دقيقة.

State-machine per instrument that REMEMBERS a liquidity sweep and waits for the
break-of-structure on a later scan — the fix for "sweep and BOS never coincide
in one snapshot". Returns (signal_or_None, human-readable reason) so the caller
can log exactly where each scan stops.
"""

import json
import logging
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Optional

from indicators import BOSEvent, FVG, find_fvg
from strategy_1_sweep import (
    SweepEvent, Setup1Signal, build_liquidity_map, calculate_setup1_levels,
)

logger = logging.getLogger(__name__)

STATE_FILE = Path("setup_state.json")

# كم شمعة 15M نرجع للبحث عن Sweep حديث (بدل آخر 3 فقط)
SWEEP_SCAN_CANDLES_15M = 8
# مهلة انتظار الـ BOS بعد الـ Sweep (دقائق) قبل إلغاء الإعداد
CONFIRM_WINDOW_MIN     = 90
# تهدئة على نفس المستوى بعد إطلاق إشارة أو انتهاء المهلة (دقائق)
COOLDOWN_MIN           = 120
# نطاق مطابقة الأسعار حين نقارن مستوى Sweep سبق التعامل معه
LEVEL_MATCH_PCT        = 0.0005   # 0.05%


# ─── تحميل / حفظ الحالة ──────────────────────────────────────────────────────
def _load() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {}


def _save(state: dict) -> None:
    try:
        STATE_FILE.write_text(json.dumps(state, indent=2))
    except OSError:
        pass


def _parse(t: str) -> Optional[datetime]:
    if not t:
        return None
    try:
        dt = datetime.fromisoformat(t.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _now(candles) -> datetime:
    """أحدث وقت شمعة كمرجع زمني (أدق من ساعة الجهاز مقابل بيانات السوق)."""
    return _parse(candles[-1]["time"]) or datetime.now(timezone.utc)


# ─── اكتشاف Sweep حديث ضمن نافذة أوسع ────────────────────────────────────────
def _find_recent_sweep(candles_15m, liquidity_map, trend) -> Optional[SweepEvent]:
    """أحدث Sweep صالح ضمن آخر SWEEP_SCAN_CANDLES_15M شمعة، متوافق مع الاتجاه.
    Bullish sweep (اصطياد sell-side) يُتوقّع فقط في اتجاه صاعد، والعكس."""
    n = len(candles_15m)
    window = candles_15m[-SWEEP_SCAN_CANDLES_15M:]
    base = n - len(window)
    ssl = liquidity_map.get("nearest_ssl")
    bsl = liquidity_map.get("nearest_bsl")

    # نمسح من الأحدث للأقدم ونعيد أول (أحدث) Sweep مطابق للاتجاه
    for off in range(len(window) - 1, -1, -1):
        c = window[off]
        if trend == "bullish" and ssl is not None:
            if c["low"] < ssl and c["close"] > ssl:
                return SweepEvent("bullish", ssl, c["low"], c["high"], base + off)
        if trend == "bearish" and bsl is not None:
            if c["high"] > bsl and c["close"] < bsl:
                return SweepEvent("bearish", bsl, c["low"], c["high"], base + off)
    return None


# ─── كسر البنية بعد الـ Sweep على 5M ─────────────────────────────────────────
def _bos_since_sweep(candles_5m, direction, sweep_time) -> Optional[BOSEvent]:
    """أول شمعة 5M تُغلق كاسرةً بنية ما بعد الـ Sweep (running high/low). أكثر
    موثوقية من اشتراط قمة وقاع كسريّين في نافذة 6 شموع."""
    st = _parse(sweep_time)
    post = [c for c in candles_5m if (_parse(c["time"]) or datetime.min.replace(tzinfo=timezone.utc)) > st] if st else []
    if len(post) < 2:
        return None
    if direction == "bullish":
        ref = post[0]["high"]
        for i, c in enumerate(post[1:], start=1):
            if c["close"] > ref:
                return BOSEvent("bullish", ref, "BOS", i)
            ref = max(ref, c["high"])
    else:
        ref = post[0]["low"]
        for i, c in enumerate(post[1:], start=1):
            if c["close"] < ref:
                return BOSEvent("bearish", ref, "BOS", i)
            ref = min(ref, c["low"])
    return None


def _matching_fvg(candles_5m, direction, sweep_time) -> Optional[FVG]:
    st = _parse(sweep_time)
    for f in reversed(find_fvg(candles_5m, lookback=min(len(candles_5m), 20))):
        if f.direction != direction:
            continue
        ct = _parse(candles_5m[f.candle_index]["time"]) if 0 <= f.candle_index < len(candles_5m) else None
        if st is None or (ct and ct > st):
            return f
    return None


def _levels_close(a: float, b: float) -> bool:
    return a and b and abs(a - b) / b <= LEVEL_MATCH_PCT


# ─── الدالة الرئيسية: تُستدعى كل فحص ──────────────────────────────────────────
def process(symbol, trend, candles_15m, candles_5m, current_price):
    """تُقدّم آلة الحالة خطوة واحدة لهذه الأداة.
    تُعيد (Setup1Signal أو None, سبب نصّي للتسجيل)."""
    if trend not in ("bullish", "bearish"):
        return None, "no-trend"
    if len(candles_15m) < 5 or len(candles_5m) < 5:
        return None, "insufficient-candles"

    allst = _load()
    st = allst.get(symbol) or {"phase": "idle"}
    now = _now(candles_15m)
    liq = build_liquidity_map(candles_15m)

    # تهدئة على مستوى سبق التعامل معه
    if st.get("phase") == "cooldown":
        until = _parse(st.get("cooldown_until"))
        if until and now >= until:
            st = {"phase": "idle"}
        else:
            allst[symbol] = st; _save(allst)
            return None, "cooldown"

    # AWAIT_CONFIRM: عندنا Sweep محفوظ — ننتظر الـ BOS
    if st.get("phase") == "await_confirm":
        # اتجاه الـ Sweep يجب أن يبقى متوافقاً مع الاتجاه الحالي
        if st["direction"] != trend:
            allst[symbol] = {"phase": "idle"}; _save(allst)
            return None, "trend-flipped→reset"

        swept = _parse(st["sweep_time"])
        if swept and (now - swept) > timedelta(minutes=CONFIRM_WINDOW_MIN):
            allst[symbol] = {"phase": "cooldown",
                             "cooldown_until": (now + timedelta(minutes=COOLDOWN_MIN)).isoformat(),
                             "handled_level": st["swept_level"]}
            _save(allst)
            return None, "confirm-window-expired→cooldown"

        bos = _bos_since_sweep(candles_5m, st["direction"], st["sweep_time"])
        if bos is None:
            allst[symbol] = st; _save(allst)
            return None, f"await-BOS (swept {st['swept_level']:.2f})"

        # تأكيد الـ BOS — نبني الإشارة
        sweep = SweepEvent(st["direction"], st["swept_level"],
                           st["sweep_low"], st["sweep_high"], st.get("sweep_index", 0))
        fvg = _matching_fvg(candles_5m, st["direction"], st["sweep_time"])
        signal = calculate_setup1_levels(symbol, trend, sweep, bos, fvg, liq, current_price)

        # بعد التأكيد ننتقل للتهدئة حتى لا نكرّر على نفس المستوى
        allst[symbol] = {"phase": "cooldown",
                         "cooldown_until": (now + timedelta(minutes=COOLDOWN_MIN)).isoformat(),
                         "handled_level": st["swept_level"]}
        _save(allst)
        if signal is None:
            return None, "BOS-confirmed but RR<min"
        return signal, f"CONFIRMED {bos.kind} {bos.direction}"

    # IDLE: نبحث عن Sweep حديث
    sweep = _find_recent_sweep(candles_15m, liq, trend)
    if sweep is None:
        allst[symbol] = {"phase": "idle"}; _save(allst)
        return None, "no-sweep"

    # لا نُعِد التسليح على مستوى عُولج للتو
    if _levels_close(sweep.swept_level, (st.get("handled_level") or 0)):
        allst[symbol] = st; _save(allst)
        return None, "sweep=handled-level"

    sweep_time = candles_15m[sweep.candle_index]["time"] if 0 <= sweep.candle_index < len(candles_15m) else candles_15m[-1]["time"]
    allst[symbol] = {"phase": "await_confirm", "direction": sweep.direction,
                     "swept_level": sweep.swept_level, "sweep_low": sweep.sweep_low,
                     "sweep_high": sweep.sweep_high, "sweep_index": sweep.candle_index,
                     "sweep_time": sweep_time}
    _save(allst)
    return None, f"sweep@{sweep.swept_level:.2f} → awaiting BOS"
