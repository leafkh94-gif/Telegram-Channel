"""
tracker.py — تسجيل الإشارات ومتابعة نتائجها آلياً

لماذا هذا الملف موجود:
البوت كان يرسل الإشارات ولا يحتفظ بها في أي مكان — حارس التكرار في الذاكرة فقط،
و risk_state.json يعدّ الصفقات المنفّذة لا الإشارات. فلم تكن هناك أي طريقة للإجابة
عن السؤال البديهي: كم إشارة نجحت؟ (تتبّع النتائج كان في sweep_alert_agent.py
القديم وضاع حين استُبدل بالبوت المُجزّأ.)

المتابعة آلية بالكامل — لا تحتاجين لتأكيد أي صفقة يدوياً:
  1. كل إشارة تُرسل تُسجَّل في CSV بحالة PENDING
  2. كل دورة فحص نقرأ شموع 5M ونحدّث الإشارات المفتوحة:
       - وصل السعر لنقطة الدخول؟   → FILLED
       - لم يصل خلال مهلة الصلاحية؟ → NO_FILL (لم تدخل الصفقة أصلاً)
       - بعد الدخول: TP1 / TP2 / SL
  3. R المحققة تُحسب من الحركة الفعلية، لا من الهدف النظري

الإعداد الرابع يدخل بسعر السوق فيُسجَّل FILLED مباشرة؛ باقي الإعدادات أوامر
LIMIT تنتظر ارتداد السعر، ولهذا التمييز بين NO_FILL و SL مهم: الأولى ليست خسارة.
"""

import csv
import logging
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

SIGNALS_CSV = Path("signals_log.csv")

FIELDS = [
    "id", "time_utc", "symbol", "setup", "direction", "trend",
    "entry", "sl", "tp1", "tp2", "rr", "lot",
    "status", "filled_time", "closed_time", "exit_price",
    "realized_r", "mfe_r", "mae_r", "counter_trend",
]

# مهلة صلاحية أمر الدخول (يطابق ما تقوله الرسالة: "الدخول صالح لـ 30 دقيقة")
FILL_WINDOW_MIN   = 30
# أقصى عمر لصفقة مفتوحة قبل اعتبارها منتهية (سكالبينغ — لا نتركها أياماً)
MAX_TRADE_AGE_MIN = 480


# ─── قراءة/كتابة ──────────────────────────────────────────────────────────────
def _read_all() -> list[dict]:
    if not SIGNALS_CSV.exists():
        return []
    try:
        with SIGNALS_CSV.open(newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        # ترحيل: أي عمود جديد يُملأ فارغاً بدل أن ينهار القارئ
        for r in rows:
            for k in FIELDS:
                r.setdefault(k, "")
        return rows
    except Exception as e:
        logger.error(f"❌ خطأ قراءة سجل الإشارات: {e}")
        return []


def _write_all(rows: list[dict]) -> None:
    try:
        with SIGNALS_CSV.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            w.writeheader()
            for r in rows:
                w.writerow({k: r.get(k, "") for k in FIELDS})
    except Exception as e:
        logger.error(f"❌ خطأ كتابة سجل الإشارات: {e}")


def _f(v) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ─── تسجيل إشارة جديدة ────────────────────────────────────────────────────────
def log_signal(signal, setup_num: int, symbol: str, trend: str, lot: float) -> None:
    rows = _read_all()
    now  = datetime.now(timezone.utc)

    # الإعداد الرابع دخول سوق — يُعتبر منفّذاً فوراً
    market_entry = (setup_num == 4)

    rows.append({
        "id":         f"{symbol}-{setup_num}-{now.strftime('%Y%m%d%H%M%S')}",
        "time_utc":   now.isoformat(timespec="seconds"),
        "symbol":     symbol,
        "setup":      str(setup_num),
        "direction":  signal.direction,
        "trend":      trend,
        "entry":      f"{signal.entry}",
        "sl":         f"{signal.sl}",
        "tp1":        f"{signal.tp1}",
        "tp2":        f"{signal.tp2}",
        "rr":         f"{signal.rr}",
        "lot":        f"{lot}",
        "status":     "FILLED" if market_entry else "PENDING",
        "filled_time": now.isoformat(timespec="seconds") if market_entry else "",
        "closed_time": "", "exit_price": "",
        "realized_r": "", "mfe_r": "", "mae_r": "",
        "counter_trend": "1" if getattr(signal, "counter_trend", False) else "0",
    })
    _write_all(rows)
    logger.info(f"📝 سُجّلت الإشارة {rows[-1]['id']} بحالة {rows[-1]['status']}")


# ─── متابعة الإشارات المفتوحة ─────────────────────────────────────────────────
def update_open_signals(candles_by_symbol: dict[str, list[dict]]) -> None:
    """
    candles_by_symbol: {"US100": [شموع 5M], ...}
    يُحدّث كل إشارة غير مغلقة بناءً على الشموع التي وقعت **بعد** وقت الإشارة.
    """
    rows = _read_all()
    if not rows:
        return

    now     = datetime.now(timezone.utc)
    changed = False

    for r in rows:
        if r.get("status") in ("TP1", "TP2", "SL", "NO_FILL", "EXPIRED"):
            continue

        candles = candles_by_symbol.get(r.get("symbol", ""))
        if not candles:
            continue

        entry = _f(r.get("entry")); sl = _f(r.get("sl"))
        tp1   = _f(r.get("tp1"));   tp2 = _f(r.get("tp2"))
        if None in (entry, sl, tp1):
            continue

        is_buy = r.get("direction") == "BUY"
        risk   = abs(entry - sl)
        if risk == 0:
            continue

        try:
            t_signal = datetime.fromisoformat(r["time_utc"])
        except Exception:
            continue

        # الشموع الواقعة بعد لحظة الإشارة فقط
        after = []
        for c in candles:
            ct = _candle_time(c)
            if ct is not None and ct >= t_signal:
                after.append(c)

        # ─ مرحلة الانتظار: هل وصل السعر لنقطة الدخول؟
        if r.get("status") == "PENDING":
            filled = False
            for c in after:
                touched = (c["low"] <= entry <= c["high"])
                if touched:
                    r["status"]      = "FILLED"
                    r["filled_time"] = (_candle_time(c) or now).isoformat(timespec="seconds")
                    filled = True
                    changed = True
                    break
            if not filled:
                if now - t_signal > timedelta(minutes=FILL_WINDOW_MIN):
                    r["status"]      = "NO_FILL"
                    r["closed_time"] = now.isoformat(timespec="seconds")
                    r["realized_r"]  = "0"
                    changed = True
                    logger.info(f"⬜ {r['id']} لم تُنفّذ — السعر لم يعد لنقطة الدخول")
                continue

        # ─ مرحلة الصفقة المفتوحة
        try:
            t_fill = datetime.fromisoformat(r["filled_time"]) if r.get("filled_time") else t_signal
        except Exception:
            t_fill = t_signal

        live = [c for c in after if (_candle_time(c) or t_fill) >= t_fill]
        if not live:
            continue

        best = worst = 0.0
        outcome = None
        exit_px = None

        for c in live:
            if is_buy:
                best  = max(best,  (c["high"] - entry) / risk)
                worst = min(worst, (c["low"]  - entry) / risk)
                hit_sl = c["low"]  <= sl
                hit_t1 = c["high"] >= tp1
                hit_t2 = tp2 is not None and c["high"] >= tp2
            else:
                best  = max(best,  (entry - c["low"])  / risk)
                worst = min(worst, (entry - c["high"]) / risk)
                hit_sl = c["high"] >= sl
                hit_t1 = c["low"]  <= tp1
                hit_t2 = tp2 is not None and c["low"] <= tp2

            # داخل الشمعة الواحدة لا نعرف الترتيب — نفترض الأسوأ (الوقف أولاً).
            # هذا يجعل الإحصاءات متحفّظة بدل أن تُجمّل النتيجة.
            if hit_sl:
                outcome, exit_px = "SL", sl
                break
            if hit_t2:
                outcome, exit_px = "TP2", tp2
                break
            if hit_t1:
                outcome, exit_px = "TP1", tp1
                break

        r["mfe_r"] = f"{best:.2f}"
        r["mae_r"] = f"{worst:.2f}"

        if outcome:
            r["status"]      = outcome
            r["exit_price"]  = f"{exit_px}"
            r["closed_time"] = now.isoformat(timespec="seconds")
            r["realized_r"]  = f"{((exit_px - entry) if is_buy else (entry - exit_px)) / risk:.2f}"
            changed = True
            logger.info(f"🎯 {r['id']} → {outcome} | R محققة {r['realized_r']}")
        elif now - t_fill > timedelta(minutes=MAX_TRADE_AGE_MIN):
            last_px = live[-1]["close"]
            r["status"]      = "EXPIRED"
            r["exit_price"]  = f"{last_px}"
            r["closed_time"] = now.isoformat(timespec="seconds")
            r["realized_r"]  = f"{((last_px - entry) if is_buy else (entry - last_px)) / risk:.2f}"
            changed = True
            logger.info(f"⏳ {r['id']} انتهت مهلتها | R محققة {r['realized_r']}")
        else:
            changed = True

    if changed:
        _write_all(rows)


def _candle_time(c: dict) -> Optional[datetime]:
    t = c.get("time")
    if not t:
        return None
    try:
        dt = datetime.fromisoformat(str(t).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


# ─── التقرير ──────────────────────────────────────────────────────────────────
def build_report(days: int = 1) -> str:
    rows  = _read_all()
    since = datetime.now(timezone.utc) - timedelta(days=days)

    recent = []
    for r in rows:
        try:
            if datetime.fromisoformat(r["time_utc"]) >= since:
                recent.append(r)
        except Exception:
            continue

    if not recent:
        return (
            f"📊 <b>تقرير آخر {days} يوم</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"لا توجد إشارات مسجّلة في هذه الفترة."
        )

    tp1 = [r for r in recent if r["status"] == "TP1"]
    tp2 = [r for r in recent if r["status"] == "TP2"]
    sl  = [r for r in recent if r["status"] == "SL"]
    nof = [r for r in recent if r["status"] == "NO_FILL"]
    exp = [r for r in recent if r["status"] == "EXPIRED"]
    opn = [r for r in recent if r["status"] in ("PENDING", "FILLED")]

    wins   = len(tp1) + len(tp2)
    losses = len(sl)
    closed = wins + losses + len(exp)

    total_r = 0.0
    for r in recent:
        v = _f(r.get("realized_r"))
        if v is not None and r["status"] in ("TP1", "TP2", "SL", "EXPIRED"):
            total_r += v

    wr = (wins / (wins + losses) * 100) if (wins + losses) else 0.0

    lines = [
        f"📊 <b>تقرير آخر {days} يوم</b>",
        "━━━━━━━━━━━━━━━━━━━━",
        f"إشارات أُرسلت : <b>{len(recent)}</b>",
        "",
        f"✅ وصلت الهدف   : <b>{wins}</b>  (TP1: {len(tp1)} | TP2: {len(tp2)})",
        f"❌ ضربت الوقف   : <b>{losses}</b>",
        f"⬜ لم تدخل أصلاً : <b>{len(nof)}</b>",
        f"⏳ انتهت مهلتها : <b>{len(exp)}</b>",
        f"🔄 ما زالت مفتوحة: <b>{len(opn)}</b>",
        "━━━━━━━━━━━━━━━━━━━━",
    ]

    if wins + losses:
        lines.append(f"نسبة النجاح : <b>{wr:.0f}%</b>  ({wins} من {wins + losses})")
    if closed:
        lines.append(f"محصلة R     : <b>{total_r:+.2f}R</b>")
        lines.append(f"متوسط لكل صفقة: <b>{total_r / closed:+.2f}R</b>")

    # نقطة التعادل حسب متوسط RR المستهدف — تخبرك هل النسبة كافية فعلاً
    rrs = [_f(r.get("rr")) for r in recent]
    rrs = [x for x in rrs if x]
    if rrs and (wins + losses):
        avg_rr = sum(rrs) / len(rrs)
        be     = 100 / (1 + avg_rr)
        verdict = "فوق التعادل ✅" if wr > be else "تحت التعادل ⚠️"
        lines += ["━━━━━━━━━━━━━━━━━━━━",
                  f"متوسط RR المستهدف: {avg_rr:.1f}:1",
                  f"نسبة التعادل     : {be:.0f}%",
                  f"الحكم            : <b>{verdict}</b>"]

    # تفصيل حسب نوع الإعداد — يكشف أي إعداد يسحب النتيجة للأسفل
    by_setup: dict[str, list] = {}
    for r in recent:
        by_setup.setdefault(r.get("setup", "?"), []).append(r)
    names = {"1": "اصطياد سيولة", "2": "أخبار", "3": "عرض/طلب", "4": "استمرارية"}
    detail = []
    for k in sorted(by_setup):
        g  = by_setup[k]
        gw = sum(1 for r in g if r["status"] in ("TP1", "TP2"))
        gl = sum(1 for r in g if r["status"] == "SL")
        if gw + gl:
            detail.append(f"• {names.get(k, k)}: {gw}✅ / {gl}❌")
    if detail:
        lines += ["━━━━━━━━━━━━━━━━━━━━", "<b>حسب الإعداد:</b>"] + detail

    return "\n".join(lines)
