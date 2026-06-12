"""
Capital.com live price feed.

Fetches H1, H4, and D1 candles for any instrument via the Capital.com REST API.
Credentials are read from environment variables — never hard-coded.

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

_DEMO_BASE = "https://demo-api-capital.backend-capital.com/api/v1"
_LIVE_BASE = "https://api-capital.backend-capital.com/api/v1"
_PING_INTERVAL = 8 * 60   # keepalive ping every 8 min (tokens expire at ~10 min)
_TIMEOUT       = 15


class CapitalComFeed(PriceFeed):
    """
    Fetches D1 + H4 + H1 candles for a given Capital.com epic (GOLD, US500 …).
    Handles session auth and auto-reauth on 401.
    Credentials are read from env vars — no secrets in constructor.
    """

    def __init__(self, epic: str, demo: bool | None = None):
        self._epic  = epic
        self._base  = _DEMO_BASE if self._is_demo(demo) else _LIVE_BASE
        self._cst   = ""
        self._security_token = ""
        self._lock  = threading.Lock()
        self._connect()

    # ── PriceFeed interface ───────────────────────────────────────────────────

    def get_candles(self) -> MultiTimeframeCandles:
        return {
            TF_D1: self._fetch("DAY",    300),
            TF_H4: self._fetch("HOUR_4", 300),
            TF_H1: self._fetch("HOUR",   800),
        }

    # ── Internal ──────────────────────────────────────────────────────────────

    @staticmethod
    def _is_demo(override: bool | None) -> bool:
        if override is not None:
            return override
        return os.getenv("CAPITAL_DEMO", "").lower() == "true"

    def _connect(self) -> None:
        self._login()
        t = threading.Thread(target=self._keepalive, daemon=True,
                             name=f"capital-ping-{self._epic}")
        t.start()
        logger.info("CapitalComFeed: connected (epic=%s, env=%s)",
                    self._epic, "demo" if self._base == _DEMO_BASE else "live")

    def _login(self) -> None:
        api_key    = os.getenv("CAPITAL_API_KEY", "").strip()
        identifier = os.getenv("CAPITAL_IDENTIFIER", "").strip()
        password   = os.getenv("CAPITAL_PASSWORD", "").strip()
        if not (api_key and identifier and password):
            raise EnvironmentError(
                "Capital.com feed requires CAPITAL_API_KEY, "
                "CAPITAL_IDENTIFIER, and CAPITAL_PASSWORD in .env"
            )
        r = _req.post(
            f"{self._base}/session",
            headers={"X-CAP-API-KEY": api_key, "Content-Type": "application/json"},
            json={"identifier": identifier, "password": password,
                  "encryptedPassword": False},
            timeout=_TIMEOUT,
        )
        r.raise_for_status()
        with self._lock:
            self._cst            = r.headers["CST"]
            self._security_token = r.headers["X-SECURITY-TOKEN"]

    def _keepalive(self) -> None:
        while True:
            time.sleep(_PING_INTERVAL)
            try:
                self._request("GET", "/ping")
            except Exception as exc:
                logger.warning("CapitalComFeed keepalive failed for %s: %s",
                               self._epic, exc)

    def _auth_headers(self) -> dict:
        with self._lock:
            return {
                "CST":              self._cst,
                "X-SECURITY-TOKEN": self._security_token,
                "Content-Type":     "application/json",
            }

    def _request(self, method: str, path: str, **kwargs) -> _req.Response:
        r = _req.request(method, f"{self._base}{path}",
                         headers=self._auth_headers(), timeout=_TIMEOUT, **kwargs)
        if r.status_code == 401:
            logger.info("CapitalComFeed: session expired — re-authenticating")
            self._login()
            r = _req.request(method, f"{self._base}{path}",
                             headers=self._auth_headers(), timeout=_TIMEOUT, **kwargs)
        r.raise_for_status()
        return r

    def _fetch(self, resolution: str, max_candles: int) -> list[Candle]:
        try:
            r = self._request("GET", f"/prices/{self._epic}",
                              params={"resolution": resolution, "max": max_candles})
            return self._parse(r.json())
        except Exception as exc:
            logger.error("CapitalComFeed fetch failed (%s %s): %s",
                         self._epic, resolution, exc)
            return []

    @staticmethod
    def _parse(data: dict) -> list[Candle]:
        candles = []
        for p in data.get("prices", []):
            try:
                def mid(side: dict) -> float:
                    bid = side.get("bid") or 0.0
                    ask = side.get("ask") or 0.0
                    return (float(bid) + float(ask)) / 2.0 if bid and ask else float(bid or ask or 0.0)
                candles.append(Candle(
                    timestamp=p["snapshotTime"],
                    open=mid(p["openPrice"]),
                    high=mid(p["highPrice"]),
                    low=mid(p["lowPrice"]),
                    close=mid(p["closePrice"]),
                    volume=float(p.get("lastTradedVolume") or 0),
                ))
            except (KeyError, TypeError, ValueError) as exc:
                logger.debug("CapitalComFeed: skipping malformed candle: %s", exc)
        return candles
