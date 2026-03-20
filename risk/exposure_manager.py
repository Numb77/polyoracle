"""
Exposure manager — enforces max concurrent position limits.
"""

from __future__ import annotations

from core.config import get_config
from core.logger import get_logger

logger = get_logger(__name__)
cfg = get_config()


class ExposureManager:
    """
    Tracks current exposure and enforces limits:
    - Max concurrent positions (default: 3)
    - Max USD at risk at any time
    """

    def __init__(self) -> None:
        self._max_positions = cfg.max_concurrent_positions
        self._active_count: int = 0
        self._active_usd: float = 0.0

    def can_open_position(self, size_usd: float) -> tuple[bool, str]:
        """
        Check if we can open a new position.
        Returns (allowed, reason).
        """
        if self._active_count >= self._max_positions:
            return (
                False,
                f"Max positions reached ({self._active_count}/{self._max_positions})",
            )
        return True, "OK"

    def open_position(self, size_usd: float) -> None:
        """Register a new open position."""
        self._active_count += 1
        self._active_usd += size_usd

    def close_position(self, size_usd: float) -> None:
        """Remove a closed position."""
        self._active_count = max(0, self._active_count - 1)
        self._active_usd = max(0.0, self._active_usd - size_usd)

    def reset(self) -> None:
        """Reset all tracked positions (e.g., after restart)."""
        self._active_count = 0
        self._active_usd = 0.0

    @property
    def active_count(self) -> int:
        return self._active_count

    @property
    def active_usd(self) -> float:
        return self._active_usd

    @property
    def utilization_pct(self) -> float:
        if self._max_positions == 0:
            return 0.0
        return self._active_count / self._max_positions * 100

    def to_dict(self) -> dict:
        return {
            "active_count": self._active_count,
            "max_count": self._max_positions,
            "active_usd": round(self._active_usd, 2),
            "utilization_pct": round(self.utilization_pct, 1),
        }
