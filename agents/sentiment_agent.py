"""
SentimentAgent — social media and news sentiment analysis.

Implements the third data pillar described in the trading system:
"sentiment analysis from social media and news" (Sentiment Analysis).

Data sources (all free, no auth):
  Reddit — r/wallstreetbets, r/investing, r/stocks, r/Forex, r/Gold (RSS)
  News   — Yahoo Finance, MarketWatch (reuses existing RSS infrastructure)

Scoring:
  Each post/headline from the last SENTIMENT_WINDOW_H hours is scored:
    +1  if it contains bullish keywords
    -1  if it contains bearish keywords
    0   if neutral or no instrument mention
  Net score = sum of all per-post scores for the instrument.

Verdict:
  GO    — sentiment neutral or aligned with the proposed trade direction
  HOLD  — sentiment strongly contradicts direction (net ≤ BEAR_THRESHOLD
           for a BUY, or net ≥ BULL_THRESHOLD for a SELL)

The SentimentAgent never BLOCKs — sentiment is a soft confirmation layer,
not a hard gate. A single HOLD from this agent will stop the trade but the
reason is always surfaced in the Telegram alert.

Fails open on all network errors so a dead feed never silences signals.
"""
import logging
import time
import xml.etree.ElementTree as ET
import urllib.request
from email.utils import parsedate_to_datetime
from typing import Optional

from agents.base import AgentVerdict

logger = logging.getLogger(__name__)

# ── Feed lists ────────────────────────────────────────────────────────────────

_REDDIT_FEEDS = [
    "https://www.reddit.com/r/wallstreetbets/.rss",
    "https://www.reddit.com/r/investing/.rss",
    "https://www.reddit.com/r/stocks/.rss",
    "https://www.reddit.com/r/Forex/.rss",
    "https://www.reddit.com/r/Gold/.rss",
]

_NEWS_FEEDS = [
    "https://finance.yahoo.com/rss/topfinstories",
    "https://www.marketwatch.com/rss/topstories",
]

_SENTIMENT_WINDOW_H = 3    # hours of posts to consider
_CACHE_TTL_S        = 15 * 60   # refresh sentiment every 15 min
_TIMEOUT_S          = 8
_BEAR_THRESHOLD     = -2   # net ≤ this blocks a BUY signal
_BULL_THRESHOLD     = 2    # net ≥ this blocks a SELL signal

# ── Sentiment keywords ────────────────────────────────────────────────────────

_BULLISH = frozenset({
    "bullish", "buy signal", "go long", "long position",
    "bull", "bulls", "bull run", "bull market",
    "rally", "rallies", "breakout", "surge", "surges",
    "pump", "recovery", "recovers", "uptrend", "bounced",
    "bounce", "support holds", "oversold", "rate cut",
    "fed pivot", "soft landing", "strong earnings",
    "beat expectations", "higher high",
})

_BEARISH = frozenset({
    "bearish", "sell signal", "go short", "short position",
    "bear", "bears", "bear run", "bear market",
    "crash", "crashes", "drop", "drops", "plunge", "plunges",
    "breakdown", "dump", "downtrend", "resistance holds",
    "overbought", "rate hike", "hawkish", "recession",
    "hard landing", "weak earnings", "miss expectations",
    "lower low", "default", "inflation surge",
})

# Per-instrument trigger words (post must mention these to be counted)
_INSTRUMENT_TERMS: dict[str, list[str]] = {
    "GOLD":  ["gold", "xau", "bullion", "precious metal", "safe haven",
              "xauusd", "gold price"],
    "US500": ["s&p", "spy", "spx", "s&p 500", "s&p500", "sp500",
              "stock market", "wall street", "equities"],
    "US100": ["nasdaq", "qqq", "ndx", "nasdaq 100", "tech stock",
              "technology sector"],
    "US30":  ["dow", "djia", "dow jones", "industrial average"],
}

# ── Cache ─────────────────────────────────────────────────────────────────────

_cache: list[dict] = []        # [{"title": str, "pub_ts": float, "source": str}]
_cache_at: float   = 0.0


def _fetch_feed(url: str, source: str) -> list[dict]:
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "Mozilla/5.0 (compatible; TradingBot/1.0)"}
        )
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
            tree = ET.parse(resp)
        items = []
        for item in tree.getroot().iter("item"):
            title = (item.findtext("title") or "").lower().strip()
            pub   = item.findtext("pubDate") or ""
            try:
                ts = parsedate_to_datetime(pub).timestamp()
            except Exception:
                ts = time.time()
            items.append({"title": title, "pub_ts": ts, "source": source})
        return items
    except Exception as exc:
        logger.debug("SentimentAgent: feed %s failed: %s", url, exc)
        return []


def _refresh_cache() -> None:
    global _cache, _cache_at
    posts: list[dict] = []
    for url in _REDDIT_FEEDS:
        posts.extend(_fetch_feed(url, "reddit"))
    for url in _NEWS_FEEDS:
        posts.extend(_fetch_feed(url, "news"))
    if posts:
        _cache    = posts
        _cache_at = time.time()
        logger.debug("SentimentAgent: cache refreshed — %d posts", len(posts))
    else:
        logger.warning("SentimentAgent: all feeds unavailable — using stale cache")


def _get_posts() -> list[dict]:
    if time.time() - _cache_at > _CACHE_TTL_S:
        _refresh_cache()
    return _cache


# ── Scoring ───────────────────────────────────────────────────────────────────

def _score_post(title: str) -> int:
    """Return +1 (bullish), -1 (bearish), or 0 (neutral) for a single post."""
    bull = sum(1 for kw in _BULLISH if kw in title)
    bear = sum(1 for kw in _BEARISH if kw in title)
    if bull > bear:
        return 1
    if bear > bull:
        return -1
    return 0


def _instrument_sentiment(epic: str, window_h: float = _SENTIMENT_WINDOW_H) -> tuple[int, int]:
    """
    Scan recent posts for the instrument and return (net_score, post_count).
    Only posts that mention the instrument and are within window_h hours
    are included.
    """
    posts      = _get_posts()
    terms      = _INSTRUMENT_TERMS.get(epic, [])
    cutoff     = time.time() - window_h * 3600
    net        = 0
    count      = 0

    for post in posts:
        if post["pub_ts"] < cutoff:
            continue
        title = post["title"]
        if not any(term in title for term in terms):
            continue
        score = _score_post(title)
        net  += score
        count += 1

    return net, count


# ── Agent ─────────────────────────────────────────────────────────────────────

class SentimentAgent:
    """
    Analyses social media and financial news sentiment for the instrument
    and validates it against the proposed trade direction.
    """

    def __init__(
        self,
        bear_threshold: int = _BEAR_THRESHOLD,
        bull_threshold: int = _BULL_THRESHOLD,
    ):
        self._bear_threshold = bear_threshold
        self._bull_threshold = bull_threshold

    def evaluate(self, epic: str, direction: str) -> AgentVerdict:
        net, count = _instrument_sentiment(epic)

        if count == 0:
            return AgentVerdict(
                agent="sentiment", verdict="GO",
                confidence=0.60,
                reason=f"no recent {epic} mentions in social/news feeds",
            )

        sentiment_label = (
            "bullish" if net >= self._bull_threshold
            else "bearish" if net <= self._bear_threshold
            else "neutral"
        )

        # Aligned or neutral → GO
        if sentiment_label == "neutral":
            return AgentVerdict(
                agent="sentiment", verdict="GO",
                confidence=0.70,
                reason=f"neutral sentiment (score {net:+d} from {count} posts)",
            )

        aligned = (
            (direction == "buy"  and sentiment_label == "bullish") or
            (direction == "sell" and sentiment_label == "bearish")
        )

        if aligned:
            conf = min(0.90, 0.70 + abs(net) * 0.05)
            return AgentVerdict(
                agent="sentiment", verdict="GO",
                confidence=round(conf, 2),
                reason=f"{sentiment_label} sentiment aligns with {direction.upper()} "
                       f"(score {net:+d}, {count} posts)",
            )

        # Contradicts direction → HOLD
        return AgentVerdict(
            agent="sentiment", verdict="HOLD",
            confidence=0.75,
            reason=f"{sentiment_label} sentiment contradicts {direction.upper()} "
                   f"(score {net:+d}, {count} posts)",
        )
