"""
points_study.py — "بدي 70 إلى 100 نقطة" مقاسة على البيانات

السؤال بصيغته الحرفية: هل تستطيع استراتيجية أن تعطي 70-100 نقطة لكل صفقة على
الذهب و US500 و US30 و US100؟

قبل أي قياس، مشكلة في السؤال نفسه: "نقطة" ليست وحدة واحدة. مئة نقطة على US30
حركة روتينية داخل الساعة، ومئة نقطة على الذهب حركة تدوم أياماً. لذلك يطبع هذا
الملف أولاً معنى الـ100 نقطة على كل أداة — نسبةً من السعر، وبوحدات ATR — ثم
يقيس ثلاثة أشياء:

  1. توزيع أقصى ربح متاح (MFE): كم نقطة أعطت كل إشارة في أفضل لحظاتها. هذا
     سقف ما يستطيع أي هدف ثابت التقاطه.

  2. نسبة الإصابة والحصيلة الصافية لهدف ثابت بالنقاط — لأن نسبة إصابة عالية
     بهدف صغير مقابل وقف كبير تخسر، وهذه هي المصيدة المعتادة.

  3. مسح شبكي على (الوقف، الهدف) بوحدات ATR: هل توجد تركيبة هدف-ثابت موجبة
     أصلاً، وكم تساوي بالنقاط على كل أداة.

لتمكين (3) بلا إعادة تشغيل الاستراتيجية لكل تركيبة، نسجّل لكل إشارة مسارها
الأمامي (أعلى وأدنى كل شمعة، منسوبَين إلى الدخول ومقسومَين على ATR). التقييم
بعدها حسابٌ على المسار، والإشارات نفسها لا تتغيّر لأن الدخول لا يعتمد على
الوقف.

القياس يستدعي strategy.scan نفسها التي يعمل بها البوت.
"""

import os
import statistics
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "trend_bot"))

import strategy                                          # noqa: E402
from config import STOP_ATR, TRAIL_ATR                   # noqa: E402
from position import open_position, update, risk_unit    # noqa: E402

TARGETS  = [30, 50, 70, 100, 150, 200, 300]
MAX_PATH = 400        # شمعة ساعة ≈ 3 أسابيع تداول — أطول من أي صفقة رصدناها


def _coarse_index(fine: list[dict], coarse: list[dict]) -> list[int]:
    """لكل شمعة ساعة: فهرس آخر شمعة خشنة مكتملة. الـ -1 يمنع النظر للمستقبل."""
    out, j = [], -1
    ct = [c["time"] for c in coarse]
    for c in fine:
        while j + 1 < len(ct) and ct[j + 1] < c["time"]:
            j += 1
        out.append(j - 1)
    return out


def collect(symbol: str, daily: list[dict], h4: list[dict], h1: list[dict],
            spread: float = 0.0, warmup: int = 80) -> list[dict]:
    """
    يجمع كل إشارة مع مسارها الأمامي وحصيلة الوقف المتحرّك الحالي.

    الإشارات تُولَّد بمنطق البوت غير المعدَّل، ثم يُسجَّل المسار مستقلاً عن أي
    قرار خروج. صفقة واحدة في الأداة الواحدة في الوقت الواحد — كما يعمل البوت —
    فلا نعدّ إشارات ما كان ليأخذها أصلاً.
    """
    i4, idd = _coarse_index(h1, h4), _coarse_index(h1, daily)
    out, pos, rec, path_i = [], None, None, 0

    for i in range(warmup, len(h1)):
        bar = h1[i]

        if pos is not None:
            pos, ex = update(pos, bar, spread)
            if ex:
                rec["trail_pts"] = ex.price - pos.entry - spread
                rec["trail_r"]   = ex.r
                rec["bars"]      = pos.bars
                out.append(rec)
                pos, rec = None, None
            continue

        di, fi = idd[i], i4[i]
        if di < 25 or fi < 25:
            continue

        sig = strategy.scan(symbol, daily[:di + 1], h4[:fi + 1], h1[:i + 1], [bar])
        if not sig:
            continue

        entry = sig.entry + spread / 2
        pos = open_position(symbol, entry, sig.atr, bar["time"])

        # المسار الأمامي بوحدات ATR — يبدأ من الشمعة التالية، لأن شمعة الإشارة
        # نفسها أُغلقت قبل الدخول فلا تخصّنا.
        path = [((c["high"] - entry) / sig.atr, (c["low"] - entry) / sig.atr)
                for c in h1[i + 1: i + 1 + MAX_PATH]]

        rec = {"symbol": symbol, "time": bar["time"], "entry": entry,
               "atr": sig.atr, "path": path,
               "trail_pts": 0.0, "trail_r": 0.0, "bars": 0}

    return out


def outcome(rec: dict, stop_atr: float, target_atr: float) -> float:
    """
    حصيلة صفقة واحدة بوحدات R لو أُديرت بوقف ثابت وهدف ثابت.

    داخل الشمعة الواحدة نفحص الوقف أولاً. الشمعة تعطينا قمة وقاعاً بلا ترتيب
    زمني، وافتراض أن الهدف سبق الوقف يجمّل النتيجة بلا سند.

    إن لم يُضرب أيّهما حتى نهاية المسار، نُغلق على آخر سعر معروف — لا نفترض
    ربحاً ولا نتجاهل الصفقة.
    """
    for hi, lo in rec["path"]:
        if lo <= -stop_atr:
            return -1.0
        if hi >= target_atr:
            return target_atr / stop_atr
    if not rec["path"]:
        return 0.0
    return rec["path"][-1][0] / stop_atr      # تقريب: إغلاق عند آخر قمة معروفة


def fixed_target_pts(rec: dict, target_pts: float) -> tuple[bool, float]:
    """
    نفس المنطق لكن بالنقاط: هدف ثابت بالنقاط، ووقف البوت الحالي (STOP_ATR×ATR).
    يُرجع (هل أُصيب الهدف، الحصيلة بالنقاط).
    """
    stop_pts = STOP_ATR * rec["atr"]
    for hi, lo in rec["path"]:
        if lo * rec["atr"] <= -stop_pts:
            return False, -stop_pts
        if hi * rec["atr"] >= target_pts:
            return True, target_pts
    last = rec["path"][-1][0] * rec["atr"] if rec["path"] else 0.0
    return False, last


def report(rows: list[dict], label: str) -> None:
    if not rows:
        print(f"{label}: لا إشارات")
        return

    n     = len(rows)
    stop  = statistics.mean(STOP_ATR * r["atr"] for r in rows)
    atr   = statistics.mean(r["atr"] for r in rows)
    price = statistics.mean(r["entry"] for r in rows)
    mfe   = sorted(max((h for h, _ in r["path"]), default=0.0) * r["atr"]
                   for r in rows)

    def pct(v):
        return v / price * 100

    print(f"\n{'=' * 70}\n{label}   |   {n} إشارة")
    print(f"متوسط السعر {price:,.1f} | ATR الساعة {atr:.2f} ({pct(atr):.3f}%) "
          f"| وقف البوت {stop:.0f} نقطة ({pct(stop):.2f}%)")
    print(f"‏100 نقطة هنا = {pct(100):.2f}% من السعر = {100 / atr:.2f}× ATR "
          f"= {100 / stop:.2f}× وقف البوت")

    print("\n  أقصى ربح متاح لكل إشارة (MFE) بالنقاط:")
    for q, name in ((0.25, "الربع الأدنى"), (0.5, "الوسيط"),
                    (0.75, "الربع الأعلى"), (0.9, "أفضل 10%")):
        print(f"    {name:<14} {mfe[int(q * (n - 1))]:>10,.0f}")
    print(f"    {'الأقصى':<14} {mfe[-1]:>10,.0f}")

    print(f"\n  {'الهدف':>10} {'إصابة':>8} {'نقطة/صفقة':>12} {'الإجمالي':>12}")
    for t in TARGETS:
        res  = [fixed_target_pts(r, t) for r in rows]
        wins = sum(1 for hit, _ in res if hit)
        tot  = sum(v for _, v in res)
        mark = "✅" if tot > 0 else "❌"
        print(f"  {t:>6} نقطة {wins / n * 100:>6.1f}% "
              f"{tot / n:>+12.1f} {tot:>+12,.0f}  {mark}")

    trail_tot = sum(r["trail_pts"] for r in rows)
    trail_win = sum(1 for r in rows if r["trail_pts"] > 0)
    print(f"\n  الوقف المتحرّك الحالي ({TRAIL_ATR}×ATR): "
          f"إصابة {trail_win / n * 100:.1f}% | "
          f"{trail_tot / n:+.1f} نقطة/صفقة | {trail_tot:+,.0f} إجمالي | "
          f"أكبر رابحة {max(r['trail_pts'] for r in rows):+,.0f}")


def grid(rows: list[dict], label: str) -> None:
    """
    مسح على (الوقف، الهدف) بوحدات ATR: هل يوجد هدف ثابت موجب أصلاً؟

    النتيجة بوحدات R حتى تكون قابلة للمقارنة بين الأدوات، ويُطبع بجانبها
    متوسط ما تساويه بالنقاط على هذه الأداة تحديداً.
    """
    if not rows:
        return
    atr = statistics.mean(r["atr"] for r in rows)
    print(f"\n  مسح الهدف الثابت — {label} (R لكل صفقة، وبالنقاط بين قوسين)")
    print("     وقف↓ هدف→ " + "".join(f"{t:>16.0f}×ATR" for t in (1, 2, 3, 4, 6)))
    for s in (1.0, 1.5, 2.0, 3.0):
        cells = []
        for t in (1, 2, 3, 4, 6):
            rs  = [outcome(r, s, t) for r in rows]
            per = sum(rs) / len(rs)
            cells.append(f"{per:>+8.3f}({per * s * atr:>+6.0f})")
        print(f"  {s:>6.1f}×ATR " + "".join(cells))
