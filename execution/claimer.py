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

import data.trade_db as trade_db
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

USDC_ABI = [
    {"inputs": [{"name": "account", "type": "address"}],
     "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}],
     "stateMutability": "view", "type": "function"},
]

# Polymarket data API — used for ghost claim recovery
DATA_API_URL = "https://data-api.polymarket.com"

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
        # Track order_ids that already have an active retry task running.
        # Prevents process_pending_claims() from spawning duplicate tasks
        # when called every 5 minutes while a claim is still in-flight.
        self._active_claim_ids: set[str] = set()
        # Ghost claims (from recover_ghost_claims) that failed with
        # payoutDenominator == 0 — retried each window via process_pending_claims.
        self._ghost_retry: list[dict] = []
        self._ghost_retry_active: bool = False
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

        Each claim gets at most ONE active retry task at a time.
        If a task is already running for a claim, we skip it here — the
        existing task will keep retrying and clean up when done.
        """
        for claim, order in list(self._pending):
            if claim.order_id in self._active_claim_ids:
                # Already has a task running — don't spawn a duplicate
                continue
            self._active_claim_ids.add(claim.order_id)
            asyncio.create_task(
                self._claim_with_retry(claim, order, wallet),
                name=f"claim_{claim.order_id[:12]}",
            )

        # Retry ghost claims that failed with payoutDenominator == 0
        if self._ghost_retry and not self._ghost_retry_active and wallet:
            self._ghost_retry_active = True
            asyncio.create_task(
                self._retry_ghost_claims(wallet),
                name="ghost_retry",
            )

        return []

    async def _retry_ghost_claims(self, wallet) -> None:
        """Background task: retry ghost claims that failed with payoutDenominator == 0."""
        try:
            for pos in list(self._ghost_retry):
                result = await self._redeem_position_from_api(pos, wallet)
                if result.success:
                    self._ghost_retry.remove(pos)
                    logger.claim(  # type: ignore[attr-defined]
                        f"[GHOST RETRY ✓] {result.market_slug[:28]} | "
                        f"+${result.claimed_usd:.2f} USDC redeemed"
                    )
                elif result.error != "Not yet resolved on-chain":
                    # Non-transient error — stop retrying this position
                    self._ghost_retry.remove(pos)
                    logger.info(
                        f"Ghost retry dropped ({result.error}): "
                        f"{pos.get('conditionId', '')[:16]}"
                    )
                await asyncio.sleep(1.5)
        finally:
            self._ghost_retry_active = False

    # ── Core claim logic ──────────────────────────────────────────────────────

    async def _claim_with_retry(
        self, claim: PendingClaim, order: Order, wallet
    ) -> None:
        """Background task: retry claim until success or 2-hour timeout."""
        gross_payout = claim.filled_shares
        net_pnl = gross_payout - claim.size_usd

        try:
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

            # Reset attempts if this claim was loaded from disk with exhausted retries
            # so that a bot restart gives it a fresh window instead of 0 tries.
            if claim.attempts >= self.CLAIM_MAX_RETRIES:
                logger.info(
                    f"Claim {claim.order_id[:12]}... resetting attempt counter "
                    f"(was {claim.attempts}) for fresh retry window"
                )
                claim.attempts = 0

            for attempt in range(self.CLAIM_MAX_RETRIES - claim.attempts):
                # Guard: if claim was already removed by another path, stop.
                if not any(c.order_id == claim.order_id for c, _ in self._pending):
                    return

                claim.attempts += 1
                self._save_persisted()

                success = await self._try_claim_once(claim, order, wallet, gross_payout, net_pnl)
                if success:
                    self._remove_pending(claim.order_id)
                    return

                if attempt < self.CLAIM_MAX_RETRIES - claim.attempts:
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
        finally:
            # Always release the lock so a future process_pending_claims call
            # can spawn a new task if this one failed or timed out.
            self._active_claim_ids.discard(claim.order_id)

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

            provider = AsyncWeb3.AsyncHTTPProvider(cfg.polygon_rpc_url)
            w3 = AsyncWeb3(provider)
            w3.middleware_onion.inject(_POAMiddleware, layer=0)

            try:
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
                # If payoutDenominator == 0 the oracle hasn't finalized yet — return
                # False to schedule a retry WITHOUT sending a transaction (saves gas).
                try:
                    payout_denom = await ctf.functions.payoutDenominator(
                        condition_id_bytes
                    ).call()
                    if payout_denom == 0:
                        logger.debug(
                            f"Claim {claim.order_id[:12]}... payoutDenominator=0 "
                            f"— oracle not yet resolved, will retry"
                        )
                        return False  # Retry — no tx sent, no gas burned
                except Exception as exc:
                    logger.debug(f"payoutDenominator check failed: {exc}")
                    # Continue anyway — some RPC nodes don't expose this

                # ── Send redemption transaction ───────────────────────────────────
                # Snapshot USDC balance before tx to verify actual transfer occurred.
                # redeemPositions returns status=1 even when 0 tokens are redeemed
                # (idempotent), so we can't trust the tx receipt alone.
                usdc_contract_abi = [
                    {"inputs": [{"name": "account", "type": "address"}],
                     "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}],
                     "stateMutability": "view", "type": "function"},
                ]
                usdc = w3.eth.contract(
                    address=Web3.to_checksum_address(USDC_ADDRESS),
                    abi=usdc_contract_abi,
                )
                balance_before = await usdc.functions.balanceOf(wallet.address).call()

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

                if receipt["status"] != 1:
                    logger.warning(
                        f"Claim {claim.order_id[:12]}... tx reverted "
                        f"(tx={tx_hash.hex()[:16]}...) — will retry"
                    )
                    return False

                # Verify actual USDC landed — redeemPositions is idempotent and
                # returns status=1 even when position is already empty ($0 transferred).
                balance_after = await usdc.functions.balanceOf(wallet.address).call()
                usdc_received = (balance_after - balance_before) / 1e6  # USDC has 6 decimals

                if usdc_received < 0.01:
                    # payoutDenominator > 0 AND $0 received means the position was
                    # already fully redeemed (by ghost recovery, a previous session,
                    # or the first attempt that succeeded on-chain but we missed).
                    # Do NOT retry — there is nothing left to claim and each retry
                    # burns MATIC gas with zero chance of success.
                    logger.info(
                        f"Claim {claim.order_id[:12]}... already redeemed "
                        f"(payoutDenominator>0 but $0 received, "
                        f"tx={tx_hash.hex()[:16]}...) — marking complete"
                    )
                    return True  # Treat as done — remove from pending, stop retrying

                # Use actual on-chain USDC received to compute real P&L —
                # the pre-computed net_pnl is based on an estimated filled_shares
                # (size_usd / best_ask at order time) which can differ from the
                # actual shares delivered when a FOK sweeps multiple price levels.
                real_net_pnl = round(usdc_received - claim.size_usd, 2)
                self._orders.mark_resolved(order.order_id, won=True, pnl=real_net_pnl)
                # Overwrite the estimated P&L recorded at resolution time with the
                # real value now that we have the actual on-chain amount.
                asyncio.create_task(trade_db.resolve_trade(
                    order_id=claim.order_id,
                    won=True,
                    actual_direction=claim.direction,
                    pnl=real_net_pnl,
                ))
                logger.claim(  # type: ignore[attr-defined]
                    f"[CLAIM ✓] {claim.market_slug[:24]} | "
                    f"+${usdc_received:.2f} USDC redeemed | "
                    f"net P&L: {real_net_pnl:+.2f} (tx={tx_hash.hex()[:16]}...)"
                )
                return True
            finally:
                # Close the aiohttp session inside AsyncHTTPProvider to avoid leaks.
                try:
                    await provider.disconnect()
                except Exception:
                    pass

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

    # ── Ghost claim recovery ───────────────────────────────────────────────────

    async def recover_ghost_claims(self, wallet) -> list[ClaimResult]:
        """
        Scan the Polymarket data API for positions that still have value but were
        never claimed by the normal bot flow (e.g. bot restarted mid-claim, oracle
        check silently failed, or redeemPositions transferred $0 due to timing).

        Skips any positions already tracked in self._pending (those are handled
        by the normal retry flow). Runs once through all candidates — no retry.
        Called on bot startup and when the dashboard 'collect_claims' button fires.
        """
        if self._is_paper or not wallet:
            return []

        try:
            positions = await self._fetch_open_positions(wallet.address)
        except Exception as exc:
            logger.warning(f"Ghost claim scan: data API unavailable — {exc}")
            return []

        if not positions:
            logger.debug("Ghost claim scan: no unclaimed positions in account")
            return []

        # Skip anything already tracked — those have their own retry loop
        pending_cids = {c.condition_id for c, _ in self._pending}
        candidates = [p for p in positions if p.get("conditionId", "") not in pending_cids]

        if not candidates:
            logger.info("Ghost claim scan: all open positions already tracked as pending claims")
            return []

        total_value = sum(float(p.get("currentValue", 0)) for p in candidates)
        logger.info(
            f"Ghost claim scan: {len(candidates)} untracked position(s) "
            f"worth ~${total_value:.2f} — attempting redemption"
        )

        results: list[ClaimResult] = []
        for pos in candidates:
            result = await self._redeem_position_from_api(pos, wallet)
            results.append(result)
            if result.success:
                logger.claim(  # type: ignore[attr-defined]
                    f"[GHOST CLAIM ✓] {result.market_slug[:28]} | "
                    f"+${result.claimed_usd:.2f} USDC redeemed"
                )
            elif result.error == "Not yet resolved on-chain":
                # Oracle not ready yet — queue for automatic retry each window
                cid = pos.get("conditionId", "")
                if not any(p.get("conditionId") == cid for p in self._ghost_retry):
                    self._ghost_retry.append(pos)
                    logger.info(
                        f"Ghost claim queued for auto-retry: "
                        f"{result.market_slug[:28]} (oracle not yet resolved)"
                    )
            await asyncio.sleep(1.5)  # space txs to avoid nonce conflicts

        recovered = sum(r.claimed_usd for r in results if r.success)
        ok = sum(1 for r in results if r.success)
        if ok > 0:
            logger.info(
                f"Ghost claim recovery: {ok}/{len(candidates)} positions redeemed, "
                f"+${recovered:.2f} USDC total"
            )
        elif candidates:
            logger.info("Ghost claim recovery: 0 positions redeemed (may not be resolved yet)")

        return results

    async def _fetch_open_positions(self, wallet_address: str) -> list[dict]:
        """Return positions with currentValue > $0.01 from the Polymarket data API."""
        import aiohttp
        params = {"user": wallet_address, "sizeThreshold": "0.01"}
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{DATA_API_URL}/positions", params=params) as resp:
                resp.raise_for_status()
                data = await resp.json()
        return [p for p in data if float(p.get("currentValue", 0)) > 0.01]

    async def _redeem_position_from_api(self, pos: dict, wallet) -> ClaimResult:
        """
        Attempt a single on-chain redemption for a position from the data API.
        Returns ClaimResult(success=True, claimed_usd=X) on actual USDC receipt.
        """
        condition_id = pos.get("conditionId", "")
        outcome = pos.get("outcome", "")
        market_slug = pos.get("title", pos.get("market", "unknown"))[:40]
        ref = condition_id[:12] if condition_id else "unknown"

        def _err(msg: str) -> ClaimResult:
            return ClaimResult(
                order_id=condition_id,
                market_slug=market_slug,
                condition_id=condition_id,
                claimed_usd=0.0,
                success=False,
                is_paper=False,
                error=msg,
            )

        if not condition_id:
            return _err("No conditionId in position data")

        try:
            cid_bytes = bytes.fromhex(
                condition_id[2:] if condition_id.startswith("0x") else condition_id
            )
        except ValueError as exc:
            return _err(f"Bad conditionId format: {exc}")

        outcome_lower = outcome.lower()
        if outcome_lower in ("yes", "up"):
            index_set = 1
        elif outcome_lower in ("no", "down"):
            index_set = 2
        else:
            return _err(f"Unknown outcome value: '{outcome}'")

        try:
            from web3 import Web3, AsyncWeb3
            try:
                from web3.middleware import ExtraDataToPOAMiddleware as _POAMiddleware
            except ImportError:
                from web3.middleware import geth_poa_middleware as _POAMiddleware

            provider = AsyncWeb3.AsyncHTTPProvider(cfg.polygon_rpc_url)
            w3 = AsyncWeb3(provider)
            w3.middleware_onion.inject(_POAMiddleware, layer=0)

            try:
                ctf = w3.eth.contract(
                    address=Web3.to_checksum_address(CONDITIONAL_TOKENS_ADDRESS),
                    abi=CONDITIONAL_TOKENS_ABI,
                )
                usdc = w3.eth.contract(
                    address=Web3.to_checksum_address(USDC_ADDRESS),
                    abi=USDC_ABI,
                )

                # Must be resolved on-chain before we can redeem
                payout_denom = await ctf.functions.payoutDenominator(cid_bytes).call()
                if payout_denom == 0:
                    logger.debug(f"Ghost claim {ref}: payoutDenominator=0 — not yet resolved")
                    return _err("Not yet resolved on-chain")

                balance_before = await usdc.functions.balanceOf(wallet.address).call()

                nonce = await w3.eth.get_transaction_count(wallet.address)
                gas_price = int((await w3.eth.gas_price) * 1.1)

                tx = await ctf.functions.redeemPositions(
                    Web3.to_checksum_address(USDC_ADDRESS),
                    b"\x00" * 32,
                    cid_bytes,
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

                if receipt["status"] != 1:
                    return _err(f"Tx reverted (tx={tx_hash.hex()[:16]})")

                balance_after = await usdc.functions.balanceOf(wallet.address).call()
                usdc_received = (balance_after - balance_before) / 1e6

                if usdc_received < 0.01:
                    logger.debug(
                        f"Ghost claim {ref}: tx ok but $0 USDC received "
                        f"({balance_before/1e6:.2f} → {balance_after/1e6:.2f}) "
                        f"— likely already redeemed"
                    )
                    return _err("Tx ok but $0 USDC received (already redeemed?)")

                return ClaimResult(
                    order_id=condition_id,
                    market_slug=market_slug,
                    condition_id=condition_id,
                    claimed_usd=usdc_received,
                    success=True,
                    is_paper=False,
                )
            finally:
                # Always close the aiohttp session inside AsyncHTTPProvider.
                # Without this, each call leaks a ClientSession → "Unclosed client session" errors.
                try:
                    await provider.disconnect()
                except Exception:
                    pass

        except Exception as exc:
            logger.warning(f"Ghost claim {ref}: {type(exc).__name__}: {exc}")
            return _err(str(exc))

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
