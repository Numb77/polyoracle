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
        # Guards against concurrent fill-poll tasks spawning duplicate reprice ops.
        # _poll_running: only one check_and_update_fills runs at a time per executor.
        # _repricing: set of order_ids currently being cancelled+replaced; skip if seen again.
        self._poll_running = False
        self._repricing: set[str] = set()

    @staticmethod
    def _compute_fill_vwap(asks: list, size_usd: float) -> tuple[float, float]:
        """
        Compute the volume-weighted average price (VWAP) a FOK market order would
        achieve when sweeping the ask side of the book for a given USD spend.

        Returns (vwap, total_available_usd).

        - vwap: average price per share across all levels consumed by this order.
          Compared against max_fair_ask to detect when sweeping the book loses edge.
        - total_available_usd: total USDC value of all asks at or below the sweep.
          Used to detect thin books where a FOK would only partially fill.
        """
        remaining = size_usd
        total_shares = 0.0
        total_depth_usd = 0.0
        for ask in sorted(asks, key=lambda a: float(a["price"])):
            price = float(ask["price"])
            avail_shares = float(ask.get("size", ask.get("quantity", 0)))
            avail_usd = price * avail_shares
            total_depth_usd += avail_usd
            if remaining > 0:
                fill_usd = min(remaining, avail_usd)
                total_shares += fill_usd / price
                remaining -= fill_usd
        if total_shares == 0:
            return 1.0, 0.0
        actual_spent = size_usd - remaining
        return actual_spent / total_shares, total_depth_usd

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

        # Edge-based ceiling: only trade when confidence exceeds market-implied
        # probability by at least min_trade_edge. If the market is already pricing
        # in 85% probability and our confidence is only 82%, there is no edge.
        # Hard ceiling (max_token_price) remains as an absolute safety net.
        max_fair_ask = min(confidence / 100.0 - cfg.min_trade_edge, cfg.max_token_price)
        if best_ask > max_fair_ask:
            logger.info(
                f"No edge: ask={best_ask:.3f} vs conf={confidence:.0f}% "
                f"(max fair={max_fair_ask:.3f}) — market has priced in the move"
            )
            return None

        # ── Fetch live fee rate (cache for 60s) ──────────────────────────────
        if not self._fees.has_live_rate:
            await self._refresh_fee_rate()

        # ── Fee check ─────────────────────────────────────────────────────────
        # Edge = how much our model's implied win probability (confidence/100)
        # exceeds the market price (best_ask).  Negative edge means the market
        # has already priced in our view; we only trade when edge > fee.
        implied_edge = max(0.0, confidence / 100.0 - best_ask)
        fee_est = self._fees.estimate(
            token_price=best_ask,
            notional_usd=position_size_usd,
            edge_pct=implied_edge,
        )
        if not fee_est.is_worth_trading:
            logger.info(
                f"Not worth trading: fee={fee_est.fee_pct:.1%} ≥ edge={implied_edge:.1%} "
                f"(ask={best_ask:.3f}, conf={confidence:.0f}) — skip"
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

        # ── Book depth + VWAP check (live FOK only) ──────────────────────────
        # best_ask is the TOP of the book. A FOK sweeps through multiple levels,
        # so the actual average fill price (VWAP) is always >= best_ask.
        # If the book is too thin or the VWAP sweeps past max_fair_ask, skip —
        # we'd be paying more per share than our edge allows, and the win payout
        # (actual_shares × $1) might not cover what we spent.
        if not self._is_paper:
            try:
                from data.polymarket_rest import PolymarketRestClient
                async with PolymarketRestClient() as rest:
                    book = await rest.get_order_book(token_id)
                asks = book.get("asks", [])
                if asks:
                    vwap, total_depth_usd = self._compute_fill_vwap(asks, position_size_usd)
                    if total_depth_usd < position_size_usd:
                        logger.info(
                            f"Insufficient depth: ${total_depth_usd:.2f} available "
                            f"< ${position_size_usd:.2f} order size — skip"
                        )
                        return None
                    if vwap > max_fair_ask:
                        logger.info(
                            f"Sweep VWAP={vwap:.3f} > max fair={max_fair_ask:.3f} "
                            f"— filling full order loses edge, skip"
                        )
                        return None
            except Exception as exc:
                logger.debug(f"FOK depth/VWAP check failed (non-blocking): {exc}")

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
                result = await self._post_order_with_retry(
                    self._place_fok_sync, client, token_id, amount
                )

                if result and result.get("orderID"):
                    real_order_id = result["orderID"]
                    # Prefer the actual matched size from the API response.
                    # Polymarket returns size_matched (or takerAmount in some versions)
                    # reflecting the true shares received after sweeping the book.
                    # Fall back to the estimate (amount / price) if not present.
                    raw_matched = (
                        result.get("size_matched")
                        or result.get("sizeMatched")
                        or result.get("takerAmount")
                    )
                    if raw_matched:
                        filled_shares = round(float(raw_matched), 4)
                    else:
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
                exc_str = str(exc).lower()
                is_no_liquidity = any(k in exc_str for k in (
                    "no match", "no asks", "book.asks is none",
                ))
                is_depth = any(k in exc_str for k in (
                    "couldn't be fully filled", "insufficient", "not enough",
                    "fully filled or killed", "fok", "400",
                ))
                is_timeout = "timeout" in exc_str or "readtimeout" in exc_str
                if is_no_liquidity:
                    # Empty order book — retrying won't help
                    logger.info("FOK skipped — no asks in order book (illiquid market)")
                    break
                elif is_depth and attempt == 0:
                    logger.info(
                        f"FOK insufficient depth at ${amount:.2f} — "
                        f"retrying at ${amounts_to_try[1]:.2f}"
                    )
                    continue
                elif is_depth:
                    # Second attempt FOK kill — expected, not an error
                    logger.info(
                        f"FOK killed at reduced ${amount:.2f} — insufficient depth, skipping"
                    )
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
        Side is always BUY — we take a position by purchasing YES or NO tokens.
        """
        from py_clob_client.clob_types import MarketOrderArgs, OrderType

        args = MarketOrderArgs(
            token_id=token_id,
            amount=round(amount_usd, 2),  # USDC ≤ 2dp
            side="BUY",
        )
        order = client.create_market_order(args)
        logger.debug(f"FOK order: token={token_id[:16]}... amount=${amount_usd:.2f}")
        return client.post_order(order, OrderType.FOK)

    async def execute_gtc(
        self,
        market: ResolvedMarket,
        direction: str,
        confidence: float,
        position_size_usd: float,
        bid_price: float,
    ) -> Order | None:
        """
        Place a GTC (Good-Till-Cancelled) maker limit order.

        bid_price is the caller's fair-value estimate (confidence / 100).
        We then fetch the live order book and place our bid at:
            min(best_ask - 0.01, bid_price)   capped at max_token_price

        This makes the order competitive — sitting just below the current ask
        so counter-parties crossing the spread hit us first.  If the ask is
        already above our fair-value estimate, we bid at our estimate anyway
        (no point in over-paying to maker-fill a mis-priced market).

        Unfilled orders are cancelled on window close via cancel_all_open().
        """
        token_id = market.get_token_id(direction)
        outcome = "YES" if direction == "UP" else "NO"

        # Fetch live ask so our bid is competitive in the current book
        best_ask = await self._get_best_ask(token_id, direction, market)
        if best_ask is not None and best_ask > 0:
            fair_value = round(confidence / 100.0, 2)
            if best_ask <= fair_value:
                # Ask is within our fair-value estimate — bid AT the ask.
                # This crosses the spread and fills immediately (marketable limit).
                bid_price = best_ask
                logger.debug(
                    f"GTC aggressive: ask={best_ask:.3f} ≤ fair={fair_value:.3f} "
                    f"→ bid AT ask for immediate fill"
                )
            else:
                # Ask is above our estimate — bid just below, rest on book,
                # hope for a pullback. Spread check below will skip if gap > 10¢.
                market_derived_bid = round(best_ask - 0.01, 2)
                bid_price = min(market_derived_bid, bid_price)
                logger.debug(
                    f"GTC passive: ask={best_ask:.3f} > fair={fair_value:.3f} "
                    f"→ bid={bid_price:.3f} (resting below ask)"
                )
        # else: no live ask available — use caller's fair-value estimate as-is

        # Snap bid to 0.01 tick size, ensure sensible range
        bid_price = round(round(bid_price / 0.01) * 0.01, 2)
        bid_price = max(0.51, min(0.98, bid_price))

        # Skip if our bid is too far below the current ask — the order will
        # rest the entire window unfilled (market has priced in the move).
        # Widened to 0.18: early-window books naturally have wide spreads
        # (ask=0.65, bid=0.52 is a 0.13 gap but the order will fill as sellers
        # reprice downward if direction reverses). 0.20+ still means no edge.
        MAX_GTC_SPREAD = 0.18
        if best_ask is not None and best_ask > 0 and (best_ask - bid_price) > MAX_GTC_SPREAD:
            logger.info(
                f"GTC spread too wide: ask={best_ask:.3f} bid={bid_price:.3f} "
                f"(gap={best_ask - bid_price:.2f} > {MAX_GTC_SPREAD}) — skip"
            )
            return None

        # Sanity: don't bid above max_token_price
        if bid_price > cfg.max_token_price:
            logger.info(
                f"GTC bid {bid_price:.2f} > max_token_price {cfg.max_token_price} — skip"
            )
            return None

        # Fee check at bid price.
        # Edge = fair_value - bid_price (how much cheaper we're buying vs our estimate).
        if not self._fees.has_live_rate:
            await self._refresh_fee_rate()
        fair_value = confidence / 100.0
        gtc_edge = max(0.0, fair_value - bid_price)

        # Distinguish resting bids from aggressive (crossing) bids:
        #   Resting  (bid < ask):  fills as MAKER → 0% fee on Polymarket.
        #                          Any non-negative edge is profitable.
        #   Aggressive (bid ≥ ask): fills as TAKER → ~3% fee.
        #                          Must satisfy the normal fee check.
        is_resting = best_ask is not None and bid_price < best_ask
        if not is_resting:
            # Aggressive (taker) bid — apply real fee check
            fee_est = self._fees.estimate(
                token_price=bid_price,
                notional_usd=position_size_usd,
                edge_pct=gtc_edge,
            )
            if not fee_est.is_worth_trading:
                logger.info(
                    f"GTC not worth trading: fee={fee_est.fee_pct:.1%} ≥ edge={gtc_edge:.1%} "
                    f"(bid={bid_price:.3f}, conf={confidence:.0f}) — skip"
                )
                return None
        else:
            # Resting maker bid — fee is 0%, spread check already guards dead orders.
            fee_est = self._fees.estimate(
                token_price=bid_price,
                notional_usd=position_size_usd,
                fee_rate_bps=0,
                edge_pct=gtc_edge,
            )

        # Size in shares
        size_shares = position_size_usd / bid_price
        if size_shares < cfg.min_trade_shares:
            logger.info(
                f"GTC too small: {size_shares:.1f} shares (min {cfg.min_trade_shares})"
            )
            return None

        if self._is_paper:
            return await self._execute_paper_gtc(
                market, direction, outcome, token_id, bid_price,
                position_size_usd, size_shares, fee_est.fee_usd, confidence,
            )
        else:
            return await self._execute_live_gtc(
                market, direction, outcome, token_id, bid_price,
                position_size_usd, size_shares, fee_est.fee_usd, confidence,
            )

    async def _execute_paper_gtc(
        self,
        market: ResolvedMarket,
        direction: str,
        outcome: str,
        token_id: str,
        bid_price: float,
        size_usd: float,
        size_shares: float,
        fee_usd: float,
        confidence: float,
    ) -> Order:
        """
        Simulate a GTC fill in paper mode.

        We assume the bid fills immediately at bid_price — optimistic but gives
        useful P&L data when the signal is correct.  The key difference from FOK
        paper fills is that bid_price < current_ask, so paper P&L reflects the
        better entry cost basis.
        """
        order_id = f"paper_gtc_{uuid.uuid4().hex[:12]}"
        order = Order(
            order_id=order_id,
            market_slug=market.slug,
            condition_id=market.condition_id,
            token_id=token_id,
            direction=direction,
            outcome=outcome,
            price=bid_price,
            size_usd=size_usd,
            size_shares=size_shares,
            fee_usd=fee_usd,
            confidence=confidence,
            status=OrderStatus.FILLED,
            filled_shares=size_shares,
            filled_price=bid_price,
            filled_at=time.time(),
            window_ts=market.window_ts,
            is_paper=True,
        )
        self._orders.add_order(order)
        self._orders.mark_filled(order_id, size_shares, bid_price)

        logger.trade(  # type: ignore[attr-defined]
            f"[PAPER-GTC] {direction} {outcome} {market.slug[:20]}... "
            f"@ bid={bid_price:.3f} × {size_shares:.1f} shares = ${size_usd:.2f}"
        )
        return order

    async def _execute_live_gtc(
        self,
        market: ResolvedMarket,
        direction: str,
        outcome: str,
        token_id: str,
        bid_price: float,
        size_usd: float,
        size_shares: float,
        fee_usd: float,
        confidence: float,
    ) -> Order | None:
        """
        Place a live GTC limit (maker) order.

        The order rests on the book at bid_price and fills if a counter-party
        crosses it.  On window close, cancel_all_open() will cancel it if unfilled.
        """
        order_id = f"live_gtc_{uuid.uuid4().hex[:12]}"
        order = Order(
            order_id=order_id,
            market_slug=market.slug,
            condition_id=market.condition_id,
            token_id=token_id,
            direction=direction,
            outcome=outcome,
            price=bid_price,
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

        try:
            result = await self._post_order_with_retry(
                self._place_gtc_sync, client, token_id, bid_price, size_shares
            )
            if result and result.get("orderID"):
                real_order_id = result["orderID"]
                # Update the order record with the exchange-assigned ID
                self._orders._active.pop(order_id, None)
                order.order_id = real_order_id
                self._orders._active[real_order_id] = order

                logger.trade(  # type: ignore[attr-defined]
                    f"[LIVE-GTC] {direction} {outcome} {market.slug[:20]}... "
                    f"@ bid={bid_price:.3f} × {size_shares:.1f} shares = ${size_usd:.2f} "
                    f"[{real_order_id[:12]}...] (resting — awaiting fill)"
                )
                return order
            else:
                logger.warning(f"GTC order returned no orderID: {result}")
        except Exception as exc:
            logger.error(f"GTC order failed: {exc}", exc_info=True)

        self._orders.mark_cancelled(order_id, "GTC placement failed")
        return None

    async def _post_order_with_retry(self, executor_fn, *args, max_425_retries: int = 4) -> dict:
        """
        Call a synchronous CLOB order function via run_in_executor, retrying on HTTP 425.

        HTTP 425 ("Too Early" / "service not ready") is returned by Polymarket when
        the CLOB backend is temporarily unavailable or rate-limiting the connection.
        It is transient — retrying with exponential backoff (1 → 2 → 4 → 8s) reliably
        succeeds once the server is ready.  Any other exception is re-raised immediately
        so callers can handle it (no-liquidity, depth-insufficient, timeout, etc).
        """
        for attempt in range(max_425_retries + 1):
            try:
                return await asyncio.get_event_loop().run_in_executor(None, executor_fn, *args)
            except Exception as exc:
                if "425" in str(exc) and attempt < max_425_retries:
                    delay = 1 << attempt  # 1, 2, 4, 8s
                    logger.debug(
                        f"CLOB returned HTTP 425 (service not ready) — "
                        f"retry {attempt + 1}/{max_425_retries} in {delay}s"
                    )
                    await asyncio.sleep(delay)
                    continue
                raise  # non-425 or retries exhausted

    def _place_gtc_sync(
        self, client, token_id: str, bid_price: float, size_shares: float
    ) -> dict:
        """
        Synchronous GTC limit order placement.
        Called via run_in_executor to avoid blocking the event loop.

        Side is always BUY — we take a long position by purchasing YES or NO tokens.
        Price must match the market's tick size (0.01 on most Polymarket markets).
        """
        from py_clob_client.clob_types import OrderArgs, OrderType

        args = OrderArgs(
            token_id=token_id,
            price=bid_price,
            size=round(size_shares, 4),
            side="BUY",
        )
        order = client.create_order(args)
        logger.debug(
            f"GTC order: token={token_id[:16]}... "
            f"bid={bid_price:.3f} × {size_shares:.1f} shares"
        )
        return client.post_order(order, OrderType.GTC)

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
                        best = min(float(a["price"]) for a in asks)
                        logger.debug(
                            f"Order book {token_id[:16]}... "
                            f"asks={len(asks)} first={asks[0]['price']} best={best:.3f}"
                        )
                        return best
            except Exception as exc:
                logger.warning(f"Failed to get live order book for ask: {exc}")
            # Fall back to Gamma price only if CLOB unreachable
            price = market.yes_price if direction == "UP" else market.no_price
            return price if 0 < price < 1.0 else None

        # Paper mode — use live REST ask so paper P&L reflects real market prices.
        # Gamma price can be 30-60s stale which would make paper trades look
        # better than they are (executing at 0.78 when real ask is 0.92).
        try:
            from data.polymarket_rest import PolymarketRestClient
            async with PolymarketRestClient() as rest:
                book = await rest.get_order_book(token_id)
                asks = book.get("asks", [])
                if asks:
                    best = min(float(a["price"]) for a in asks)
                    logger.debug(
                        f"Order book {token_id[:16]}... "
                        f"asks={len(asks)} first={asks[0]['price']} best={best:.3f}"
                    )
                    return best
        except Exception as exc:
            logger.debug(f"REST ask unavailable, falling back to Gamma: {exc}")

        # Gamma fallback only if REST unreachable
        price = market.yes_price if direction == "UP" else market.no_price
        return price if 0 < price < 1.0 else None

    async def _refresh_fee_rate(self) -> None:
        """Fetch the current taker fee rate from Polymarket and cache it."""
        try:
            import aiohttp
            fee_url = f"{cfg.polymarket_clob_url}/fee-rate"
            async with aiohttp.ClientSession() as session, session.get(
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

    async def check_and_update_fills(self) -> tuple[list[str], list[str]]:
        """
        Poll active live orders for fill status.  Call this periodically so the
        bot knows about fills before window close (and logs progress).

        Returns (newly_filled_ids, newly_cancelled_ids).
        Cancelled IDs are orders dropped mid-poll (no-edge cancel or reprice
        cancel where the replacement failed) — caller must push trade_cancelled
        to the dashboard so the UI removes them from activePositions.

        Guard: if a previous call is still in-flight (async I/O takes longer than
        the 5s poll interval), skip this call entirely.  Without this guard,
        concurrent calls each reprice the same order → multiple orders placed.
        """
        if self._poll_running:
            return [], []
        self._poll_running = True
        newly_filled: list[str] = []
        newly_cancelled: list[str] = []
        client = self._get_clob_client()
        if client is None:
            self._poll_running = False
            return [], []
        try:

            for order in list(self._orders.get_active_orders()):
                if order.is_paper:
                    continue
                try:
                    status = await asyncio.get_event_loop().run_in_executor(
                        None, client.get_order, order.order_id
                    )
                    if not status:
                        continue
                    order_status = status.get("status", "")
                    filled_price = float(status.get("price", order.price))
                    raw_matched = (
                        status.get("size_matched")
                        or status.get("sizeMatched")
                        or "0"
                    )
                    size_matched_poll = float(raw_matched) if raw_matched else 0.0

                    if order_status == "MATCHED":
                        # Use size_matched_poll (already parsed above) rather than
                        # falling back to order.size_shares — an absent size_matched
                        # field should not silently record the full intended quantity.
                        size_matched = size_matched_poll if size_matched_poll > 0 else order.size_shares
                        self._orders.mark_filled(order.order_id, size_matched, filled_price)
                        logger.trade(  # type: ignore[attr-defined]
                            f"[GTC FILLED] {order.direction} {order.outcome} "
                            f"{order.market_slug[:20]}... @ {filled_price:.3f} "
                            f"× {size_matched:.1f} shares"
                        )
                        newly_filled.append(order.order_id)
                    elif size_matched_poll > 0:
                        # Partially filled — accept what we have, do NOT reprice.
                        # Repricing after a partial fill would place a second full-size
                        # order on top of the existing partial, doubling our exposure.
                        self._orders.mark_filled(order.order_id, size_matched_poll, filled_price)
                        logger.trade(  # type: ignore[attr-defined]
                            f"[GTC PARTIAL] {order.direction} {order.outcome} "
                            f"{order.market_slug[:20]}... @ {filled_price:.3f} "
                            f"× {size_matched_poll:.1f} shares (partial, skip reprice)"
                        )
                        newly_filled.append(order.order_id)
                    else:
                        logger.debug(
                            f"GTC {order.order_id[:12]}... still {order_status} "
                            f"({order.direction} @ {order.price:.3f})"
                        )
                        # ── Dynamic repricing ──────────────────────────────────────
                        # If the best ask has moved since we placed the order,
                        # cancel and re-place at the new price so we stay competitive.
                        # Returns True=cancelled, None=fill detected mid-reprice, False=no action.
                        reprice_result = await self._reprice_gtc_if_needed(order, client)
                        if reprice_result is True:
                            newly_cancelled.append(order.order_id)
                        elif reprice_result is None:
                            # Fill raced between the poll and the pre-cancel check.
                            # Treat as a normal fill so the caller records it.
                            newly_filled.append(order.order_id)
                except Exception as exc:
                    logger.debug(f"Fill poll failed for {order.order_id[:12]}...: {exc}")

        except Exception as exc:
            logger.debug(f"check_and_update_fills error: {exc}")
        finally:
            self._poll_running = False

        return newly_filled, newly_cancelled

    async def refresh_gtc_fill(self, order) -> bool:
        """
        Re-query the CLOB for the latest fill on a GTC order and update the Order
        object in-place.  Call this at window close before computing P&L so that
        fills accumulated after the initial poll are captured.

        Returns True if the fill data changed, False if unchanged or unavailable.
        """
        if self._is_paper or not order.order_id:
            return False
        try:
            client = self._get_clob_client()
            if client is None:
                return False
            loop = asyncio.get_event_loop()
            status = await loop.run_in_executor(None, client.get_order, order.order_id)
            if not status:
                return False
            raw = status.get("size_matched") or status.get("sizeMatched") or "0"
            latest_shares = float(raw) if raw else 0.0
            if latest_shares <= 0:
                return False
            latest_price = float(status.get("price", order.filled_price or order.price))
            if abs(latest_shares - order.filled_shares) < 0.001:
                return False  # No change
            logger.debug(
                f"GTC fill refresh {order.order_id[:12]}...: "
                f"{order.filled_shares:.4f} → {latest_shares:.4f} shares @ {latest_price:.4f}"
            )
            self._orders.mark_filled(order.order_id, latest_shares, latest_price)
            return True
        except Exception as exc:
            logger.debug(f"refresh_gtc_fill {order.order_id[:12]}...: {exc}")
            return False

    async def _reprice_gtc_if_needed(self, order, client) -> bool | None:
        """
        Cancel and re-place a GTC order if the ask has moved enough to warrant
        a better bid price.  Keeps the order competitive without human intervention.

        Rules:
          - Hard cap: never bid above confidence/100. Above that price the trade
            has negative expected value (paying more than our probability estimate).
            If the ask has risen past our confidence cap, cancel and walk away —
            the FOK path will handle the deadline if there's still time.
          - Bid AT the ask when ask ≤ confidence_cap (we have edge → fill immediately)
          - Bid ask-0.01 when ask above our original entry but ≤ confidence_cap
          - Do nothing if new bid == current bid (already optimal)

        Guard: if this order_id is already being repriced (cancel+replace in-flight),
        skip silently.  Without this a second concurrent poll cycle could see the
        same order before mark_cancelled() runs and launch a second reprice.
        """
        if order.order_id in self._repricing:
            return False
        self._repricing.add(order.order_id)
        cancelled_permanently = False
        try:
            loop = asyncio.get_event_loop()

            # Safety check: re-query the order before cancelling.
            # Between the fill-poll GET and now, the order may have partially filled.
            # A partial fill + new full-size order = double exposure.
            pre_cancel_status = await loop.run_in_executor(
                None, client.get_order, order.order_id
            )
            if pre_cancel_status:
                raw = (
                    pre_cancel_status.get("size_matched")
                    or pre_cancel_status.get("sizeMatched")
                    or "0"
                )
                pre_matched = float(raw) if raw else 0.0
                if pre_matched > 0:
                    # Fill raced between poll and cancel — accept it, no reprice.
                    fp = float(pre_cancel_status.get("price", order.price))
                    self._orders.mark_filled(order.order_id, pre_matched, fp)
                    logger.info(
                        f"GTC {order.order_id[:12]}... pre-cancel fill detected "
                        f"({pre_matched:.1f} shares @ {fp:.3f}) — skip reprice"
                    )
                    return None  # Signals caller to add to newly_filled

            # Fetch current ask directly via REST order book
            book = await loop.run_in_executor(
                None, client.get_order_book, order.token_id
            )
            if not book or not book.asks:
                return False

            new_ask = round(min(float(a.price) for a in book.asks), 2)
            if new_ask <= 0:
                return False

            # Hard cap: confidence/100 is the maximum we should ever pay.
            # Above this price the EV is negative regardless of fill speed.
            confidence_cap = round(order.confidence / 100, 2)

            if new_ask > confidence_cap:
                # Ask has risen past our confidence cap, but the existing bid
                # (order.price) is still well below cap and has positive EV.
                # Skip repricing — don't touch the bid. It may fill if the market
                # pulls back. cancel_all_open() handles cleanup at window close.
                logger.debug(
                    f"GTC ask {new_ask:.3f} > cap {confidence_cap:.3f} "
                    f"— skipping reprice, keeping bid at {order.price:.3f}"
                )
                return False

            if new_ask <= order.price:
                return False  # Ask came back down or hasn't moved — order is still competitive

            # Ask has risen but is still within our edge threshold.
            # Reprice to new_ask (cross spread) for immediate fill.
            new_bid = new_ask
            new_bid = round(round(new_bid / 0.01) * 0.01, 2)

            if abs(new_bid - order.price) < 0.01:
                return False  # Already at optimal price — no change needed

            # Cancel old order
            await asyncio.get_event_loop().run_in_executor(
                None, client.cancel, order.order_id
            )
            self._orders.mark_cancelled(order.order_id, "Repriced")

            # Re-place at new price (same size_usd, recalculate shares)
            new_size_shares = round(order.size_usd / new_bid, 4)
            new_order = await self._execute_live_gtc(
                type("_Market", (), {
                    "slug": order.market_slug,
                    "condition_id": order.condition_id,
                    "window_ts": order.window_ts,
                    "get_token_id": lambda _d, _ti=order.token_id: _ti,
                })(),
                order.direction,
                order.outcome,
                order.token_id,
                new_bid,
                order.size_usd,
                new_size_shares,
                order.fee_usd,
                order.confidence,
            )
            if new_order:
                logger.info(
                    f"GTC repriced: {order.market_slug[:20]}... "
                    f"{order.price:.3f} → {new_bid:.3f} (ask={new_ask:.3f})"
                )
            else:
                # Replacement failed — old order is already cancelled. Signal to
                # caller so it can push trade_cancelled to the dashboard.
                cancelled_permanently = True
            return cancelled_permanently
        except Exception as exc:
            logger.debug(f"GTC reprice check failed for {order.order_id[:12]}...: {exc}")
            return False
        finally:
            self._repricing.discard(order.order_id)

    async def cancel_all_open(self) -> list:
        """
        Cancel all open orders. Returns list of unfilled orders that were cancelled
        (callers must release exposure and refund balance for these).

        For live GTC orders, checks fill status first — a GTC may have filled
        between the last poll and the window close.
        """
        active = self._orders.get_active_orders()
        cancelled_orders = []

        for order in active:
            if order.is_paper:
                self._orders.mark_cancelled(order.order_id, "Window closed")
                cancelled_orders.append(order)
            else:
                try:
                    client = self._get_clob_client()
                    if client:
                        status = await asyncio.get_event_loop().run_in_executor(
                            None, client.get_order, order.order_id
                        )
                        if status and status.get("status") == "MATCHED":
                            # Filled — record and skip cancel; caller handles exposure via resolution
                            filled_price = float(status.get("price", order.price))
                            raw_sm = status.get("size_matched") or status.get("sizeMatched") or "0"
                            size_matched_val = float(raw_sm) if raw_sm else 0.0
                            size_matched = size_matched_val if size_matched_val > 0 else order.size_shares
                            self._orders.mark_filled(
                                order.order_id, size_matched, filled_price
                            )
                            logger.info(
                                f"GTC order {order.order_id[:12]}... filled @ "
                                f"{filled_price:.3f} × {size_matched:.1f} shares"
                            )
                            continue
                except Exception as exc:
                    logger.debug(f"Fill status check failed for {order.order_id[:12]}...: {exc}")

                try:
                    client = self._get_clob_client()
                    if client:
                        await asyncio.get_event_loop().run_in_executor(
                            None, client.cancel, order.order_id
                        )
                    self._orders.mark_cancelled(order.order_id, "Window closed")
                    cancelled_orders.append(order)
                except Exception as exc:
                    logger.error(f"Failed to cancel order {order.order_id}: {exc}")

        if cancelled_orders:
            logger.info(f"Cancelled {len(cancelled_orders)} open orders")
        return cancelled_orders
