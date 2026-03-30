"""
Token resolver — resolves market slugs to condition IDs and token IDs.

The 5-minute market slug is deterministic: btc-updown-5m-{window_ts}
We resolve this to get the YES and NO token IDs needed for order placement.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from core.clock import get_window_ts
from core.config import get_config
from core.logger import get_logger
from data.gamma_api import GammaClient, BtcMarket
from data.polymarket_rest import PolymarketRestClient

logger = get_logger(__name__)
cfg = get_config()


@dataclass
class ResolvedMarket:
    """A fully resolved market with token IDs ready for trading."""
    condition_id: str
    slug: str
    window_ts: int
    yes_token_id: str
    no_token_id: str
    yes_price: float = 0.5
    no_price: float = 0.5
    close_time: float = 0.0

    def get_token_id(self, direction: str) -> str:
        """Get token ID for a direction ('UP' or 'DOWN')."""
        return self.yes_token_id if direction == "UP" else self.no_token_id

    @property
    def seconds_until_close(self) -> float:
        if self.close_time <= 0:
            return 0.0
        return self.close_time - time.time()


class TokenResolver:
    """
    Resolves the current BTC or ETH 5-minute market to its token IDs.
    Caches resolution to avoid repeated API calls.
    """

    CACHE_TTL = 30.0   # Cache for 30 seconds

    def __init__(
        self,
        gamma_client: GammaClient,
        rest_client: PolymarketRestClient,
        asset: str = "btc",
        clob_keywords: list[str] | None = None,
    ) -> None:
        self._gamma = gamma_client
        self._rest = rest_client
        self._asset = asset.lower()
        self._clob_keywords = clob_keywords
        self._cache: dict[int, tuple[ResolvedMarket, float]] = {}

    async def resolve_current(self) -> ResolvedMarket | None:
        """Resolve the market for the current 5-minute window."""
        window_ts = get_window_ts()
        return await self.resolve_window(window_ts)

    async def resolve_window(self, window_ts: int) -> ResolvedMarket | None:
        """Resolve the market for a specific window timestamp."""
        # Check cache
        cached = self._cache.get(window_ts)
        if cached:
            market, cached_at = cached
            if time.time() - cached_at < self.CACHE_TTL:
                return market

        slug = f"{self._asset}-updown-5m-{window_ts}"

        # Try Gamma API first (has richer metadata)
        market = await self._resolve_via_gamma(slug, window_ts)
        if market:
            self._cache[window_ts] = (market, time.time())
            return market

        # Fallback: search via CLOB API
        market = await self._resolve_via_clob_search(window_ts)
        if market:
            self._cache[window_ts] = (market, time.time())
            return market

        logger.warning(f"Could not resolve market for window {window_ts} ({slug})")
        return None

    async def _resolve_via_gamma(self, slug: str, window_ts: int) -> ResolvedMarket | None:
        """Resolve using the Gamma API."""
        try:
            btc_market = await self._gamma.get_market_by_slug(slug)
            if not btc_market:
                return None

            return ResolvedMarket(
                condition_id=btc_market.condition_id,
                slug=btc_market.slug,
                window_ts=window_ts,
                yes_token_id=btc_market.yes_token.token_id,
                no_token_id=btc_market.no_token.token_id,
                yes_price=btc_market.yes_token.price,
                no_price=btc_market.no_token.price,
                close_time=btc_market.close_time,
            )
        except Exception as exc:
            logger.warning(f"Gamma resolution failed for {slug}: {exc}")
            return None

    async def _resolve_via_clob_search(self, window_ts: int) -> ResolvedMarket | None:
        """Fallback: search CLOB API for active asset markets."""
        asset_keywords = self._clob_keywords or {
            "btc": ["btc", "bitcoin"],
            "eth": ["eth", "ethereum"],
        }.get(self._asset, [self._asset])

        try:
            markets = await self._rest.get_markets(active=True, tag="crypto", limit=50)
            now = time.time()

            for m in markets:
                # Look for asset 5-minute markets closing near our window
                title = m.get("question", m.get("description", "")).lower()
                if not any(kw in title for kw in asset_keywords):
                    continue

                close_time_raw = m.get("end_date_iso", m.get("end_time", 0))
                try:
                    if isinstance(close_time_raw, str):
                        from datetime import datetime
                        close_ts = datetime.fromisoformat(
                            close_time_raw.replace("Z", "+00:00")
                        ).timestamp()
                    else:
                        close_ts = float(close_time_raw)
                except (ValueError, TypeError):
                    continue

                # Match: closes within the expected window range
                expected_close = window_ts + 300
                if abs(close_ts - expected_close) < 60:
                    tokens = m.get("tokens", [])
                    if len(tokens) >= 2:
                        return ResolvedMarket(
                            condition_id=m.get("condition_id", ""),
                            slug=m.get("slug", ""),
                            window_ts=window_ts,
                            yes_token_id=tokens[0].get("token_id", ""),
                            no_token_id=tokens[1].get("token_id", ""),
                            close_time=close_ts,
                        )

        except Exception as exc:
            logger.error(f"CLOB search resolution failed: {exc}")

        return None

    def invalidate(self, window_ts: int | None = None) -> None:
        """Invalidate cache (all or specific window)."""
        if window_ts is not None:
            self._cache.pop(window_ts, None)
        else:
            self._cache.clear()
