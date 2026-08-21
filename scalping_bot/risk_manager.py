"""
risk_manager.py — إدارة المخاطرة وحساب حجم الصفقة
"""

import json
import logging
from datetime import datetime, date, timezone
from pathlib import Path
from config import (
    RISK_PER_TRADE_PCT, MAX_OPEN_TRADES, MAX_TRADES_PER_DAY,
    MAX_DAILY_LOSS_PCT, MAX_CONSEC_LOSSES, SYMBOLS
)

logger = logging.getLogger(__name__)

STATE_FILE = Path("risk_state.json")


def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {
        "date":          str(date.today()),
        "trades_today":  0,
        "daily_pnl":     0.0,
        "consec_losses": 0,
        "halted":        False,
        "halt_reason":   "",
    }


def _save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def _reset_if_new_day(state: dict) -> dict:
    today = str(date.today())
    if state["date"] != today:
        state.update({
            "date":          today,
            "trades_today":  0,
            "daily_pnl":     0.0,
            "consec_losses": 0,
            "halted":        False,
            "halt_reason":   "",
        })
        _save_state(state)
    return state


# ─── فحص هل مسموح بالتداول ───────────────────────────────────────────────────
def can_trade(balance: float, open_positions_count: int) -> tuple[bool, str]:
    state = _reset_if_new_day(_load_state())

    if state["halted"]:
        return False, f"⛔ متوقف: {state['halt_reason']}"

    if open_positions_count >= MAX_OPEN_TRADES:
        return False, f"⛔ {open_positions_count} صفقات مفتوحة — الحد الأقصى {MAX_OPEN_TRADES}"

    if state["trades_today"] >= MAX_TRADES_PER_DAY:
        return False, f"⛔ وصلت الحد اليومي: {MAX_TRADES_PER_DAY} صفقات"

    max_loss = balance * MAX_DAILY_LOSS_PCT
    if abs(state["daily_pnl"]) >= max_loss and state["daily_pnl"] < 0:
        state["halted"]     = True
        state["halt_reason"] = f"خسارة يومية وصلت {MAX_DAILY_LOSS_PCT*100:.0f}%"
        _save_state(state)
        return False, f"⛔ {state['halt_reason']}"

    if state["consec_losses"] >= MAX_CONSEC_LOSSES:
        state["halted"]     = True
        state["halt_reason"] = f"{MAX_CONSEC_LOSSES} خسائر متتالية — توقف 24 ساعة"
        _save_state(state)
        return False, f"⛔ {state['halt_reason']}"

    return True, "✅"


# ─── حساب حجم اللوت ──────────────────────────────────────────────────────────
def calculate_lot_size(symbol: str, balance: float,
                        entry: float, sl: float) -> float:
    """
    حجم اللوت = مبلغ المخاطرة / (نقاط SL × قيمة النقطة)
    """
    sym_config = SYMBOLS.get(symbol, {})
    pip_size   = sym_config.get("pip_size", 1.0)
    pip_value  = sym_config.get("pip_value_per_lot", 1.0)

    risk_amount = balance * RISK_PER_TRADE_PCT
    sl_points   = abs(entry - sl) / pip_size

    if sl_points == 0:
        return 0.01

    lot_size = risk_amount / (sl_points * pip_value)
    lot_size = max(0.01, round(lot_size, 2))

    logger.info(
        f"💰 حجم اللوت | balance={balance:.2f} | "
        f"risk={risk_amount:.2f} | sl_pts={sl_points:.1f} | lots={lot_size}"
    )
    return lot_size


# ─── تسجيل الصفقة ────────────────────────────────────────────────────────────
def record_trade_open():
    state = _reset_if_new_day(_load_state())
    state["trades_today"] += 1
    _save_state(state)


def record_trade_close(pnl: float):
    state = _reset_if_new_day(_load_state())
    state["daily_pnl"] += pnl
    if pnl < 0:
        state["consec_losses"] += 1
    else:
        state["consec_losses"] = 0  # إعادة تصفير عند ربح
    _save_state(state)
    logger.info(f"📊 P&L اليوم: {state['daily_pnl']:.2f} | خسائر متتالية: {state['consec_losses']}")


def get_daily_stats() -> dict:
    return _reset_if_new_day(_load_state())


def resume_trading():
    """يُستخدم بعد التوقف اليدوي عبر تيليغرام"""
    state = _load_state()
    state["halted"]      = False
    state["halt_reason"] = ""
    state["consec_losses"] = 0
    _save_state(state)
    logger.info("✅ استُؤنف التداول يدوياً")
