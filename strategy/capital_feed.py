"""
Capital.com live price feed.

Fetches H1, H4, and D1 candles for any instrument via the Capital.com REST API.
Credentials are read from environment variables — never hard-coded.

A single shared session is created on first use and reused across all instruments
to avoid rate-limiting Capital.com with multiple simultaneous logins.

Required .env keys:
  CAPITAL_API_KEY       — API key from My Account → API integrations
  CAPITAL_IDENTIFIER    — login email address
  CAPITAL_PASSWORD      — account password

Optional .env keys:
  CAPITAL_DEMO          — "true" to use the demo environment (default: live)
"""
import logging
import os
import threading
import time

import requests as _req

from strategy.base import Candle, MultiTimeframeCandles, TF_D1, TF_H1, TF_H4
from strategy.feed import PriceFeed

logger = logging.getLogger(__name__)

_DEMO_BASE     = "https://demo-api-capital.backend-capital.com/api/v1"
_LIVE_BASE     = "https://api-capital.backend-capital.com/api/v1"
_PING_INTERVAL = 8 * 60   # keepalive ping every 8 min (tokens expire at ~10 min)
_TIMEOUT       = 15


# ── Shared singleton session ──────────────────────────────────────────────────

class _SharedSession:
    """One login shared across all CapitalComFeed instances."""

    def __init__(self):
        self._base: str = ""
        self._cst: str = ""
        self._token: str = ""
        self._lock = threading.Lock()
        self._ping_started = False

    def init(self, base: str) -> None:
        """Authenticate once. Safe to call multiple times — only runs once."""
        with self._lock:
            if self._cst:
                return
            self._base = base
            self._login_locked()
            if not self._ping_started:
                t = threading.Thread(target=self._keepalive, daemon=True,
                                     name="capital-keepalive")
                t.start()
                self._ping_started = True
            logger.info("Capital.com shared session ready (%s)",
                        "demo" if "demo" in base else "live")

    def _login_locked(self) -> None:
        api_key    = os.getenv("CAPITAL_API_KEY", "").strip()
        identifier = os.getenv("CAPITAL_IDENTIFIER", "").strip()
        password   = os.getenv("CAPITAL_PASSWORD", "").strip()
        r = _req.post(
            f"{self._base}/session",
            headers={"X-CAP-API-KEY": api_key, "Content-Type": "application/json"},
            json={"identifier": identifier, "password": password,
                  "encryptedPassword": False},
            timeout=_TIMEOUT,
        )
        r.raise_for_status()
        self._cst   = r.headers["CST"]
        self._token = r.headers["X-SECURITY-TOKEN"]

    def _auth_headers(self) -> dict:
        with self._lock:
            return {
                "CST":              self._cst,
                "X-SECURITY-TOKEN": self._token,
                "Content-Type":     "application/json",
            }

    def get(self, path: str, params: dict | None = None) -> dict:
        r = _req.get(f"{self._base}{path}", headers=self._auth_headers(),
                     params=params, timeout=_TIMEOUT)
        if r.status_code == 401:
            logger.info("Capital.com session expired — re-authenticating")
            with self._lock:
                self._login_locked()
            r = _req.get(f"{self._base}{path}", headers=self._auth_headers(),
                         params=params, timeout=_TIMEOUT)
        r.raise_for_status()
        return r.json()

    def _keepalive(self) -> None:
        while True:
            time.sleep(_PING_INTERVAL)
            try:
                _req.get(f"{self._base}/ping", headers=self._auth_headers(),
                         timeout=_TIMEOUT)
            except Exception as exc:
                logger.warning("Capital.com keepalive failed: %s", exc)


_session = _SharedSession()


class CapitalComFeed(PriceFeed):
    """
    Fetches D1 + H4 + H1 candles for a given Capital.com epic.
    All instances share one authenticated session — only one login request
    is made at startup regardless of how many instruments are watched.
    """

    def __init__(self, epic: str, demo: bool | None = None):
        self._epic = epic
        use_demo   = demo if demo is not None else (
            os.getenv("CAPITAL_DEMO", "").lower() == "true"
        )
        base = _DEMO_BASE if use_demo else _LIVE_BASE
        _session.init(base)   # no-op after first call
        logger.info("CapitalComFeed ready: %s", epic)

    def get_candles(self) -> MultiTimeframeCandles:
        try:
            d1 = self._fetch("DAY",    300)
            h4 = self._fetch("HOUR_4", 300)
            h1 = self._fetch("HOUR",   800)
            logger.debug("CapitalComFeed %s: %d H1, %d H4, %d D1",
                         self._epic, len(h1), len(h4), len(d1))
            return {TF_D1: d1, TF_H4: h4, TF_H1: h1}
        except Exception as exc:
            logger.error("CapitalComFeed %s: fetch error: %s", self._epic, exc)
            return {TF_D1: [], TF_H4: [], TF_H1: []}

    def _fetch(self, resolution: str, count: int) -> list[Candle]:
        data = _session.get(f"/prices/{self._epic}",
                            params={"resolution": resolution, "max": count})
        return [self._to_candle(p) for p in data.get("prices", [])]

    @staticmethod
    def _to_candle(p: dict) -> Candle:
        def mid(side: dict) -> float:
            bid = side.get("bid") or 0.0
            ask = side.get("ask") or 0.0
            return (float(bid) + float(ask)) / 2.0 if bid and ask else float(bid or ask or 0.0)
        try:
            return Candle(
                timestamp=p["snapshotTime"],
                open=mid(p["openPrice"]),
                high=mid(p["highPrice"]),
                low=mid(p["lowPrice"]),
                close=mid(p["closePrice"]),
                volume=float(p.get("lastTradedVolume") or 0),
            )
        except (KeyError, TypeError, ValueError) as exc:
            logger.debug("CapitalComFeed: malformed candle skipped: %s", exc)
            return Candle("", 0.0, 0.0, 0.0, 0.0)
