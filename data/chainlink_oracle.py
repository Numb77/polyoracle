"""
Chainlink BTC/USD price oracle on Polygon.

Reads the latestRoundData() from the Chainlink aggregator proxy.
This gives us the "official" on-chain BTC price that Polymarket uses
for market resolution. Comparing this to CEX price reveals latency edges.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

from web3 import AsyncWeb3, Web3
try:
    from web3.middleware import ExtraDataToPOAMiddleware as _POAMiddleware
except ImportError:
    from web3.middleware import geth_poa_middleware as _POAMiddleware  # web3 < v6

from core.config import get_config
from core.logger import get_logger

logger = get_logger(__name__)
cfg = get_config()

# Chainlink Aggregator V3 ABI (minimal — only latestRoundData)
AGGREGATOR_V3_ABI: list[dict] = [
    {
        "inputs": [],
        "name": "latestRoundData",
        "outputs": [
            {"internalType": "uint80", "name": "roundId", "type": "uint80"},
            {"internalType": "int256", "name": "answer", "type": "int256"},
            {"internalType": "uint256", "name": "startedAt", "type": "uint256"},
            {"internalType": "uint256", "name": "updatedAt", "type": "uint256"},
            {"internalType": "uint80", "name": "answeredInRound", "type": "uint80"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "decimals",
        "outputs": [{"internalType": "uint8", "name": "", "type": "uint8"}],
        "stateMutability": "view",
        "type": "function",
    },
]


@dataclass
class OraclePrice:
    """A price reading from the Chainlink oracle."""
    price: float            # BTC/USD price
    round_id: int
    updated_at: float       # Unix timestamp of last oracle update
    latency_sec: float      # Seconds since last oracle update (stale if large)

    @property
    def is_stale(self) -> bool:
        """Oracle is considered stale if not updated in 90 seconds."""
        return self.latency_sec > 90

    def to_dict(self) -> dict:
        return {
            "price": self.price,
            "round_id": self.round_id,
            "updated_at": self.updated_at,
            "latency_sec": round(self.latency_sec, 1),
            "is_stale": self.is_stale,
        }


class ChainlinkOracle:
    """
    Polls the Chainlink BTC/USD aggregator on Polygon.

    Usage:
        oracle = ChainlinkOracle()
        await oracle.start()
        price = oracle.latest_price
    """

    POLL_INTERVAL = 12.0    # Poll every 12 seconds (Polygon block time ~2s, Chainlink updates ~30-60s)

    # Free public Polygon RPC endpoints tried in order
    FALLBACK_RPC_URLS = [
        "https://polygon-mainnet.public.blastapi.io",
        "https://polygon-bor-rpc.publicnode.com",
        "https://rpc.ankr.com/polygon",
        "https://polygon.llamarpc.com",
    ]

    def __init__(self, proxy_address: str | None = None) -> None:
        self._rpc_urls = [cfg.polygon_rpc_url] + [
            u for u in self.FALLBACK_RPC_URLS if u != cfg.polygon_rpc_url
        ]
        self._rpc_index = 0
        # Use explicit proxy if provided (even empty string); only fall back to
        # the default BTC proxy when caller passes None.
        self._proxy_address = proxy_address if proxy_address is not None else cfg.chainlink_btc_usd_proxy
        self._w3: AsyncWeb3 | None = None
        self._contract = None
        self._decimals: int = 8   # Chainlink BTC/USD uses 8 decimal places
        self._latest: OraclePrice | None = None
        self._running = False

    @property
    def _rpc_url(self) -> str:
        return self._rpc_urls[self._rpc_index % len(self._rpc_urls)]

    async def _close_current_session(self) -> None:
        """Close any open aiohttp sessions held by the current web3 instance."""
        if self._w3 is None:
            return
        try:
            cache = self._w3.provider._request_session_manager.session_cache
            for session in list(cache._data.values()):
                if hasattr(session, "close") and not session.closed:
                    await session.close()
        except Exception:
            pass

    async def _next_rpc(self) -> None:
        """Rotate to the next RPC endpoint, closing the current session first."""
        await self._close_current_session()
        self._rpc_index += 1
        self._w3 = None
        self._contract = None
        logger.warning(f"Switching to RPC endpoint: {self._rpc_url}")

    async def _init_web3(self) -> None:
        """Initialize Web3 connection with current RPC endpoint."""
        self._w3 = AsyncWeb3(AsyncWeb3.AsyncHTTPProvider(self._rpc_url))
        # Polygon uses POA consensus — inject middleware
        self._w3.middleware_onion.inject(_POAMiddleware, layer=0)

        checksum_addr = Web3.to_checksum_address(self._proxy_address)
        self._contract = self._w3.eth.contract(
            address=checksum_addr,
            abi=AGGREGATOR_V3_ABI,
        )

        try:
            self._decimals = await self._contract.functions.decimals().call()
            logger.info(f"Chainlink oracle initialized: {self._rpc_url}, decimals={self._decimals}")
        except Exception as exc:
            logger.warning(f"Failed to read Chainlink decimals: {exc}, using 8")
            self._decimals = 8

    async def fetch_latest(self) -> OraclePrice | None:
        """Fetch the latest price from Chainlink."""
        if self._contract is None:
            await self._init_web3()

        try:
            result = await self._contract.functions.latestRoundData().call()
            round_id, answer, started_at, updated_at, answered_in_round = result

            price = answer / (10 ** self._decimals)
            latency = time.time() - updated_at

            oracle_price = OraclePrice(
                price=price,
                round_id=round_id,
                updated_at=float(updated_at),
                latency_sec=latency,
            )
            self._latest = oracle_price
            return oracle_price

        except Exception as exc:
            err_str = str(exc)
            # Any connectivity / SSL / auth error → rotate to next RPC
            rotate_triggers = (
                "401", "403", "Unauthorized", "Server disconnected",
                "SSL", "ssl", "TLS", "tls", "Cannot connect",
                "ClientConnectorError", "TimeoutError", "ConnectionRefused",
            )
            if any(t in err_str for t in rotate_triggers):
                logger.warning(f"Chainlink RPC unavailable ({self._rpc_url}): {exc} — rotating endpoint")
                await self._next_rpc()
            else:
                logger.warning(f"Failed to read Chainlink oracle: {exc}")
            return None

    async def start(self) -> None:
        """Start polling the oracle in the background."""
        if not self._proxy_address:
            return  # No Chainlink feed configured for this asset — skip polling.

        self._running = True
        await self._init_web3()
        logger.info("ChainlinkOracle polling started")

        try:
            while self._running:
                try:
                    await self.fetch_latest()
                except Exception as exc:
                    logger.warning(f"Oracle poll error: {exc}")
                await asyncio.sleep(self.POLL_INTERVAL)
        finally:
            await self._close_current_session()

    def stop(self) -> None:
        self._running = False

    @property
    def latest_price(self) -> float:
        """Most recent oracle price. Returns 0.0 if not yet fetched."""
        return self._latest.price if self._latest else 0.0

    @property
    def latest(self) -> OraclePrice | None:
        return self._latest

    def get_cex_oracle_delta_pct(self, cex_price: float) -> float:
        """
        Calculate the divergence between CEX price and oracle price.
        Positive = CEX is higher than oracle (potential UP signal).
        Negative = CEX is lower than oracle (potential DOWN signal).
        """
        if not self._latest or self._latest.price <= 0:
            return 0.0
        oracle_price = self._latest.price
        return (cex_price - oracle_price) / oracle_price * 100
