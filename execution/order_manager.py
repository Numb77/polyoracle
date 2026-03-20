"""
Order manager — tracks open orders, fills, and positions.

Maintains a record of all active and historical orders.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional

from core.logger import get_logger

logger = get_logger(__name__)


class OrderStatus(Enum):
    PENDING = auto()     # Order placed, awaiting fill
    FILLED = auto()      # Fully filled
    PARTIAL = auto()     # Partially filled
    CANCELLED = auto()   # Cancelled
    REJECTED = auto()    # Rejected by exchange
    EXPIRED = auto()     # Time-expired


@dataclass
class Order:
    """A single order record."""
    order_id: str
    market_slug: str
    condition_id: str
    token_id: str
    direction: str          # 'UP' or 'DOWN'
    outcome: str            # 'YES' or 'NO'
    price: float            # Token price paid
    size_usd: float         # Size in USDC
    size_shares: float      # Number of shares
    fee_usd: float
    confidence: float
    status: OrderStatus = OrderStatus.PENDING
    filled_shares: float = 0.0
    filled_price: float = 0.0
    pnl: float | None = None
    created_at: float = field(default_factory=time.time)
    filled_at: float | None = None
    window_ts: int = 0
    is_paper: bool = False

    @property
    def is_active(self) -> bool:
        return self.status in (OrderStatus.PENDING, OrderStatus.PARTIAL)

    @property
    def is_closed(self) -> bool:
        return self.status in (
            OrderStatus.FILLED, OrderStatus.CANCELLED,
            OrderStatus.REJECTED, OrderStatus.EXPIRED
        )

    @property
    def cost_basis(self) -> float:
        """Total cost including fees."""
        return self.size_usd + self.fee_usd

    def to_dict(self) -> dict:
        return {
            "order_id": self.order_id,
            "market_slug": self.market_slug,
            "direction": self.direction,
            "outcome": self.outcome,
            "price": self.price,
            "size_usd": self.size_usd,
            "size_shares": self.size_shares,
            "fee_usd": self.fee_usd,
            "confidence": self.confidence,
            "status": self.status.name,
            "pnl": self.pnl,
            "created_at": self.created_at,
            "filled_at": self.filled_at,
            "window_ts": self.window_ts,
            "is_paper": self.is_paper,
        }


class OrderManager:
    """
    Maintains the state of all orders (active + historical).
    Thread-safe via asyncio (single-threaded event loop).
    """

    def __init__(self) -> None:
        self._active: dict[str, Order] = {}
        self._history: list[Order] = []
        self._order_count = 0

    def add_order(self, order: Order) -> None:
        """Register a new order."""
        self._active[order.order_id] = order
        self._order_count += 1
        logger.info(
            f"Order created: {order.order_id[:12]}... "
            f"{order.direction} {order.outcome} @ {order.price:.3f} "
            f"(${order.size_usd:.2f}, conf={order.confidence:.0f})"
        )

    def mark_filled(
        self,
        order_id: str,
        filled_shares: float | None = None,
        filled_price: float | None = None,
    ) -> Order | None:
        """Mark an order as filled."""
        order = self._active.pop(order_id, None)
        if not order:
            logger.warning(f"Order {order_id} not found for fill")
            return None

        order.status = OrderStatus.FILLED
        order.filled_at = time.time()
        if filled_shares is not None:
            order.filled_shares = filled_shares
        if filled_price is not None:
            order.filled_price = filled_price
        else:
            order.filled_price = order.price

        self._history.append(order)
        logger.info(f"Order filled: {order_id[:12]}... {order.direction}")
        return order

    def mark_cancelled(self, order_id: str, reason: str = "") -> Order | None:
        """Mark an order as cancelled."""
        order = self._active.pop(order_id, None)
        if not order:
            return None

        order.status = OrderStatus.CANCELLED
        self._history.append(order)
        logger.info(f"Order cancelled: {order_id[:12]}... ({reason})")
        return order

    def mark_resolved(self, order_id: str, won: bool, pnl: float) -> Order | None:
        """Record the outcome (win/loss) for a filled order."""
        # Find in history
        for order in reversed(self._history):
            if order.order_id == order_id:
                order.pnl = pnl
                logger.info(
                    f"Order resolved: {order_id[:12]}... "
                    f"{'WON' if won else 'LOST'} ${pnl:+.2f}"
                )
                return order
        return None

    def get_active_orders(self) -> list[Order]:
        return list(self._active.values())

    def get_active_for_window(self, window_ts: int) -> list[Order]:
        return [o for o in self._active.values() if o.window_ts == window_ts]

    def get_recent_history(self, n: int = 50) -> list[Order]:
        return self._history[-n:]

    @property
    def active_count(self) -> int:
        return len(self._active)

    @property
    def total_trades(self) -> int:
        return len(self._history)

    def get_open_exposure_usd(self) -> float:
        """Total USD at risk across all open positions."""
        return sum(o.size_usd for o in self._active.values())
