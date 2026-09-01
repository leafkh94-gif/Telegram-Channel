"""
fetch_history.py — سحب شموع تاريخية حقيقية للبحث

لماذا:
كل ما قِسناه حتى الآن جاء من عيّنات لوقز يومين — 203 صفقة، كلاهما يوم تذبذب.
أي استراتيجية تُعاير على هذا الحجم ستلائم الضجيج لا السوق، وستفشل بنفس الطريقة
عند أول نظام سوق مختلف. لبناء شيء يصمد نحتاج أسابيع من الشموع الحقيقية،
واختباراً داخل العيّنة وخارجها.

يُشغّل داخل GitHub Actions لأن بيانات الدخول والشبكة متاحتان هناك فقط، ثم يرفع
الناتج إلى فرع البيانات ليصير قابلاً للقراءة والتحليل لاحقاً.

الاستعمال:
    python research/fetch_history.py --days 30 --resolution MINUTE_5
"""

import argparse
import base64
import csv
import io
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "vwap_bot"))

from config import SYMBOLS, CAPITAL_BASE_URL  # noqa: E402
from capital_client import CapitalClient      # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("HISTORY")

# فرع منفصل عن status عمداً: البوت يرفع signals_log.csv إلى status كل خمس
# دقائق، وكتابة الاثنين على نفس المرجع تتسابق فيرجع 409 Conflict — وهذا بالضبط
# ما أفشل أول تشغيل بعد أن كان السحب نفسه ناجحاً بالكامل.
BRANCH = "data"
API    = "https://api.github.com"

# Capital.com يحدّ عدد الشموع في الطلب الواحد، فنقسّم المدى إلى نوافذ ونصل بينها.
# القيمة متحفّظة عمداً: تجاوز الحد يُرجع نتيجة مبتورة بصمت لا خطأ صريح.
MAX_PER_REQUEST = 900

RESOLUTION_MINUTES = {
    "MINUTE":    1,
    "MINUTE_5":  5,
    "MINUTE_15": 15,
    "HOUR":      60,
    "HOUR_4":    240,
    "DAY":       1440,
}


def fetch_range(client: CapitalClient, epic: str, resolution: str,
                start: datetime, end: datetime) -> list[dict]:
    """يسحب المدى كاملاً على دفعات، ويرتّب ويزيل التكرار."""
    step_min = RESOLUTION_MINUTES.get(resolution, 5)
    window   = timedelta(minutes=step_min * MAX_PER_REQUEST)

    rows: dict[str, dict] = {}
    cursor = start
    while cursor < end:
        chunk_end = min(cursor + window, end)
        params = {
            "resolution": resolution,
            "max": MAX_PER_REQUEST,
            "from": cursor.strftime("%Y-%m-%dT%H:%M:%S"),
            "to":   chunk_end.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        try:
            client._ensure_auth()
            r = requests.get(f"{CAPITAL_BASE_URL}/prices/{epic}",
                             headers=client.headers, params=params, timeout=30)
            if r.status_code == 429:
                logger.warning("⏳ تجاوزنا حد الطلبات — انتظار 20 ثانية")
                time.sleep(20)
                continue
            r.raise_for_status()
            prices = r.json().get("prices", [])
        except Exception as e:
            logger.error(f"❌ {epic} {cursor:%Y-%m-%d %H:%M}: {e}")
            cursor = chunk_end
            continue

        for c in prices:
            try:
                t = c.get("snapshotTimeUTC") or c.get("snapshotTime")
                if not t:
                    continue
                rows[t] = {
                    "time":   t,
                    "open":   float(c["openPrice"]["bid"]),
                    "high":   float(c["highPrice"]["bid"]),
                    "low":    float(c["lowPrice"]["bid"]),
                    "close":  float(c["closePrice"]["bid"]),
                    "volume": float(c.get("lastTradedVolume", 0)),
                }
            except (KeyError, TypeError, ValueError):
                continue

        logger.info(f"  {epic} {cursor:%Y-%m-%d %H:%M} → {len(prices)} شمعة "
                    f"(المجموع {len(rows)})")
        cursor = chunk_end
        time.sleep(0.4)          # لطف مع الـ API

    return [rows[k] for k in sorted(rows)]


def to_csv(rows: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=["time", "open", "high", "low", "close", "volume"])
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue()


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"}


def ensure_branch(token: str, repo: str) -> bool:
    """ينشئ فرع البيانات من الفرع الافتراضي إن لم يكن موجوداً."""
    h = _headers(token)
    try:
        r = requests.get(f"{API}/repos/{repo}/git/ref/heads/{BRANCH}", headers=h, timeout=20)
        if r.ok:
            return True
        base = requests.get(f"{API}/repos/{repo}", headers=h, timeout=20)
        base.raise_for_status()
        default = base.json().get("default_branch", "main")
        head = requests.get(f"{API}/repos/{repo}/git/ref/heads/{default}", headers=h, timeout=20)
        head.raise_for_status()
        sha = head.json()["object"]["sha"]
        c = requests.post(f"{API}/repos/{repo}/git/refs", headers=h, timeout=20,
                          json={"ref": f"refs/heads/{BRANCH}", "sha": sha})
        c.raise_for_status()
        logger.info(f"🌱 أُنشئ فرع {BRANCH}")
        return True
    except Exception as e:
        logger.error(f"❌ تعذّر تجهيز فرع {BRANCH}: {e}")
        return False


def push(path: str, content: str, token: str, repo: str, attempts: int = 5) -> bool:
    """
    يرفع الملف مع إعادة محاولة عند التعارض.

    الرفع المتتالي لعدة ملفات يحرّك رأس الفرع في كل مرة، فقد يصير الـ sha الذي
    قرأناه قديماً قبل أن نكتب. عند 409/422 نُحدّث المرجع ونعيد مع تراجع تصاعدي.
    """
    h = _headers(token)
    for i in range(attempts):
        try:
            g = requests.get(f"{API}/repos/{repo}/contents/{path}",
                             headers=h, params={"ref": BRANCH}, timeout=20)
            sha = g.json().get("sha") if g.ok else None
            body = {"message": f"بيانات تاريخية: {path}",
                    "content": base64.b64encode(content.encode()).decode(),
                    "branch": BRANCH}
            if sha:
                body["sha"] = sha
            r = requests.put(f"{API}/repos/{repo}/contents/{path}",
                             headers=h, json=body, timeout=60)
            if r.status_code in (409, 422):
                wait = 2 ** i
                logger.warning(f"↻ تعارض على {path} — إعادة بعد {wait}s "
                               f"(محاولة {i + 1}/{attempts})")
                time.sleep(wait)
                continue
            r.raise_for_status()
            return True
        except Exception as e:
            logger.error(f"❌ تعذّر رفع {path}: {e}")
            if i == attempts - 1:
                return False
            time.sleep(2 ** i)
    logger.error(f"❌ فشل رفع {path} بعد {attempts} محاولات")
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--resolution", default="MINUTE_5")
    args = ap.parse_args()

    end   = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    start = end - timedelta(days=args.days)
    logger.info(f"سحب {args.resolution} من {start:%Y-%m-%d} إلى {end:%Y-%m-%d}")

    client = CapitalClient()
    token  = (os.getenv("GITHUB_TOKEN", "") or "").strip()
    repo   = (os.getenv("GITHUB_REPOSITORY", "") or "").strip()

    if token and repo and not ensure_branch(token, repo):
        logger.error("❌ لا يمكن المتابعة بلا فرع بيانات")
        return 1

    ok = True
    for symbol, cfg in SYMBOLS.items():
        logger.info(f"── {symbol} ({cfg['epic']})")
        rows = fetch_range(client, cfg["epic"], args.resolution, start, end)
        if not rows:
            logger.error(f"❌ {symbol}: لا بيانات")
            ok = False
            continue
        logger.info(f"✅ {symbol}: {len(rows)} شمعة | "
                    f"{rows[0]['time']} → {rows[-1]['time']}")
        path = f"data/{symbol}_{args.resolution}.csv"
        if token and repo:
            if push(path, to_csv(rows), token, repo):
                logger.info(f"📤 رُفع {path}")
            else:
                ok = False
        else:
            logger.warning("⚠️ لا GITHUB_TOKEN — لن يُرفع الناتج")

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
