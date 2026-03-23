"""
Base agent class for the multi-agent decision system.

Each agent has a distinct analytical lens and votes UP/DOWN/ABSTAIN
with a conviction level (0-1).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum, auto

import pandas as pd

from core.logger import get_logger


class Vote(Enum):
    UP = "UP"
    DOWN = "DOWN"
    ABSTAIN = "ABSTAIN"


@dataclass
class AgentVote:
    """A single agent's vote on market direction."""
    agent_name: str
    vote: Vote
    conviction: float       # 0.0 to 1.0
    reasoning: str
    accuracy: float = 0.5   # Rolling accuracy from meta-learner (default 50%)
    weight: float = 1.0     # Current weight (adjusted by meta-learner)
    is_muted: bool = False   # Muted if accuracy < 45%
    trend: str = "→"        # "↑", "↓", "→" — from meta-learner
    session_accuracy: dict = None  # Per-session accuracy from meta-learner

    def __post_init__(self):
        if self.session_accuracy is None:
            self.session_accuracy = {}

    @property
    def effective_conviction(self) -> float:
        """Conviction adjusted for agent accuracy and weight."""
        if self.is_muted:
            return 0.0
        return self.conviction * self.accuracy * self.weight

    def to_dict(self) -> dict:
        return {
            "agent": self.agent_name,
            "vote": self.vote.value,
            "conviction": round(self.conviction, 3),
            "reasoning": self.reasoning,
            "accuracy": round(self.accuracy, 3),
            "weight": round(self.weight, 3),
            "is_muted": self.is_muted,
            "effective_conviction": round(self.effective_conviction, 3),
            "trend": self.trend,
            "session_accuracy": {
                k: round(v, 3) for k, v in self.session_accuracy.items()
            },
        }


class BaseAgent(ABC):
    """
    Abstract base class for all trading agents.
    Each agent implements a specific analytical lens and returns a vote.
    """

    def __init__(self) -> None:
        self.logger = get_logger(f"agents.{self.name}")

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique agent identifier."""
        ...

    @property
    @abstractmethod
    def emoji(self) -> str:
        """Emoji representation for the dashboard."""
        ...

    @property
    @abstractmethod
    def persona(self) -> str:
        """Human-readable persona description."""
        ...

    @abstractmethod
    async def vote(
        self,
        window_delta_pct: float,
        df_1m: pd.DataFrame,
        df_5s: pd.DataFrame | None,
        ob_imbalance: float | None,
        oracle_delta_pct: float,
        atr_pct: float,
        **kwargs,
    ) -> AgentVote:
        """
        Cast a vote on the current market direction.

        Args:
            window_delta_pct:  BTC % change from window open
            df_1m:             1-minute candle DataFrame
            df_5s:             5-second candle DataFrame (may be None)
            ob_imbalance:      Polymarket order book imbalance [-1, +1]
            oracle_delta_pct:  CEX vs Chainlink oracle divergence %
            atr_pct:           ATR as % of price

        Returns:
            AgentVote with direction and conviction
        """
        ...
