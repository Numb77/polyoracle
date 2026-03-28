"""
Polymarket REST API client — CLOB and Gamma API.

Handles market discovery, order book snapshots, trade history,
and position queries via the official CLOB API.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import aiohttp

from core.config import get_config
from core.logger import get_logger

logger = get_logger(__name__)
cfg = get_config()


class PolymarketRestClient:
    """
    Async client for Polymarket CLOB and Gamma REST APIs.
    """

    REQUEST_TIMEOUT = 10.0
    RATE_LIMIT_DELAY = 0.2   # seconds between requests

    def __init__(self) -> None:
        self._clob_url = cfg.polymarket_clob_url
        self._gamma_url = cfg.polymarket_gamma_url
        self._session: aiohttp.ClientSession | None = None
        self._last_request_ts: float = 0.0

    async def __aenter__(self) -> "PolymarketRestClient":
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self.REQUEST_TIMEOUT),
            headers={"Accept": "application/json"},
        )
        return self

    async def __aexit__(self, *args: Any) -> None:
        if self._session:
            await self._session.close()

    # ── Internal HTTP helpers ─────────────────────────────────────────────────

    async def _get(self, base_url: str, path: str, params: dict | None = None) -> Any:
        """Rate-limited GET request."""
        # Enforce rate limit
        now = time.time()
        elapsed = now - self._last_request_ts
        if elapsed < self.RATE_LIMIT_DELAY:
            await asyncio.sleep(self.RATE_LIMIT_DELAY - elapsed)

        url = f"{base_url}{path}"

        if self._session is None:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.REQUEST_TIMEOUT),
            )

        try:
            async with self._session.get(url, params=params) as resp:
                self._last_request_ts = time.time()
                resp.raise_for_status()
                return await resp.json(content_type=None)
        except aiohttp.ClientResponseError as exc:
            if exc.status == 404:
                # 404 is expected in several contexts (market resolved = book closed,
                # market not yet listed, etc.). Callers handle and log appropriately.
                logger.debug(f"HTTP 404 for {url}")
            else:
                logger.error(f"HTTP {exc.status} for {url}: {exc.message}")
            raise
        except aiohttp.ClientError as exc:
            logger.error(f"Request failed for {url}: {exc}")
            raise

    # ── CLOB API ──────────────────────────────────────────────────────────────

    async def get_markets(
        self,
        active: bool = True,
        tag: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        """List markets from the CLOB API."""
        params: dict = {"limit": limit, "offset": offset}
        if active:
            params["active"] = "true"
        if tag:
            params["tag"] = tag
        data = await self._get(self._clob_url, "/markets", params)
        if isinstance(data, dict):
            return data.get("data", [])
        return data or []

    async def get_market(self, condition_id: str) -> dict:
        """Get a specific market by condition ID."""
        return await self._get(self._clob_url, f"/markets/{condition_id}")

    async def get_order_book(self, token_id: str) -> dict:
        """Get order book snapshot for a token."""
        return await self._get(self._clob_url, "/book", {"token_id": token_id})

    async def get_market_winner(self, condition_id: str) -> str | None:
        """
        Check which outcome won for a resolved market.

        Returns 'UP' if the YES token won, 'DOWN' if the NO token won,
        or None if the market has not resolved yet.

        Uses the CLOB /markets/{condition_id} endpoint — each token has a
        `winner` boolean field that Polymarket sets once the market settles.
        """
        data = await self._get(self._clob_url, f"/markets/{condition_id}")
        tokens = data.get("tokens", [])
        for token in tokens:
            if token.get("winner"):
                outcome = token.get("outcome", "").upper()
                if outcome == "YES":
                    return "UP"
                if outcome == "NO":
                    return "DOWN"
        return None  # not yet resolved

    async def get_tick_size(self, token_id: str) -> float:
        """Get minimum price increment for a token."""
        data = await self._get(self._clob_url, "/tick-size", {"token_id": token_id})
        return float(data.get("minimum_tick_size", 0.01))

    async def get_fee_rate(self, token_id: str) -> dict:
        """Get fee rate information for a token."""
        return await self._get(self._clob_url, "/fee-rate", {"token_id": token_id})

    async def get_last_trade_price(self, token_id: str) -> float:
        """Get the last traded price for a token."""
        data = await self._get(
            self._clob_url, "/last-trade-price", {"token_id": token_id}
        )
        return float(data.get("price", 0.0))

    async def get_prices(self, token_ids: list[str]) -> dict[str, float]:
        """Get mid-market prices for multiple tokens."""
        results = {}
        for token_id in token_ids:
            try:
                book = await self.get_order_book(token_id)
                bids = book.get("bids", [])
                asks = book.get("asks", [])
                if bids and asks:
                    best_bid = max(float(b["price"]) for b in bids)
                    best_ask = min(float(a["price"]) for a in asks)
                    results[token_id] = (best_bid + best_ask) / 2
                elif asks:
                    results[token_id] = min(float(a["price"]) for a in asks)
                elif bids:
                    results[token_id] = max(float(b["price"]) for b in bids)
            except Exception as exc:
                logger.warning(f"Failed to get price for {token_id}: {exc}")
        return results

    async def get_server_time(self) -> float:
        """Get Polymarket server time (for clock synchronization)."""
        data = await self._get(self._clob_url, "/time")
        return float(data.get("time", time.time()))

    # ── Gamma API ─────────────────────────────────────────────────────────────

    async def get_gamma_markets(
        self,
        slug: str | None = None,
        active: bool = True,
        limit: int = 20,
        offset: int = 0,
        tag_slug: str | None = None,
    ) -> list[dict]:
        """Search Gamma API for markets."""
        params: dict = {"limit": limit, "offset": offset}
        if slug:
            params["slug"] = slug
        if active:
            params["active"] = "true"
        if tag_slug:
            params["tag_slug"] = tag_slug

        data = await self._get(self._gamma_url, "/markets", params)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return data.get("markets", data.get("data", []))
        return []

    async def get_gamma_market_by_slug(self, slug: str) -> dict | None:
        """Look up a specific market by its slug."""
        markets = await self.get_gamma_markets(slug=slug, limit=1)
        return markets[0] if markets else None

    async def find_btc_5min_markets(self, limit: int = 10) -> list[dict]:
        """Find active BTC 5-minute prediction markets."""
        # Try tag-based search first
        try:
            markets = await self.get_gamma_markets(
                tag_slug="btc-5-minute", active=True, limit=limit
            )
            if markets:
                return markets
        except Exception:
            pass

        # Fallback: search by keywords
        try:
            all_markets = await self.get_gamma_markets(active=True, limit=100)
            btc_5min = [
                m for m in all_markets
                if "btc" in m.get("slug", "").lower()
                and ("5m" in m.get("slug", "").lower() or "5-min" in m.get("title", "").lower())
            ]
            return btc_5min[:limit]
        except Exception as exc:
            logger.error(f"Failed to find BTC 5-min markets: {exc}")
            return []

    async def get_gamma_prices(
        self, condition_id: str, start_ts: int | None = None
    ) -> list[dict]:
        """Get historical price data for a market."""
        params: dict = {"condition_id": condition_id}
        if start_ts:
            params["start_ts"] = start_ts
        return await self._get(self._gamma_url, "/prices-history", params)
