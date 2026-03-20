"""
Gamma API — market metadata, token resolution, and price history.

Polymarket's Gamma API provides rich market metadata including:
- Market descriptions, open/close times
- Token IDs for YES/NO tokens
- Historical prices for backtesting
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from core.clock import get_window_ts
from core.config import get_config
from core.logger import get_logger
from data.polymarket_rest import PolymarketRestClient

logger = get_logger(__name__)
cfg = get_config()


@dataclass
class MarketToken:
    """A YES or NO token within a prediction market."""
    token_id: str
    outcome: str          # "YES" or "NO"
    price: float = 0.0
    winner: bool | None = None


@dataclass
class BtcMarket:
    """Metadata for a BTC 5-minute Up/Down prediction market."""
    condition_id: str
    slug: str
    title: str
    window_ts: int          # Unix timestamp of window open
    close_time: float       # When the market resolves
    yes_token: MarketToken
    no_token: MarketToken
    is_active: bool = True

    @property
    def window_slug(self) -> str:
        return f"btc-updown-5m-{self.window_ts}"

    @property
    def seconds_until_close(self) -> float:
        return self.close_time - time.time()

    @property
    def is_resolved(self) -> bool:
        return self.seconds_until_close < 0

    def get_token(self, direction: str) -> MarketToken:
        """Get token by direction ('UP' or 'DOWN')."""
        return self.yes_token if direction == "UP" else self.no_token


class GammaClient:
    """
    Fetches and caches BTC 5-minute market metadata from the Gamma API.
    """

    CACHE_TTL = 60.0    # Cache market data for 60 seconds

    def __init__(self, rest_client: PolymarketRestClient | None = None) -> None:
        self._rest = rest_client
        self._cache: dict[str, tuple[BtcMarket, float]] = {}  # slug → (market, ts)
        self._owns_client = rest_client is None

    async def _get_client(self) -> PolymarketRestClient:
        if self._rest is None:
            self._rest = PolymarketRestClient()
        return self._rest

    async def get_current_market(self) -> BtcMarket | None:
        """
        Get the BTC 5-minute market for the current window.
        Uses deterministic slug: btc-updown-5m-{window_ts}
        """
        window_ts = get_window_ts()
        return await self.get_market_by_window(window_ts)

    async def get_market_by_window(self, window_ts: int) -> BtcMarket | None:
        """Get a market by its window timestamp."""
        slug = f"btc-updown-5m-{window_ts}"
        return await self.get_market_by_slug(slug)

    async def get_market_by_slug(self, slug: str) -> BtcMarket | None:
        """Fetch market metadata by slug, with caching."""
        # Check cache
        cached = self._cache.get(slug)
        if cached:
            market, cached_at = cached
            if time.time() - cached_at < self.CACHE_TTL:
                return market

        client = await self._get_client()
        try:
            raw = await client.get_gamma_market_by_slug(slug)
            if not raw:
                logger.debug(f"No market found for slug: {slug}")
                return None

            market = self._parse_market(raw)
            if market:
                self._cache[slug] = (market, time.time())
            return market

        except Exception as exc:
            logger.error(f"Failed to fetch market {slug}: {exc}")
            return None

    async def get_upcoming_markets(self, count: int = 3) -> list[BtcMarket]:
        """Get current and next N-1 upcoming markets."""
        now = time.time()
        markets = []
        for i in range(count):
            window_ts = get_window_ts(now) + (i * 300)
            market = await self.get_market_by_window(window_ts)
            if market:
                markets.append(market)
        return markets

    def _parse_market(self, raw: dict) -> BtcMarket | None:
        """Parse a raw Gamma API market response into a BtcMarket."""
        import json as _json

        def _parse_json_field(val, default):
            """Parse a field that may be a JSON-encoded string or already a list/dict."""
            if isinstance(val, str):
                try:
                    return _json.loads(val)
                except Exception:
                    return default
            return val if val is not None else default

        try:
            # Try structured token list first (CLOB API format)
            tokens = raw.get("tokens")
            if tokens and isinstance(tokens, list) and len(tokens) >= 2 and isinstance(tokens[0], dict):
                # CLOB format: list of dicts with token_id/price
                yes_raw = tokens[0]
                no_raw = tokens[1]

                def _token(t: dict, outcome: str) -> MarketToken:
                    return MarketToken(
                        token_id=t.get("token_id", t.get("tokenId", "")),
                        outcome=outcome,
                        price=float(t.get("price", 0.5)),
                        winner=t.get("winner"),
                    )

            else:
                # Gamma API format: outcomes/outcomePrices/clobTokenIds are JSON strings
                outcome_names = _parse_json_field(raw.get("outcomes"), ["Up", "Down"])
                outcome_prices = _parse_json_field(raw.get("outcomePrices"), ["0.5", "0.5"])
                clob_token_ids = _parse_json_field(raw.get("clobTokenIds"), ["", ""])

                if len(clob_token_ids) < 2:
                    logger.warning(f"Market {raw.get('slug', '?')} has < 2 token IDs")
                    return None

                # Map "Up" → YES, "Down" → NO
                def _outcome_to_std(name: str) -> str:
                    return "YES" if name.lower() == "up" else "NO"

                def _token_from_gamma(idx: int, outcome: str) -> MarketToken:
                    return MarketToken(
                        token_id=clob_token_ids[idx],
                        outcome=outcome,
                        price=float(outcome_prices[idx]) if idx < len(outcome_prices) else 0.5,
                        winner=None,
                    )

                yes_idx = next(
                    (i for i, n in enumerate(outcome_names) if n.lower() == "up"), 0
                )
                no_idx = 1 - yes_idx

                def _token(t, outcome: str) -> MarketToken:  # unused stub kept for close_time block
                    return MarketToken(token_id="", outcome=outcome)

                yes_raw_token = _token_from_gamma(yes_idx, "YES")
                no_raw_token = _token_from_gamma(no_idx, "NO")
                yes_raw = None  # sentinel: use pre-built tokens below
                no_raw = None

            # Find the window timestamp from the market close time
            close_time_raw = raw.get("endDate", raw.get("end_date", raw.get("close_time", 0)))
            if isinstance(close_time_raw, str):
                from datetime import datetime
                # Handle ISO format
                close_time_raw = close_time_raw.replace("Z", "+00:00")
                try:
                    close_time = datetime.fromisoformat(close_time_raw).timestamp()
                except ValueError:
                    close_time = float(time.time()) + 300
            else:
                close_time = float(close_time_raw)

            window_ts = int(close_time) - 300

            # Resolve final tokens (CLOB branch built dicts; Gamma branch pre-built MarketTokens)
            if yes_raw is None:
                final_yes = yes_raw_token
                final_no = no_raw_token
            else:
                final_yes = _token(yes_raw, "YES")
                final_no = _token(no_raw, "NO")

            return BtcMarket(
                condition_id=raw.get("conditionId", raw.get("condition_id", "")),
                slug=raw.get("slug", ""),
                title=raw.get("title", raw.get("question", "")),
                window_ts=window_ts,
                close_time=close_time,
                yes_token=final_yes,
                no_token=final_no,
                is_active=raw.get("active", True),
            )

        except Exception as exc:
            logger.error(f"Failed to parse market: {exc}, raw={raw}")
            return None

    def clear_cache(self) -> None:
        """Clear all cached market data."""
        self._cache.clear()
