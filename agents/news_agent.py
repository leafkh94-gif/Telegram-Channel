"""
NewsAgent — blocks trades around scheduled economic events and breaking headlines.

Two data sources:
  1. ForexFactory calendar  — scheduled high-impact USD events (cached 4h)
  2. RSS headlines          — breaking news from CNBC and MarketWatch (cached 10 min)

Verdict logic:
  BLOCK — a hard-stop breaking headline found (crash, halt, emergency, etc.)
  HOLD  — high-impact event within ±30 min, or instrument-relevant headline < 20 min old
  GO    — no news interference detected

Fails open on every network error so a dead RSS feed never silences signals.
"""
import logging
import time
import xml.etree.ElementTree as ET
import urllib.request
from email.utils import parsedate_to_datetime
from typing import Optional

from agents.base import AgentVerdict
from strategy.news_filter import high_impact_news_within

logger = logging.getLogger(__name__)

# ── RSS feeds (tried in order; first successful fetch wins) ───────────────────
_RSS_FEEDS = [
    "https://www.cnbc.com/id/100003114/device/rss/rss.html",
    "https://www.marketwatch.com/rss/topstories",
]
_RSS_CACHE_TTL_S = 10 * 60   # refresh every 10 min
_RSS_TIMEOUT_S   = 8

# ── Keyword sets ──────────────────────────────────────────────────────────────

# These trigger BLOCK — extreme market-disruption language
_BLOCK_KEYWORDS = [
    "market crash", "trading halt", "circuit breaker", "flash crash",
    "emergency rate", "emergency cut", "black monday", "financial crisis",
    "market collapse", "stock market crash",
]

# Instrument → relevant keywords that trigger HOLD if headline < 20 min old
_INSTRUMENT_KEYWORDS: dict[str, list[str]] = {
    "GOLD":  ["gold price", "gold falls", "gold rallies", "gold drops",
              "xau", "bullion", "precious metal", "safe haven demand"],
    "US500": ["s&p 500", "s&p500", "spx", "stock market", "wall street",
              "equities fall", "equities rally", "dow jones", "nasdaq"],
    "US100": ["nasdaq", "tech stocks", "ndx", "technology stocks"],
    "US30":  ["dow jones", "dow falls", "dow rallies", "industrial average"],
}
# Keywords that may affect all instruments
_UNIVERSAL_KEYWORDS = [
    "federal reserve", "fed raises", "fed cuts", "rate hike", "rate cut",
    "fomc", "jerome powell", "inflation shock", "cpi surprise", "jobs report",
    "nfp", "payrolls", "recession fears", "banking crisis",
]

# ── RSS cache ─────────────────────────────────────────────────────────────────
_rss_cache: list[dict] = []       # [{"title": str, "pub_ts": float}]
_rss_fetched_at: float = 0.0


def _fetch_rss() -> list[dict]:
    """Try each feed in order; return parsed items on first success."""
    for url in _RSS_FEEDS:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=_RSS_TIMEOUT_S) as resp:
                tree = ET.parse(resp)
            items = []
            for item in tree.getroot().iter("item"):
                title    = (item.findtext("title") or "").lower().strip()
                pub_date = item.findtext("pubDate") or ""
                try:
                    pub_ts = parsedate_to_datetime(pub_date).timestamp()
                except Exception:
                    pub_ts = time.time()   # fallback: treat as just-published
                items.append({"title": title, "pub_ts": pub_ts})
            logger.debug("NewsAgent: fetched %d RSS items from %s", len(items), url)
            return items
        except Exception as exc:
            logger.debug("NewsAgent: RSS fetch failed for %s: %s", url, exc)
    logger.warning("NewsAgent: all RSS feeds unavailable — fail open")
    return []


def _get_rss() -> list[dict]:
    global _rss_cache, _rss_fetched_at
    if time.time() - _rss_fetched_at > _RSS_CACHE_TTL_S:
        items = _fetch_rss()
        if items:
            _rss_cache = items
            _rss_fetched_at = time.time()
    return _rss_cache


class NewsAgent:
    def __init__(self, news_window_min: int = 30):
        self._news_window_min = news_window_min

    def evaluate(self, epic: str) -> AgentVerdict:
        # ── Gate 1: ForexFactory scheduled events ─────────────────────────────
        if high_impact_news_within(self._news_window_min):
            return AgentVerdict(
                agent="news", verdict="HOLD",
                confidence=0.95,
                reason=f"high-impact USD event within ±{self._news_window_min} min",
            )

        # ── Gate 2: RSS breaking headlines ────────────────────────────────────
        headlines = _get_rss()
        now = time.time()

        for item in headlines:
            title = item["title"]
            age_min = (now - item["pub_ts"]) / 60

            # BLOCK on extreme market disruption regardless of age (last 2h)
            if age_min <= 120:
                for kw in _BLOCK_KEYWORDS:
                    if kw in title:
                        return AgentVerdict(
                            agent="news", verdict="BLOCK",
                            confidence=0.90,
                            reason=f"breaking: '{kw}' in headline (age {age_min:.0f} min)",
                        )

            # HOLD on instrument-relevant news < 20 min old
            if age_min <= 20:
                relevant = (
                    _INSTRUMENT_KEYWORDS.get(epic, []) + _UNIVERSAL_KEYWORDS
                )
                for kw in relevant:
                    if kw in title:
                        return AgentVerdict(
                            agent="news", verdict="HOLD",
                            confidence=0.80,
                            reason=f"recent headline: '{kw}' (age {age_min:.0f} min)",
                        )

        return AgentVerdict(
            agent="news", verdict="GO",
            confidence=0.95,
            reason="no high-impact events or relevant headlines",
        )
