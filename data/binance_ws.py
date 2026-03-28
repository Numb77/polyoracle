"""
Binance BTC/USDT WebSocket trade stream.

Connects to Binance's trade stream and provides a real-time BTC price feed.
Handles reconnection with exponential backoff automatically.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Callable, Awaitable

import websockets
from websockets.exceptions import ConnectionClosed, WebSocketException

from core.config import get_config
from core.logger import get_logger

logger = get_logger(__name__)

cfg = get_config()

TickCallback = Callable[["BtcTick"], Awaitable[None]]


@dataclass
class BtcTick:
    """A single BTC trade tick from Binance."""
    price: float
    qty: float
    timestamp_ms: int       # Event time in milliseconds
    trade_id: int
    is_buyer_maker: bool    # True = sell; False = buy

    @property
    def timestamp(self) -> float:
        return self.timestamp_ms / 1000.0

    @property
    def side(self) -> str:
        return "SELL" if self.is_buyer_maker else "BUY"

    def to_dict(self) -> dict:
        return {
            "price": self.price,
            "qty": self.qty,
            "timestamp_ms": self.timestamp_ms,
            "trade_id": self.trade_id,
            "side": self.side,
        }


class BinanceWebSocket:
    """
    Connects to a Binance @trade stream and fires tick callbacks.

    Usage:
        bws = BinanceWebSocket()                          # BTC (default)
        bws = BinanceWebSocket(cfg.binance_eth_ws_url)   # ETH
        bws.subscribe(my_tick_handler)
        await bws.run()
    """

    RECONNECT_DELAY_BASE = 1.0      # seconds
    RECONNECT_DELAY_MAX = 60.0      # seconds
    PING_INTERVAL = 20              # seconds

    def __init__(self, ws_url: str | None = None) -> None:
        self._url = ws_url or cfg.binance_ws_url
        self._callbacks: list[TickCallback] = []
        self._running = False
        self._last_tick: BtcTick | None = None
        self._last_price: float = 0.0
        self._tick_count: int = 0
        self._reconnect_count: int = 0

    def subscribe(self, callback: TickCallback) -> None:
        """Register a callback that receives each BtcTick."""
        self._callbacks.append(callback)

    @property
    def last_price(self) -> float:
        """Most recent BTC price. Returns 0.0 if no tick received yet."""
        return self._last_price

    @property
    def last_tick(self) -> BtcTick | None:
        return self._last_tick

    @property
    def is_connected(self) -> bool:
        return self._running

    async def run(self) -> None:
        """
        Main loop — connects to Binance, fires callbacks on each trade.
        Reconnects automatically with exponential backoff.
        """
        self._running = True
        delay = self.RECONNECT_DELAY_BASE

        while self._running:
            try:
                logger.info(f"Connecting to Binance WS: {self._url}")
                async with websockets.connect(
                    self._url,
                    ping_interval=self.PING_INTERVAL,
                    ping_timeout=10,
                    close_timeout=5,
                    max_size=2**20,   # 1 MB
                ) as ws:
                    self._reconnect_count = 0
                    delay = self.RECONNECT_DELAY_BASE
                    logger.info("Binance WS connected")

                    async for raw_msg in ws:
                        if not self._running:
                            break
                        try:
                            tick = self._parse(raw_msg)
                            if tick:
                                self._last_tick = tick
                                self._last_price = tick.price
                                self._tick_count += 1
                                await self._fire(tick)
                        except Exception as exc:
                            logger.warning(f"Tick parse error: {exc}")

            except ConnectionClosed as exc:
                logger.warning(f"Binance WS disconnected: {exc}. Reconnecting in {delay}s")
            except WebSocketException as exc:
                logger.error(f"Binance WS error: {exc}. Reconnecting in {delay}s")
            except OSError as exc:
                logger.error(f"Network error: {exc}. Reconnecting in {delay}s")
            except Exception as exc:
                logger.error(f"Unexpected Binance WS error: {exc}", exc_info=True)

            if not self._running:
                break

            self._reconnect_count += 1
            await asyncio.sleep(delay)
            delay = min(delay * 2, self.RECONNECT_DELAY_MAX)

    def _parse(self, raw_msg: str) -> BtcTick | None:
        """Parse a Binance trade message into a BtcTick."""
        data = json.loads(raw_msg)

        # Binance trade stream format:
        # { "e": "trade", "E": 1234567890, "s": "BTCUSDT",
        #   "t": trade_id, "p": price, "q": qty,
        #   "b": buyer_order_id, "a": seller_order_id,
        #   "T": trade_time, "m": is_buyer_maker, "M": true }
        if data.get("e") != "trade":
            return None

        return BtcTick(
            price=float(data["p"]),
            qty=float(data["q"]),
            timestamp_ms=int(data["T"]),
            trade_id=int(data["t"]),
            is_buyer_maker=bool(data["m"]),
        )

    async def _fire(self, tick: BtcTick) -> None:
        """Fire all registered callbacks."""
        for cb in self._callbacks:
            try:
                await cb(tick)
            except Exception as exc:
                logger.error(f"Tick callback error in {cb}: {exc}", exc_info=True)

    def stop(self) -> None:
        """Signal the run loop to stop."""
        self._running = False
        logger.info(f"BinanceWebSocket stopped after {self._tick_count} ticks")


async def get_current_btc_price_rest() -> float:
    """
    Fetch current BTC/USDT price from Binance REST API.
    Used as a fallback when WebSocket is not yet connected.
    """
    import aiohttp

    url = f"{cfg.binance_rest_url}/api/v3/ticker/price?symbol=BTCUSDT"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                data = await resp.json()
                return float(data["price"])
    except Exception as exc:
        logger.error(f"Failed to fetch BTC price from REST: {exc}")
        return 0.0


async def get_window_open_price(symbol: str, window_ts: int) -> float:
    """
    Fetch the open price for a 5-minute window from Binance klines.

    Queries the PREVIOUS completed 5-minute candle (the one that just closed at
    window_ts) and returns its close price, which equals the current window's open.
    This avoids a timing race where Binance REST hasn't yet indexed the candle
    that literally just opened — causing a "no kline data" miss every window.
    """
    import aiohttp

    url = f"{cfg.binance_rest_url}/api/v3/klines"
    params = {
        "symbol": symbol,
        "interval": "5m",
        "endTime": window_ts * 1000 - 1,   # 1 ms before window open = previous candle
        "limit": 1,
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url, params=params, timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                data = await resp.json()
                if data and isinstance(data, list) and len(data) > 0:
                    # kline format: [openTime, open, high, low, close, ...]
                    # close (index 4) of the previous candle = open of current window
                    return float(data[0][4])
                logger.warning(f"No kline data for {symbol} window_ts={window_ts}")
                return 0.0
    except Exception as exc:
        logger.error(f"Failed to fetch {symbol} window open price from klines: {exc}")
        return 0.0


async def get_btc_window_open_price(window_ts: int) -> float:
    """Backwards-compatible alias for get_window_open_price('BTCUSDT', ...)."""
    return await get_window_open_price("BTCUSDT", window_ts)


async def get_window_close_price(symbol: str, window_ts: int) -> float:
    """
    Fetch the exact close price of the 5-minute window that started at window_ts.

    The window closes at window_ts + 300.  The Binance kline for that window
    may not be indexed immediately — we retry up to 6 times (30 s total) to
    give the exchange time to publish the closed candle.
    """
    import aiohttp

    url = f"{cfg.binance_rest_url}/api/v3/klines"
    # Query the candle whose openTime == window_ts (the window that just closed).
    # We use startTime / endTime to pin the exact candle rather than asking for
    # the latest one (which could be the NEW open window).
    params = {
        "symbol": symbol,
        "interval": "5m",
        "startTime": window_ts * 1000,
        "endTime": (window_ts + 300) * 1000 - 1,
        "limit": 1,
    }

    for attempt in range(6):   # retry up to ~30 s
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url, params=params, timeout=aiohttp.ClientTimeout(total=5)
                ) as resp:
                    data = await resp.json()
                    if data and isinstance(data, list) and len(data) > 0:
                        close_price = float(data[0][4])  # index 4 = close
                        logger.debug(
                            f"Binance kline close for {symbol} "
                            f"window_ts={window_ts}: {close_price}"
                        )
                        return close_price
        except Exception as exc:
            logger.warning(f"get_window_close_price attempt {attempt+1}: {exc}")

        if attempt < 5:
            await asyncio.sleep(5)

    logger.warning(
        f"get_window_close_price: no kline for {symbol} window_ts={window_ts} after retries"
    )
    return 0.0
