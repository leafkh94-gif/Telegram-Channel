"""
run_explore.py — يمسح قواعد الدخول ويعرض الحدّ بين التكرار والأفضلية.

الاستعمال:
    python research/run_explore.py <ملف لوق السحب>

يطبع كل تركيبة مع صفقاتها في الأسبوع لكل أداة، ونتيجتها داخل العيّنة وخارجها،
وما يبقى منها بعد حذف أكبر صفقة. التركيبة تُقبل فقط إذا اجتمع فيها:
التكرار المطلوب، وربح خارج العيّنة، وبقاء موجب بعد حذف الأكبر.
"""

import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(__file__))

from decode_history import extract          # noqa: E402
from explore import Rule, signals, walk, SPLIT, WEEKS_TOTAL   # noqa: E402

TARGET_LO, TARGET_HI = 2.0, 3.0             # صفقات لكل أداة في الأسبوع


def resample(h1, hours):
    """شمعة اليوم تُبنى بالتاريخ لا بعدّ 24 ساعة — الأسواق تغلق ليلاً وعطلاً."""
    from datetime import datetime
    out, cur, key = [], None, None
    for c in h1:
        t = datetime.fromisoformat(c["time"].replace("Z", "+00:00"))
        k = t.date() if hours >= 24 else (t.date(), t.hour // hours)
        if k != key:
            if cur:
                out.append(cur)
            key, cur = k, dict(c)
        else:
            cur["high"]  = max(cur["high"], c["high"])
            cur["low"]   = min(cur["low"],  c["low"])
            cur["close"] = c["close"]
    if cur:
        out.append(cur)
    return out


RULES = [
    Rule("قناة48 + يومي + 4س  (القديمة)", 48, True,  True),
    Rule("قناة48 + يومي",                 48, True,  False),
    Rule("قناة24 + يومي + 4س",            24, True,  True),
    Rule("قناة24 + يومي",                 24, True,  False),
    Rule("قناة24 بلا مرشّح",              24, False, False),
    Rule("قناة12 + يومي + 4س",            12, True,  True),
    Rule("قناة12 + يومي",                 12, True,  False),
    Rule("قناة12 بلا مرشّح",              12, False, False),
    Rule("قناة8  + يومي",                  8, True,  False),
    Rule("قناة8  + يومي + 4س",             8, True,  True),
    Rule("قناة24 + يومي — اتجاهين",       24, True,  False, "both"),
    Rule("قناة12 + يومي — اتجاهين",       12, True,  False, "both"),
]

EXITS = [
    ("وقف2 تتبّع4",           dict(stop_atr=2.0, trail_atr=4.0)),
    ("وقف2 تتبّع4 + جزئي1",   dict(stop_atr=2.0, trail_atr=4.0, partial_atr=1.0)),
    ("وقف1.5 تتبّع3 + جزئي1", dict(stop_atr=1.5, trail_atr=3.0, partial_atr=1.0)),
]


def main():
    data = extract(open(sys.argv[1], encoding="utf-8", errors="replace").read())
    prepared = {}
    for sym, h1 in sorted(data.items()):
        prepared[sym] = (h1, resample(h1, 4), resample(h1, 24))
    n_sym = len(prepared)

    print(f"البيانات: {n_sym} أدوات × سنتان بفريم الساعة")
    print(f"الهدف: {TARGET_LO}-{TARGET_HI} صفقة لكل أداة أسبوعياً "
          f"(أي {TARGET_LO * n_sym:.0f}-{TARGET_HI * n_sym:.0f} إجمالاً)\n")

    rows = []
    for rule in RULES:
        sigs = {s: signals(h1, h4, d, rule) for s, (h1, h4, d) in prepared.items()}
        for ex_name, ex_kw in EXITS:
            trades = []
            for s, (h1, _, _) in prepared.items():
                trades += walk(h1, sigs[s], **ex_kw)
            if len(trades) < 40:
                continue

            rs  = [r for _, r in trades]
            ins = [r for t, r in trades if t <  SPLIT]
            oos = [r for t, r in trades if t >= SPLIT]
            if not ins or not oos:
                continue

            rows.append({
                "rule": rule.name, "exit": ex_name,
                "n": len(rs),
                "per_wk": len(rs) / WEEKS_TOTAL / n_sym,
                "wr": sum(1 for r in rs if r > 0) / len(rs) * 100,
                "R": statistics.mean(rs),
                "ins": statistics.mean(ins),
                "oos": statistics.mean(oos),
                "nb": statistics.mean(sorted(rs)[:-1]),
            })

    rows.sort(key=lambda r: -r["oos"])
    print(f"{'قاعدة الدخول':<34}{'الخروج':<24}{'ن':>5}{'/أسبوع':>8}"
          f"{'إصابة':>7}{'R':>8}{'تصميم':>8}{'أعمى':>8}{'بلا الأكبر':>11}")
    print("-" * 113)
    for r in rows:
        ok = (TARGET_LO <= r["per_wk"] <= TARGET_HI
              and r["oos"] > 0 and r["nb"] > 0)
        near = TARGET_LO * 0.7 <= r["per_wk"] <= TARGET_HI * 1.4
        mark = " ✅" if ok else ("  ·" if near else "   ")
        print(f"{r['rule']:<34}{r['exit']:<24}{r['n']:>5}{r['per_wk']:>8.2f}"
              f"{r['wr']:>6.1f}%{r['R']:>+8.3f}{r['ins']:>+8.3f}{r['oos']:>+8.3f}"
              f"{r['nb']:>+11.3f}{mark}")

    print("\n✅ = التكرار مطلوب، وموجبة خارج العيّنة، وتبقى موجبة بلا أكبر صفقة")
    print(" · = التكرار قريب من المطلوب")


if __name__ == "__main__":
    import logging
    logging.disable(logging.INFO)
    main()
