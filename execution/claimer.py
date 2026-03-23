"""
Auto-claimer — redeems winning positions after market resolution.

Winning tokens resolve to $1.00 each. We call redeemPositions on the
Gnosis ConditionalTokens contract to convert them back to USDC.

Key facts:
  • redeemPositions lives on ConditionalTokens (0x4D97DC...), NOT on the
    ClobExchange (0x4bFb41...) — previous code had the wrong address.
  • Polymarket's UMA oracle can take 30-120 minutes to call reportPayouts
    on-chain; we retry patiently up to 2 hours.
  • Pending claims are persisted to disk so restarts don't lose them.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from core.config import get_config
from core.logger import get_logger
from execution.order_manager import Order, OrderManager, OrderStatus

logger = get_logger(__name__)
cfg = get_config()

# ── Correct contract addresses (Polygon mainnet) ──────────────────────────────

# Gnosis ConditionalTokens Framework — this is where redeemPositions lives.
# Do NOT use the ClobExchange address (0x4bFb41...) for redemption.
CONDITIONAL_TOKENS_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"

CONDITIONAL_TOKENS_ABI = [
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
    },
    {
        # Used to check if redemption is possible (payout denominator > 0)
        "inputs": [{"name": "conditionId", "type": "bytes32"}],
        "name": "payoutDenominator",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
]

USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

# ── Persistence ───────────────────────────────────────────────────────────────

CLAIMS_FILE = Path("logs/pending_claims.json")


@dataclass
class PendingClaim:
    order_id: str
    market_slug: str
    condition_id: str
    outcome: str           # 'YES' or 'NO'
    direction: str         # 'UP' or 'DOWN'
    filled_shares: float
    size_usd: float
    fee_usd: float
    is_paper: bool
    scheduled_at: float    # epoch when claim was first scheduled
    attempts: int = 0


@dataclass
class ClaimResult:
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

    Retry window: up to 2 hours (120 attempts × 60s each).
    Persistent: pending claims survive bot restarts via logs/pending_claims.json.
    """

    # Initial delay before first attempt — oracle needs time to report on-chain
    CLAIM_DELAY_SEC = 120.0
    # Retry window: 120 × 60s = 2 hours
    CLAIM_MAX_RETRIES = 120
    CLAIM_RETRY_INTERVAL = 60.0

    def __init__(self, order_manager: OrderManager) -> None:
        self._orders = order_manager
        self._is_paper = cfg.paper_mode
        self._pending: list[tuple[PendingClaim, Order]] = []
        self._load_persisted()

    # ── Scheduling ────────────────────────────────────────────────────────────

    def schedule_claim(self, order: Order, actual_direction: str) -> None:
        """Schedule a winning claim. Only call for orders that won."""
        won = order.direction == actual_direction
        if not won:
            # Lost — nothing to claim, just record
            pnl = -(order.size_usd + order.fee_usd)
            self._orders.mark_resolved(order.order_id, won=False, pnl=pnl)
            return

        if order.status != OrderStatus.FILLED:
            return

        claim = PendingClaim(
            order_id=order.order_id,
            market_slug=order.market_slug,
            condition_id=order.condition_id,
            outcome=order.outcome,
            direction=order.direction,
            filled_shares=order.filled_shares,
            size_usd=order.size_usd,
            fee_usd=order.fee_usd,
            is_paper=order.is_paper,
            scheduled_at=time.time(),
        )
        self._pending.append((claim, order))
        self._save_persisted()
        logger.info(
            f"Claim scheduled: {order.market_slug[:20]}... "
            f"({order.filled_shares:.1f} shares, "
            f"starting in {self.CLAIM_DELAY_SEC:.0f}s)"
        )

    async def process_pending_claims(self, wallet=None) -> list[ClaimResult]:
        """
        Process all pending claims asynchronously.
        Called on every window close — claims run in background tasks.
        """
        results = []
        for claim, order in list(self._pending):
            asyncio.create_task(
                self._claim_with_retry(claim, order, wallet),
                name=f"claim_{claim.order_id[:12]}"
            )
        # Return immediately — claims run in background
        return results

    # ── Core claim logic ──────────────────────────────────────────────────────

    async def _claim_with_retry(
        self, claim: PendingClaim, order: Order, wallet
    ) -> None:
        """Background task: retry claim until success or 2-hour timeout."""
        gross_payout = claim.filled_shares
        net_pnl = gross_payout - claim.size_usd

        if claim.is_paper or self._is_paper:
            await asyncio.sleep(2.0)  # Small delay for realism
            self._orders.mark_resolved(order.order_id, won=True, pnl=net_pnl)
            logger.claim(  # type: ignore[attr-defined]
                f"[CLAIM ✓] {claim.market_slug[:24]} | "
                f"+${gross_payout:.2f} gross | net P&L: {net_pnl:+.2f} [PAPER]"
            )
            self._remove_pending(claim.order_id)
            return

        # Wait for oracle before first attempt
        elapsed_since_schedule = time.time() - claim.scheduled_at
        remaining_delay = max(0.0, self.CLAIM_DELAY_SEC - elapsed_since_schedule)
        if remaining_delay > 0:
            logger.info(
                f"Claim {claim.order_id[:12]}... waiting {remaining_delay:.0f}s "
                f"for on-chain resolution"
            )
            await asyncio.sleep(remaining_delay)

        remaining_retries = self.CLAIM_MAX_RETRIES - claim.attempts

        for attempt in range(remaining_retries):
            claim.attempts += 1
            self._save_persisted()

            success = await self._try_claim_once(claim, order, wallet, gross_payout, net_pnl)
            if success:
                self._remove_pending(claim.order_id)
                return

            if attempt < remaining_retries - 1:
                logger.info(
                    f"Claim {claim.order_id[:12]}... attempt {claim.attempts}/"
                    f"{self.CLAIM_MAX_RETRIES} — waiting {self.CLAIM_RETRY_INTERVAL:.0f}s "
                    f"for oracle resolution"
                )
                await asyncio.sleep(self.CLAIM_RETRY_INTERVAL)

        # Exhausted retries — claim stays in pending_claims.json for next session
        logger.error(
            f"Claim TIMEOUT: {claim.order_id} after {claim.attempts} attempts "
            f"({claim.attempts * self.CLAIM_RETRY_INTERVAL / 60:.0f} min). "
            f"Claim persisted to {CLAIMS_FILE} and will retry on next bot start.\n"
            f"Manual redemption: conditionId={claim.condition_id}, "
            f"indexSet={'1' if claim.outcome == 'YES' else '2'}"
        )

    async def _try_claim_once(
        self,
        claim: PendingClaim,
        order: Order,
        wallet,
        gross_payout: float,
        net_pnl: float,
    ) -> bool:
        """
        Attempt one on-chain redemption.
        Returns True on success, False if we should retry.
        """
        try:
            from web3 import Web3, AsyncWeb3
            try:
                from web3.middleware import ExtraDataToPOAMiddleware as _POAMiddleware
            except ImportError:
                from web3.middleware import geth_poa_middleware as _POAMiddleware

            w3 = AsyncWeb3(AsyncWeb3.AsyncHTTPProvider(cfg.polygon_rpc_url))
            w3.middleware_onion.inject(_POAMiddleware, layer=0)

            ctf = w3.eth.contract(
                address=Web3.to_checksum_address(CONDITIONAL_TOKENS_ADDRESS),
                abi=CONDITIONAL_TOKENS_ABI,
            )

            # Parse conditionId to bytes32
            cid = claim.condition_id
            condition_id_bytes = bytes.fromhex(cid[2:] if cid.startswith("0x") else cid)

            # Binary markets: YES=indexSet 1 (bit 0), NO=indexSet 2 (bit 1)
            index_set = 1 if claim.outcome == "YES" else 2

            # ── Pre-flight: check oracle has resolved this condition ───────────
            try:
                payout_denom = await ctf.functions.payoutDenominator(
                    condition_id_bytes
                ).call()
                if payout_denom == 0:
                    logger.debug(
                        f"Claim {claim.order_id[:12]}... payoutDenominator=0 "
                        f"— oracle not yet resolved"
                    )
                    return False  # Retry — not resolved yet
            except Exception as exc:
                logger.debug(f"payoutDenominator check failed: {exc}")
                # Continue anyway — some RPC nodes don't support this

            # ── Send redemption transaction ───────────────────────────────────
            nonce = await w3.eth.get_transaction_count(wallet.address)
            gas_price = await w3.eth.gas_price
            # Add 10% tip to avoid stuck transactions
            gas_price = int(gas_price * 1.1)

            tx = await ctf.functions.redeemPositions(
                Web3.to_checksum_address(USDC_ADDRESS),
                b"\x00" * 32,             # parentCollectionId = 0 (top-level)
                condition_id_bytes,
                [index_set],
            ).build_transaction({
                "from": wallet.address,
                "nonce": nonce,
                "gasPrice": gas_price,
                "gas": 250_000,
            })

            signed = w3.eth.account.sign_transaction(tx, cfg.normalized_private_key)
            tx_hash = await w3.eth.send_raw_transaction(signed.raw_transaction)
            receipt = await w3.eth.wait_for_transaction_receipt(tx_hash, timeout=90)

            if receipt["status"] == 1:
                self._orders.mark_resolved(order.order_id, won=True, pnl=net_pnl)
                logger.claim(  # type: ignore[attr-defined]
                    f"[CLAIM ✓] {claim.market_slug[:24]} | "
                    f"+${gross_payout:.2f} USDC redeemed | "
                    f"net P&L: {net_pnl:+.2f} (tx={tx_hash.hex()[:16]}...)"
                )
                return True
            else:
                logger.warning(
                    f"Claim {claim.order_id[:12]}... tx reverted "
                    f"(tx={tx_hash.hex()[:16]}...) — oracle pending"
                )
                return False

        except Exception as exc:
            exc_str = str(exc).lower()
            is_nonce_err = "nonce" in exc_str or "replacement" in exc_str
            is_timeout = "timeout" in exc_str
            logger.warning(
                f"Claim {claim.order_id[:12]}... attempt failed: "
                f"{type(exc).__name__}: {exc}"
                + (" — will retry" if not is_nonce_err else " — nonce conflict, retrying")
            )
            if is_timeout:
                await asyncio.sleep(5.0)  # Brief pause before retry on network issues
            return False

    # ── Persistence ───────────────────────────────────────────────────────────

    def _save_persisted(self) -> None:
        """Save pending claims to disk so they survive restarts."""
        try:
            CLAIMS_FILE.parent.mkdir(parents=True, exist_ok=True)
            data = [asdict(claim) for claim, _ in self._pending]
            with open(CLAIMS_FILE, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as exc:
            logger.debug(f"Failed to persist claims: {exc}")

    def _load_persisted(self) -> None:
        """On startup, reload any claims that didn't complete in the last session."""
        if not CLAIMS_FILE.exists():
            return
        try:
            with open(CLAIMS_FILE) as f:
                data = json.load(f)
            for item in data:
                claim = PendingClaim(**item)
                # Reconstruct a minimal Order for mark_resolved
                dummy_order = Order(
                    order_id=claim.order_id,
                    market_slug=claim.market_slug,
                    condition_id=claim.condition_id,
                    token_id="",
                    direction=claim.direction,
                    outcome=claim.outcome,
                    price=claim.size_usd / max(claim.filled_shares, 1),
                    size_usd=claim.size_usd,
                    size_shares=claim.filled_shares,
                    fee_usd=claim.fee_usd,
                    confidence=0.0,
                    status=OrderStatus.FILLED,
                    filled_shares=claim.filled_shares,
                    filled_price=claim.size_usd / max(claim.filled_shares, 1),
                    is_paper=claim.is_paper,
                )
                self._pending.append((claim, dummy_order))
            if self._pending:
                logger.info(
                    f"Loaded {len(self._pending)} pending claim(s) from previous session"
                )
        except Exception as exc:
            logger.warning(f"Failed to load persisted claims: {exc}")

    def _remove_pending(self, order_id: str) -> None:
        self._pending = [(c, o) for c, o in self._pending if c.order_id != order_id]
        self._save_persisted()
