"""
Three-tier circuit breaker system.

TIER 1 — YELLOW: Reduce position size 50%
TIER 2 — ORANGE: Pause trading for 30 minutes
TIER 3 — RED:    Emergency stop, require manual restart
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum, auto
from typing import Optional

from core.config import get_config
from core.logger import get_logger

logger = get_logger(__name__)
cfg = get_config()


class CircuitTier(Enum):
    GREEN = "GREEN"      # Normal operation
    YELLOW = "YELLOW"    # Reduced size
    ORANGE = "ORANGE"    # Trading paused
    RED = "RED"          # Emergency stop


@dataclass
class BreakerStatus:
    """Current circuit breaker status."""
    tier: CircuitTier
    reason: str
    triggered_at: float | None
    resume_at: float | None       # For ORANGE tier — when trading can resume
    size_multiplier: float        # Position size multiplier (1.0, 0.5, or 0.0)

    @property
    def can_trade(self) -> bool:
        if self.tier == CircuitTier.GREEN:
            return True
        if self.tier == CircuitTier.YELLOW:
            return True   # Can trade but at reduced size
        if self.tier == CircuitTier.ORANGE:
            return (
                self.resume_at is not None
                and time.time() >= self.resume_at
            )
        return False  # RED = no trading

    @property
    def is_paused(self) -> bool:
        return self.tier in (CircuitTier.ORANGE, CircuitTier.RED)

    def to_dict(self) -> dict:
        return {
            "tier": self.tier.value,
            "reason": self.reason,
            "triggered_at": self.triggered_at,
            "resume_at": self.resume_at,
            "size_multiplier": self.size_multiplier,
            "can_trade": self.can_trade,
        }


class CircuitBreaker:
    """
    Monitors trading metrics and activates protection tiers when thresholds hit.
    """

    ORANGE_PAUSE_MINUTES = 30   # How long to pause on ORANGE trigger

    def __init__(self) -> None:
        self._status = BreakerStatus(
            tier=CircuitTier.GREEN,
            reason="Normal operation",
            triggered_at=None,
            resume_at=None,
            size_multiplier=1.0,
        )
        self._manual_pause = False

    def evaluate(
        self,
        daily_loss_usd: float,
        drawdown_pct: float,
        consecutive_losses: int,
        balance: float,
        has_errors: bool = False,
    ) -> BreakerStatus:
        """
        Evaluate all risk metrics and update circuit breaker tier.
        """
        # ── Check for manual pause ────────────────────────────────────────────
        if self._manual_pause:
            self._set_tier(CircuitTier.ORANGE, "Manual pause", resume_at=None)
            return self._status

        # ── Check for auto-resume from ORANGE ────────────────────────────────
        if (
            self._status.tier == CircuitTier.ORANGE
            and self._status.resume_at is not None
            and time.time() >= self._status.resume_at
        ):
            logger.info("Circuit breaker resuming from ORANGE pause")
            self._set_tier(CircuitTier.GREEN, "Resumed after pause timeout")

        # ── TIER 3 — RED checks ───────────────────────────────────────────────
        if has_errors:
            self._set_tier(CircuitTier.RED, "Execution errors detected")
            return self._status

        if balance < cfg.min_usdc_balance:
            self._set_tier(
                CircuitTier.RED,
                f"Balance ${balance:.2f} below minimum ${cfg.min_usdc_balance:.2f}",
            )
            return self._status

        # ── TIER 2 — ORANGE checks ────────────────────────────────────────────
        if self._status.tier not in (CircuitTier.RED, CircuitTier.ORANGE):
            if daily_loss_usd >= cfg.max_daily_loss_usd:
                self._trigger_orange(
                    f"Daily loss ${daily_loss_usd:.2f} hit limit ${cfg.max_daily_loss_usd:.2f}"
                )
                return self._status

            if drawdown_pct >= cfg.max_drawdown_pct:
                self._trigger_orange(
                    f"Drawdown {drawdown_pct:.1f}% hit limit {cfg.max_drawdown_pct:.1f}%"
                )
                return self._status

            if consecutive_losses >= cfg.max_consecutive_losses:
                self._trigger_orange(
                    f"{consecutive_losses} consecutive losses hit limit {cfg.max_consecutive_losses}"
                )
                return self._status

        # ── TIER 1 — YELLOW checks ────────────────────────────────────────────
        yellow_triggered = False
        yellow_reason = ""

        if daily_loss_usd >= cfg.max_daily_loss_usd * 0.75:
            yellow_triggered = True
            yellow_reason = f"Daily loss ${daily_loss_usd:.2f} > 75% of limit"

        if drawdown_pct >= cfg.max_drawdown_pct * 0.67:
            yellow_triggered = True
            yellow_reason = f"Drawdown {drawdown_pct:.1f}% approaching limit"

        if consecutive_losses >= 3:
            yellow_triggered = True
            yellow_reason = f"{consecutive_losses} consecutive losses"

        if yellow_triggered and self._status.tier == CircuitTier.GREEN:
            self._set_tier(CircuitTier.YELLOW, yellow_reason)
            return self._status

        # ── Return to GREEN if conditions improved ────────────────────────────
        if (
            not yellow_triggered
            and self._status.tier == CircuitTier.YELLOW
        ):
            self._set_tier(CircuitTier.GREEN, "Conditions improved")

        return self._status

    def _trigger_orange(self, reason: str) -> None:
        """Activate ORANGE tier with auto-resume timer."""
        resume_at = time.time() + self.ORANGE_PAUSE_MINUTES * 60
        self._set_tier(CircuitTier.ORANGE, reason, resume_at=resume_at)
        logger.warning(
            f"CIRCUIT BREAKER ORANGE: {reason}. "
            f"Pausing {self.ORANGE_PAUSE_MINUTES} minutes."
        )

    def _set_tier(
        self,
        tier: CircuitTier,
        reason: str,
        resume_at: float | None = None,
    ) -> None:
        """Update the circuit breaker tier."""
        old_tier = self._status.tier

        size_multipliers = {
            CircuitTier.GREEN: 1.0,
            CircuitTier.YELLOW: 0.5,
            CircuitTier.ORANGE: 0.0,
            CircuitTier.RED: 0.0,
        }

        self._status = BreakerStatus(
            tier=tier,
            reason=reason,
            triggered_at=time.time() if tier != CircuitTier.GREEN else None,
            resume_at=resume_at,
            size_multiplier=size_multipliers[tier],
        )

        if tier != old_tier:
            level = "critical" if tier == CircuitTier.RED else "warning"
            log_fn = logger.critical if tier == CircuitTier.RED else logger.warning
            log_fn(
                f"Circuit breaker: {old_tier.value} → {tier.value}: {reason}"
            )

    def trigger_emergency_stop(self, reason: str = "Manual emergency stop") -> None:
        """Manually trigger emergency stop (RED tier)."""
        self._set_tier(CircuitTier.RED, reason)

    def manual_pause(self) -> None:
        """Manually pause trading."""
        self._manual_pause = True
        self._set_tier(CircuitTier.ORANGE, "Manual pause")

    def manual_resume(self) -> None:
        """Resume from manual pause."""
        self._manual_pause = False
        if self._status.tier == CircuitTier.ORANGE:
            self._set_tier(CircuitTier.GREEN, "Manually resumed")

    @property
    def status(self) -> BreakerStatus:
        return self._status

    @property
    def can_trade(self) -> bool:
        return self._status.can_trade

    @property
    def size_multiplier(self) -> float:
        return self._status.size_multiplier
