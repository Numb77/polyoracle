"""
Multi-source price aggregator.

Combines prices from Binance (real-time), Chainlink (on-chain oracle),
and computes VWAP and other aggregate metrics.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from typing import Optional

from core.logger import get_logger

logger = get_logger(__name__)


@dataclass
class AggregatedPrice:
    """A consolidated price view from all sources."""
    binance_price: float        # Real-time CEX price
    oracle_price: float         # Chainlink on-chain price
    oracle_latency_sec: float   # Seconds since last oracle update
    vwap_1m: float              # 1-minute VWAP from Binance trades
    vwap_5m: float              # 5-minute VWAP from Binance trades
    consensus_price: float      # Best estimate combining all sources
    timestamp: float

    @property
    def cex_oracle_delta_pct(self) -> float:
        """CEX vs oracle divergence as a percentage."""
        if self.oracle_price <= 0:
            return 0.0
        return (self.binance_price - self.oracle_price) / self.oracle_price * 100

    def to_dict(self) -> dict:
        return {
            "binance_price": self.binance_price,
            "oracle_price": self.oracle_price,
            "oracle_latency_sec": round(self.oracle_latency_sec, 1),
            "vwap_1m": self.vwap_1m,
            "vwap_5m": self.vwap_5m,
            "consensus_price": self.consensus_price,
            "cex_oracle_delta_pct": round(self.cex_oracle_delta_pct, 4),
            "timestamp": self.timestamp,
        }


class _VwapAccumulator:
    """Computes rolling VWAP over a time window."""

    def __init__(self, window_sec: int) -> None:
        self.window_sec = window_sec
        self._entries: deque[tuple[float, float, float]] = deque()  # (ts, price, qty)

    def add(self, price: float, qty: float, ts: float) -> None:
        self._entries.append((ts, price, qty))
        # Remove old entries
        cutoff = ts - self.window_sec
        while self._entries and self._entries[0][0] < cutoff:
            self._entries.popleft()

    @property
    def vwap(self) -> float:
        if not self._entries:
            return 0.0
        pv = sum(p * q for _, p, q in self._entries)
        v = sum(q for _, _, q in self._entries)
        return pv / v if v > 0 else 0.0

    @property
    def total_volume(self) -> float:
        return sum(q for _, _, q in self._entries)


class PriceAggregator:
    """
    Aggregates price data from multiple sources.
    Updated by the data layer as new ticks/oracle prices arrive.
    """

    def __init__(self) -> None:
        self._binance_price: float = 0.0
        self._oracle_price: float = 0.0
        self._oracle_updated_at: float = 0.0

        self._vwap_1m = _VwapAccumulator(window_sec=60)
        self._vwap_5m = _VwapAccumulator(window_sec=300)

    def update_binance(self, price: float, qty: float = 0.0) -> None:
        """Update with a new Binance trade tick."""
        self._binance_price = price
        ts = time.time()
        if qty > 0:
            self._vwap_1m.add(price, qty, ts)
            self._vwap_5m.add(price, qty, ts)

    def update_oracle(self, price: float, updated_at: float) -> None:
        """Update with a new Chainlink oracle price."""
        self._oracle_price = price
        self._oracle_updated_at = updated_at

    def get_aggregated(self) -> AggregatedPrice:
        """Return the current aggregated price view."""
        now = time.time()
        oracle_latency = now - self._oracle_updated_at if self._oracle_updated_at > 0 else 9999.0

        # Consensus: use Binance as primary, fall back to oracle if Binance unavailable
        consensus = self._binance_price if self._binance_price > 0 else self._oracle_price

        return AggregatedPrice(
            binance_price=self._binance_price,
            oracle_price=self._oracle_price,
            oracle_latency_sec=oracle_latency,
            vwap_1m=self._vwap_1m.vwap or self._binance_price,
            vwap_5m=self._vwap_5m.vwap or self._binance_price,
            consensus_price=consensus,
            timestamp=now,
        )

    @property
    def current_price(self) -> float:
        return self._binance_price or self._oracle_price
