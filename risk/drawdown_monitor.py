"""
Drawdown monitor — tracks peak balance and current drawdown %.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from core.config import get_config
from core.logger import get_logger

logger = get_logger(__name__)
cfg = get_config()


@dataclass
class DrawdownState:
    """Current drawdown state."""
    peak_balance: float
    current_balance: float
    drawdown_usd: float
    drawdown_pct: float
    is_in_drawdown: bool

    @property
    def size_multiplier(self) -> float:
        """Scale position size inversely with drawdown."""
        if self.drawdown_pct <= 5.0:
            return 1.0
        return max(0.3, 1.0 - (self.drawdown_pct - 5.0) / 20.0)

    def to_dict(self) -> dict:
        return {
            "peak_balance": round(self.peak_balance, 2),
            "current_balance": round(self.current_balance, 2),
            "drawdown_usd": round(self.drawdown_usd, 2),
            "drawdown_pct": round(self.drawdown_pct, 2),
            "size_multiplier": round(self.size_multiplier, 3),
        }


class DrawdownMonitor:
    """Tracks the running peak balance and current drawdown."""

    def __init__(self, initial_balance: float) -> None:
        self._peak = initial_balance
        self._current = initial_balance
        self._history: list[tuple[float, float]] = []   # (timestamp, balance)

    def update(self, balance: float) -> DrawdownState:
        """Update with the current balance and return drawdown state."""
        import time

        self._current = balance
        self._peak = max(self._peak, balance)
        self._history.append((time.time(), balance))

        # Keep only last 1000 balance points
        if len(self._history) > 1000:
            self._history = self._history[-1000:]

        drawdown_usd = self._peak - self._current
        drawdown_pct = (drawdown_usd / self._peak * 100) if self._peak > 0 else 0.0

        return DrawdownState(
            peak_balance=self._peak,
            current_balance=self._current,
            drawdown_usd=drawdown_usd,
            drawdown_pct=drawdown_pct,
            is_in_drawdown=drawdown_pct > 0.5,
        )

    @property
    def peak(self) -> float:
        return self._peak

    @property
    def current(self) -> float:
        return self._current

    @property
    def drawdown_pct(self) -> float:
        if self._peak <= 0:
            return 0.0
        return (self._peak - self._current) / self._peak * 100

    def get_chart_data(self) -> list[dict]:
        """Return balance history for chart rendering."""
        return [
            {"timestamp": ts, "balance": bal, "peak": self._peak}
            for ts, bal in self._history
        ]

    def reset_peak(self) -> None:
        """Manually reset peak to current balance."""
        self._peak = self._current
