"""
One-time wallet setup script.

Run this before your first live trade:
    python scripts/setup_wallet.py

This will:
1. Derive your Polygon address from the private key
2. Check USDC and MATIC balances
3. Approve USDC spending to CTF Exchange (if not already approved)
4. Generate and save Polymarket API credentials
5. Verify connectivity to the CLOB API
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.config import get_config
from core.logger import get_logger, setup_logging

setup_logging(level="INFO")
logger = get_logger("setup_wallet")
cfg = get_config()


async def main() -> None:
    print("\n" + "=" * 60)
    print("  PolyOracle — Wallet Setup")
    print("=" * 60)

    # ── Check private key ─────────────────────────────────────────────────────
    if not cfg.has_wallet():
        print("\n❌ No private key found.")
        print("   Set PRIVATE_KEY=0x... in your .env file.")
        sys.exit(1)

    # ── Import wallet ─────────────────────────────────────────────────────────
    from execution.wallet import Wallet, CTF_EXCHANGE, USDC_CONTRACT, USDC_DECIMALS  # noqa: F401
    from web3 import Web3

    wallet = Wallet()
    print(f"\n✓ Wallet address: {wallet.address}")

    # ── Check balances ────────────────────────────────────────────────────────
    print("\nChecking balances...")
    usdc, matic = await asyncio.gather(
        wallet.get_usdc_balance(),
        wallet.get_matic_balance(),
    )
    print(f"  USDC:   ${usdc:.2f}")
    print(f"  MATIC:  {matic:.4f} POL")

    if matic < 0.1:
        print("\n⚠️  Low MATIC balance — you need at least 0.1 MATIC for gas")
        print("   Bridge MATIC to Polygon at https://wallet.polygon.technology/")

    if usdc < 20:
        print("\n⚠️  Low USDC balance — deposit USDC to Polygon via Polymarket")
        print(f"   Deposit address: {cfg.funder_address or wallet.address}")

    # ── Check USDC allowance ──────────────────────────────────────────────────
    print("\nChecking USDC approval...")
    allowance = await wallet.get_usdc_allowance(CTF_EXCHANGE)
    print(f"  Current allowance: ${allowance:.2f}")

    if allowance < 1000:
        print("\n  Approving USDC spend to CTF Exchange...")
        print(f"  Contract: {CTF_EXCHANGE}")
        confirm = input("  Proceed? (yes/no): ").strip().lower()
        if confirm != "yes":
            print("  Skipped approval.")
        else:
            await _approve_usdc(wallet)

    # ── Generate API credentials ──────────────────────────────────────────────
    print("\nGenerating Polymarket API credentials...")
    try:
        client = wallet.get_clob_client()
        # Run in thread pool (blocking call)
        creds = await asyncio.get_event_loop().run_in_executor(
            None, client.create_or_derive_api_creds
        )
        print(f"  ✓ API key: {str(creds.api_key)[:20]}...")
        print("\n  Add these to your .env file:")
        print(f"  CLOB_API_KEY={creds.api_key}")
        print(f"  CLOB_SECRET={creds.api_secret}")
        print(f"  CLOB_PASS_PHRASE={creds.api_passphrase}")
    except Exception as exc:
        print(f"  ⚠️  Could not generate API creds: {exc}")
        print("     You may need to set SIGNATURE_TYPE in .env")

    # ── Verify CLOB connectivity ──────────────────────────────────────────────
    print("\nVerifying CLOB API connectivity...")
    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{cfg.polymarket_clob_url}/time",
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                data = await resp.json()
                server_time = data.get("time", 0)
                print(f"  ✓ CLOB API reachable (server time: {server_time})")
    except Exception as exc:
        print(f"  ⚠️  CLOB API check failed: {exc}")

    print("\n" + "=" * 60)
    print("  Setup complete! Next steps:")
    print("  1. Set PAPER_MODE=true in .env (start with paper trading!)")
    print("  2. Run: python -m core.main --paper")
    print("  3. Monitor at: http://localhost:3000")
    print("=" * 60 + "\n")


async def _approve_usdc(wallet) -> None:
    """Approve max USDC spending to the CTF Exchange."""
    from execution.wallet import CTF_EXCHANGE, USDC_CONTRACT, USDC_DECIMALS, ERC20_ABI
    from web3 import Web3, AsyncWeb3
    try:
        from web3.middleware import ExtraDataToPOAMiddleware as _POAMiddleware
    except ImportError:
        from web3.middleware import geth_poa_middleware as _POAMiddleware

    MAX_UINT256 = 2**256 - 1

    try:
        w3 = AsyncWeb3(AsyncWeb3.AsyncHTTPProvider(cfg.polygon_rpc_url))
        w3.middleware_onion.inject(_POAMiddleware, layer=0)

        usdc = w3.eth.contract(
            address=Web3.to_checksum_address(USDC_CONTRACT),
            abi=ERC20_ABI,
        )

        nonce = await w3.eth.get_transaction_count(wallet.address)
        gas_price = await w3.eth.gas_price

        tx = await usdc.functions.approve(
            Web3.to_checksum_address(CTF_EXCHANGE),
            MAX_UINT256,
        ).build_transaction({
            "from": wallet.address,
            "nonce": nonce,
            "gasPrice": gas_price,
            "gas": 100_000,
        })

        signed = w3.eth.account.sign_transaction(tx, cfg.normalized_private_key)
        tx_hash = await w3.eth.send_raw_transaction(signed.raw_transaction)
        print(f"  Transaction sent: {tx_hash.hex()}")

        receipt = await w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        if receipt["status"] == 1:
            print("  ✓ USDC approval confirmed!")
        else:
            print("  ❌ Approval transaction failed")

    except Exception as exc:
        print(f"  ❌ Approval failed: {exc}")


if __name__ == "__main__":
    asyncio.run(main())
