"""
Backtest engine — runs the strategy on historical data.

Downloads historical BTC 1-minute OHLCV from Binance and simulates
5-minute window trades using the late-window strategy logic.

Usage:
    python scripts/backtest.py --days 30
    python scripts/backtest.py --days 7 --confidence 70 --kelly 0.25
"""

from __future__ import annotations

import asyncio
import sys
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import click
import aiohttp
import pandas as pd
import numpy as np

from core.logger import setup_logging, get_logger

setup_logging(level="INFO")
logger = get_logger("backtest")

BINANCE_REST = "https://api.binance.com"


@dataclass
class BacktestTrade:
    window_ts: int
    direction: str
    actual: str
    won: bool
    entry_price: float   # Token price paid (simulated at 0.80 for in-range signals)
    size_usd: float
    pnl: float
    window_delta_pct: float
    confidence_sim: float


async def fetch_klines(days: int = 30) -> pd.DataFrame:
    """Fetch historical 1-minute OHLCV from Binance."""
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - days * 24 * 3600 * 1000

    all_klines = []
    batch_start = start_ms

    async with aiohttp.ClientSession() as session:
        while batch_start < end_ms:
            url = (
                f"{BINANCE_REST}/api/v3/klines"
                f"?symbol=BTCUSDT&interval=1m"
                f"&startTime={batch_start}&limit=1000"
            )
            async with session.get(url) as resp:
                klines = await resp.json()
                if not klines:
                    break
                all_klines.extend(klines)
                batch_start = klines[-1][0] + 60000
                await asyncio.sleep(0.1)  # rate limit

    logger.info(f"Downloaded {len(all_klines)} 1-minute candles ({days} days)")

    df = pd.DataFrame(all_klines, columns=[
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_vol", "trades", "taker_buy_base",
        "taker_buy_quote", "ignore"
    ])
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)
    df.set_index("open_time", inplace=True)
    return df


def simulate_5min_windows(
    df: pd.DataFrame,
    min_confidence: int = 65,
    trade_size: float = 10.0,
    kelly_fraction: float = 0.25,
) -> list[BacktestTrade]:
    """Simulate 5-minute window trades using simplified strategy logic."""
    trades = []

    # Resample to 5-minute windows
    df_5m = df.resample("5min").agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
    }).dropna()

    logger.info(f"Simulating {len(df_5m)} 5-minute windows...")

    for i in range(10, len(df_5m)):
        window = df_5m.iloc[i]
        window_open = window["open"]
        window_close = window["close"]

        # Get last 30 seconds of the window (T-30s price)
        # Approximate: use price from 30s before close (1-min bar)
        # For simplicity, use the last 1-min candle's close as T-30s price
        t_minus_30_idx = max(0, i * 5 - 1)  # approximate
        late_price = df.iloc[t_minus_30_idx]["close"] if t_minus_30_idx < len(df) else window["close"]

        # Window delta at T-30s
        window_delta_pct = (late_price - window_open) / window_open * 100

        # Simple confidence simulation
        abs_delta = abs(window_delta_pct)

        # Delta contribution (0-40 pts max from signal)
        signal_pts = min(abs_delta / 0.1 * 40, 40)

        # Regime: use 1-min candles from last 5 windows
        recent_df = df.iloc[max(0, (i - 5) * 5): i * 5]
        if len(recent_df) >= 10:
            atr_approx = (
                (recent_df["high"] - recent_df["low"]) / recent_df["close"]
            ).mean() * 100
            regime_bonus = 10.0 if atr_approx > 0.05 else 3.0
        else:
            regime_bonus = 5.0

        # Simple agent consensus sim: random with bias toward window delta
        agent_agreement = 0.6 + (abs_delta / 0.2) * 0.2
        agent_agreement = min(agent_agreement, 1.0)
        agent_pts = 20.0 if agent_agreement >= 0.8 else 10.0

        # Delta pts
        delta_pts = 20.0 if abs_delta >= 0.10 else (12.0 if abs_delta >= 0.05 else (5.0 if abs_delta >= 0.02 else 0.0))

        confidence = min(signal_pts + agent_pts + delta_pts + regime_bonus, 100.0)

        if confidence < min_confidence:
            continue

        if abs_delta < 0.015:
            continue

        # Direction
        direction = "UP" if window_delta_pct > 0 else "DOWN"
        actual = "UP" if window_close > window_open else "DOWN"
        won = direction == actual

        # Simulate token price: 0.70-0.90 range
        entry_price = min(0.90, max(0.55, 0.50 + abs_delta * 4))

        # P&L
        pnl = (trade_size / entry_price - trade_size) if won else -trade_size

        trades.append(BacktestTrade(
            window_ts=int(df_5m.index[i].timestamp()),
            direction=direction,
            actual=actual,
            won=won,
            entry_price=entry_price,
            size_usd=trade_size,
            pnl=pnl,
            window_delta_pct=window_delta_pct,
            confidence_sim=confidence,
        ))

    return trades


def print_results(trades: list[BacktestTrade], days: int) -> None:
    """Print backtest results."""
    if not trades:
        print("No trades simulated.")
        return

    total = len(trades)
    wins = sum(1 for t in trades if t.won)
    total_pnl = sum(t.pnl for t in trades)
    win_rate = wins / total

    print(f"""
╔══════════════════════════════════════════════════╗
║           POLYORACLE BACKTEST RESULTS            ║
╠══════════════════════════════════════════════════╣
  Period:         {days} days
  Total trades:   {total}
  Win rate:       {win_rate:.1%}
  Wins/Losses:    {wins}/{total - wins}

  Total P&L:      ${total_pnl:+.2f}
  Avg per trade:  ${total_pnl/total:+.2f}
  Best trade:     ${max(t.pnl for t in trades):+.2f}
  Worst trade:    ${min(t.pnl for t in trades):+.2f}

  Avg confidence: {sum(t.confidence_sim for t in trades)/total:.1f}
  Avg delta:      {sum(abs(t.window_delta_pct) for t in trades)/total:.3f}%

  Trades/day:     {total/days:.1f}
╚══════════════════════════════════════════════════╝
    """)

    # Win rate by confidence bucket
    print("Win rate by confidence:")
    for low, high in [(65, 75), (75, 85), (85, 100)]:
        bucket = [t for t in trades if low <= t.confidence_sim < high]
        if bucket:
            wr = sum(1 for t in bucket if t.won) / len(bucket)
            print(f"  {low}-{high}: {wr:.1%} ({len(bucket)} trades)")


@click.command()
@click.option("--days", default=30, type=int, help="Days of history to backtest")
@click.option("--confidence", default=65, type=int, help="Minimum confidence threshold")
@click.option("--size", default=10.0, type=float, help="Trade size in USDC")
@click.option("--kelly", default=0.25, type=float, help="Kelly fraction")
def main(days: int, confidence: int, size: float, kelly: float) -> None:
    """Run historical backtest of the Late-Window strategy."""
    print(f"\nDownloading {days} days of BTC 1-min data from Binance...")

    async def run():
        df = await fetch_klines(days)
        trades = simulate_5min_windows(df, confidence, size, kelly)
        print_results(trades, days)

    asyncio.run(run())


if __name__ == "__main__":
    main()
