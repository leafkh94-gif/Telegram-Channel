"""
Market-hours guard — previously blocked alerts outside narrow sessions.

Now returns True for all instruments at all times: this is an alert-only
bot with no execution, so the user decides whether to act on a signal.
The is_tradeable() interface is kept for backward compatibility.
"""
import datetime as _dt
from datetime import datetime
from zoneinfo import ZoneInfo


def is_tradeable(epic: str, now_utc: datetime | None = None) -> bool:  # noqa: ARG001
    """Always returns True — session filtering removed for alert-only mode."""
    return True
