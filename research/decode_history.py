"""فك ترميز الشموع المنقولة عبر لوق التشغيل."""
import base64, gzip, re
from datetime import datetime, timedelta

B64 = re.compile(r"[A-Za-z0-9+/=]+")

def decode_blob(blob: str) -> list[dict]:
    txt = gzip.decompress(base64.b64decode(blob)).decode().split("\n")
    t0 = datetime.fromisoformat(txt[0][1:])
    rows, prev = [], None
    for line in txt[1:]:
        if not line: continue
        parts = list(map(int, line.split(",")))
        m, vals = parts[0], parts[1:]
        cur = vals if prev is None else [v + p for v, p in zip(vals, prev)]
        prev = cur
        rows.append({"time": (t0 + timedelta(minutes=m)).isoformat(),
                     "open": cur[0]/100, "high": cur[1]/100,
                     "low": cur[2]/100, "close": cur[3]/100, "volume": cur[4]})
    return rows

def extract(log_text: str) -> dict:
    """
    يستخرج كل كتلة بيانات من اللوق.

    GitHub يسبق كل سطر بطابع زمني، والأسطر الفارغة تصير طابعاً وحده بلا مسافة
    بعده — لذلك لا نعتمد على قصّ بادئة ثابتة، بل نأخذ أطول تتابع base64 في كل
    سطر ونتجاهل ما دونه. الطول المعلن في الترويسة يتحقّق من اكتمال النقل.
    """
    out = {}
    for m in re.finditer(
        r"=== DATA_BEGIN (\S+) (\S+) rows=(\d+) b64=(\d+) ===(.*?)=== DATA_END",
        log_text, re.S):
        sym, res, n, blen, body = m.groups()
        parts = []
        for line in body.split("\n"):
            cands = B64.findall(line)
            if not cands: continue
            best = max(cands, key=len)
            if len(best) >= 100:          # الطابع الزمني أقصر بكثير من أي كتلة
                parts.append(best)
        blob = "".join(parts)
        assert len(blob) == int(blen), f"{sym}: نقل ناقص {len(blob)}/{blen}"
        out[sym] = decode_blob(blob)
        assert len(out[sym]) == int(n), f"{sym}: {len(out[sym])} != {n}"
    return out
