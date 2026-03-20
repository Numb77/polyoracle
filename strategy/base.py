"""Abstract base strategy class."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from core.clock import WindowState
from strategy.signals import CompositeSignal
from strategy.confidence import ConfidenceBreakdown


@dataclass
class TradeDecision:
    """The output of strategy evaluation — whether and how to trade."""
    should_trade: bool
    direction: str          # 'UP' or 'DOWN'
    confidence: ConfidenceBreakdown
    signal: CompositeSignal
    reason: str             # Human-readable reason

    def to_dict(self) -> dict:
        return {
            "should_trade": self.should_trade,
            "direction": self.direction,
            "confidence": self.confidence.to_dict(),
            "signal": self.signal.to_dict(),
            "reason": self.reason,
        }


class BaseStrategy(ABC):
    """Abstract base for all trading strategies."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Strategy identifier."""
        ...

    @abstractmethod
    async def evaluate(self, window: WindowState) -> TradeDecision:
        """
        Evaluate current market conditions and return a trade decision.
        Called at T-30s and again at T-10s (decision point).
        """
        ...
