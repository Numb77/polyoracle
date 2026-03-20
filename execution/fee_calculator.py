"""
Polymarket taker fee calculator.

Dynamic taker fees on prediction markets depend on the token price.
Fees are highest at p=0.50 (~3.15%) and drop toward extremes.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass

from core.logger import get_logger

logger = get_logger(__name__)


@dataclass
class FeeEstimate:
    """Estimated trading fees for a position."""
    token_price: float          # Entry price (0-1)
    fee_rate_bps: int           # Fee rate in basis points
    fee_pct: float              # Fee as % of notional
    fee_usd: float              # Fee in USDC for the given notional
    notional_usd: float         # Position size in USDC
    net_edge_pct: float         # Expected edge minus fee
    is_worth_trading: bool      # True if edge > fee

    def to_dict(self) -> dict:
        return {
            "token_price": self.token_price,
            "fee_rate_bps": self.fee_rate_bps,
            "fee_pct": round(self.fee_pct, 4),
            "fee_usd": round(self.fee_usd, 4),
            "is_worth_trading": self.is_worth_trading,
        }


class FeeCalculator:
    """
    Calculates taker fees using Polymarket's dynamic fee formula.

    Fee formula (simplified):
        fee_pct = C × fee_rate × p × (1 - p)
    Where:
        C = constant scaling factor (~1 for standard markets)
        fee_rate = rate from API (e.g., 150 bps = 1.5%)
        p = token price (0-1)

    At p=0.50: fee ≈ C × rate × 0.25 (maximum)
    At p=0.85: fee ≈ C × rate × 0.1275 (reduced)
    At p=0.95: fee ≈ C × rate × 0.0475 (very low)

    NOTE: Always query the actual fee rate from the API before trading.
    The formula is an approximation; the API gives the definitive rate.
    """

    # Default fee rate (bps) when API not available
    DEFAULT_FEE_RATE_BPS = 150   # 1.5% = 150 bps
    CACHE_TTL_SEC = 60           # Cached rate expires after 60 seconds

    def __init__(self) -> None:
        self._cached_rate_bps: int | None = None
        self._cache_ts: float = 0.0

    def update_rate(self, fee_rate_bps: int) -> None:
        """
        Update the cached fee rate. Call this after querying the Polymarket API
        before each trade so estimates use the live rate instead of the default.
        """
        self._cached_rate_bps = fee_rate_bps
        self._cache_ts = time.monotonic()
        logger.debug(f"Fee rate updated: {fee_rate_bps} bps")

    def _get_rate(self, fee_rate_bps: int | None) -> int:
        """Resolve the fee rate to use, preferring fresh cached rate."""
        if fee_rate_bps is not None:
            return fee_rate_bps
        if (
            self._cached_rate_bps is not None
            and time.monotonic() - self._cache_ts < self.CACHE_TTL_SEC
        ):
            return self._cached_rate_bps
        logger.debug(
            f"No fresh fee rate available — using default {self.DEFAULT_FEE_RATE_BPS} bps"
        )
        return self.DEFAULT_FEE_RATE_BPS

    def estimate(
        self,
        token_price: float,
        notional_usd: float,
        fee_rate_bps: int | None = None,
        edge_pct: float = 0.0,
    ) -> FeeEstimate:
        """
        Estimate the fee for a trade.

        Args:
            token_price:    Token price (0.0 to 1.0)
            notional_usd:   Trade size in USDC
            fee_rate_bps:   Taker fee rate in basis points (from API)
            edge_pct:       Expected edge (e.g., 0.05 = 5% edge)
        """
        fee_rate_bps = self._get_rate(fee_rate_bps)

        rate = fee_rate_bps / 10000.0   # Convert bps to decimal

        # Dynamic fee formula: fee_pct = rate × p × (1 - p) × 4
        # Multiplied by 4 to normalize: at p=0.5, p(1-p)=0.25, × 4 = 1.0
        fee_pct = rate * token_price * (1 - token_price) * 4
        fee_usd = fee_pct * notional_usd

        # Minimum fee floor
        fee_usd = max(fee_usd, 0.0)

        # Expected edge on a binary prediction:
        # If buying at price p, win probability = 1 - p (market is efficient)
        # But our strategy has an edge: actual win prob > p
        # The edge needs to exceed the fee to be profitable
        net_edge_pct = edge_pct - fee_pct

        is_worth_trading = (
            net_edge_pct > 0
            and fee_pct < 0.05   # Never pay more than 5% fee
        )

        return FeeEstimate(
            token_price=token_price,
            fee_rate_bps=fee_rate_bps,
            fee_pct=fee_pct,
            fee_usd=fee_usd,
            notional_usd=notional_usd,
            net_edge_pct=net_edge_pct,
            is_worth_trading=is_worth_trading,
        )

    def is_price_worth_trading(
        self, token_price: float, fee_rate_bps: int | None = None
    ) -> bool:
        """Quick check: is this price level worth trading given fees?"""
        if token_price < 0.55 or token_price > 0.95:
            return False   # Outside our price range
        est = self.estimate(token_price, 10.0, fee_rate_bps, edge_pct=0.02)
        return est.fee_pct < 0.03   # Fee under 3% is acceptable

    def get_fee_at_midpoint(self, fee_rate_bps: int | None = None) -> float:
        """Fee at p=0.50 (maximum fee point)."""
        rate = self._get_rate(fee_rate_bps) / 10000.0
        return rate * 0.5 * 0.5 * 4  # = rate × 1.0

    @property
    def has_live_rate(self) -> bool:
        """True if a fresh API rate is cached and not expired."""
        return (
            self._cached_rate_bps is not None
            and time.monotonic() - self._cache_ts < self.CACHE_TTL_SEC
        )

    def log_fee_analysis(self, token_price: float, trade_usd: float) -> None:
        """Log a fee breakdown for a proposed trade."""
        est = self.estimate(token_price, trade_usd)
        logger.debug(
            f"Fee analysis: price={token_price:.3f}, "
            f"notional=${trade_usd:.2f}, "
            f"fee={est.fee_pct:.2%} (${est.fee_usd:.4f}), "
            f"{'TRADE' if est.is_worth_trading else 'SKIP'}"
        )
