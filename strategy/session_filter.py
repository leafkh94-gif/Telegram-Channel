"""
Session filter — returns True when current UTC time is inside a major
trading session where liquidity and follow-through are highest.

London session : 07:00 – 12:00 UTC
New York session: 13:00 – 17:00 UTC
Weekend        : always False
"""
from __future__ import annotations

from datetime import datetime, time, timezone


def is_london_or_ny_session(now_utc: datetime | None = None) -> bool:
    """
    Return True if now_utc falls inside the London or New York session window.
    Returns False on Saturday and Sunday.
    """
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)

    weekday = now_utc.weekday()   # 0=Mon … 6=Sun
    if weekday >= 5:
        return False

    t = now_utc.time().replace(tzinfo=None)
    london = time(7, 0) <= t < time(12, 0)
    ny     = time(13, 0) <= t < time(17, 0)
    return london or ny


def session_label(now_utc: datetime | None = None) -> str:
    """Human-readable label for the current session."""
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    if now_utc.weekday() >= 5:
        return "Weekend"
    t = now_utc.time().replace(tzinfo=None)
    if time(7, 0) <= t < time(12, 0):
        return "London session"
    if time(13, 0) <= t < time(17, 0):
        return "New York session"
    return "Outside major sessions"
