"""
tracker.py — تسجيل الإشارات ومتابعة نتائجها آلياً

لماذا هذا الملف موجود:
البوت يرسل الإشارات ولا يحتفظ بها في أي مكان — حارس التكرار في الذاكرة فقط،
و risk_state.json يعدّ الصفقات المنفّذة لا الإشارات. فلا توجد أي طريقة للإجابة
عن السؤال البديهي: كم إشارة نجحت؟

المتابعة آلية بالكامل — لا تحتاجين لتأكيد أي صفقة يدوياً:
  1. كل إشارة تُرسل تُسجَّل في CSV بحالة FILLED (هذه الاستراتيجية تدخل بسعر
     السوق لحظة الإشارة، فلا مرحلة انتظار تنفيذ)
  2. كل دورة فحص نقرأ شموع 5M ونحدّث الإشارات المفتوحة: TP / SL / انتهاء مهلة
  3. R المحققة تُحسب من الحركة الفعلية، لا من الهدف النظري

حين تلمس شمعة واحدة الوقف والهدف معاً نفترض الوقف أولاً — ترتيب الحركة داخل
الشمعة غير معروف من OHLC، والقراءة المتشائمة تُبقي الإحصاء صادقاً.
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
    "id", "time_utc", "symbol", "direction",
    "entry", "sl", "tp", "rr", "lot",
    "status", "filled_time", "closed_time", "exit_price",
    "realized_r", "mfe_r", "mae_r",
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
def log_signal(signal, symbol: str, lot: float) -> None:
    rows = _read_all()
    now  = datetime.now(timezone.utc)

    rows.append({
        "id":         f"{symbol}-{now.strftime('%Y%m%d%H%M%S')}",
        "time_utc":   now.isoformat(timespec="seconds"),
        "symbol":     symbol,
        "direction":  signal.direction,
        "entry":      f"{signal.entry}",
        "sl":         f"{signal.sl}",
        "tp":         f"{signal.tp}",
        "rr":         f"{signal.rr}",
        "lot":        f"{lot}",
        "status":     "FILLED",
        "filled_time": now.isoformat(timespec="seconds"),
        "closed_time": "", "exit_price": "",
        "realized_r": "", "mfe_r": "", "mae_r": "",
    })
    _write_all(rows)
    logger.info(f"📝 سُجّلت الإشارة {rows[-1]['id']} بحالة {rows[-1]['status']}")


# ─── متابعة الإشارات المفتوحة ─────────────────────────────────────────────────
def update_open_signals(candles_by_symbol: dict[str, list[dict]]) -> None:
    """
    candles_by_symbol: {"US100": [شموع 5M], ...}
    يُحدّث كل إشارة غير مغلقة بناءً على الشموع الواقعة **بعد** لحظة الإشارة.
    """
    rows = _read_all()
    if not rows:
        return

    now, changed = datetime.now(timezone.utc), False

    for r in rows:
        if r.get("status") in ("TP", "SL", "EXPIRED"):
            continue

        candles = candles_by_symbol.get(r.get("symbol", ""))
        if not candles:
            continue

        entry, sl, tp = _f(r.get("entry")), _f(r.get("sl")), _f(r.get("tp"))
        if None in (entry, sl, tp):
            continue

        risk = abs(entry - sl)
        if risk == 0:
            continue

        try:
            t_signal = datetime.fromisoformat(r["time_utc"])
        except Exception:
            continue

        live = [c for c in candles
                if (_candle_time(c) or t_signal) >= t_signal]
        if not live:
            continue

        is_buy = r.get("direction") == "BUY"
        best = worst = 0.0
        outcome = exit_px = None

        for c in live:
            if is_buy:
                best   = max(best,  (c["high"] - entry) / risk)
                worst  = min(worst, (c["low"]  - entry) / risk)
                hit_sl = c["low"]  <= sl
                hit_tp = c["high"] >= tp
            else:
                best   = max(best,  (entry - c["low"])  / risk)
                worst  = min(worst, (entry - c["high"]) / risk)
                hit_sl = c["high"] >= sl
                hit_tp = c["low"]  <= tp

            # الوقف أولاً عند التعادل داخل الشمعة — إحصاء متحفّظ لا مُجمَّل
            if hit_sl:
                outcome, exit_px = "SL", sl
                break
            if hit_tp:
                outcome, exit_px = "TP", tp
                break

        r["mfe_r"], r["mae_r"] = f"{best:.2f}", f"{worst:.2f}"

        if outcome:
            r["status"], r["exit_price"] = outcome, f"{exit_px}"
            r["closed_time"] = now.isoformat(timespec="seconds")
            r["realized_r"]  = f"{((exit_px - entry) if is_buy else (entry - exit_px)) / risk:.2f}"
            changed = True
            logger.info(f"🎯 {r['id']} → {outcome} | R محققة {r['realized_r']}")
        elif now - t_signal > timedelta(minutes=MAX_TRADE_AGE_MIN):
            last = live[-1]["close"]
            r["status"], r["exit_price"] = "EXPIRED", f"{last}"
            r["closed_time"] = now.isoformat(timespec="seconds")
            r["realized_r"]  = f"{((last - entry) if is_buy else (entry - last)) / risk:.2f}"
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
        return (f"📊 <b>تقرير آخر {days} يوم</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"لا توجد إشارات مسجّلة في هذه الفترة.")

    tp  = [r for r in recent if r["status"] == "TP"]
    sl  = [r for r in recent if r["status"] == "SL"]
    exp = [r for r in recent if r["status"] == "EXPIRED"]
    opn = [r for r in recent if r["status"] == "FILLED"]

    wins, losses = len(tp), len(sl)
    closed = wins + losses + len(exp)
    total_r = sum(_f(r.get("realized_r")) or 0.0
                  for r in recent if r["status"] in ("TP", "SL", "EXPIRED"))
    wr = (wins / (wins + losses) * 100) if (wins + losses) else 0.0

    lines = [
        f"📊 <b>تقرير آخر {days} يوم</b>",
        "━━━━━━━━━━━━━━━━━━━━",
        f"إشارات أُرسلت : <b>{len(recent)}</b>",
        "",
        f"✅ وصلت الهدف   : <b>{wins}</b>",
        f"❌ ضربت الوقف   : <b>{losses}</b>",
        f"⏳ انتهت مهلتها : <b>{len(exp)}</b>",
        f"🔄 ما زالت مفتوحة: <b>{len(opn)}</b>",
        "━━━━━━━━━━━━━━━━━━━━",
    ]
    if wins + losses:
        lines.append(f"نسبة النجاح : <b>{wr:.0f}%</b>  ({wins} من {wins + losses})")
    if closed:
        lines.append(f"محصلة R     : <b>{total_r:+.2f}R</b>")
        lines.append(f"متوسط لكل صفقة: <b>{total_r / closed:+.2f}R</b>")

    # الهدف 2×ATR والوقف 1×ATR ⇒ RR ثابت 2:1 ⇒ نقطة التعادل 33%
    if wins + losses:
        be = 100 / (1 + 2.0)
        lines += ["━━━━━━━━━━━━━━━━━━━━",
                  f"RR المستهدف : 2.0:1",
                  f"نسبة التعادل : {be:.0f}%",
                  f"الحكم        : <b>{'فوق التعادل ✅' if wr > be else 'تحت التعادل ⚠️'}</b>"]

    # تفصيل حسب الأداة — يكشف أي أداة تسحب النتيجة للأسفل
    by_sym: dict[str, list] = {}
    for r in recent:
        by_sym.setdefault(r.get("symbol", "?"), []).append(r)
    detail = []
    for k in sorted(by_sym):
        g  = by_sym[k]
        gw = sum(1 for r in g if r["status"] == "TP")
        gl = sum(1 for r in g if r["status"] == "SL")
        if gw + gl:
            detail.append(f"• {k}: {gw}✅ / {gl}❌")
    if detail:
        lines += ["━━━━━━━━━━━━━━━━━━━━", "<b>حسب الأداة:</b>"] + detail

    return "\n".join(lines)
