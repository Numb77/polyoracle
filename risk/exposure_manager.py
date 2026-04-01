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
    - Max concurrent positions across all symbols (default: 2)
    - Max 1 open position per symbol at a time
    - Max USD at risk at any time
    """

    def __init__(self) -> None:
        self._max_positions = cfg.max_concurrent_positions
        self._max_exposure_usd = cfg.max_exposure_usd
        self._active_count: int = 0
        self._active_usd: float = 0.0
        self._symbol_count: dict[str, int] = {}

    def can_open_position(self, size_usd: float, symbol: str = "") -> tuple[bool, str]:
        """
        Check if we can open a new position.
        Returns (allowed, reason).
        """
        if self._active_count >= self._max_positions:
            return (
                False,
                f"Max positions reached ({self._active_count}/{self._max_positions})",
            )
        if symbol and self._symbol_count.get(symbol, 0) >= 1:
            return (
                False,
                f"{symbol} already has an open position",
            )
        if self._active_usd + size_usd > self._max_exposure_usd:
            return (
                False,
                f"USD exposure limit: ${self._active_usd:.2f} + ${size_usd:.2f} "
                f"> ${self._max_exposure_usd:.2f} max",
            )
        return True, "OK"

    def open_position(self, size_usd: float, symbol: str = "") -> None:
        """Register a new open position."""
        self._active_count += 1
        self._active_usd += size_usd
        if symbol:
            self._symbol_count[symbol] = self._symbol_count.get(symbol, 0) + 1

    def close_position(self, size_usd: float, symbol: str = "") -> None:
        """Remove a closed position."""
        self._active_count = max(0, self._active_count - 1)
        self._active_usd = max(0.0, self._active_usd - size_usd)
        if symbol and symbol in self._symbol_count:
            self._symbol_count[symbol] = max(0, self._symbol_count[symbol] - 1)

    def reset(self) -> None:
        """Reset all tracked positions (e.g., after restart)."""
        self._active_count = 0
        self._active_usd = 0.0
        self._symbol_count.clear()

    def reconcile(self, true_count: int, true_usd: float, symbol_counts: dict[str, int] | None = None) -> None:
        """
        Overwrite the counter with ground-truth values derived from the order
        managers.  Call this periodically to self-heal any drift caused by
        missed close_position() calls (e.g. no-edge cancels, bot restarts).
        """
        if self._active_count != true_count or abs(self._active_usd - true_usd) > 0.01:
            logger.info(
                f"Exposure reconciled: {self._active_count}→{true_count} positions, "
                f"${self._active_usd:.2f}→${true_usd:.2f} USD"
            )
        self._active_count = true_count
        self._active_usd = true_usd
        if symbol_counts is not None:
            self._symbol_count = symbol_counts

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
            "max_exposure_usd": self._max_exposure_usd,
            "utilization_pct": round(self.utilization_pct, 1),
        }
