"""
run_points_study.py — يفكّ لوق التشغيل ويشغّل قياس النقاط عليه.

الاستعمال:
    python research/run_points_study.py <ملف اللوق>

البيانات المسحوبة بإطار الساعة فقط، فنبني منها 4 ساعات واليومي بإعادة تجميع
بدل سحبها مستقلة: التجميع من نفس الشموع يضمن اتساق الأطر الثلاثة، وسحبها
منفصلة يترك فجوات في العطل تجعل الفهرسة تنزلق.
"""

import sys
from datetime import datetime

from decode_history import extract


def resample(h1: list[dict], hours: int) -> list[dict]:
    """
    يجمّع شموع الساعة إلى إطار أكبر.

    الشمعة اليومية تُبنى بالتاريخ لا بعدّ 24 ساعة، لأن أسواق المؤشرات تغلق
    ساعات كل يوم وعطلة كاملة كل أسبوع؛ العدّ الأعمى يخلط يومين في شمعة.
    """
    out, cur, key = [], None, None
    for c in h1:
        t = datetime.fromisoformat(c["time"].replace("Z", "+00:00"))
        k = t.date() if hours >= 24 else (t.date(), t.hour // hours)
        if k != key:
            if cur:
                out.append(cur)
            key = k
            cur = dict(c)
        else:
            cur["high"]   = max(cur["high"], c["high"])
            cur["low"]    = min(cur["low"], c["low"])
            cur["close"]  = c["close"]
            cur["volume"] = cur.get("volume", 0) + c.get("volume", 0)
    if cur:
        out.append(cur)
    return out


def main() -> None:
    from points_study import collect, report, grid

    data = extract(open(sys.argv[1], encoding="utf-8", errors="replace").read())
    if not data:
        print("لم يُعثر على أي كتلة بيانات في اللوق")
        return

    everything = []
    for symbol, h1 in sorted(data.items()):
        h4    = resample(h1, 4)
        daily = resample(h1, 24)
        print(f"\n{symbol}: {len(h1):,} ساعة | {len(h4):,} أربع ساعات | "
              f"{len(daily):,} يوم | {h1[0]['time'][:10]} → {h1[-1]['time'][:10]}")
        rows = collect(symbol, daily, h4, h1)
        report(rows, symbol)
        grid(rows, symbol)
        everything += rows

    report(everything, "الأربع أدوات مجتمعة")


if __name__ == "__main__":
    import logging
    logging.disable(logging.INFO)     # scan يسجّل سطراً لكل شمعة مرفوضة
    sys.path.insert(0, __file__.rsplit("/", 1)[0])
    main()
