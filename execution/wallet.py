"""
Wallet management — ONLY module that touches the private key.

Loads the private key from .env, derives the wallet address,
checks USDC and MATIC balances, and provides signing capabilities
via py-clob-client.

SECURITY: The private key is NEVER logged, printed, or sent over any network.
"""

from __future__ import annotations

import asyncio

from web3 import Web3, AsyncWeb3
try:
    from web3.middleware import ExtraDataToPOAMiddleware as _POAMiddleware
except ImportError:
    from web3.middleware import geth_poa_middleware as _POAMiddleware

from core.config import get_config
from core.logger import get_logger

logger = get_logger(__name__)
cfg = get_config()

# Polygon USDC contract address
USDC_CONTRACT = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
# Polymarket CTF Exchange
CTF_EXCHANGE = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
# USDC decimals on Polygon
USDC_DECIMALS = 6

# Minimal ERC-20 ABI for balance and allowance checks
ERC20_ABI = [
    {
        "inputs": [{"name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "owner", "type": "address"},
            {"name": "spender", "type": "address"},
        ],
        "name": "allowance",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "spender", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "name": "approve",
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "nonpayable",
        "type": "function",
    },
]


class Wallet:
    """
    Manages wallet operations for Polymarket trading.

    SECURITY: Private key is accessed exactly once (during init) and
    stored only in memory. Never logged, never transmitted.
    """

    def __init__(self) -> None:
        if not cfg.has_wallet():
            raise ValueError(
                "No private key configured. "
                "Set PRIVATE_KEY in .env before trading."
            )

        # Derive address from private key
        # Private key is accessed here and only here
        self._w3 = Web3()
        # Use normalized key (always 0x-prefixed) — MetaMask exports without 0x
        account = self._w3.eth.account.from_key(cfg.normalized_private_key)
        self._address: str = account.address
        # DO NOT store the private key in a field — use cfg.normalized_private_key when needed

        # Async Web3 for on-chain queries
        self._async_w3: AsyncWeb3 | None = None

        logger.info(f"Wallet initialized: {self._address}")
        # Intentionally NOT logging the private key or any derivative

    @property
    def address(self) -> str:
        """The wallet's Ethereum/Polygon address."""
        return self._address

    async def _get_async_w3(self) -> AsyncWeb3:
        """Get or create the async Web3 connection."""
        if self._async_w3 is None:
            self._async_w3 = AsyncWeb3(
                AsyncWeb3.AsyncHTTPProvider(cfg.polygon_rpc_url)
            )
            self._async_w3.middleware_onion.inject(_POAMiddleware, layer=0)
        return self._async_w3

    async def get_matic_balance(self) -> float:
        """Get MATIC/POL balance (for gas)."""
        try:
            w3 = await self._get_async_w3()
            balance_wei = await w3.eth.get_balance(
                Web3.to_checksum_address(self._address)
            )
            return float(Web3.from_wei(balance_wei, "ether"))
        except Exception as exc:
            logger.error(f"Failed to get MATIC balance: {exc}")
            return 0.0

    async def get_usdc_balance(self) -> float:
        """Get USDC balance on Polygon."""
        try:
            w3 = await self._get_async_w3()
            usdc = w3.eth.contract(
                address=Web3.to_checksum_address(USDC_CONTRACT),
                abi=ERC20_ABI,
            )
            balance_raw = await usdc.functions.balanceOf(
                Web3.to_checksum_address(self._address)
            ).call()
            return balance_raw / (10 ** USDC_DECIMALS)
        except Exception as exc:
            logger.error(f"Failed to get USDC balance: {exc}")
            return 0.0

    async def get_usdc_allowance(self, spender: str = CTF_EXCHANGE) -> float:
        """Get USDC allowance for the CTF Exchange."""
        try:
            w3 = await self._get_async_w3()
            usdc = w3.eth.contract(
                address=Web3.to_checksum_address(USDC_CONTRACT),
                abi=ERC20_ABI,
            )
            allowance_raw = await usdc.functions.allowance(
                Web3.to_checksum_address(self._address),
                Web3.to_checksum_address(spender),
            ).call()
            return allowance_raw / (10 ** USDC_DECIMALS)
        except Exception as exc:
            logger.error(f"Failed to get USDC allowance: {exc}")
            return 0.0

    async def check_sufficient_balance(self, amount_usd: float) -> bool:
        """Check if we have enough USDC and MATIC for a trade."""
        usdc = await self.get_usdc_balance()
        if usdc < amount_usd:
            logger.warning(
                f"Insufficient USDC: have ${usdc:.2f}, need ${amount_usd:.2f}"
            )
            return False

        if usdc < cfg.min_usdc_balance:
            logger.warning(
                f"USDC balance ${usdc:.2f} below minimum ${cfg.min_usdc_balance:.2f}"
            )
            return False

        matic = await self.get_matic_balance()
        if matic < 0.1:
            logger.warning(f"Low MATIC balance: {matic:.4f} POL — may run out of gas")

        return True

    def get_clob_client(self):
        """
        Create and return an authenticated py-clob-client ClobClient.
        Uses L2 credentials (API key/secret) when available for full auth.
        Falls back to L1 (private key only) which supports order placement
        via EIP-712 signing but not authenticated REST queries.
        """
        try:
            import httpx
            import py_clob_client.http_helpers.helpers as _helpers
            # py-clob-client creates its module-level httpx.Client with no
            # timeout (defaults to 5s), which causes ReadTimeout on POST /order.
            # Patch it to 30s before any requests are made.
            _helpers._http_client = httpx.Client(http2=True, timeout=30.0)

            from py_clob_client.client import ClobClient

            if cfg.has_clob_creds():
                from py_clob_client.clob_types import ApiCreds
                creds = ApiCreds(
                    api_key=cfg.clob_api_key,
                    api_secret=cfg.clob_secret,
                    api_passphrase=cfg.clob_pass_phrase,
                )
                client = ClobClient(
                    host=cfg.polymarket_clob_url,
                    chain_id=cfg.polygon_chain_id,
                    key=cfg.normalized_private_key,
                    signature_type=cfg.signature_type,
                    funder=cfg.funder_address or self._address,
                    creds=creds,
                )
                logger.debug("CLOB client initialized with L2 API credentials")
            else:
                client = ClobClient(
                    host=cfg.polymarket_clob_url,
                    chain_id=cfg.polygon_chain_id,
                    key=cfg.normalized_private_key,
                    signature_type=cfg.signature_type,
                    funder=cfg.funder_address or self._address,
                )
                logger.warning(
                    "CLOB client initialized in L1 mode (no API credentials). "
                    "Run: python scripts/setup_wallet.py to generate credentials."
                )
            return client
        except ImportError:
            logger.error(
                "py-clob-client not installed. Run: pip install py-clob-client"
            )
            raise

    async def log_balances(self) -> dict:
        """Log and return current balances."""
        usdc, matic = await asyncio.gather(
            self.get_usdc_balance(),
            self.get_matic_balance(),
        )
        logger.info(
            f"Wallet {self._address[:8]}...{self._address[-6:]}: "
            f"USDC=${usdc:.2f}, MATIC={matic:.4f}"
        )
        return {"usdc": usdc, "matic": matic, "address": self._address}
