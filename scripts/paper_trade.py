"""
Paper trading mode — runs all strategy logic with simulated fills.
No real orders are placed. Safe to run with or without a private key.

Usage:
    python scripts/paper_trade.py
    python scripts/paper_trade.py --balance 500
    python scripts/paper_trade.py --confidence 70
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import click

from core.config import get_config
from core.logger import get_logger, setup_logging

cfg = get_config()


@click.command()
@click.option("--balance", default=1000.0, type=float, help="Starting paper balance in USDC")
@click.option("--confidence", default=None, type=int, help="Override confidence threshold (0-100)")
@click.option("--log-level", default="INFO", help="Log level")
def main(balance: float, confidence: int | None, log_level: str) -> None:
    """
    Start PolyOracle in paper trading mode.

    All trading logic runs normally but no real orders are placed.
    Simulated fills at the best available ask price.
    """
    setup_logging(level=log_level, log_file=cfg.log_file)
    logger = get_logger("paper_trade")

    # Force paper mode
    os.environ["PAPER_MODE"] = "true"
    os.environ["PAPER_INITIAL_BALANCE"] = str(balance)

    if confidence is not None:
        os.environ["MIN_CONFIDENCE_SCORE"] = str(confidence)

    # Reload config
    from core.config import Config
    import importlib
    import core.config as config_module
    config_module.cfg = Config()
    cfg_live = config_module.cfg

    logger.info(
        f"Starting paper trading mode | "
        f"balance=${balance:.2f} | "
        f"confidence={cfg_live.min_confidence_score}"
    )
    print(f"""
╔══════════════════════════════════════════════╗
║        PolyOracle — PAPER TRADING MODE       ║
║                                              ║
║  Starting balance: ${balance:<8.2f}               ║
║  Confidence threshold: {cfg_live.min_confidence_score:<3}                  ║
║  Dashboard: http://localhost:{cfg_live.dashboard_port}           ║
║                                              ║
║  Press Ctrl+C to stop                        ║
╚══════════════════════════════════════════════╝
    """)

    from core.main import PolyOracle
    import signal

    bot = PolyOracle(paper_mode=True)
    loop = asyncio.get_event_loop()

    def _shutdown():
        logger.info("Shutting down paper trader...")
        for task in asyncio.all_tasks(loop):
            task.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _shutdown)

    try:
        loop.run_until_complete(bot.run())
    except KeyboardInterrupt:
        pass
    finally:
        loop.close()


if __name__ == "__main__":
    main()
