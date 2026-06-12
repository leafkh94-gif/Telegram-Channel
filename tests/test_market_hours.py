"""Tests for strategy/market_hours.py — always-on alert mode."""
import datetime
from zoneinfo import ZoneInfo

from strategy.market_hours import is_tradeable

_ET  = ZoneInfo("America/New_York")


def _et(year, month, day, hour, minute=0) -> datetime.datetime:
    return datetime.datetime(year, month, day, hour, minute, tzinfo=_ET).astimezone(
        ZoneInfo("UTC")
    )


# All instruments are tradeable at all times in alert-only mode.

def test_equity_during_session():
    assert is_tradeable("US500", _et(2026, 3, 3, 10, 0)) is True

def test_equity_after_old_cutoff():
    assert is_tradeable("US500", _et(2026, 3, 3, 15, 31)) is True

def test_equity_before_old_open():
    assert is_tradeable("US500", _et(2026, 3, 3, 8, 0)) is True

def test_equity_weekend():
    assert is_tradeable("US500", _et(2026, 3, 7, 11, 0)) is True

def test_equity_holiday():
    assert is_tradeable("US500", _et(2026, 1, 1, 11, 0)) is True

def test_equity_black_friday():
    assert is_tradeable("US500", _et(2026, 11, 27, 14, 0)) is True

def test_gold_always_on():
    assert is_tradeable("GOLD", _et(2026, 3, 7, 12, 0)) is True

def test_gold_sunday():
    assert is_tradeable("GOLD", _et(2026, 3, 8, 17, 0)) is True

def test_gold_maintenance_break():
    assert is_tradeable("GOLD", _et(2026, 3, 4, 17, 30)) is True

def test_gold_holiday():
    assert is_tradeable("GOLD", _et(2026, 11, 26, 11, 0)) is True
