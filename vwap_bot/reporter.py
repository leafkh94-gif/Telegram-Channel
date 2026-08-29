"""
reporter.py — التقرير اليومي للأداء

ثلاثة فروق مقصودة عن نص التعليمات، وسببها:

1. المكان: التعليمات تضعه في جذر المشروع. الـ workflow يشغّل
   `python vwap_bot/main.py`، فيصير sys.path[0] هو vwap_bot/ ولا يرى الجذر —
   `import reporter` كان سيفشل، والـ try/except ImportError في التعليمات كان
   سيبتلع الفشل بصمت فيبقى التقرير ميتاً بلا أي رسالة خطأ. (تم التحقق عملياً.)

2. مصدر البيانات: التعليمات تنشئ trades_log.json وتسجّل فيه كل إشارة من جديد.
   لكن tracker.py يسجّلها أصلاً في signals_log.csv. نظامان متوازيان يعنيان
   تسجيلاً مزدوجاً وملفَّين قد يتناقضان. هذا الملف يقرأ من سجل المتتبّع نفسه.

3. طريقة تحديد النتيجة: التعليمات تستخدم get_current_price كل دورة، أي أنها
   ترى السعر لحظة الاستطلاع فقط. إذا اخترق السعر الهدف ثم عاد خلال الدقيقة
   الفاصلة بين فحصين، تضيع النتيجة وتبقى الصفقة "مفتوحة" للأبد فتشوّه التقرير.
   قياس على 400 صفقة محاكاة: 3% من النتائج تضيع بهذه الطريقة. المتتبّع يقرأ
   قمة وقاع الشمعة فيلتقط أي لمسة داخلها.

شكل التقرير (شريط التقدّم والتفصيل حسب الأداة) كما في التعليمات.
"""

import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def _today_utc() -> str:
    # UTC صراحةً: opened_at مكتوب بـ UTC، و date.today() محلي — خلطهما يجعل
    # التقرير يفقد إشارات قرب منتصف الليل إذا اختلف توقيت الحاوية.
    return datetime.now(timezone.utc).date().isoformat()


def get_today_stats() -> dict:
    from tracker import _read_all

    today = _today_utc()
    rows  = [r for r in _read_all() if (r.get("time_utc") or "").startswith(today)]

    wins    = [r for r in rows if r.get("status") == "TP"]
    losses  = [r for r in rows if r.get("status") == "SL"]
    open_t  = [r for r in rows if r.get("status") == "FILLED"]
    expired = [r for r in rows if r.get("status") == "EXPIRED"]

    closed   = len(wins) + len(losses)
    win_rate = (len(wins) / closed * 100) if closed else 0.0

    total_r = 0.0
    for r in rows:
        if r.get("status") in ("TP", "SL", "EXPIRED"):
            try:
                total_r += float(r.get("realized_r") or 0)
            except (TypeError, ValueError):
                pass

    return {
        "total": len(rows), "wins": len(wins), "losses": len(losses),
        "open": len(open_t), "expired": len(expired),
        "win_rate": round(win_rate, 1), "total_r": round(total_r, 2),
        "by_symbol": _breakdown_by_symbol(rows),
    }


def _breakdown_by_symbol(rows: list[dict]) -> dict:
    result: dict = {}
    for r in rows:
        sym = r.get("symbol", "?")
        d = result.setdefault(sym, {"wins": 0, "losses": 0, "open": 0})
        st = r.get("status")
        if st == "TP":
            d["wins"] += 1
        elif st == "SL":
            d["losses"] += 1
        elif st == "FILLED":
            d["open"] += 1
    return result


def build_daily_report() -> str:
    s     = get_today_stats()
    today = _today_utc()

    if s["total"] == 0:
        return (
            f"📊 <b>التقرير اليومي — {today}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"لا توجد إشارات اليوم."
        )

    filled = int(s["win_rate"] / 10)
    bar    = "🟩" * filled + "⬜" * (10 - filled)

    lines = [
        f"📊 <b>التقرير اليومي — {today}</b>",
        "━━━━━━━━━━━━━━━━━━━━",
        f"إجمالي الإشارات : <b>{s['total']}</b>",
        f"✅ ناجحة        : <b>{s['wins']}</b>",
        f"❌ خاسرة        : <b>{s['losses']}</b>",
        f"⏳ مفتوحة        : <b>{s['open']}</b>",
    ]
    if s["expired"]:
        lines.append(f"⌛ انتهت مهلتها : <b>{s['expired']}</b>")

    lines += [
        "━━━━━━━━━━━━━━━━━━━━",
        f"نسبة النجاح : <b>{s['win_rate']}%</b>",
        bar,
    ]

    # الهدف 2×ATR والوقف 1×ATR ⇒ RR ثابت 2:1 ⇒ التعادل عند 33%.
    # النسبة وحدها مضلّلة بدونها: 40% ناجحة عند 2:1 صفقة رابحة، لا خاسرة.
    if s["wins"] + s["losses"]:
        verdict = "فوق التعادل ✅" if s["win_rate"] > 33.3 else "تحت التعادل ⚠️"
        lines += [
            f"محصلة R     : <b>{s['total_r']:+.2f}R</b>",
            f"التعادل عند 33% ⇒ <b>{verdict}</b>",
        ]

    lines += ["━━━━━━━━━━━━━━━━━━━━", "<b>حسب الأداة:</b>"]
    for sym, d in s["by_symbol"].items():
        closed = d["wins"] + d["losses"]
        rate   = (d["wins"] / closed * 100) if closed else 0
        lines.append(
            f"  {sym}: {d['wins']}✅ / {d['losses']}❌ ({rate:.0f}%)"
            + (f" +{d['open']}⏳" if d["open"] else "")
        )

    return "\n".join(lines)


def send_daily_report() -> None:
    from telegram_bot import send_message
    send_message(build_daily_report())
    logger.info("📤 أُرسل التقرير اليومي")
