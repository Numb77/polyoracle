"""
Polymarket order executor — places and cancels orders via py-clob-client.

Handles both live trading (real orders) and paper trading (simulated fills).
Uses GTC orders so thin books can fill; unfilled orders are cancelled on window close.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Optional

from core.config import get_config
from core.logger import get_logger
from execution.fee_calculator import FeeCalculator
from execution.order_manager import Order, OrderManager, OrderStatus
from execution.token_resolver import ResolvedMarket
from execution.wallet import Wallet

logger = get_logger(__name__)
cfg = get_config()


class PolymarketExecutor:
    """
    Executes trades on Polymarket.

    Live mode: Uses py-clob-client to place actual orders.
    Paper mode: Simulates orders with realistic assumptions.
    """

    RETRY_INTERVAL = 2.0    # seconds between retries
    MAX_RETRIES = 3

    def __init__(
        self,
        wallet: Wallet,
        order_manager: OrderManager,
        fee_calculator: FeeCalculator,
    ) -> None:
        self._wallet = wallet
        self._orders = order_manager
        self._fees = fee_calculator
        self._is_paper = cfg.paper_mode
        self._clob_client = None

    def _get_clob_client(self):
        """Get or create the CLOB client."""
        if self._clob_client is None and not self._is_paper:
            self._clob_client = self._wallet.get_clob_client()
        return self._clob_client

    async def execute(
        self,
        market: ResolvedMarket,
        direction: str,
        confidence: float,
        position_size_usd: float,
    ) -> Order | None:
        """
        Execute a trade.

        Args:
            market:             Resolved market with token IDs
            direction:          'UP' or 'DOWN'
            confidence:         Confidence score (0-100)
            position_size_usd:  Position size in USDC

        Returns:
            Order if placed, None if skipped/failed
        """
        token_id = market.get_token_id(direction)
        outcome = "YES" if direction == "UP" else "NO"

        # ── Get best ask price ────────────────────────────────────────────────
        best_ask = await self._get_best_ask(token_id, direction, market)
        if best_ask is None:
            logger.warning(f"No ask available for {market.slug} {outcome}")
            return None

        # ── Price validation ──────────────────────────────────────────────────
        if best_ask < cfg.min_token_price:
            logger.info(
                f"Ask too low ({best_ask:.3f} < {cfg.min_token_price}) — "
                f"unusual market, skipping"
            )
            return None

        if best_ask > cfg.max_token_price:
            logger.info(
                f"Ask too high ({best_ask:.3f} > {cfg.max_token_price}) — "
                f"no edge, skipping"
            )
            return None

        # ── Fetch live fee rate (cache for 60s) ──────────────────────────────
        if not self._fees.has_live_rate:
            await self._refresh_fee_rate()

        # ── Fee check ─────────────────────────────────────────────────────────
        fee_est = self._fees.estimate(
            token_price=best_ask,
            notional_usd=position_size_usd,
            edge_pct=0.02,   # Assume ~2% edge
        )
        if not fee_est.is_worth_trading:
            logger.info(
                f"Fee too high ({fee_est.fee_pct:.1%}) — skipping trade"
            )
            return None

        # ── Size calculation ──────────────────────────────────────────────────
        size_shares = position_size_usd / best_ask
        if size_shares < cfg.min_trade_shares:
            logger.info(
                f"Position too small: {size_shares:.1f} shares "
                f"(min: {cfg.min_trade_shares})"
            )
            return None

        # ── Execute ───────────────────────────────────────────────────────────
        if self._is_paper:
            return await self._execute_paper(
                market, direction, outcome, token_id, best_ask,
                position_size_usd, size_shares, fee_est.fee_usd, confidence
            )
        else:
            return await self._execute_live(
                market, direction, outcome, token_id, best_ask,
                position_size_usd, size_shares, fee_est.fee_usd, confidence
            )

    async def _execute_paper(
        self,
        market: ResolvedMarket,
        direction: str,
        outcome: str,
        token_id: str,
        price: float,
        size_usd: float,
        size_shares: float,
        fee_usd: float,
        confidence: float,
    ) -> Order:
        """Simulate order execution in paper trading mode."""
        order_id = f"paper_{uuid.uuid4().hex[:12]}"

        order = Order(
            order_id=order_id,
            market_slug=market.slug,
            condition_id=market.condition_id,
            token_id=token_id,
            direction=direction,
            outcome=outcome,
            price=price,
            size_usd=size_usd,
            size_shares=size_shares,
            fee_usd=fee_usd,
            confidence=confidence,
            status=OrderStatus.FILLED,
            filled_shares=size_shares,
            filled_price=price,
            filled_at=time.time(),
            window_ts=market.window_ts,
            is_paper=True,
        )

        self._orders.add_order(order)
        # Immediately mark as filled in paper mode
        self._orders.mark_filled(order_id, size_shares, price)

        logger.trade(  # type: ignore[attr-defined]
            f"[PAPER] {direction} {outcome} {market.slug[:20]}... "
            f"@ {price:.3f} × {size_shares:.1f} shares = ${size_usd:.2f}"
        )
        return order

    async def _execute_live(
        self,
        market: ResolvedMarket,
        direction: str,
        outcome: str,
        token_id: str,
        price: float,
        size_usd: float,
        size_shares: float,
        fee_usd: float,
        confidence: float,
    ) -> Order | None:
        """
        Place a live FOK (Fill-Or-Kill) market order via py-clob-client.

        FOK is the correct order type for immediate taker execution on Polymarket.
        GTC orders are maker-only — they rest on the book and never cross the spread,
        so a GTC bid above the ask will sit unfilled indefinitely.

        FOK fills at the best available market prices and returns immediately.
        If there is insufficient depth to fill the full amount, FOK is rejected.
        In that case we retry once with a reduced amount (50% of original).
        """
        order_id = f"live_{uuid.uuid4().hex[:12]}"

        order = Order(
            order_id=order_id,
            market_slug=market.slug,
            condition_id=market.condition_id,
            token_id=token_id,
            direction=direction,
            outcome=outcome,
            price=price,
            size_usd=size_usd,
            size_shares=size_shares,
            fee_usd=fee_usd,
            confidence=confidence,
            window_ts=market.window_ts,
            is_paper=False,
        )
        self._orders.add_order(order)

        client = self._get_clob_client()
        if client is None:
            self._orders.mark_cancelled(order_id, "CLOB client unavailable")
            return None

        # Try FOK with full amount, then 50% if depth is insufficient
        amounts_to_try = [round(size_usd, 2), round(size_usd * 0.5, 2)]

        for attempt, amount in enumerate(amounts_to_try):
            if amount < 1.0:
                break
            try:
                result = await asyncio.get_event_loop().run_in_executor(
                    None, self._place_fok_sync, client, token_id, amount
                )

                if result and result.get("orderID"):
                    real_order_id = result["orderID"]
                    filled_shares = round(amount / price, 4)

                    self._orders._active.pop(order_id, None)
                    order.order_id = real_order_id
                    order.size_usd = amount
                    order.filled_shares = filled_shares
                    order.filled_price = price
                    order.filled_at = time.time()
                    order.status = OrderStatus.FILLED
                    self._orders._history.append(order)

                    logger.trade(  # type: ignore[attr-defined]
                        f"[LIVE] {direction} {outcome} {market.slug[:20]}... "
                        f"@ {price:.3f} × {filled_shares:.1f} = ${amount:.2f} "
                        f"[{real_order_id[:12]}...]"
                        + (f" (reduced to {amount:.2f})" if attempt > 0 else "")
                    )
                    return order
                else:
                    logger.warning(f"FOK attempt {attempt+1} returned no orderID: {result}")

            except Exception as exc:
                exc_str = str(exc)
                is_depth = any(k in exc_str.lower() for k in (
                    "couldn't be fully filled", "insufficient", "not enough"
                ))
                is_timeout = "timeout" in exc_str.lower() or "ReadTimeout" in exc_str

                if is_depth and attempt == 0:
                    logger.info(
                        f"FOK insufficient depth at ${amount:.2f} — "
                        f"retrying at ${amounts_to_try[1]:.2f}"
                    )
                    continue
                elif is_timeout:
                    logger.error(f"FOK timeout on attempt {attempt+1}: {exc}")
                else:
                    logger.error(f"FOK failed: {exc}", exc_info=True)
                break

        self._orders.mark_cancelled(order_id, "FOK failed — no fill")
        return None

    def _place_fok_sync(self, client, token_id: str, amount_usd: float) -> dict:
        """
        Synchronous FOK market order placement.
        Called via run_in_executor to avoid blocking the event loop.

        FOK (Fill-Or-Kill) is a taker order that executes immediately against
        resting orders at the best available prices, or is cancelled entirely.

        Precision: maker (USDC amount) must be ≤ 2 decimal places.
        """
        from py_clob_client.clob_types import MarketOrderArgs, OrderType

        args = MarketOrderArgs(
            token_id=token_id,
            amount=round(amount_usd, 2),  # USDC ≤ 2dp
        )
        order = client.create_market_order(args)
        logger.debug(f"FOK order: token={token_id[:16]}... amount=${amount_usd:.2f}")
        return client.post_order(order, OrderType.FOK)

    async def _get_best_ask(
        self,
        token_id: str,
        direction: str,
        market: ResolvedMarket,
    ) -> float | None:
        """
        Get the best ask price for a token.

        Live mode: always fetches from the live CLOB order book so the pre-trade
        price check uses the real current ask, not the (potentially stale) Gamma price.

        Paper mode: uses the Gamma price cached in `market` (no CLOB creds needed).
        """
        if not self._is_paper:
            # Live mode — always use the live order book
            try:
                from data.polymarket_rest import PolymarketRestClient
                async with PolymarketRestClient() as rest:
                    book = await rest.get_order_book(token_id)
                    asks = book.get("asks", [])
                    if asks:
                        return float(asks[0]["price"])
            except Exception as exc:
                logger.warning(f"Failed to get live order book for ask: {exc}")
            # Fall back to Gamma price only if CLOB unreachable
            price = market.yes_price if direction == "UP" else market.no_price
            return price if 0 < price < 1.0 else None

        # Paper mode — Gamma price is good enough
        price = market.yes_price if direction == "UP" else market.no_price
        if price > 0 and price < 1.0:
            return price

        # Paper fallback: REST
        try:
            from data.polymarket_rest import PolymarketRestClient
            async with PolymarketRestClient() as rest:
                book = await rest.get_order_book(token_id)
                asks = book.get("asks", [])
                if asks:
                    return float(asks[0]["price"])
        except Exception as exc:
            logger.warning(f"Failed to get order book for ask: {exc}")

        return None

    async def _refresh_fee_rate(self) -> None:
        """Fetch the current taker fee rate from Polymarket and cache it."""
        try:
            import aiohttp
            url = f"{cfg.polymarket_clob_url}/neg-risk"
            # Polymarket fee-rate endpoint: GET /fee-rate
            fee_url = f"{cfg.polymarket_clob_url}/fee-rate"
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    fee_url, timeout=aiohttp.ClientTimeout(total=5)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        # API returns e.g. {"rate": "0.015"} or {"fee_rate_bps": 150}
                        rate_raw = data.get("fee_rate_bps") or data.get("rate")
                        if rate_raw is not None:
                            bps = int(rate_raw) if float(rate_raw) > 1 else int(float(rate_raw) * 10000)
                            self._fees.update_rate(bps)
                            return
        except Exception as exc:
            logger.debug(f"Fee rate fetch failed, using cached/default: {exc}")

    async def cancel_all_open(self) -> int:
        """Cancel all open orders. Returns count of cancelled orders."""
        active = self._orders.get_active_orders()
        cancelled = 0

        for order in active:
            if order.is_paper:
                self._orders.mark_cancelled(order.order_id, "Manual cancel")
                cancelled += 1
            else:
                try:
                    client = self._get_clob_client()
                    if client:
                        await asyncio.get_event_loop().run_in_executor(
                            None, client.cancel, order.order_id
                        )
                    self._orders.mark_cancelled(order.order_id, "Manual cancel")
                    cancelled += 1
                except Exception as exc:
                    logger.error(f"Failed to cancel order {order.order_id}: {exc}")

        if cancelled:
            logger.info(f"Cancelled {cancelled} open orders")
        return cancelled
