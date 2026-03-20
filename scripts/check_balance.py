"""Check wallet USDC + MATIC balance."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.config import get_config
from core.logger import setup_logging, get_logger

setup_logging(level="WARNING")
cfg = get_config()


async def main() -> None:
    if not cfg.has_wallet():
        print("No PRIVATE_KEY configured in .env")
        return

    from execution.wallet import Wallet
    wallet = Wallet()
    balances = await wallet.log_balances()
    print(f"\nWallet: {wallet.address}")
    print(f"USDC:   ${balances['usdc']:.4f}")
    print(f"MATIC:  {balances['matic']:.6f} POL\n")


if __name__ == "__main__":
    asyncio.run(main())
