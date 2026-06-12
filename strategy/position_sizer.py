"""
Dynamic position sizer.

Combines three methods and returns the most conservative result:
  1. Kelly Criterion   — fraction of account based on win-rate + payoff
  2. Volatility-adjusted — risk a fixed % of account per 1 ATR move
  3. Hard cap          — never exceed RiskLimits.max_position_size_lots

Inputs are kept simple so the sizer works without a live broker connection:
the caller provides recent candle ATR and a rough account-value estimate.
"""
from __future__ import annotations

import math
import logging

logger = logging.getLogger(__name__)

# Conservative fractional Kelly multiplier (25 % of full Kelly)
_KELLY_FRACTION = 0.25
# Never bet more than this fraction of account regardless of Kelly
_MAX_ACCOUNT_FRACTION = 0.02   # 2 % per trade


def kelly_lots(
    win_rate: float,
    avg_win_usd: float,
    avg_loss_usd: float,
    account_value_usd: float,
    lot_value_usd: float,
    max_lots: float,
) -> float:
    """
    Kelly Criterion position size in lots.

    Parameters
    ----------
    win_rate        : historical win rate (0-1)
    avg_win_usd     : average winning trade in USD
    avg_loss_usd    : average losing trade in USD (positive number)
    account_value_usd: current account equity
    lot_value_usd   : USD notional per 0.01 lot (instrument-specific)
    max_lots        : hard ceiling (from RiskLimits)
    """
    if avg_loss_usd <= 0 or lot_value_usd <= 0 or account_value_usd <= 0:
        return max_lots * 0.5  # safe fallback

    payoff = avg_win_usd / avg_loss_usd
    full_kelly = win_rate - (1.0 - win_rate) / payoff
    fractional_kelly = max(full_kelly * _KELLY_FRACTION, 0.0)

    dollar_risk = account_value_usd * min(fractional_kelly, _MAX_ACCOUNT_FRACTION)
    lots = dollar_risk / lot_value_usd

    capped = min(lots, max_lots)
    logger.debug(
        "kelly: win_rate=%.2f payoff=%.2f full_kelly=%.3f frac=%.3f "
        "dollar_risk=%.2f lots=%.3f capped=%.3f",
        win_rate, payoff, full_kelly, fractional_kelly, dollar_risk, lots, capped,
    )
    return capped


def volatility_adjusted_lots(
    current_atr: float,
    entry_price: float,
    account_value_usd: float,
    risk_per_trade_pct: float,
    lot_value_usd: float,
    max_lots: float,
) -> float:
    """
    Size so that a 1-ATR adverse move costs exactly risk_per_trade_pct of account.

    Parameters
    ----------
    current_atr        : latest ATR value in price units
    entry_price        : anticipated entry price
    account_value_usd  : current account equity
    risk_per_trade_pct : fraction of account to risk (e.g. 0.01 = 1 %)
    lot_value_usd      : USD per 0.01 lot
    max_lots           : hard ceiling
    """
    if current_atr <= 0 or entry_price <= 0 or lot_value_usd <= 0:
        return max_lots * 0.5

    dollar_risk = account_value_usd * risk_per_trade_pct
    # ATR is already in price-unit points; lot_value_usd converts points → USD.
    # Do NOT divide by entry_price — that would shrink index ATRs by ~20,000×.
    atr_in_usd = current_atr * lot_value_usd
    lots = dollar_risk / atr_in_usd if atr_in_usd > 0 else max_lots * 0.5

    capped = min(lots, max_lots)
    logger.debug(
        "vol_adj: atr=%.4f entry=%.2f dollar_risk=%.2f atr_usd=%.4f lots=%.3f capped=%.3f",
        current_atr, entry_price, dollar_risk, atr_in_usd, lots, capped,
    )
    return capped


class PositionSizer:
    """
    Stateless helper that returns the final lot size to use.

    Takes the minimum of Kelly-based and volatility-adjusted sizes,
    then rounds to the nearest 0.01 lot.
    """

    def __init__(
        self,
        account_value_usd: float = 10_000.0,
        lot_value_usd: float = 100.0,       # ~XAU/USD micro-lot
        risk_per_trade_pct: float = 0.01,   # 1 % per trade
        win_rate: float = 0.50,
        avg_win_usd: float = 150.0,
        avg_loss_usd: float = 100.0,
        fallback_lots: float = 0.05,
        max_lots: float = 0.10,
    ):
        self.account_value_usd = account_value_usd
        self.lot_value_usd = lot_value_usd
        self.risk_per_trade_pct = risk_per_trade_pct
        self.win_rate = win_rate
        self.avg_win_usd = avg_win_usd
        self.avg_loss_usd = avg_loss_usd
        self.fallback_lots = fallback_lots
        self.max_lots = max_lots

    def compute(self, current_atr: float, entry_price: float) -> float:
        """Return lot size rounded to 0.01."""
        if math.isnan(current_atr) or current_atr <= 0 or entry_price <= 0:
            logger.debug("position_sizer: invalid inputs, using fallback %.2f", self.fallback_lots)
            return self.fallback_lots

        k_lots = kelly_lots(
            self.win_rate, self.avg_win_usd, self.avg_loss_usd,
            self.account_value_usd, self.lot_value_usd, self.max_lots,
        )
        v_lots = volatility_adjusted_lots(
            current_atr, entry_price,
            self.account_value_usd, self.risk_per_trade_pct,
            self.lot_value_usd, self.max_lots,
        )

        # Conservative: take the smaller of the two methods
        raw = min(k_lots, v_lots)
        # Round to nearest 0.01 lot, minimum 0.01
        result = max(round(raw * 100) / 100, 0.01)
        logger.debug("position_sizer: kelly=%.3f vol_adj=%.3f → %.2f lots", k_lots, v_lots, result)
        return result
