"""
P&L tracker — real-time P&L, win rate, Sharpe ratio, and statistics.
"""

from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

from core.config import get_config
from core.logger import get_logger

logger = get_logger(__name__)
cfg = get_config()


@dataclass
class TradeRecord:
    """A completed trade's P&L record."""
    trade_id: str
    direction: str
    won: bool
    pnl: float
    entry_price: float
    confidence: float
    window_ts: int
    closed_at: float = field(default_factory=time.time)


@dataclass
class PnlStats:
    """Full P&L statistics snapshot."""
    total_pnl: float
    win_rate: float
    total_trades: int
    winning_trades: int
    losing_trades: int
    avg_win: float
    avg_loss: float
    best_trade: float
    worst_trade: float
    sharpe_ratio: float
    daily_pnl: float
    consecutive_wins: int
    consecutive_losses: int
    expected_value: float   # Expected P&L per trade
    avg_confidence_wins: float
    avg_confidence_losses: float

    def to_dict(self) -> dict:
        return {
            "total_pnl": round(self.total_pnl, 2),
            "win_rate": round(self.win_rate, 3),
            "total_trades": self.total_trades,
            "winning_trades": self.winning_trades,
            "losing_trades": self.losing_trades,
            "avg_win": round(self.avg_win, 3),
            "avg_loss": round(self.avg_loss, 3),
            "best_trade": round(self.best_trade, 2),
            "worst_trade": round(self.worst_trade, 2),
            "sharpe_ratio": round(self.sharpe_ratio, 3),
            "daily_pnl": round(self.daily_pnl, 2),
            "consecutive_wins": self.consecutive_wins,
            "consecutive_losses": self.consecutive_losses,
            "expected_value": round(self.expected_value, 4),
            "avg_confidence_wins": round(self.avg_confidence_wins, 1),
            "avg_confidence_losses": round(self.avg_confidence_losses, 1),
        }


class PnlTracker:
    """
    Tracks P&L and computes trading statistics in real-time.
    """

    def __init__(self, initial_balance: float | None = None) -> None:
        self._initial_balance = initial_balance or cfg.paper_initial_balance
        self._trades: list[TradeRecord] = []
        self._daily_start_balance: float = self._initial_balance
        self._daily_start_ts: float = self._get_day_start()

        # Rolling pnl buffer for Sharpe calculation
        self._pnl_per_trade: deque[float] = deque(maxlen=200)

        # Streak tracking
        self._consecutive_wins = 0
        self._consecutive_losses = 0

    def record_trade(
        self,
        trade_id: str,
        direction: str,
        won: bool,
        pnl: float,
        entry_price: float,
        confidence: float,
        window_ts: int,
    ) -> None:
        """Record a completed trade."""
        record = TradeRecord(
            trade_id=trade_id,
            direction=direction,
            won=won,
            pnl=pnl,
            entry_price=entry_price,
            confidence=confidence,
            window_ts=window_ts,
        )
        self._trades.append(record)
        self._pnl_per_trade.append(pnl)

        # Update streaks
        if won:
            self._consecutive_wins += 1
            self._consecutive_losses = 0
        else:
            self._consecutive_losses += 1
            self._consecutive_wins = 0

        logger.info(
            f"P&L recorded: {'WIN' if won else 'LOSS'} ${pnl:+.2f} "
            f"(conf={confidence:.0f}, streak: "
            f"+{self._consecutive_wins}/-{self._consecutive_losses})"
        )

    def get_stats(self) -> PnlStats:
        """Compute and return current statistics."""
        if not self._trades:
            return self._empty_stats()

        winners = [t for t in self._trades if t.won]
        losers = [t for t in self._trades if not t.won]

        total_pnl = sum(t.pnl for t in self._trades)
        win_rate = len(winners) / len(self._trades)

        avg_win = sum(t.pnl for t in winners) / len(winners) if winners else 0.0
        avg_loss = sum(t.pnl for t in losers) / len(losers) if losers else 0.0

        best = max(t.pnl for t in self._trades)
        worst = min(t.pnl for t in self._trades)

        # Sharpe ratio (annualized, assuming ~288 trades/day at 5-min intervals)
        sharpe = self._compute_sharpe()

        # Daily P&L (reset at midnight UTC)
        now_ts = time.time()
        if now_ts - self._daily_start_ts > 86400:
            self._daily_start_ts = self._get_day_start()
            # Don't reset daily_start_balance here — PnL tracker holds balance externally

        day_trades = [
            t for t in self._trades
            if t.closed_at >= self._daily_start_ts
        ]
        daily_pnl = sum(t.pnl for t in day_trades)

        # Expected value
        ev = (win_rate * avg_win) + ((1 - win_rate) * avg_loss)

        # Confidence analysis
        avg_conf_wins = (
            sum(t.confidence for t in winners) / len(winners) if winners else 0.0
        )
        avg_conf_losses = (
            sum(t.confidence for t in losers) / len(losers) if losers else 0.0
        )

        return PnlStats(
            total_pnl=total_pnl,
            win_rate=win_rate,
            total_trades=len(self._trades),
            winning_trades=len(winners),
            losing_trades=len(losers),
            avg_win=avg_win,
            avg_loss=avg_loss,
            best_trade=best,
            worst_trade=worst,
            sharpe_ratio=sharpe,
            daily_pnl=daily_pnl,
            consecutive_wins=self._consecutive_wins,
            consecutive_losses=self._consecutive_losses,
            expected_value=ev,
            avg_confidence_wins=avg_conf_wins,
            avg_confidence_losses=avg_conf_losses,
        )

    def get_daily_loss(self) -> float:
        """Total losses today (positive number = losses)."""
        day_trades = [
            t for t in self._trades
            if t.closed_at >= self._daily_start_ts
        ]
        return abs(sum(t.pnl for t in day_trades if t.pnl < 0))

    def get_consecutive_losses(self) -> int:
        return self._consecutive_losses

    def _compute_sharpe(self) -> float:
        """Compute annualized Sharpe ratio from trade P&L history."""
        pnls = list(self._pnl_per_trade)
        if len(pnls) < 5:
            return 0.0

        import statistics
        mean = statistics.mean(pnls)
        stdev = statistics.stdev(pnls)
        if stdev == 0:
            return 0.0

        # Annualize: assuming ~288 5-min windows per day × 365 days
        trades_per_year = 288 * 365
        return (mean / stdev) * math.sqrt(trades_per_year)

    def _empty_stats(self) -> PnlStats:
        return PnlStats(
            total_pnl=0.0,
            win_rate=0.0,
            total_trades=0,
            winning_trades=0,
            losing_trades=0,
            avg_win=0.0,
            avg_loss=0.0,
            best_trade=0.0,
            worst_trade=0.0,
            sharpe_ratio=0.0,
            daily_pnl=0.0,
            consecutive_wins=0,
            consecutive_losses=0,
            expected_value=0.0,
            avg_confidence_wins=0.0,
            avg_confidence_losses=0.0,
        )

    @staticmethod
    def _get_day_start() -> float:
        """Unix timestamp of today's UTC midnight."""
        import datetime
        now = datetime.datetime.now(datetime.timezone.utc)
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return midnight.timestamp()
