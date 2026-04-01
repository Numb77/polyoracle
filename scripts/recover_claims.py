"""
Recovery script — claims all unclaimed winning positions from Polymarket.

Fetches all positions with currentValue > 0 from the Polymarket data API,
checks each one is resolved on-chain (payoutDenominator > 0), then calls
redeemPositions on the Gnosis ConditionalTokens contract to collect USDC.

Usage:
    python scripts/recover_claims.py [--dry-run] [--speed-up]

    --dry-run    Show what would be claimed without sending any transactions.
    --speed-up   Replace any stuck mempool tx with a 50% gas bump, then claim.
"""

from __future__ import annotations

import asyncio
import sys
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.config import get_config
from core.logger import setup_logging, get_logger

setup_logging(level="INFO")
cfg = get_config()
logger = get_logger("recover_claims")


# ── Contract addresses (Polygon mainnet) ─────────────────────────────────────

CONDITIONAL_TOKENS_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

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
    {
        "inputs": [{"name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
]

DATA_API_URL = "https://data-api.polymarket.com"


async def fetch_positions(wallet_address: str) -> list[dict]:
    """Fetch all open positions from the Polymarket data API."""
    import aiohttp

    url = f"{DATA_API_URL}/positions"
    params = {"user": wallet_address, "sizeThreshold": "0.01"}

    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params) as resp:
            resp.raise_for_status()
            data = await resp.json()

    # Filter to positions with meaningful value
    positions = [p for p in data if float(p.get("currentValue", 0)) > 0.01]
    logger.info(f"Found {len(positions)} positions with value > $0.01 "
                f"(total API response: {len(data)} positions)")
    return positions


async def speed_up_stuck_tx(w3, wallet, confirmed: int, pending: int) -> bool:
    """
    Replace stuck mempool txs by sending a 0-MATIC self-transfer with the same
    nonce(s) but 50% higher gas.  Returns True once all stuck nonces are cleared.
    """
    from web3 import Web3
    logger.info(f"Attempting to speed up {pending - confirmed} stuck tx(s)...")
    for nonce in range(confirmed, pending):
        try:
            current_gas = await w3.eth.gas_price
            bump_gas = int(current_gas * 1.5)
            tx = {
                "from": wallet.address,
                "to": wallet.address,
                "value": 0,
                "nonce": nonce,
                "gasPrice": bump_gas,
                "gas": 21_000,
                "chainId": 137,
            }
            signed = w3.eth.account.sign_transaction(tx, cfg.normalized_private_key)
            tx_hash = await w3.eth.send_raw_transaction(signed.raw_transaction)
            logger.info(f"  Speed-up tx submitted for nonce {nonce}: {tx_hash.hex()[:20]}...")
        except Exception as exc:
            exc_s = str(exc).lower()
            if "nonce too low" in exc_s or "already known" in exc_s:
                logger.info(f"  Nonce {nonce} already confirmed — skipping")
            else:
                logger.warning(f"  Speed-up for nonce {nonce} failed: {exc}")

    # Wait up to 3 min for the replacements to confirm
    logger.info("Waiting for speed-up tx(s) to confirm (max 3 min)...")
    for i in range(18):
        await asyncio.sleep(10.0)
        new_confirmed = await w3.eth.get_transaction_count(wallet.address, 'latest')
        if new_confirmed >= pending:
            logger.info(f"All stuck tx(s) cleared — nonce now {new_confirmed}")
            return True
        logger.info(f"  Still waiting ({(i+1)*10}s): confirmed={new_confirmed}, pending={pending}")
    logger.error("Speed-up txs not confirmed after 3 min — try again or wait longer")
    return False


async def recover_all(dry_run: bool = False, speed_up: bool = False) -> None:
    if not cfg.has_wallet():
        logger.error("No PRIVATE_KEY configured in .env — cannot sign transactions")
        return

    from web3 import Web3, AsyncWeb3
    try:
        from web3.middleware import ExtraDataToPOAMiddleware as _POAMiddleware
    except ImportError:
        from web3.middleware import geth_poa_middleware as _POAMiddleware

    from execution.wallet import Wallet
    wallet = Wallet()
    logger.info(f"Wallet: {wallet.address}")

    # ── Connect to Polygon ────────────────────────────────────────────────────
    w3 = AsyncWeb3(AsyncWeb3.AsyncHTTPProvider(cfg.polygon_rpc_url))
    w3.middleware_onion.inject(_POAMiddleware, layer=0)

    ctf = w3.eth.contract(
        address=Web3.to_checksum_address(CONDITIONAL_TOKENS_ADDRESS),
        abi=CONDITIONAL_TOKENS_ABI,
    )
    usdc = w3.eth.contract(
        address=Web3.to_checksum_address(USDC_ADDRESS),
        abi=USDC_ABI,
    )

    # ── Wait for any stuck mempool txs to confirm ────────────────────────────
    # Dry-run doesn't submit any transactions, so skip the mempool check.
    confirmed = await w3.eth.get_transaction_count(wallet.address, 'latest')
    pending = await w3.eth.get_transaction_count(wallet.address, 'pending')
    if not dry_run and pending > confirmed:
        count = pending - confirmed
        logger.warning(
            f"Detected {count} stuck tx(s) in mempool (nonces {confirmed}–{pending-1})."
        )
        if speed_up:
            cleared = await speed_up_stuck_tx(w3, wallet, confirmed, pending)
        else:
            logger.warning(
                "Waiting for them to confirm (max 5 min). "
                "Run with --speed-up to replace them immediately."
            )
            cleared = False
            for i in range(30):  # 30 × 10s = 5 min
                await asyncio.sleep(10.0)
                new_confirmed = await w3.eth.get_transaction_count(wallet.address, 'latest')
                if new_confirmed >= pending:
                    logger.info(f"Stuck tx(s) confirmed — nonce now {new_confirmed}. Proceeding.")
                    cleared = True
                    break
                logger.info(f"  Still waiting ({(i+1)*10}s): confirmed={new_confirmed}, pending={pending}")
        if not cleared:
            logger.error("Stuck txs still pending — aborting. Try again later.")
            return

    # ── Fetch positions ───────────────────────────────────────────────────────
    logger.info("Fetching unclaimed positions from Polymarket data API...")
    positions = await fetch_positions(wallet.address)

    if not positions:
        logger.info("No unclaimed positions found.")
        return

    total_value = sum(float(p.get("currentValue", 0)) for p in positions)
    logger.info(f"\n{'='*60}")
    logger.info(f"  {len(positions)} positions to claim, total value: ${total_value:.2f}")
    logger.info(f"{'='*60}\n")

    for i, pos in enumerate(positions, 1):
        condition_id = pos.get("conditionId", "")
        outcome = pos.get("outcome", "")
        market_slug = pos.get("title", pos.get("market", "unknown"))[:40]
        current_value = float(pos.get("currentValue", 0))
        size = float(pos.get("size", 0))

        logger.info(
            f"[{i}/{len(positions)}] {market_slug} | "
            f"outcome={outcome} | value=${current_value:.2f} | shares={size:.2f}"
        )

        if not condition_id:
            logger.warning(f"  Skipping: no conditionId in position data")
            continue

        # Parse conditionId to bytes32
        cid = condition_id
        try:
            condition_id_bytes = bytes.fromhex(cid[2:] if cid.startswith("0x") else cid)
        except ValueError as e:
            logger.warning(f"  Skipping: invalid conditionId format: {e}")
            continue

        # ── Check on-chain resolution ─────────────────────────────────────
        try:
            payout_denom = await ctf.functions.payoutDenominator(
                condition_id_bytes
            ).call()
        except Exception as exc:
            logger.warning(f"  payoutDenominator check failed: {exc} — skipping")
            continue

        if payout_denom == 0:
            logger.info(f"  Not yet resolved on-chain (payoutDenominator=0) — skipping")
            continue

        logger.info(f"  Resolved on-chain (payoutDenominator={payout_denom}) ✓")

        # indexSet: YES / Up = 1 (bit 0), NO / Down = 2 (bit 1)
        outcome_lower = outcome.lower()
        if outcome_lower in ("yes", "up"):
            index_set = 1
        elif outcome_lower in ("no", "down"):
            index_set = 2
        else:
            # Try to infer from position data
            logger.warning(f"  Unknown outcome '{outcome}' — defaulting to indexSet=1 (YES). "
                           f"Verify manually if this fails.")
            index_set = 1

        if dry_run:
            logger.info(
                f"  [DRY RUN] Would call redeemPositions("
                f"conditionId={cid[:16]}..., indexSet={index_set})"
            )
            continue

        # ── Send redemption transaction ───────────────────────────────────
        try:
            balance_before = await usdc.functions.balanceOf(wallet.address).call()

            # Wait for any in-flight tx from a previous claim to confirm.
            # The RPC enforces a 1-tx-in-flight limit, so we must confirm
            # each claim before submitting the next.
            for attempt in range(60):  # up to 5 min (60 × 5s)
                cur_confirmed = await w3.eth.get_transaction_count(wallet.address, 'latest')
                cur_pending = await w3.eth.get_transaction_count(wallet.address, 'pending')
                if cur_pending <= cur_confirmed:
                    break
                if attempt == 0:
                    logger.info(f"  Waiting for previous tx to confirm (nonce {cur_confirmed}→{cur_pending-1})...")
                await asyncio.sleep(5.0)
            else:
                logger.error("  Previous tx still unconfirmed after 5 min — skipping this claim")
                continue

            nonce = await w3.eth.get_transaction_count(wallet.address, 'pending')
            gas_price = int((await w3.eth.gas_price) * 1.2)

            tx = await ctf.functions.redeemPositions(
                Web3.to_checksum_address(USDC_ADDRESS),
                b"\x00" * 32,       # parentCollectionId = 0 (top-level)
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

            logger.info(f"  Tx submitted: {tx_hash.hex()}")
            receipt = await w3.eth.wait_for_transaction_receipt(tx_hash, timeout=300)

            if receipt["status"] != 1:
                logger.warning(f"  Tx REVERTED — condition may not be fully settled yet")
                continue

            balance_after = await usdc.functions.balanceOf(wallet.address).call()
            usdc_received = (balance_after - balance_before) / 1e6

            if usdc_received < 0.01:
                logger.warning(
                    f"  Tx succeeded but $0 USDC received "
                    f"(balance: ${balance_before/1e6:.2f} → ${balance_after/1e6:.2f}) "
                    f"— position may already be redeemed or wrong indexSet"
                )
            else:
                logger.info(
                    f"  ✓ CLAIMED ${usdc_received:.4f} USDC "
                    f"(tx={tx_hash.hex()[:20]}...)"
                )

        except Exception as exc:
            logger.error(f"  Transaction failed: {type(exc).__name__}: {exc}")
            await asyncio.sleep(3.0)
            continue

    # ── Final balance ─────────────────────────────────────────────────────────
    if not dry_run:
        final_balance = await usdc.functions.balanceOf(wallet.address).call()
        logger.info(f"\n{'='*60}")
        logger.info(f"  Recovery complete. USDC balance: ${final_balance/1e6:.4f}")
        logger.info(f"{'='*60}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Recover unclaimed Polymarket positions")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show positions without sending transactions")
    parser.add_argument("--speed-up", action="store_true",
                        help="Replace stuck mempool txs with 50%% higher gas, then claim")
    args = parser.parse_args()

    if args.dry_run:
        logger.info("DRY RUN MODE — no transactions will be sent")

    asyncio.run(recover_all(dry_run=args.dry_run, speed_up=args.speed_up))


if __name__ == "__main__":
    main()
