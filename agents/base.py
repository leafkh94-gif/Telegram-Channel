"""
Shared data types for the multi-agent trading system.

Each agent produces an AgentVerdict.
The Orchestrator combines all verdicts into a single TradeDecision.
"""
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class AgentVerdict:
    """Structured decision returned by one agent."""
    agent: str          # "market" | "news" | "risk"
    verdict: str        # "GO" | "HOLD" | "BLOCK"
    confidence: float   # 0.0–1.0
    reason: str
    direction: Optional[str] = None  # "buy" | "sell" — market agent only
    lots: Optional[float] = None     # computed position size — risk agent only

    def __post_init__(self):
        if self.verdict not in ("GO", "HOLD", "BLOCK"):
            raise ValueError(f"verdict must be GO/HOLD/BLOCK, got {self.verdict!r}")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"confidence must be 0–1, got {self.confidence}")

    def emoji(self) -> str:
        return {"GO": "✅", "HOLD": "⏸", "BLOCK": "🚫"}.get(self.verdict, "?")


@dataclass
class TradeDecision:
    """Final decision produced by the Orchestrator."""
    action: str           # "buy" | "sell" | "skip"
    lots: float           # 0.0 when action="skip"
    reason: str
    confidence: float     # min confidence across all GO verdicts; 0.0 on skip
    verdicts: list = field(default_factory=list)  # list[AgentVerdict]
