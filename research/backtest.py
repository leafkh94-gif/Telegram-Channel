"""
backtest.py — يشغّل **كود البوت نفسه** على بيانات تاريخية

هذا هو الفرق الجوهري عن كل قياس سابق: لا يعيد كتابة المنطق. يستدعي
strategy.scan و position.update — نفس الدوال التي ستعمل حيّاً. أي رقم يخرج
من هنا هو رقم عن سلوك البوت الفعلي، لا عن نسخة موازية منه.

الحاجة إليه ظهرت بالطريقة الصعبة: قياسان مستقلان لنفس الاستراتيجية على نفس
البيانات أعطيا ‎+0.105R و ‎-0.021R.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "trend_bot"))

import strategy                                    # noqa: E402
from position import open_position, update         # noqa: E402


def _coarse_index(fine: list[dict], coarse: list[dict]) -> list[int]:
    """
    لكل شمعة دقيقة: فهرس آخر شمعة خشنة **مكتملة**.

    الـ -1 مقصود: الشمعة الخشنة التي بدأت ولم تُغلق بعد لا يعرف البوت الحيّ
    نتيجتها، فاستعمالها هنا نظر إلى المستقبل يجعل النتائج أفضل مما ستكون.
    """
    out, j = [], -1
    ct = [c["time"] for c in coarse]
    for c in fine:
        while j + 1 < len(ct) and ct[j + 1] < c["time"]:
            j += 1
        out.append(j - 1)
    return out


def run(symbol: str, daily: list[dict], h4: list[dict], h1: list[dict],
        spread: float = 0.0, warmup: int = 80) -> dict:
    i4, idd = _coarse_index(h1, h4), _coarse_index(h1, daily)
    trades, pos = [], None

    for i in range(warmup, len(h1)):
        bar = h1[i]

        if pos is not None:
            pos, ex = update(pos, bar, spread)
            if ex:
                trades.append(ex.r)
                pos = None
            continue

        di, fi = idd[i], i4[i]
        if di < 25 or fi < 25:
            continue

        # الشموع المُمرّرة مغلقة كلها، تماماً كما يستدعيها البوت الحيّ.
        # m5 هنا الشمعة الحالية: حارس الملاحقة يقارن السعر بمستوى الاختراق،
        # وسعر الإغلاق هو أدقّ ما نملكه تاريخياً لتلك اللحظة.
        sig = strategy.scan(symbol, daily[:di + 1], h4[:fi + 1],
                            h1[:i + 1], [bar])
        if sig:
            pos = open_position(symbol, sig.entry + spread / 2, sig.atr,
                                bar["time"])

    n = len(trades)
    if n == 0:
        return {"n": 0, "wr": 0.0, "R": 0.0, "per": 0.0}
    wins = [t for t in trades if t > 0]
    return {"n": n, "wr": len(wins) / n * 100,
            "R": sum(trades), "per": sum(trades) / n,
            "best": max(trades), "worst": min(trades)}
