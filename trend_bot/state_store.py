"""
state_store.py — حفظ سجل الإشارات خارج حاوية التشغيل

المشكلة التي يحلّها:
سجل الإشارات (signals_log.csv) كان يُحفظ عبر actions/cache، وخطوة الحفظ تقع في
نهاية الـ job. لكن التشغيل يُلغى قبل أن تصلها، من مصدرين:

  1. السلسلة الذاتية: بعد ~5س40د يُطلق التشغيل التالي، و cancel-in-progress
     يلغي الحالي فوراً — فتُتخطّى خطوة الحفظ (تحقّقنا: Post cache = skipped).
  2. الـ cron الدوري: يُطلق تشغيلاً جديداً يقتل تشغيلاً **سليماً** في منتصف
     عمله، فيضيع كل ما جُمع منذ بدايته.

النتيجة كانت أن كل تشغيل يبدأ بسجل فارغ، فالتقرير اليومي لا يرى إلا جزءاً من
اليوم — وهذا يُبطل الغرض منه.

الحل هنا لا يعتمد على انتهاء الـ job إطلاقاً: نرفع الملف إلى فرع `status` عبر
GitHub Contents API بشكل دوري أثناء التشغيل، ونسحبه عند الإقلاع. أي إلغاء، في
أي لحظة، لا يكلّفنا أكثر من الدقائق التي مضت منذ آخر رفع.

كل الأخطاء هنا غير قاتلة: فشل المزامنة يجب ألا يوقف البوت عن إرسال الإشارات.
"""

import base64
import logging
import os
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger(__name__)

BRANCH   = "status"
REMOTE   = "signals_log.csv"
API      = "https://api.github.com"

_TOKEN   = (os.getenv("GITHUB_TOKEN", "") or "").strip()
_REPO    = (os.getenv("GITHUB_REPOSITORY", "") or "").strip()   # "owner/repo"
_sha: Optional[str] = None      # sha الحالي للملف البعيد، لازم للتحديث


def enabled() -> bool:
    return bool(_TOKEN and _REPO)


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def pull(local_path: Path) -> bool:
    """يسحب السجل المحفوظ إلى الملف المحلي. يُستدعى مرة عند الإقلاع."""
    global _sha
    if not enabled():
        logger.warning("⚠️ مزامنة السجل معطّلة (لا GITHUB_TOKEN) — السجل محلي فقط")
        return False
    try:
        r = requests.get(
            f"{API}/repos/{_REPO}/contents/{REMOTE}",
            headers=_headers(), params={"ref": BRANCH}, timeout=15,
        )
        if r.status_code == 404:
            logger.info("📥 لا سجل محفوظ بعد — نبدأ من الصفر")
            _sha = None
            return True
        r.raise_for_status()
        data = r.json()
        _sha = data.get("sha")
        content = base64.b64decode(data.get("content", "")).decode("utf-8")
        local_path.write_text(content, encoding="utf-8")
        rows = max(0, content.count("\n") - 1)
        logger.info(f"📥 استُرجع سجل الإشارات من فرع {BRANCH} ({rows} صفاً)")
        return True
    except Exception as e:
        logger.error(f"❌ تعذّر سحب السجل (البوت يكمل عادي): {e}")
        return False


def push(local_path: Path, message: str = "تحديث سجل الإشارات") -> bool:
    """يرفع الملف المحلي. يُستدعى دورياً — لا ينتظر نهاية التشغيل."""
    global _sha
    if not enabled() or not local_path.exists():
        return False
    try:
        content = local_path.read_text(encoding="utf-8")
        payload = {
            "message": message,
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
            "branch": BRANCH,
        }
        if _sha:
            payload["sha"] = _sha

        r = requests.put(
            f"{API}/repos/{_REPO}/contents/{REMOTE}",
            headers=_headers(), json=payload, timeout=20,
        )
        # 409/422 = تعارض sha (تشغيل آخر رفع قبلنا): نُحدّث الـ sha ونعيد مرة واحدة
        if r.status_code in (409, 422):
            logger.info("↻ تعارض في الرفع — نُحدّث المرجع ونعيد المحاولة")
            g = requests.get(
                f"{API}/repos/{_REPO}/contents/{REMOTE}",
                headers=_headers(), params={"ref": BRANCH}, timeout=15,
            )
            if g.ok:
                _sha = g.json().get("sha")
                payload["sha"] = _sha
                r = requests.put(
                    f"{API}/repos/{_REPO}/contents/{REMOTE}",
                    headers=_headers(), json=payload, timeout=20,
                )
        r.raise_for_status()
        _sha = (r.json().get("content") or {}).get("sha", _sha)
        return True
    except Exception as e:
        logger.error(f"❌ تعذّر رفع السجل (البوت يكمل عادي): {e}")
        return False
