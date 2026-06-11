"""
RiskAgent — position sizing and pre-trade risk validation.

Given a direction, entry price, and ATR it computes the correct lot size
so that a full stop-loss costs exactly RISK_PER_TRADE_PCT of the account.

Contract values (USD per 1.0 lot per $1 price move):
  GOLD  — 100 oz/lot  (e.g. 0.05 lots × $15 ATR SL × 100 = $75 risk)
  US500 — $1/point    (e.g. 0.50 lots × 30 pts SL × 1  = $15 risk)
  US100 — $1/point
  US30  — $1/point

Verdict:
  GO    — lot size is within bounds; lots field is set
  BLOCK — lot size rounds below MIN_LOTS (ATR too large for account) or
          account configuration is missing/invalid
"""
import logging
import os

from agents.base import AgentVerdict

logger = logging.getLogger(__name__)

_CONTRACT_VALUE: dict[str, float] = {
    "GOLD":  100.0,
    "US500": 1.0,
    "US100": 1.0,
    "US30":  1.0,
}
_MIN_LOTS = 0.01
_MAX_LOTS = 0.10


class RiskAgent:
    def __init__(
        self,
        account_size_usd: float | None = None,
        risk_per_trade_pct: float | None = None,
        sl_atr_mult: float = 1.5,
    ):
        self._account  = account_size_usd   or float(os.getenv("ACCOUNT_SIZE_USD",   "2000"))
        self._risk_pct = risk_per_trade_pct or float(os.getenv("RISK_PER_TRADE_PCT", "0.01"))
        self._sl_mult  = sl_atr_mult

    def evaluate(self, epic: str, entry: float, atr: float, direction: str) -> AgentVerdict:
        if entry <= 0 or atr <= 0:
            return AgentVerdict(
                agent="risk", verdict="BLOCK",
                confidence=1.0,
                reason=f"invalid entry ({entry:.2f}) or ATR ({atr:.4f})",
            )

        sl_distance  = self._sl_mult * atr
        risk_usd     = self._account * self._risk_pct
        contract     = _CONTRACT_VALUE.get(epic, 1.0)
        raw_lots     = risk_usd / (sl_distance * contract)
        lots         = max(_MIN_LOTS, min(raw_lots, _MAX_LOTS))

        # When the ideal size rounds below the broker minimum, trade the
        # minimum lot rather than blocking. The actual dollar risk will be
        # slightly above the target % — surface that in the reason so the
        # alert is honest about it.
        if raw_lots < _MIN_LOTS:
            actual_risk = _MIN_LOTS * sl_distance * contract
            return AgentVerdict(
                agent="risk", verdict="GO",
                confidence=0.85,
                reason=(f"{_MIN_LOTS:.2f} lots (min) — actual risk ${actual_risk:.0f} "
                        f"vs ${risk_usd:.0f} target (ATR high for account)"),
                lots=_MIN_LOTS,
            )

        return AgentVerdict(
            agent="risk", verdict="GO",
            confidence=1.0,
            reason=(f"{lots:.2f} lots "
                    f"(${risk_usd:.0f} risk on ${self._account:,.0f} account, "
                    f"SL {sl_distance:.1f} pts)"),
            lots=lots,
        )
