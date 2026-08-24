"""
capital_client.py — واجهة Capital.com API
تنبيه: الـ API يُعيد الشموع من الأحدث للأقدم → نعكسها دائماً
"""

import requests
import logging
from typing import Optional
from config import (
    CAPITAL_API_KEY, CAPITAL_PASSWORD, CAPITAL_IDENTIFIER,
    CAPITAL_BASE_URL, CAPITAL_DEMO
)

logger = logging.getLogger(__name__)


class CapitalClient:

    def __init__(self):
        self.session_token = None
        self.cst_token = None
        self.headers = {"Content-Type": "application/json", "X-CAP-API-KEY": CAPITAL_API_KEY}
        self._authenticate()

    # ─── المصادقة ──────────────────────────────────────────────────────────────
    def _authenticate(self):
        url = f"{CAPITAL_BASE_URL}/session"
        payload = {
            "identifier": CAPITAL_IDENTIFIER,
            "password": CAPITAL_PASSWORD,
            "encryptedPassword": False
        }
        try:
            r = requests.post(url, json=payload, headers=self.headers, timeout=10)
            r.raise_for_status()
            self.cst_token     = r.headers.get("CST")
            self.session_token = r.headers.get("X-SECURITY-TOKEN")
            self.headers["CST"]               = self.cst_token
            self.headers["X-SECURITY-TOKEN"]  = self.session_token
            logger.info("✅ تسجيل دخول Capital.com ناجح")
        except Exception as e:
            logger.error(f"❌ فشل تسجيل الدخول: {e}")
            raise

    def _ensure_auth(self):
        """إعادة المصادقة إذا انتهت الجلسة"""
        if not self.cst_token:
            self._authenticate()

    # ─── جلب الشموع ────────────────────────────────────────────────────────────
    def get_candles(self, epic: str, resolution: str, count: int = 50) -> list[dict]:
        """
        جلب الشموع مرتبةً من الأقدم للأحدث حسب الوقت الحقيقي للشمعة.

        ⚠️ إصلاح حرج: الكود السابق كان يفترض أن Capital.com يُعيد الأحدث أولاً
        ويقلب القائمة دائماً (reversed). عملياً الـ API لا يضمن هذا الترتيب —
        وحين يُعيد الأقدم أولاً كان القلب يجعل candles[-1] هي **أقدم** شمعة
        (مثال حقيقي: آخر شمعة 1H = 29996 بينما السعر الحي 29050 — فرق 950 نقطة
        ثابت لا يتحرك). النتيجة: EMA والقمم/القيعان والـ Sweep كلها تُحسب على
        سلسلة مقلوبة زمنياً → اتجاه بلا معنى وصفر إشارات.

        الحل: لا نفترض أي ترتيب — نرتّب فعلياً حسب snapshotTimeUTC تصاعدياً.

        resolution: MINUTE_5 | MINUTE_15 | HOUR | HOUR_4 | DAY
        """
        self._ensure_auth()
        url = f"{CAPITAL_BASE_URL}/prices/{epic}"
        params = {"resolution": resolution, "max": count}
        try:
            r = requests.get(url, headers=self.headers, params=params, timeout=10)
            r.raise_for_status()
            raw = r.json().get("prices", [])

            # تحويل لصيغة موحدة (نتخطى أي شمعة ناقصة بدل أن نُسقط الطلب كله)
            result = []
            for c in raw:
                try:
                    result.append({
                        "time":  c.get("snapshotTimeUTC") or c.get("snapshotTime"),
                        "open":  float(c["openPrice"]["bid"]),
                        "high":  float(c["highPrice"]["bid"]),
                        "low":   float(c["lowPrice"]["bid"]),
                        "close": float(c["closePrice"]["bid"]),
                        "volume": float(c.get("lastTradedVolume", 0))
                    })
                except (KeyError, TypeError, ValueError):
                    continue

            # ✅ الترتيب الحقيقي: الأقدم أولاً حسب وقت الشمعة، مع إزالة التكرار
            result.sort(key=lambda x: x["time"] or "")
            deduped, seen = [], set()
            for c in result:
                if c["time"] in seen:
                    continue
                seen.add(c["time"])
                deduped.append(c)
            result = deduped

            if result:
                logger.info(
                    f"CANDLES {epic} {resolution} | عدد={len(result)} | "
                    f"أقدم={result[0]['time']} ({result[0]['close']:.2f}) | "
                    f"أحدث={result[-1]['time']} ({result[-1]['close']:.2f})"
                )
            return result
        except Exception as e:
            logger.error(f"❌ خطأ جلب شموع {epic} {resolution}: {e}")
            return []

    # ─── معلومات الحساب ────────────────────────────────────────────────────────
    def get_account_balance(self) -> float:
        self._ensure_auth()
        url = f"{CAPITAL_BASE_URL}/accounts"
        try:
            r = requests.get(url, headers=self.headers, timeout=10)
            r.raise_for_status()
            accounts = r.json().get("accounts", [])
            if accounts:
                return float(accounts[0].get("balance", {}).get("balance", 0))
        except Exception as e:
            logger.error(f"❌ خطأ جلب الرصيد: {e}")
        return 0.0

    def get_open_positions(self) -> list[dict]:
        self._ensure_auth()
        url = f"{CAPITAL_BASE_URL}/positions"
        try:
            r = requests.get(url, headers=self.headers, timeout=10)
            r.raise_for_status()
            return r.json().get("positions", [])
        except Exception as e:
            logger.error(f"❌ خطأ جلب الصفقات المفتوحة: {e}")
        return []

    def get_current_price(self, epic: str) -> Optional[dict]:
        self._ensure_auth()
        url = f"{CAPITAL_BASE_URL}/markets/{epic}"
        try:
            r = requests.get(url, headers=self.headers, timeout=10)
            r.raise_for_status()
            data = r.json()
            snap = data.get("snapshot", {})
            return {
                "bid": float(snap.get("bid", 0)),
                "ask": float(snap.get("offer", 0)),
                "mid": (float(snap.get("bid", 0)) + float(snap.get("offer", 0))) / 2
            }
        except Exception as e:
            logger.error(f"❌ خطأ جلب السعر الحالي {epic}: {e}")
        return None

    # ─── فتح صفقة ──────────────────────────────────────────────────────────────
    def place_order(self, epic: str, direction: str, size: float,
                    stop_level: float, profit_level: float) -> Optional[dict]:
        """
        direction: "BUY" أو "SELL"
        size: حجم اللوت
        """
        self._ensure_auth()
        url = f"{CAPITAL_BASE_URL}/positions"
        payload = {
            "epic":          epic,
            "direction":     direction,
            "size":          size,
            "guaranteedStop": False,
            "stopLevel":     stop_level,
            "profitLevel":   profit_level,
        }
        try:
            r = requests.post(url, json=payload, headers=self.headers, timeout=10)
            r.raise_for_status()
            data = r.json()
            logger.info(f"✅ صفقة مفتوحة: {direction} {epic} @ size={size}")
            return data
        except Exception as e:
            logger.error(f"❌ خطأ فتح صفقة {epic}: {e}")
        return None

    def close_position(self, deal_id: str) -> bool:
        self._ensure_auth()
        url = f"{CAPITAL_BASE_URL}/positions/{deal_id}"
        try:
            r = requests.delete(url, headers=self.headers, timeout=10)
            r.raise_for_status()
            logger.info(f"✅ صفقة مغلقة: {deal_id}")
            return True
        except Exception as e:
            logger.error(f"❌ خطأ إغلاق صفقة {deal_id}: {e}")
        return False
