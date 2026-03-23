"""
Polymarket CLOB WebSocket — real-time order book stream.

Subscribes to order book updates for specific token IDs.
Maintains a local order book snapshot with bid/ask levels.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Callable, Awaitable

import websockets
from websockets.exceptions import ConnectionClosed

from core.logger import get_logger
from core.config import get_config

logger = get_logger(__name__)
cfg = get_config()

BookCallback = Callable[["OrderBook"], Awaitable[None]]


@dataclass
class PriceLevel:
    """A single price level in the order book."""
    price: float
    size: float


@dataclass
class OrderBook:
    """Snapshot of a token's order book."""
    token_id: str
    timestamp: float
    bids: list[PriceLevel]   # Sorted descending (best bid first)
    asks: list[PriceLevel]   # Sorted ascending (best ask first)

    @property
    def best_bid(self) -> float:
        return self.bids[0].price if self.bids else 0.0

    @property
    def best_ask(self) -> float:
        return self.asks[0].price if self.asks else 1.0

    @property
    def mid_price(self) -> float:
        if self.bids and self.asks:
            return (self.best_bid + self.best_ask) / 2
        return 0.0

    @property
    def spread(self) -> float:
        if self.bids and self.asks:
            return self.best_ask - self.best_bid
        return 1.0

    @property
    def bid_depth(self) -> float:
        """Total liquidity on the bid side (top 10 levels)."""
        return sum(level.size for level in self.bids[:10])

    @property
    def ask_depth(self) -> float:
        """Total liquidity on the ask side (top 10 levels)."""
        return sum(level.size for level in self.asks[:10])

    @property
    def imbalance_ratio(self) -> float:
        """
        Price-weighted order book imbalance. Range: [-1, +1]
        +1 = pure bid pressure (UP signal)
        -1 = pure ask pressure (DOWN signal)
        0  = balanced

        Levels closer to mid-price are weighted more heavily — they represent
        actionable intent rather than distant limit orders (which are often noise).
        Weight = 1 / (distance_from_mid + epsilon)
        """
        mid = self.mid_price
        if mid == 0.0:
            # Fallback to flat depth if no mid price
            total = self.bid_depth + self.ask_depth
            if total == 0:
                return 0.0
            return (self.bid_depth - self.ask_depth) / total

        epsilon = 1e-4  # Avoid division by zero at mid
        weighted_bids = sum(
            level.size / (abs(mid - level.price) + epsilon)
            for level in self.bids[:10]
        )
        weighted_asks = sum(
            level.size / (abs(level.price - mid) + epsilon)
            for level in self.asks[:10]
        )
        total = weighted_bids + weighted_asks
        if total == 0:
            return 0.0
        return (weighted_bids - weighted_asks) / total

    def to_dict(self) -> dict:
        return {
            "token_id": self.token_id,
            "timestamp": self.timestamp,
            "best_bid": self.best_bid,
            "best_ask": self.best_ask,
            "mid_price": self.mid_price,
            "spread": round(self.spread, 4),
            "bid_depth": round(self.bid_depth, 2),
            "ask_depth": round(self.ask_depth, 2),
            "imbalance_ratio": round(self.imbalance_ratio, 4),
        }


class PolymarketWebSocket:
    """
    Connects to Polymarket CLOB WebSocket and streams order book updates
    for subscribed token IDs.
    """

    RECONNECT_DELAY_BASE = 2.0
    RECONNECT_DELAY_MAX = 60.0
    # Only reset backoff if connection stayed alive for this many seconds
    STABLE_CONNECTION_THRESHOLD = 30.0

    def __init__(self) -> None:
        self._url = cfg.polymarket_ws_url
        self._subscribed_tokens: set[str] = set()
        self._order_books: dict[str, OrderBook] = {}
        self._callbacks: list[BookCallback] = []
        self._running = False
        self._ws = None

    def subscribe_token(self, token_id: str) -> None:
        """Subscribe to order book updates for a token."""
        if token_id in self._subscribed_tokens:
            return
        self._subscribed_tokens.add(token_id)
        # If WS is already connected, send the subscription immediately.
        # _send_subscriptions() only runs at connect-time, so new tokens
        # added later would never be sent to the server otherwise.
        if self._ws is not None:
            try:
                asyncio.get_event_loop().create_task(
                    self._send_token_subscription(token_id)
                )
            except RuntimeError:
                pass  # No running loop — will be sent on next reconnect

    def unsubscribe_token(self, token_id: str) -> None:
        """Unsubscribe from a token."""
        self._subscribed_tokens.discard(token_id)
        self._order_books.pop(token_id, None)

    def on_book_update(self, cb: BookCallback) -> None:
        """Register a callback for order book updates."""
        self._callbacks.append(cb)

    def get_order_book(self, token_id: str) -> OrderBook | None:
        return self._order_books.get(token_id)

    async def run(self) -> None:
        """Main connection loop with reconnection."""
        self._running = True
        delay = self.RECONNECT_DELAY_BASE

        while self._running:
            if not self._subscribed_tokens:
                await asyncio.sleep(1)
                continue

            try:
                logger.info(f"Connecting to Polymarket WS: {self._url}")
                async with websockets.connect(
                    self._url,
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=5,
                    open_timeout=15,
                ) as ws:
                    self._ws = ws
                    connect_time = time.time()
                    logger.info("Polymarket WS connected")

                    # Subscribe to all registered tokens
                    await self._send_subscriptions(ws)

                    async for raw_msg in ws:
                        if not self._running:
                            break
                        try:
                            await self._handle_message(raw_msg)
                        except Exception as exc:
                            logger.warning(f"Message handling error: {exc}")

                    # Only reset backoff if the connection was stable
                    if time.time() - connect_time >= self.STABLE_CONNECTION_THRESHOLD:
                        delay = self.RECONNECT_DELAY_BASE

            except ConnectionClosed as exc:
                logger.warning(f"Polymarket WS disconnected: {exc}")
            except Exception as exc:
                logger.error(f"Polymarket WS error: {exc}", exc_info=True)

            if not self._running:
                break

            self._ws = None
            logger.info(f"Reconnecting in {delay:.0f}s...")
            await asyncio.sleep(delay)
            delay = min(delay * 2, self.RECONNECT_DELAY_MAX)

    async def _send_subscriptions(self, ws) -> None:
        """Send subscription messages for all registered tokens."""
        tokens = list(self._subscribed_tokens)
        if not tokens:
            return
        # Polymarket CLOB WS expects all asset IDs in a single message
        sub_msg = {
            "assets_ids": tokens,
            "type": "Market",
        }
        await ws.send(json.dumps(sub_msg))
        for token_id in tokens:
            logger.info(f"Subscribed to order book: {token_id[:16]}...")

    async def _send_token_subscription(self, token_id: str) -> None:
        """Send a live subscription for a single token while already connected."""
        if self._ws is None:
            return
        try:
            sub_msg = {"assets_ids": [token_id], "type": "Market"}
            await self._ws.send(json.dumps(sub_msg))
            logger.info(f"Live-subscribed to order book: {token_id[:16]}...")
        except Exception as exc:
            logger.warning(f"Failed to live-subscribe to order book: {exc}")

    async def _handle_message(self, raw_msg) -> None:
        """Parse and handle a WebSocket message."""
        # Decode bytes (binary ping-pong / ACK frames)
        if isinstance(raw_msg, bytes):
            try:
                raw_msg = raw_msg.decode("utf-8")
            except Exception:
                return
        if not raw_msg or not raw_msg.strip():
            return  # Ignore empty frames
        try:
            data = json.loads(raw_msg)
        except json.JSONDecodeError:
            # Subscription ACKs and keepalive frames are plain text, not JSON
            logger.debug(f"Non-JSON frame ignored: {raw_msg[:80]!r}")
            return

        # Polymarket sends messages as JSON arrays
        events = data if isinstance(data, list) else [data]

        for event in events:
            msg_type = event.get("type", "")

            if msg_type == "book":
                await self._handle_book_snapshot(event)
            elif msg_type == "price_change":
                await self._handle_price_change(event)
            elif msg_type == "tick_size_change":
                pass  # Ignore
            elif msg_type == "last_trade_price":
                pass  # Ignore for now

    async def _handle_book_snapshot(self, data: dict) -> None:
        """Handle a full order book snapshot."""
        token_id = data.get("asset_id", "")
        if not token_id:
            return

        bids = [
            PriceLevel(price=float(b["price"]), size=float(b["size"]))
            for b in data.get("bids", [])
        ]
        asks = [
            PriceLevel(price=float(a["price"]), size=float(a["size"]))
            for a in data.get("asks", [])
        ]

        # Sort: bids descending, asks ascending
        bids.sort(key=lambda x: x.price, reverse=True)
        asks.sort(key=lambda x: x.price)

        book = OrderBook(
            token_id=token_id,
            timestamp=time.time(),
            bids=bids,
            asks=asks,
        )
        self._order_books[token_id] = book
        await self._fire(book)

    async def _handle_price_change(self, data: dict) -> None:
        """Handle incremental price change updates."""
        token_id = data.get("asset_id", "")
        book = self._order_books.get(token_id)
        if not book:
            return

        changes = data.get("changes", [])
        for change in changes:
            side = change.get("side", "")
            price = float(change.get("price", 0))
            size = float(change.get("size", 0))

            if side == "BUY":
                levels = book.bids
                reverse = True
            else:
                levels = book.asks
                reverse = False

            # Update or remove the price level
            existing = next((l for l in levels if l.price == price), None)
            if existing:
                if size == 0:
                    levels.remove(existing)
                else:
                    existing.size = size
            elif size > 0:
                levels.append(PriceLevel(price=price, size=size))

            levels.sort(key=lambda x: x.price, reverse=reverse)

        book.timestamp = time.time()
        await self._fire(book)

    async def _fire(self, book: OrderBook) -> None:
        for cb in self._callbacks:
            try:
                await cb(book)
            except Exception as exc:
                logger.error(f"Book callback error: {exc}", exc_info=True)

    def stop(self) -> None:
        self._running = False
