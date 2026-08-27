"""
risk_manager.py — إدارة المخاطرة وحجم الصفقة
"""

import json
import logging
from datetime import date
from pathlib import Path
from config import (
    RISK_PER_TRADE_PCT, MAX_OPEN_TRADES, MAX_TRADES_PER_DAY,
    MAX_DAILY_LOSS_PCT, MAX_CONSEC_LOSSES, SYMBOLS
)

logger = logging.getLogger(__name__)
STATE_FILE = Path("risk_state.json")


def _load() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"date": str(date.today()), "trades_today": 0,
            "daily_pnl": 0.0, "consec_losses": 0, "halted": False, "halt_reason": ""}


def _save(s: dict):
    STATE_FILE.write_text(json.dumps(s, indent=2))


def _reset_if_new_day(s: dict) -> dict:
    if s["date"] != str(date.today()):
        s.update({"date": str(date.today()), "trades_today": 0,
                  "daily_pnl": 0.0, "consec_losses": 0, "halted": False, "halt_reason": ""})
        _save(s)
    return s


def can_trade(balance: float, open_count: int) -> tuple[bool, str]:
    s = _reset_if_new_day(_load())
    if s["halted"]:
        return False, f"⛔ {s['halt_reason']}"
    if open_count >= MAX_OPEN_TRADES:
        return False, f"⛔ {open_count} صفقات مفتوحة (حد {MAX_OPEN_TRADES})"
    if s["trades_today"] >= MAX_TRADES_PER_DAY:
        return False, f"⛔ حد يومي {MAX_TRADES_PER_DAY}"
    if s["daily_pnl"] < 0 and abs(s["daily_pnl"]) >= balance * MAX_DAILY_LOSS_PCT:
        s["halted"] = True; s["halt_reason"] = "خسارة يومية 2%"; _save(s)
        return False, "⛔ خسارة يومية 2%"
    if s["consec_losses"] >= MAX_CONSEC_LOSSES:
        s["halted"] = True; s["halt_reason"] = "3 خسائر متتالية"; _save(s)
        return False, "⛔ 3 خسائر متتالية"
    return True, "✅"


def calculate_lot_size(symbol: str, balance: float, entry: float, sl: float) -> float:
    cfg = SYMBOLS.get(symbol, {})
    pip_size = cfg.get("pip_size", 1.0)
    risk_amount = balance * RISK_PER_TRADE_PCT
    sl_points = abs(entry - sl) / pip_size
    if sl_points == 0:
        return 0.01
    lot = risk_amount / sl_points
    return max(0.01, round(lot, 2))


def record_trade_open():
    s = _reset_if_new_day(_load())
    s["trades_today"] += 1
    _save(s)


def get_daily_stats() -> dict:
    return _reset_if_new_day(_load())


def resume_trading():
    s = _load()
    s["halted"] = False; s["halt_reason"] = ""; s["consec_losses"] = 0
    _save(s)
