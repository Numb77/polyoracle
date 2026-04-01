"""
Auto-claimer — redeems winning positions after market resolution.

Winning tokens resolve to $1.00 each. We call redeemPositions on the
Gnosis ConditionalTokens contract to convert them back to USDC.

• redeemPositions lives on ConditionalTokens (0x4D97DC...), NOT ClobExchange.
• Polymarket's UMA oracle can take 30-120 minutes to settle on-chain.
• Pending claims survive bot restarts via logs/pending_claims_{lane}.json.
• Each asset lane (BTC, ETH, SOL, …) writes its own file to prevent all
  claimers from loading and re-attempting each other's claims on restart.
"""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager, suppress
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import AsyncIterator, ClassVar

import data.trade_db as trade_db
from core.config import get_config
from core.logger import get_logger
from execution.order_manager import Order, OrderManager, OrderStatus

logger = get_logger(__name__)
cfg = get_config()

# ── Contract constants (Polygon mainnet) ──────────────────────────────────────

# Gnosis ConditionalTokens — redeemPositions lives here, NOT on ClobExchange.
CONDITIONAL_TOKENS_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
DATA_API_URL = "https://data-api.polymarket.com"
_LEGACY_CLAIMS_FILE = Path("logs/pending_claims.json")


def _claims_file(lane_id: str) -> Path:
    return Path(f"logs/pending_claims_{lane_id}.json")

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
        "inputs": [{"name": "conditionId", "type": "bytes32"}],
        "name": "payoutDenominator",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
]

USDC_ABI = [
    {"inputs": [{"name": "account", "type": "address"}],
     "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}],
     "stateMutability": "view", "type": "function"},
]


def _parse_cid(condition_id: str) -> bytes:
    cid = condition_id[2:] if condition_id.startswith("0x") else condition_id
    return bytes.fromhex(cid)


@asynccontextmanager
async def _web3_session() -> AsyncIterator:
    """Yield a connected AsyncWeb3 instance; disconnect on exit."""
    from web3 import AsyncWeb3
    try:
        from web3.middleware import ExtraDataToPOAMiddleware as _M
    except ImportError:
        from web3.middleware import geth_poa_middleware as _M  # type: ignore[no-redef]
    provider = AsyncWeb3.AsyncHTTPProvider(cfg.polygon_rpc_url)
    w3 = AsyncWeb3(provider)
    w3.middleware_onion.inject(_M, layer=0)
    try:
        yield w3
    finally:
        with suppress(Exception):
            await provider.disconnect()


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class PendingClaim:
    order_id: str
    market_slug: str
    condition_id: str
    outcome: str           # 'YES' or 'NO'
    direction: str         # 'UP' or 'DOWN'
    filled_shares: float
    size_usd: float        # actual cost (filled_shares × fill_price), not intended size
    fee_usd: float
    is_paper: bool
    scheduled_at: float
    attempts: int = 0
    is_verification: bool = False


@dataclass
class ClaimResult:
    order_id: str
    market_slug: str
    condition_id: str
    claimed_usd: float
    success: bool
    is_paper: bool
    error: str | None = None


# ── Claimer ───────────────────────────────────────────────────────────────────

class Claimer:
    """
    Claims winning positions after market resolution.
    Retries up to 2 hours (120 × 60s). Persists to disk across restarts.

    One Claimer is created per asset lane. Each lane writes its own
    logs/pending_claims_{lane_id}.json so claimers don't load and
    re-process each other's claims after a restart (which would create
    multiple concurrent tasks all submitting to the same Polygon nonce).
    """

    CLAIM_DELAY_SEC = 120.0
    CLAIM_MAX_RETRIES = 120
    CLAIM_RETRY_INTERVAL = 60.0

    # Class-level lock — serialises ALL on-chain tx submissions across every
    # Claimer instance (all asset lanes share one Polygon wallet/nonce space).
    _class_tx_lock: ClassVar[asyncio.Lock | None] = None

    @classmethod
    def _get_tx_lock(cls) -> asyncio.Lock:
        if cls._class_tx_lock is None:
            cls._class_tx_lock = asyncio.Lock()
        return cls._class_tx_lock

    def __init__(self, order_manager: OrderManager, lane_id: str = "") -> None:
        self._orders = order_manager
        self._is_paper = cfg.paper_mode
        self._lane_id = lane_id
        self._file = _claims_file(lane_id) if lane_id else _LEGACY_CLAIMS_FILE
        self._pending: list[tuple[PendingClaim, Order]] = []
        self._active_claim_ids: set[str] = set()
        self._ghost_retry: list[dict] = []
        self._ghost_retry_active: bool = False
        # Track the last submitted tx so a stuck tx can be replaced with the
        # same nonce and ≥35% higher gas on the next retry.
        self._stuck_nonce: int | None = None
        self._stuck_gas_price: int | None = None
        self._load_persisted()

    def _clear_stuck(self) -> None:
        self._stuck_nonce = None
        self._stuck_gas_price = None

    # ── Scheduling ────────────────────────────────────────────────────────────

    def schedule_claim(self, order: Order, actual_direction: str) -> None:
        """Schedule a claim. Losses are also verified on-chain (Binance fallback may be wrong)."""
        won = order.direction == actual_direction

        # Actual cost = filled shares × fill price (not the intended order size,
        # which overstates cost for partial GTC fills).
        if order.filled_shares > 0 and (order.filled_price or order.price) > 0:
            actual_cost_usd = round(order.filled_shares * (order.filled_price or order.price), 2)
        else:
            actual_cost_usd = order.size_usd

        is_verification = False
        if not won:
            self._orders.mark_resolved(order.order_id, won=False, pnl=-(actual_cost_usd + order.fee_usd))
            if order.is_paper or self._is_paper or order.status != OrderStatus.FILLED:
                return
            is_verification = True
            logger.claim(  # type: ignore[attr-defined]
                f"[VERIFYING] {order.market_slug[:24]} | {order.direction} | "
                f"cost=${actual_cost_usd:.2f} — checking on-chain"
            )

        if order.status != OrderStatus.FILLED:
            return

        claim = PendingClaim(
            order_id=order.order_id,
            market_slug=order.market_slug,
            condition_id=order.condition_id,
            outcome=order.outcome,
            direction=order.direction,
            filled_shares=order.filled_shares,
            size_usd=actual_cost_usd,
            fee_usd=order.fee_usd,
            is_paper=order.is_paper,
            scheduled_at=time.time(),
            is_verification=is_verification,
        )
        self._pending.append((claim, order))
        self._save_persisted()
        logger.info(
            f"Claim scheduled: {order.market_slug[:20]}... "
            f"({order.filled_shares:.1f} shares, starting in {self.CLAIM_DELAY_SEC:.0f}s)"
        )

    async def process_pending_claims(self, wallet=None) -> list[ClaimResult]:
        """Spawn one background retry task per pending claim (deduped by order_id)."""
        for claim, order in list(self._pending):
            if claim.order_id not in self._active_claim_ids:
                self._active_claim_ids.add(claim.order_id)
                asyncio.create_task(
                    self._claim_with_retry(claim, order, wallet),
                    name=f"claim_{claim.order_id[:12]}",
                )

        if self._ghost_retry and not self._ghost_retry_active and wallet:
            self._ghost_retry_active = True
            asyncio.create_task(self._retry_ghost_claims(wallet), name="ghost_retry")

        return []

    async def _retry_ghost_claims(self, wallet) -> None:
        try:
            for pos in list(self._ghost_retry):
                result = await self._redeem_position_from_api(pos, wallet)
                if result.success:
                    self._ghost_retry.remove(pos)
                    logger.claim(  # type: ignore[attr-defined]
                        f"[GHOST RETRY ✓] {result.market_slug[:28]} | +${result.claimed_usd:.2f} USDC"
                    )
                elif result.error != "Not yet resolved on-chain" and "in-flight" not in (result.error or "").lower():
                    self._ghost_retry.remove(pos)
                    logger.info(f"Ghost retry dropped ({result.error}): {pos.get('conditionId', '')[:16]}")
                await asyncio.sleep(1.5)
        finally:
            self._ghost_retry_active = False

    # ── Core claim logic ──────────────────────────────────────────────────────

    async def _claim_with_retry(self, claim: PendingClaim, order: Order, wallet) -> None:
        try:
            if claim.is_paper or self._is_paper:
                await asyncio.sleep(2.0)
                net_pnl = claim.filled_shares - claim.size_usd
                self._orders.mark_resolved(order.order_id, won=True, pnl=net_pnl)
                logger.claim(  # type: ignore[attr-defined]
                    f"[CLAIM ✓] {claim.market_slug[:24]} | "
                    f"+${claim.filled_shares:.2f} gross | net P&L: {net_pnl:+.2f} [PAPER]"
                )
                self._remove_pending(claim.order_id)
                return

            remaining = max(0.0, self.CLAIM_DELAY_SEC - (time.time() - claim.scheduled_at))
            if remaining > 0:
                logger.info(f"Claim {claim.order_id[:12]}... waiting {remaining:.0f}s for oracle")
                await asyncio.sleep(remaining)

            if claim.attempts >= self.CLAIM_MAX_RETRIES:
                claim.attempts = 0  # Fresh window after restart with exhausted counter

            attempts_left = self.CLAIM_MAX_RETRIES - claim.attempts
            for i in range(attempts_left):
                if not any(c.order_id == claim.order_id for c, _ in self._pending):
                    return
                claim.attempts += 1
                self._save_persisted()

                if await self._try_claim_once(claim, order, wallet):
                    self._remove_pending(claim.order_id)
                    return

                if i < attempts_left - 1:
                    logger.info(
                        f"Claim {claim.order_id[:12]}... attempt {claim.attempts}/{self.CLAIM_MAX_RETRIES}"
                        f" — waiting {self.CLAIM_RETRY_INTERVAL:.0f}s"
                    )
                    await asyncio.sleep(self.CLAIM_RETRY_INTERVAL)

            logger.error(
                f"Claim TIMEOUT: {claim.order_id} after {claim.attempts} attempts "
                f"({claim.attempts * self.CLAIM_RETRY_INTERVAL / 60:.0f} min). "
                f"Persisted to {self._file} — will retry on next start.\n"
                f"Manual: conditionId={claim.condition_id}, "
                f"indexSet={'1' if claim.outcome == 'YES' else '2'}"
            )
        finally:
            self._active_claim_ids.discard(claim.order_id)

    async def _try_claim_once(self, claim: PendingClaim, order: Order, wallet) -> bool:
        """Attempt one on-chain redemption. Returns True on success, False to retry."""
        try:
            from web3 import Web3
            async with _web3_session() as w3:
                ctf = w3.eth.contract(
                    address=Web3.to_checksum_address(CONDITIONAL_TOKENS_ADDRESS),
                    abi=CONDITIONAL_TOKENS_ABI,
                )
                usdc = w3.eth.contract(
                    address=Web3.to_checksum_address(USDC_ADDRESS),
                    abi=USDC_ABI,
                )
                cid_bytes = _parse_cid(claim.condition_id)
                index_set = 1 if claim.outcome == "YES" else 2

                # Skip if oracle hasn't settled yet — saves gas on early retries.
                try:
                    if await ctf.functions.payoutDenominator(cid_bytes).call() == 0:
                        logger.debug(f"Claim {claim.order_id[:12]}... oracle not settled yet")
                        return False
                except Exception:
                    pass  # Some RPC nodes don't expose this view — proceed anyway

                # Capture balance before submitting (fallback if Transfer event absent)
                balance_before = await usdc.functions.balanceOf(wallet.address).call()

                async with self._get_tx_lock():
                    if self._stuck_nonce is not None:
                        # Replace stuck tx: same nonce, ≥35% higher gas.
                        nonce = self._stuck_nonce
                        gas_price = int(self._stuck_gas_price * 1.35)  # type: ignore[operator]
                        logger.info(
                            f"Claim {claim.order_id[:12]}... replacing stuck tx "
                            f"(nonce={nonce}, gas {self._stuck_gas_price} → {gas_price})"
                        )
                    else:
                        # Check for stuck mempool txs inside the lock so only one
                        # coroutine at a time polls/waits (prevents thundering herd).
                        confirmed = await w3.eth.get_transaction_count(wallet.address, 'latest')
                        nonce = await w3.eth.get_transaction_count(wallet.address, 'pending')
                        if nonce > confirmed:
                            await self._wait_for_nonce_clear(w3, wallet, confirmed, nonce)
                            nonce = await w3.eth.get_transaction_count(wallet.address, 'pending')
                        gas_price = int((await w3.eth.gas_price) * 1.2)

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
                    self._stuck_nonce = nonce
                    self._stuck_gas_price = gas_price

                receipt = await w3.eth.wait_for_transaction_receipt(tx_hash, timeout=90)

                if receipt["status"] != 1:
                    logger.warning(f"Claim {claim.order_id[:12]}... tx reverted — will retry")
                    self._clear_stuck()
                    return False

                # ── Measure exact USDC received from Transfer event ────────────
                # Reading balance_before/after is unreliable when multiple claim txs
                # land concurrently — the delta captures USDC from OTHER claims too.
                # Instead, parse the ERC-20 Transfer event emitted by this specific tx.
                # keccak256("Transfer(address,address,uint256)") — no 0x prefix
                TRANSFER_TOPIC = (
                    "ddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
                )
                # Strip 0x so we can match against HexBytes.hex() output (no prefix)
                wallet_topic = wallet.address.lower()[2:]
                usdc_received = 0.0
                for log_entry in receipt.get("logs", []):
                    if isinstance(log_entry, dict):
                        addr = log_entry.get("address", "")
                        topics = log_entry.get("topics", [])
                        data = log_entry.get("data", "0x")
                    else:
                        addr = getattr(log_entry, "address", "")
                        topics = getattr(log_entry, "topics", [])
                        data = getattr(log_entry, "data", "0x")

                    if addr.lower() != USDC_ADDRESS.lower() or len(topics) < 3:
                        continue
                    t0 = topics[0].hex() if hasattr(topics[0], "hex") else str(topics[0])
                    t2 = topics[2].hex() if hasattr(topics[2], "hex") else str(topics[2])
                    # t0 and t2 are bare hex (no 0x) from HexBytes.hex(); wallet_topic is also bare
                    if t0.lstrip("0x") == TRANSFER_TOPIC and wallet_topic in t2.lower():
                        raw_data = data.hex() if hasattr(data, "hex") else str(data)
                        if raw_data.startswith("0x"):
                            raw_data = raw_data[2:]
                        usdc_received = int(raw_data or "0", 16) / 1_000_000
                        break

                if usdc_received <= 0:
                    # Fallback: balance delta (less precise — captures concurrent claims)
                    balance_after = await usdc.functions.balanceOf(wallet.address).call()
                    usdc_received = (balance_after - balance_before) / 1_000_000

                self._clear_stuck()

                if usdc_received < 0.01:
                    # payoutDenominator > 0 but $0 received: either confirmed loss
                    # (verification claim) or already redeemed by a previous attempt.
                    if claim.is_verification:
                        net_loss = -(claim.size_usd + claim.fee_usd)
                        logger.claim(  # type: ignore[attr-defined]
                            f"[LOSS ✗] {claim.market_slug[:24]} | {claim.direction} | "
                            f"cost=${claim.size_usd:.2f} | net P&L: {net_loss:+.2f} "
                            f"(on-chain confirmed, tx={tx_hash.hex()[:16]}...)"
                        )
                    else:
                        logger.claim(  # type: ignore[attr-defined]
                            f"[REDEEMED] {claim.market_slug[:24]} | "
                            f"cost=${claim.size_usd:.2f} (already redeemed, tx={tx_hash.hex()[:16]}...)"
                        )
                    return True

                # ── Compute real P&L from on-chain redemption ─────────────────
                # For binary markets: winning shares pay $1.00 each.
                # actual_cost = usdc_received × entry_price
                # This is correct even when the recorded filled_shares was wrong
                # (e.g. premature partial marking or fallback-to-full-size bug),
                # as long as the fill price itself is correct.
                fill_price = (
                    claim.size_usd / claim.filled_shares
                    if claim.filled_shares > 0
                    else (order.price if order else 0.0)
                )
                if fill_price > 0:
                    actual_cost = round(usdc_received * fill_price, 2)
                    real_pnl = round(usdc_received - actual_cost, 2)
                else:
                    actual_cost = claim.size_usd
                    real_pnl = round(usdc_received - actual_cost, 2)

                # Derive the true filled_shares from redemption (winning share = $1)
                true_filled_shares = round(usdc_received, 4)

                self._orders.mark_resolved(order.order_id, won=True, pnl=real_pnl)
                asyncio.create_task(trade_db.resolve_trade(
                    order_id=claim.order_id, won=True,
                    actual_direction=claim.direction, pnl=real_pnl,
                    filled_shares=true_filled_shares,
                ))
                prefix = "[CLAIM ✓ — FALSE POSITIVE CORRECTED]" if claim.is_verification else "[CLAIM ✓]"
                logger.claim(  # type: ignore[attr-defined]
                    f"{prefix} {claim.market_slug[:24]} | "
                    f"+${usdc_received:.2f} USDC | cost=${actual_cost:.2f} | "
                    f"net P&L: {real_pnl:+.2f} "
                    f"(tx={tx_hash.hex()[:16]}...)"
                )
                return True

        except Exception as exc:
            exc_str = str(exc).lower()
            is_nonce_low = "nonce too low" in exc_str or "already known" in exc_str
            is_inflight = "in-flight" in exc_str or "in_flight" in exc_str or "limit reached" in exc_str
            is_replacement = not is_nonce_low and not is_inflight and (
                "nonce" in exc_str or "replacement" in exc_str
            )

            if is_inflight:
                self._clear_stuck()
                logger.warning(f"Claim {claim.order_id[:12]}... in-flight limit — will drain mempool")
                await asyncio.sleep(15.0)
            elif is_nonce_low:
                self._clear_stuck()
                logger.info(f"Claim {claim.order_id[:12]}... stuck tx confirmed — retrying fresh")
            elif is_replacement:
                # Keep stuck state — next attempt replaces with 35% gas bump.
                logger.warning(f"Claim {claim.order_id[:12]}... {exc} — bumping gas on retry")
                await asyncio.sleep(15.0)
            else:
                self._clear_stuck()
                logger.warning(f"Claim {claim.order_id[:12]}... {type(exc).__name__}: {exc}")
                if "timeout" in exc_str:
                    await asyncio.sleep(5.0)
            return False

    # ── Mempool management ────────────────────────────────────────────────────

    async def _wait_for_nonce_clear(self, w3, wallet, confirmed: int, pending: int) -> None:
        """
        Wait for stuck mempool txs to confirm on-chain.

        Polymarket's RPC enforces a 1-tx-in-flight limit per delegated account,
        so sending cancel txs also hits that limit and doesn't work. The only
        option is to wait for the stuck tx(s) to be processed by Polygon.
        This must be called while holding _get_tx_lock() to prevent concurrent
        coroutines from all polling simultaneously.
        """
        count = pending - confirmed
        logger.warning(
            f"Detected {count} stuck tx(s) (nonces {confirmed}–{pending - 1}) — "
            f"waiting for Polygon to process them (max 90s)"
        )
        try:
            for _ in range(18):  # 18 × 5s = 90s max
                await asyncio.sleep(5.0)
                new_confirmed = await w3.eth.get_transaction_count(wallet.address, 'latest')
                if new_confirmed >= pending:
                    logger.info(f"Stuck tx(s) confirmed — nonce now {new_confirmed}")
                    return
        except Exception as exc:
            logger.warning(f"Nonce poll failed: {exc}")
        finally:
            self._clear_stuck()
        logger.warning("Stuck txs still pending after 90s — proceeding with next nonce")

    # ── Ghost claim recovery ───────────────────────────────────────────────────

    async def recover_ghost_claims(self, wallet) -> list[ClaimResult]:
        """
        Recover positions with on-chain value that were never claimed by the
        normal flow (e.g. bot restarted mid-claim, oracle check timed out).
        Skips positions already tracked in self._pending.
        """
        if self._is_paper or not wallet:
            return []
        try:
            positions = await self._fetch_open_positions(wallet.address)
        except Exception as exc:
            logger.warning(f"Ghost claim scan: data API unavailable — {exc}")
            return []

        if not positions:
            logger.debug("Ghost claim scan: no unclaimed positions")
            return []

        pending_cids = {c.condition_id for c, _ in self._pending}
        candidates = [p for p in positions if p.get("conditionId", "") not in pending_cids]

        if not candidates:
            logger.info("Ghost claim scan: all positions already tracked")
            return []

        total = sum(float(p.get("currentValue", 0)) for p in candidates)
        logger.info(f"Ghost claim scan: {len(candidates)} position(s) worth ~${total:.2f}")

        results: list[ClaimResult] = []
        for pos in candidates:
            result = await self._redeem_position_from_api(pos, wallet)
            results.append(result)
            if result.success:
                logger.claim(  # type: ignore[attr-defined]
                    f"[GHOST CLAIM ✓] {result.market_slug[:28]} | +${result.claimed_usd:.2f} USDC"
                )
            elif result.error == "Not yet resolved on-chain":
                cid = pos.get("conditionId", "")
                if not any(p.get("conditionId") == cid for p in self._ghost_retry):
                    self._ghost_retry.append(pos)
                    logger.info(f"Ghost claim queued for retry: {result.market_slug[:28]}")
            await asyncio.sleep(1.5)

        ok = sum(1 for r in results if r.success)
        recovered = sum(r.claimed_usd for r in results if r.success)
        if ok:
            logger.info(f"Ghost recovery: {ok}/{len(candidates)} redeemed, +${recovered:.2f} USDC")
        else:
            logger.info("Ghost recovery: 0 redeemed (may not be resolved yet)")
        return results

    async def _fetch_open_positions(self, wallet_address: str) -> list[dict]:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{DATA_API_URL}/positions",
                params={"user": wallet_address, "sizeThreshold": "0.01"},
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()
        return [p for p in data if float(p.get("currentValue", 0)) > 0.01]

    async def _redeem_position_from_api(self, pos: dict, wallet) -> ClaimResult:
        """Attempt a single on-chain redemption for a position from the data API."""
        condition_id = pos.get("conditionId", "")
        outcome = pos.get("outcome", "")
        market_slug = pos.get("title", pos.get("market", "unknown"))[:40]

        def _err(msg: str) -> ClaimResult:
            return ClaimResult(
                order_id=condition_id, market_slug=market_slug,
                condition_id=condition_id, claimed_usd=0.0,
                success=False, is_paper=False, error=msg,
            )

        if not condition_id:
            return _err("No conditionId in position data")
        try:
            cid_bytes = _parse_cid(condition_id)
        except ValueError as exc:
            return _err(f"Bad conditionId: {exc}")

        outcome_lower = outcome.lower()
        if outcome_lower in ("yes", "up"):
            index_set = 1
        elif outcome_lower in ("no", "down"):
            index_set = 2
        else:
            return _err(f"Unknown outcome: '{outcome}'")

        try:
            from web3 import Web3
            async with _web3_session() as w3:
                ctf = w3.eth.contract(
                    address=Web3.to_checksum_address(CONDITIONAL_TOKENS_ADDRESS),
                    abi=CONDITIONAL_TOKENS_ABI,
                )
                usdc = w3.eth.contract(
                    address=Web3.to_checksum_address(USDC_ADDRESS),
                    abi=USDC_ABI,
                )

                if await ctf.functions.payoutDenominator(cid_bytes).call() == 0:
                    return _err("Not yet resolved on-chain")

                balance_before = await usdc.functions.balanceOf(wallet.address).call()

                async with self._get_tx_lock():
                    confirmed = await w3.eth.get_transaction_count(wallet.address, 'latest')
                    nonce = await w3.eth.get_transaction_count(wallet.address, 'pending')
                    if nonce > confirmed:
                        await self._wait_for_nonce_clear(w3, wallet, confirmed, nonce)
                        nonce = await w3.eth.get_transaction_count(wallet.address, 'pending')
                    gas_price = int((await w3.eth.gas_price) * 1.2)
                    tx = await ctf.functions.redeemPositions(
                        Web3.to_checksum_address(USDC_ADDRESS),
                        b"\x00" * 32, cid_bytes, [index_set],
                    ).build_transaction({
                        "from": wallet.address, "nonce": nonce,
                        "gasPrice": gas_price, "gas": 250_000,
                    })
                    signed = w3.eth.account.sign_transaction(tx, cfg.normalized_private_key)
                    tx_hash = await w3.eth.send_raw_transaction(signed.raw_transaction)

                receipt = await w3.eth.wait_for_transaction_receipt(tx_hash, timeout=90)
                if receipt["status"] != 1:
                    return _err(f"Tx reverted ({tx_hash.hex()[:16]})")

                balance_after = await usdc.functions.balanceOf(wallet.address).call()
                usdc_received = (balance_after - balance_before) / 1e6
                if usdc_received < 0.01:
                    return _err("$0 received (already redeemed?)")

                return ClaimResult(
                    order_id=condition_id, market_slug=market_slug,
                    condition_id=condition_id, claimed_usd=usdc_received,
                    success=True, is_paper=False,
                )
        except Exception as exc:
            logger.warning(f"Ghost claim {condition_id[:12]}: {type(exc).__name__}: {exc}")
            return _err(str(exc))

    # ── Persistence ───────────────────────────────────────────────────────────

    def _save_persisted(self) -> None:
        try:
            self._file.parent.mkdir(parents=True, exist_ok=True)
            with open(self._file, "w") as f:
                json.dump([asdict(c) for c, _ in self._pending], f, indent=2)
        except Exception as exc:
            logger.debug(f"Failed to persist claims: {exc}")

    def _load_persisted(self) -> None:
        # Prefer the per-lane file; fall back to the legacy shared file on
        # first run after the per-lane migration so pending claims aren't lost.
        src = self._file if self._file.exists() else (
            _LEGACY_CLAIMS_FILE if _LEGACY_CLAIMS_FILE.exists() and self._lane_id else None
        )
        if src is None:
            return
        try:
            with open(src) as f:
                data = json.load(f)
            # When migrating from the legacy shared file, each lane should only
            # adopt claims whose slug starts with its own prefix.
            if src == _LEGACY_CLAIMS_FILE and self._lane_id:
                data = [d for d in data if d.get("market_slug", "").startswith(self._lane_id)]
            for item in data:
                claim = PendingClaim(**item)
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
                logger.info(f"Loaded {len(self._pending)} pending claim(s) from previous session")
        except Exception as exc:
            logger.warning(f"Failed to load persisted claims: {exc}")

    def _remove_pending(self, order_id: str) -> None:
        self._pending = [(c, o) for c, o in self._pending if c.order_id != order_id]
        self._save_persisted()
