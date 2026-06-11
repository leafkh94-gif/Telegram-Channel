"""Tests for strategy/market_hours.py — holiday calendar and half-day handling."""
import datetime
import pytest
from zoneinfo import ZoneInfo

from strategy.market_hours import is_tradeable

_ET  = ZoneInfo("America/New_York")
_UTC = ZoneInfo("UTC")


def _et(year, month, day, hour, minute=0) -> datetime.datetime:
    """Build a UTC datetime corresponding to a given ET wall-clock time."""
    return datetime.datetime(year, month, day, hour, minute, tzinfo=_ET).astimezone(
        ZoneInfo("UTC")
    )


# ── Normal equity trading window ─────────────────────────────────────────────

def test_equity_open_during_session():
    # Tuesday 2026-03-03 at 10:00 ET — normal trading day
    assert is_tradeable("US500", _et(2026, 3, 3, 10, 0)) is True

def test_equity_exactly_at_open():
    assert is_tradeable("US100", _et(2026, 3, 3, 9, 30)) is True

def test_equity_cutoff_before_close():
    # 15:30 ET is the last valid minute (cutoff is inclusive)
    assert is_tradeable("US30", _et(2026, 3, 3, 15, 30)) is True

def test_equity_after_cutoff():
    # 15:31 ET → past the 30-min pre-close buffer
    assert is_tradeable("US500", _et(2026, 3, 3, 15, 31)) is False

def test_equity_before_open():
    assert is_tradeable("US500", _et(2026, 3, 3, 8, 0)) is False

def test_equity_weekend_saturday():
    # 2026-03-07 is a Saturday
    assert is_tradeable("US500", _et(2026, 3, 7, 11, 0)) is False

def test_equity_weekend_sunday():
    # 2026-03-08 is a Sunday
    assert is_tradeable("US100", _et(2026, 3, 8, 11, 0)) is False


# ── NYSE holiday full closures 2026 ──────────────────────────────────────────

@pytest.mark.parametrize("date_args", [
    (2026,  1,  1),   # New Year's Day
    (2026,  1, 19),   # MLK Day
    (2026,  2, 16),   # Presidents' Day
    (2026,  4,  3),   # Good Friday
    (2026,  5, 25),   # Memorial Day
    (2026,  6, 19),   # Juneteenth
    (2026,  7,  3),   # Independence Day (observed)
    (2026,  9,  7),   # Labor Day
    (2026, 11, 26),   # Thanksgiving
    (2026, 12, 25),   # Christmas
])
def test_equity_holiday_closed(date_args):
    y, m, d = date_args
    assert is_tradeable("US500", _et(y, m, d, 11, 0)) is False

def test_equity_day_after_holiday_open():
    # 2026-01-02 is a normal trading Friday
    assert is_tradeable("US500", _et(2026, 1, 2, 11, 0)) is True


# ── Half-day early closes 2026 ───────────────────────────────────────────────

def test_equity_black_friday_before_early_close():
    # 2026-11-27 Black Friday — 12:59 ET is still tradeable
    assert is_tradeable("US500", _et(2026, 11, 27, 12, 59)) is True

def test_equity_black_friday_at_early_close():
    # 13:00 ET on Black Friday — early close reached, not tradeable
    assert is_tradeable("US500", _et(2026, 11, 27, 13, 0)) is False

def test_equity_black_friday_after_early_close():
    assert is_tradeable("US100", _et(2026, 11, 27, 14, 0)) is False

def test_equity_christmas_eve_before_early_close():
    # 2026-12-24 Christmas Eve — 12:59 ET still open
    assert is_tradeable("US30", _et(2026, 12, 24, 12, 59)) is True

def test_equity_christmas_eve_at_early_close():
    assert is_tradeable("US500", _et(2026, 12, 24, 13, 0)) is False


# ── Holidays do NOT affect Gold (near-24h instrument) ────────────────────────

def test_gold_trades_on_holiday():
    # Thanksgiving is a NYSE holiday but Gold trades 24h
    assert is_tradeable("GOLD", _et(2026, 11, 26, 11, 0)) is True

def test_gold_trades_on_black_friday():
    assert is_tradeable("GOLD", _et(2026, 11, 27, 14, 0)) is True


# ── Gold session boundaries (unchanged) ──────────────────────────────────────

def test_gold_saturday_closed():
    # 2026-03-07 is Saturday
    assert is_tradeable("GOLD", _et(2026, 3, 7, 12, 0)) is False

def test_gold_sunday_before_open():
    # Sunday before 18:00 ET
    assert is_tradeable("GOLD", _et(2026, 3, 8, 17, 0)) is False

def test_gold_sunday_after_open():
    assert is_tradeable("GOLD", _et(2026, 3, 8, 18, 0)) is True

def test_gold_friday_session_close():
    # Friday at or after 17:00 ET → closed
    assert is_tradeable("GOLD", _et(2026, 3, 6, 17, 0)) is False

def test_gold_daily_maintenance_break():
    # 17:00-18:00 ET daily maintenance — use a Wednesday
    assert is_tradeable("GOLD", _et(2026, 3, 4, 17, 30)) is False

def test_gold_after_maintenance():
    assert is_tradeable("GOLD", _et(2026, 3, 4, 18, 0)) is True
