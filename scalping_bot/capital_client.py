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
        جلب الشموع وعكس ترتيبها من الأقدم للأحدث.
        Capital.com يُعيد الأحدث أولاً — نعكس دائماً.

        resolution: MINUTE_5 | MINUTE_15 | HOUR | HOUR_4 | DAY
        """
        self._ensure_auth()
        url = f"{CAPITAL_BASE_URL}/prices/{epic}"
        params = {"resolution": resolution, "max": count}
        try:
            r = requests.get(url, headers=self.headers, params=params, timeout=10)
            r.raise_for_status()
            raw = r.json().get("prices", [])

            # ⚠️ عكس الترتيب: الأقدم أولاً
            candles = list(reversed(raw))

            # تحويل لصيغة موحدة
            result = []
            for c in candles:
                result.append({
                    "time":  c.get("snapshotTimeUTC"),
                    "open":  float(c["openPrice"]["bid"]),
                    "high":  float(c["highPrice"]["bid"]),
                    "low":   float(c["lowPrice"]["bid"]),
                    "close": float(c["closePrice"]["bid"]),
                    "volume": float(c.get("lastTradedVolume", 0))
                })
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
