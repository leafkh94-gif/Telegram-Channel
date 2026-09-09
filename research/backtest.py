"""
backtest.py — يشغّل **كود البوت نفسه** على بيانات تاريخية

الفرق الجوهري عن أي قياس موازٍ: لا يعيد كتابة المنطق. يستدعي strategy.scan
و position.update — نفس الدالتين اللتين ستعملان حيّاً. أي رقم يخرج من هنا هو
رقم عن سلوك البوت الفعلي.

الحاجة إليه ظهرت بالطريقة الصعبة: قياسان مستقلان لنفس الاستراتيجية على نفس
البيانات أعطيا ‎+0.105R و ‎-0.021R، لأن إدارة الصفقة كانت مكتوبة مرتين.
"""

import os
import statistics
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bot"))

import strategy                                              # noqa: E402
from position import open_position, update, risk_unit        # noqa: E402


def daily_index(h1: list[dict], daily: list[dict]) -> list[int]:
    """
    لكل شمعة ساعة: فهرس آخر شمعة يومية **مكتملة**.

    الـ -1 مقصود: الشمعة اليومية التي بدأت ولم تُغلق لا يعرف البوت الحيّ
    نتيجتها، فاستعمالها هنا نظر إلى المستقبل يجعل النتائج أفضل مما ستكون.
    """
    out, j = [], -1
    dt = [c["time"] for c in daily]
    for c in h1:
        while j + 1 < len(dt) and dt[j + 1] < c["time"]:
            j += 1
        out.append(j - 1)
    return out


def run(symbol: str, daily: list[dict], h1: list[dict],
        spread: float = 0.0, warmup: int = 60) -> dict:
    """صفقة واحدة مفتوحة في كل لحظة لكل أداة — تماماً كما يعمل البوت."""
    idd = daily_index(h1, daily)
    trades, pos = [], None

    for i in range(warmup, len(h1)):
        bar = h1[i]

        if pos is not None:
            pos, ex = update(pos, bar, spread)
            if ex:
                trades.append({"time": pos.opened_at, "side": pos.side,
                               "r": ex.r, "pts": ex.r * risk_unit(pos),
                               "partial": pos.partial_price is not None})
                pos = None
            continue

        di = idd[i]
        if di < 25:
            continue

        # شموع مغلقة فقط، تماماً كما يستدعيها البوت الحيّ
        sig = strategy.scan(symbol, daily[:di + 1], h1[:i + 1])
        if sig:
            pos = open_position(symbol, sig.direction, sig.entry, sig.atr,
                                bar["time"])

    if not trades:
        return {"n": 0}
    rs = [t["r"] for t in trades]
    return {
        "n": len(rs),
        "wr": sum(1 for r in rs if r > 0) / len(rs) * 100,
        "R": statistics.mean(rs),
        "total": sum(rs),
        "pts": statistics.mean(t["pts"] for t in trades),
        "trades": trades,
    }
