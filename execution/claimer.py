"""
Auto-claimer — claims winning positions after market resolution.

Winning tokens resolve to $1.00 each. We need to redeem them to convert
to USDC. This module handles the claiming process.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from core.config import get_config
from core.logger import get_logger
from execution.order_manager import Order, OrderManager, OrderStatus

logger = get_logger(__name__)
cfg = get_config()

# Polymarket CTF Exchange contract on Polygon
CTF_EXCHANGE_ADDRESS = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"

# Minimal ABI for redeemPositions
CTF_EXCHANGE_ABI = [
    {
        "inputs": [
            {"name": "collateralToken", "type": "address"},
            {"name": "parentCollectionId", "type": "bytes32"},
            {"name": "conditionId", "type": "bytes32"},
            {"name": "indexSets", "type": "uint256[]"},
        ],
        "name": "redeemPositions",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    }
]

USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"


@dataclass
class ClaimResult:
    """Result of a claim operation."""
    order_id: str
    market_slug: str
    condition_id: str
    claimed_usd: float
    success: bool
    is_paper: bool
    error: str | None = None


class Claimer:
    """
    Claims winning positions after market resolution.

    In paper mode: simulates claims by calculating theoretical payout.
    In live mode: calls redeemPositions on the CTF Exchange contract.
    """

    # Initial delay before first claim attempt (on-chain resolution lags price by minutes)
    CLAIM_DELAY_SEC = 60.0
    # Max retries if the transaction reverts (on-chain resolution may take several minutes)
    CLAIM_MAX_RETRIES = 8
    CLAIM_RETRY_INTERVAL = 60.0  # seconds between retries

    def __init__(self, order_manager: OrderManager) -> None:
        self._orders = order_manager
        self._is_paper = cfg.paper_mode
        self._pending_claims: list[tuple[Order, str]] = []   # (order, actual_direction)

    def schedule_claim(self, order: Order, actual_direction: str) -> None:
        """Schedule a claim for a resolved market."""
        self._pending_claims.append((order, actual_direction))

    async def process_pending_claims(self, wallet=None) -> list[ClaimResult]:
        """
        Process all pending claims.
        Returns a list of ClaimResult for each processed claim.
        """
        results = []
        still_pending = []

        for order, actual_direction in self._pending_claims:
            # Only claim if we have a filled order; keep unfilled for next cycle
            if order.status != OrderStatus.FILLED:
                still_pending.append((order, actual_direction))
                continue

            # Determine if we won
            won = order.direction == actual_direction

            if won:
                result = await self._claim(order, wallet)
                results.append(result)
            else:
                # Lost — record zero pnl
                pnl = -(order.size_usd + order.fee_usd)
                self._orders.mark_resolved(order.order_id, won=False, pnl=pnl)
                results.append(ClaimResult(
                    order_id=order.order_id,
                    market_slug=order.market_slug,
                    condition_id=order.condition_id,
                    claimed_usd=0.0,
                    success=True,
                    is_paper=order.is_paper,
                ))

        self._pending_claims = still_pending
        return results

    async def _claim(self, order: Order, wallet) -> ClaimResult:
        """Claim a winning position."""
        # Winning payout: shares × $1.00 (minus fee already paid on entry)
        gross_payout = order.filled_shares  # Each winning share = $1.00
        net_pnl = gross_payout - order.size_usd  # Profit net of entry cost

        if order.is_paper or self._is_paper:
            return await self._claim_paper(order, gross_payout, net_pnl)
        else:
            return await self._claim_live(order, gross_payout, net_pnl, wallet)

    async def _claim_paper(
        self, order: Order, gross_payout: float, net_pnl: float
    ) -> ClaimResult:
        """Simulate claim in paper mode."""
        self._orders.mark_resolved(order.order_id, won=True, pnl=net_pnl)
        logger.trade(  # type: ignore[attr-defined]
            f"[PAPER CLAIM] Won {order.market_slug[:20]}... "
            f"+${gross_payout:.2f} (P&L: {net_pnl:+.2f})"
        )
        return ClaimResult(
            order_id=order.order_id,
            market_slug=order.market_slug,
            condition_id=order.condition_id,
            claimed_usd=gross_payout,
            success=True,
            is_paper=True,
        )

    async def _claim_live(
        self,
        order: Order,
        gross_payout: float,
        net_pnl: float,
        wallet,
    ) -> ClaimResult:
        """
        Claim winning position via on-chain redemption.

        Polymarket's price feed resolves in seconds, but the CTF contract's
        on-chain resolution (via UMA oracle / reportPayouts) typically takes
        1-10 minutes. We wait 60s initially then retry up to 8 times
        (≈8 minutes total) until the redemption succeeds.
        """
        await asyncio.sleep(self.CLAIM_DELAY_SEC)

        try:
            from web3 import Web3, AsyncWeb3
            try:
                from web3.middleware import ExtraDataToPOAMiddleware as _POAMiddleware
            except ImportError:
                from web3.middleware import geth_poa_middleware as _POAMiddleware

            w3 = AsyncWeb3(AsyncWeb3.AsyncHTTPProvider(cfg.polygon_rpc_url))
            w3.middleware_onion.inject(_POAMiddleware, layer=0)

            ctf = w3.eth.contract(
                address=Web3.to_checksum_address(CTF_EXCHANGE_ADDRESS),
                abi=CTF_EXCHANGE_ABI,
            )

            if order.condition_id.startswith("0x"):
                condition_id_bytes = bytes.fromhex(order.condition_id[2:])
            else:
                condition_id_bytes = bytes.fromhex(order.condition_id)

            index_set = 1 if order.outcome == "YES" else 2

            for attempt in range(self.CLAIM_MAX_RETRIES):
                try:
                    nonce = await w3.eth.get_transaction_count(wallet.address)
                    gas_price = await w3.eth.gas_price

                    tx = await ctf.functions.redeemPositions(
                        Web3.to_checksum_address(USDC_ADDRESS),
                        b"\x00" * 32,
                        condition_id_bytes,
                        [index_set],
                    ).build_transaction({
                        "from": wallet.address,
                        "nonce": nonce,
                        "gasPrice": gas_price,
                        "gas": 200_000,
                    })

                    signed = w3.eth.account.sign_transaction(tx, cfg.normalized_private_key)
                    tx_hash = await w3.eth.send_raw_transaction(signed.raw_transaction)
                    receipt = await w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)

                    if receipt["status"] == 1:
                        self._orders.mark_resolved(order.order_id, won=True, pnl=net_pnl)
                        logger.trade(  # type: ignore[attr-defined]
                            f"[CLAIM] Won {order.market_slug[:20]}... "
                            f"+${gross_payout:.2f} (tx={tx_hash.hex()[:16]}...)"
                        )
                        return ClaimResult(
                            order_id=order.order_id,
                            market_slug=order.market_slug,
                            condition_id=order.condition_id,
                            claimed_usd=gross_payout,
                            success=True,
                            is_paper=False,
                        )
                    else:
                        # Reverted — market likely not resolved on-chain yet
                        logger.warning(
                            f"Claim attempt {attempt+1}/{self.CLAIM_MAX_RETRIES} reverted "
                            f"for {order.order_id[:16]}... — on-chain resolution pending, "
                            f"retrying in {self.CLAIM_RETRY_INTERVAL:.0f}s"
                        )

                except Exception as exc:
                    logger.warning(
                        f"Claim attempt {attempt+1}/{self.CLAIM_MAX_RETRIES} failed "
                        f"for {order.order_id[:16]}...: {exc}"
                    )

                if attempt < self.CLAIM_MAX_RETRIES - 1:
                    await asyncio.sleep(self.CLAIM_RETRY_INTERVAL)

            # All retries exhausted
            raise RuntimeError(
                f"On-chain claim failed after {self.CLAIM_MAX_RETRIES} attempts "
                f"({self.CLAIM_DELAY_SEC + self.CLAIM_MAX_RETRIES * self.CLAIM_RETRY_INTERVAL:.0f}s total)"
            )

        except Exception as exc:
            logger.error(f"Claim failed for {order.order_id}: {exc}", exc_info=True)
            return ClaimResult(
                order_id=order.order_id,
                market_slug=order.market_slug,
                condition_id=order.condition_id,
                claimed_usd=0.0,
                success=False,
                is_paper=False,
                error=str(exc),
            )
