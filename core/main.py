"""
PolyOracle — Main orchestrator loop.

Wires together all components and runs the main trading loop:
  1. Start data feeds (Binance WS, Polymarket WS, Chainlink oracle)
  2. Run the window clock
  3. At T-30s: begin strategy evaluation
  4. At T-10s: if confidence high enough, execute trade
  5. At T+0s: wait for resolution, auto-claim if won
  6. Push all state updates to dashboard via WebSocket

Usage:
    python -m core.main           # Live trading (requires PAPER_MODE=false in .env)
    python -m core.main --paper   # Paper trading (default)
    python -m core.main --help
"""

from __future__ import annotations

import asyncio
import signal
import sys
import time
from pathlib import Path
from typing import Optional

import aiohttp
import click

from core.asset_config import AssetConfig
from core.asset_lane import AssetLane
from core.clock import WindowClock, WindowPhase, WindowState
from core.config import get_config
from core.logger import get_logger, setup_logging, add_dashboard_handler
from data.aggregator import PriceAggregator
import data.trade_db as trade_db
from data.binance_ws import BinanceWebSocket, BtcTick, get_window_open_price, get_btc_window_open_price, get_window_close_price
from data.candle_builder import CandleBuilder
from data.chainlink_oracle import ChainlinkOracle
from data.gamma_api import GammaClient
from data.polymarket_rest import PolymarketRestClient
from data.polymarket_ws import PolymarketWebSocket
from agents.consensus import ConsensusEngine
from agents.meta_learner import MetaLearner
from agents.momentum_agent import MomentumAgent
from agents.mean_reversion_agent import MeanReversionAgent
from agents.volatility_agent import VolatilityAgent
from agents.orderflow_agent import OrderFlowAgent
from agents.oracle_agent import OracleAgent
from execution.claimer import Claimer
from execution.fee_calculator import FeeCalculator
from execution.order_manager import OrderManager, OrderStatus
from execution.polymarket_executor import PolymarketExecutor
from execution.token_resolver import TokenResolver
from execution.wallet import Wallet
from risk.circuit_breaker import CircuitBreaker, CircuitTier
from risk.drawdown_monitor import DrawdownMonitor
from risk.exposure_manager import ExposureManager
from risk.pnl_tracker import PnlTracker
from risk.position_sizer import PositionSizer
from strategy.late_window import LateWindowStrategy
from websocket_server.server import DashboardServer

logger = get_logger(__name__)
cfg = get_config()


def _order_actual_cost(order) -> float:
    """
    Return the actual USDC at risk for an order.

    For ACTIVE orders (still resting in the book) we don't know the final fill
    amount yet, so we reserve the full intended size_usd.

    For FILLED orders, the real cost is filled_shares × filled_price.
    Using size_usd (the *intended* order size) for partial GTC fills inflates
    exposure — e.g. a $200 GTC that only matched 10 shares at $0.65 ($6.50
    actual cost) would count as $200, permanently blocking new trades.
    """
    from execution.order_manager import OrderStatus
    if (
        order.status == OrderStatus.FILLED
        and order.filled_shares > 0
        and (order.filled_price or order.price) > 0
    ):
        return round(order.filled_shares * (order.filled_price or order.price), 2)
    return order.size_usd


class PolyOracle:
    """
    Main bot orchestrator. Wires all components and runs the event loop.

    Each tradable asset gets its own AssetLane (data feeds, agents, strategy,
    executor).  Shared objects (risk stack, PolymarketWebSocket, wallet) are
    held on this class and used by all lanes.
    """

    def __init__(self, paper_mode: bool | None = None) -> None:
        # Override paper mode if explicitly specified
        if paper_mode is not None:
            cfg.paper_mode = paper_mode

        self._running = False
        self._current_window_ts = 0
        self._last_fill_check_tick: float = 0.0

        # ── Shared data layer ────────────────────────────────────────────────
        self._poly_ws = PolymarketWebSocket()
        self._rest_client = PolymarketRestClient()
        self._gamma = GammaClient(self._rest_client)

        # ── Window clock ─────────────────────────────────────────────────────
        self._clock = WindowClock(
            entry_window_start_sec=cfg.entry_window_start_sec,
            trading_window_start_sec=cfg.trading_window_start_sec,
            entry_deadline_sec=cfg.entry_deadline_sec,
        )

        # ── Risk layer (shared across all assets) ────────────────────────────
        initial_balance = cfg.paper_initial_balance if cfg.paper_mode else 0.0
        self._pnl = PnlTracker(initial_balance)
        self._drawdown = DrawdownMonitor(initial_balance)
        self._circuit = CircuitBreaker()
        self._exposure = ExposureManager()
        self._sizer = PositionSizer()
        self._fee_calc = FeeCalculator()

        # Wallet (may be None in paper mode without private key)
        self._wallet: Wallet | None = None
        self._balance = initial_balance  # shared USDC pool for all assets

        # ── Per-asset lanes ──────────────────────────────────────────────────
        self._lanes: dict[str, AssetLane] = {}
        for asset_cfg in cfg.assets:
            lane = AssetLane.create(
                config=asset_cfg,
                poly_ws=self._poly_ws,
                gamma=self._gamma,
                rest_client=self._rest_client,
                wallet=None,  # Set properly in start() after wallet init
                fee_calc=self._fee_calc,
            )
            self._lanes[asset_cfg.symbol] = lane

        # ── Dashboard ────────────────────────────────────────────────────────
        self._dashboard = DashboardServer()
        self._dashboard.set_command_handler(self._handle_command)
        add_dashboard_handler(self._dashboard)  # Route CLAIM logs → dashboard terminal

        asset_names = ", ".join(self._lanes.keys())
        logger.info(f"Configured assets: {asset_names}")

    # ── Startup ───────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Initialize all components and verify connectivity."""
        logger.info(
            f"Starting PolyOracle {'[PAPER MODE]' if cfg.paper_mode else '[LIVE MODE]'}"
        )

        # Initialize wallet
        if cfg.has_wallet():
            try:
                self._wallet = Wallet()
                balances = await self._wallet.log_balances()
                # In live mode use real USDC balance; in paper mode keep simulated balance
                if not cfg.paper_mode:
                    self._balance = balances["usdc"]
                    self._drawdown = DrawdownMonitor(self._balance)
                    self._pnl = PnlTracker(self._balance)
            except Exception as exc:
                logger.error(f"Wallet initialization failed: {exc}")
                if not cfg.paper_mode:
                    raise

        # Re-create executors now that wallet is available
        for lane in self._lanes.values():
            lane.executor = PolymarketExecutor(
                wallet=self._wallet,  # type: ignore[arg-type]
                order_manager=lane.order_manager,
                fee_calculator=self._fee_calc,
            ) if self._wallet else PolymarketExecutor(
                wallet=None,  # type: ignore[arg-type]
                order_manager=lane.order_manager,
                fee_calculator=self._fee_calc,
            )

        # Subscribe to Binance tick updates (one handler per lane)
        for symbol, lane in self._lanes.items():
            handler = self._make_tick_handler(lane)
            lane.binance_ws.subscribe(handler)

        # Register clock callbacks
        self._clock.on_window_open(self._on_window_open)
        self._clock.on_phase_change(self._on_phase_change)
        self._clock.on_tick(self._on_clock_tick)
        self._clock.on_window_close(self._on_window_close)

        # Pre-warm meta-learners from historical trade DB so agents don't start cold
        await self._warmup_meta_learners()

        # Process any claims that were pending from the previous session
        await self._flush_startup_claims()

        self._running = True
        logger.info("PolyOracle initialized successfully")

    def _make_tick_handler(self, lane: AssetLane):
        """Create a tick handler closure for one asset lane."""
        is_first_lane = lane is next(iter(self._lanes.values()))

        async def _handler(tick: BtcTick) -> None:
            lane.aggregator.update_binance(tick.price, tick.qty)
            # Only the first lane drives the clock price (BTC by convention)
            if is_first_lane:
                self._clock.update_price(tick.price)
            oracle = lane.oracle.latest
            if oracle:
                lane.aggregator.update_oracle(oracle.price, oracle.updated_at)
            await lane.candles.on_tick(tick)

        return _handler

    async def _warmup_meta_learners(self) -> None:
        """
        Load historical trade outcomes from SQLite and pre-warm all asset
        meta-learners so agent weights reflect real performance from day 1.
        Also seeds the in-memory PnL tracker and dashboard trade history.
        """
        for symbol, lane in self._lanes.items():
            try:
                records = await trade_db.load_resolved_trades(
                    asset=symbol.lower(), limit=500,
                )
                lane.meta_learner.warmup_from_db(records)
            except Exception as exc:
                logger.warning(f"{symbol} meta-learner DB warmup failed: {exc}")

        # Seed PnL tracker from full history (all assets combined, oldest→newest)
        try:
            all_records = await trade_db.load_pnl_records(limit=2000)
            seeded = 0
            for r in all_records:
                if r.get("won") is None or r.get("pnl") is None:
                    continue
                wts = int(r.get("window_ts") or 0)
                self._pnl.record_trade(
                    trade_id=r["order_id"],
                    direction=r["direction"],
                    won=bool(r["won"]),
                    pnl=float(r["pnl"]),
                    entry_price=float(r.get("entry_price") or 0.0),
                    confidence=float(r.get("confidence") or 0.0),
                    window_ts=wts,
                    closed_at=float(wts + 300) if wts else None,
                )
                seeded += 1
            if seeded:
                stats = self._pnl.get_stats()
                logger.info(
                    f"PnL tracker seeded from DB: {seeded} trades, "
                    f"win_rate={stats.win_rate:.1%}, total_pnl=${stats.total_pnl:+.2f}"
                )
        except Exception as exc:
            logger.warning(f"PnL tracker DB warmup failed: {exc}")

        # Seed dashboard trade history from DB
        try:
            display_records = await trade_db.load_resolved_trades(asset=None, limit=500)
            for r in reversed(display_records):  # oldest→newest so history is in order
                if r.get("won") is None or r.get("pnl") is None:
                    continue
                asset = (r.get("asset") or "BTC").upper()
                self._dashboard.push("trade_resolved", {
                    "order_id": r["order_id"],
                    "market": r.get("market", ""),
                    "asset": asset,
                    "direction": r["direction"],
                    "actual_direction": r["actual_direction"],
                    "won": bool(r["won"]),
                    "pnl": round(float(r["pnl"]), 2),
                    "window_ts": r["window_ts"],
                    "price": float(r.get("entry_price") or 0),
                    "size_usd": float(r.get("size_usd") or 0),
                    "confidence": float(r.get("confidence") or 0),
                    "order_type": r.get("order_type"),
                })
            if display_records:
                logger.info(f"Dashboard trade history seeded from DB: {len(display_records)} trades")
        except Exception as exc:
            logger.warning(f"Dashboard trade history DB seed failed: {exc}")

    async def _flush_startup_claims(self) -> None:
        """
        At startup:
        1. Resume any pending claims from the previous session.
        2. Run ghost-claim recovery — scans the Polymarket data API for positions
           with value that were never claimed.
        """
        total = sum(len(lane.claimer._pending) for lane in self._lanes.values())
        if total > 0:
            logger.info(f"Resuming {total} pending claim(s) from previous session")
            for lane in self._lanes.values():
                await lane.claimer.process_pending_claims(self._wallet)

        # Ghost claim recovery — runs in background so it doesn't delay startup
        if self._wallet and not cfg.paper_mode:
            asyncio.create_task(
                self._run_ghost_claim_recovery(startup=True),
                name="ghost_claim_recovery_startup",
            )

        # Re-resolve any trades that survived a bot restart with won=NULL
        asyncio.create_task(
            self._resolve_startup_trades(),
            name="startup_resolution",
        )

    async def _run_ghost_claim_recovery(self, startup: bool = False) -> None:
        """Fetch all unclaimed positions from Polymarket and redeem them."""
        if startup:
            await asyncio.sleep(10.0)

        all_results = []
        for lane in self._lanes.values():
            results = await lane.claimer.recover_ghost_claims(self._wallet)
            all_results.extend(results)

        recovered_count = sum(1 for r in all_results if r.success)
        recovered_usd = sum(r.claimed_usd for r in all_results if r.success)
        pending_count = sum(1 for r in all_results if not r.success and r.error == "Not yet resolved on-chain")

        self._dashboard.push(
            "claims_recovery_complete",
            {
                "recovered_count": recovered_count,
                "recovered_usd": round(recovered_usd, 4),
                "pending_count": pending_count,
                "total_checked": len(all_results),
            },
        )

    async def _resolve_startup_trades(self) -> None:
        """Re-resolve trades that are still won=NULL after a bot restart."""
        await asyncio.sleep(15)

        unresolved = await trade_db.load_unresolved_trades(min_age_sec=600)
        if not unresolved:
            return

        logger.info(f"Startup resolution: found {len(unresolved)} unresolved trade(s)")

        for row in unresolved:
            order_id = row["order_id"]
            asset = row["asset"]           # 'BTC', 'ETH', 'SOL', etc.
            market_slug = row["market"]
            window_ts = row["window_ts"]
            direction = row["direction"]   # 'UP' or 'DOWN'
            open_price = row["open_price"] or 0.0
            entry_price = row["entry_price"]
            size_usd = row["size_usd"]
            confidence = row["confidence"]

            # Look up the lane for this asset; fall back to BTC binance symbol
            lane = self._lanes.get(asset.upper())
            binance_symbol = lane.config.binance_symbol if lane else "BTCUSDT"
            window_close_ts = float(window_ts + 300)

            actual_direction: str | None = None

            # 1. Try Polymarket oracle
            if not cfg.paper_mode and lane:
                try:
                    market = await lane.token_resolver.resolve_window(window_ts)
                    if market:
                        async with PolymarketRestClient() as rest:
                            actual_direction = await rest.get_market_winner(market.condition_id)
                except Exception as exc:
                    logger.warning(f"Startup resolution oracle check failed for {order_id}: {exc}")

            # 2. Binance kline fallback
            if actual_direction is None:
                close_price = await get_window_close_price(binance_symbol, window_ts)
                if open_price <= 0:
                    open_price = await get_window_open_price(binance_symbol, window_ts)
                if close_price > 0 and open_price > 0:
                    actual_direction = "UP" if close_price >= open_price else "DOWN"
                    logger.info(
                        f"Startup resolution Binance fallback {order_id}: "
                        f"open={open_price:.2f} close={close_price:.2f} → {actual_direction}"
                    )

            if actual_direction is None:
                logger.warning(
                    f"Startup resolution: could not determine outcome for {order_id} "
                    f"({asset} window_ts={window_ts}) — leaving unresolved"
                )
                continue

            won = direction == actual_direction
            pnl = (size_usd / entry_price - size_usd) if won else -size_usd

            await trade_db.resolve_trade(order_id, won, actual_direction, pnl)

            self._pnl.record_trade(
                trade_id=order_id,
                direction=direction,
                won=won,
                pnl=pnl,
                entry_price=entry_price,
                confidence=confidence,
                window_ts=window_ts,
                closed_at=window_close_ts,
            )

            self._dashboard.push("trade_resolved", {
                "order_id": order_id,
                "market": market_slug,
                "asset": asset,
                "direction": direction,
                "actual_direction": actual_direction,
                "won": won,
                "pnl": round(pnl, 2),
                "window_ts": window_ts,
                "confidence": round(confidence, 1),
                "size_usd": round(size_usd, 2),
                "price": round(entry_price, 4),
            })
            logger.info(
                f"Startup resolution: {order_id} → {'WIN' if won else 'LOSS'} "
                f"(pnl={pnl:+.2f})"
            )

    # ── Main run loop ─────────────────────────────────────────────────────────

    async def run(self) -> None:
        """Run all tasks concurrently."""
        await self.start()

        tasks = []
        for symbol, lane in self._lanes.items():
            tasks.append(asyncio.create_task(lane.binance_ws.run(), name=f"{symbol}_binance"))
            tasks.append(asyncio.create_task(lane.oracle.start(), name=f"{symbol}_oracle"))

        # Shared tasks
        tasks.extend([
            asyncio.create_task(self._clock.run(), name="clock"),
            asyncio.create_task(self._dashboard.start(), name="dashboard"),
            asyncio.create_task(self._poly_ws.run(), name="poly_ws"),
            asyncio.create_task(self._heartbeat_loop(), name="heartbeat"),
        ])

        logger.info("All tasks started. PolyOracle running.")
        self._dashboard.push_log("INFO", "main", "PolyOracle started")

        try:
            await asyncio.gather(*tasks, return_exceptions=True)
        except asyncio.CancelledError:
            logger.info("Tasks cancelled — shutting down")
        finally:
            await self.shutdown()

    async def shutdown(self) -> None:
        """Graceful shutdown."""
        logger.info("Shutting down PolyOracle...")
        self._running = False

        # Cancel all open orders on shutdown
        for lane in self._lanes.values():
            if lane.executor:
                await lane.executor.cancel_all_open()
            lane.binance_ws.stop()
            lane.oracle.stop()

        self._poly_ws.stop()
        self._clock.stop()
        self._dashboard.stop()

        stats = self._pnl.get_stats()
        logger.info(
            f"Final stats: trades={stats.total_trades}, "
            f"win_rate={stats.win_rate:.1%}, "
            f"total_pnl=${stats.total_pnl:+.2f}"
        )

    # ── Cross-asset delta helper ──────────────────────────────────────────────

    def _cross_asset_delta(self, exclude_symbol: str) -> float:
        """
        Compute cross-asset delta for the given lane.

        For N=2 (BTC+ETH): returns exactly the other asset's delta — bitwise
        equivalent to the old _eth_delta_pct() / _btc_delta_pct().
        For N>2: returns the average delta of all OTHER lanes.
        """
        other_deltas = []
        for sym, lane in self._lanes.items():
            if sym == exclude_symbol:
                continue
            open_p = lane.strategy.window_open_price
            if open_p > 0:
                cur = lane.aggregator.current_price
                if cur > 0:
                    other_deltas.append((cur - open_p) / open_p * 100)
        if not other_deltas:
            return 0.0
        return sum(other_deltas) / len(other_deltas)

    def _lane_delta_pct(self, lane: AssetLane) -> float:
        """Current window delta (%) for a lane."""
        open_p = lane.strategy.window_open_price
        if open_p <= 0:
            return 0.0
        cur = lane.aggregator.current_price
        return (cur - open_p) / open_p * 100 if cur > 0 else 0.0

    # ── Clock callbacks ───────────────────────────────────────────────────────

    async def _on_window_open(self, window: WindowState) -> None:
        """New 5-minute window opened."""
        self._current_window_ts = window.window_ts
        self._last_fill_check_tick = 0.0

        # Fetch open price and initialize each lane
        open_prices = {}
        for symbol, lane in self._lanes.items():
            price = await get_window_open_price(lane.config.binance_symbol, window.window_ts)
            if price <= 0:
                price = lane.aggregator.current_price
                logger.warning(
                    f"{symbol} kline open unavailable for {window.window_slug}, "
                    f"falling back to live price ${price:,.2f}"
                )
            open_prices[symbol] = price

            lane.strategy.update_window_open_price(price)
            lane.last_eval_tick = 0.0
            lane.last_trade_votes = []

            # Push per-asset window state
            self._dashboard.push("window_state", {
                **window.to_dict(),
                "asset": symbol,
                "window_slug": f"{lane.config.slug_prefix}-updown-5m-{window.window_ts}",
                "open_price": price,
                "current_price": price,
                "delta_pct": 0.0,
            })

            # Subscribe to Polymarket order books for this window
            await self._subscribe_market_for_lane(lane, window)

        # Only the first lane's price drives the clock
        first_symbol = next(iter(self._lanes))
        self._clock.set_window_open_price(open_prices.get(first_symbol, 0.0))

        price_str = " | ".join(f"{s} ${p:,.2f}" for s, p in open_prices.items())
        logger.info(f"Window {window.window_slug} opened: {price_str}")
        self._dashboard.push_log(
            "INFO", "clock", f"New window: {window.window_slug} | {price_str}"
        )

    async def _on_phase_change(self, window: WindowState) -> None:
        """Window phase transitioned."""
        # Push window state for the first lane (clock-driven)
        self._dashboard.push_log(
            "INFO", "clock",
            f"Phase → {window.phase.name} ({window.remaining_sec:.0f}s remaining)"
        )

        if window.phase == WindowPhase.EVALUATING:
            await asyncio.gather(
                *(self._evaluate_lane(lane, window) for lane in self._lanes.values()),
                return_exceptions=True,
            )
        elif window.phase == WindowPhase.TRADING:
            await asyncio.gather(
                *(self._maybe_trade_lane(lane, window, is_deadline=False)
                  for lane in self._lanes.values()),
                return_exceptions=True,
            )
        elif window.phase == WindowPhase.DEADLINE:
            await asyncio.gather(
                *(self._maybe_trade_lane(lane, window, is_deadline=True)
                  for lane in self._lanes.values()),
                return_exceptions=True,
            )

    async def _on_clock_tick(self, window: WindowState) -> None:
        """Every-second tick — push state to dashboard and re-evaluate during active phases."""
        now = time.time()

        # Push per-asset window state and ticks
        for symbol, lane in self._lanes.items():
            price = lane.aggregator.current_price
            if price <= 0:
                continue

            open_p = lane.strategy.window_open_price
            delta = ((price - open_p) / open_p * 100) if open_p > 0 else 0.0
            agg = lane.aggregator.get_aggregated()
            regime = lane.strategy.last_regime

            self._dashboard.push("window_state", {
                **window.to_dict(),
                "asset": symbol,
                "window_slug": f"{lane.config.slug_prefix}-updown-5m-{window.window_ts}",
                "open_price": open_p,
                "current_price": price,
                "delta_pct": round(delta, 4),
                "oracle_latency_sec": round(agg.oracle_latency_sec, 1),
                "market_regime": regime.regime.name if regime else None,
                "regime_trend_strength": round(regime.trend_strength, 3) if regime else None,
            })
            self._dashboard.push("tick", {
                "asset": symbol,
                "price": price,
                "timestamp": now,
            })

        # Re-evaluate every 5s during EVALUATING or TRADING phases.
        EVAL_INTERVAL = 5.0
        active_phases = (WindowPhase.EVALUATING, WindowPhase.TRADING)
        if window.phase in active_phases:
            for lane in self._lanes.values():
                if (now - lane.last_eval_tick) >= EVAL_INTERVAL:
                    lane.last_eval_tick = now
                    asyncio.create_task(self._maybe_trade_lane(lane, window, is_deadline=False))

        # Poll active GTC orders for fills every 5s (live mode only).
        FILL_POLL_INTERVAL = 5.0
        has_active = any(
            lane.order_manager.active_count > 0 for lane in self._lanes.values()
        )
        if (
            has_active
            and not cfg.paper_mode
            and (now - self._last_fill_check_tick) >= FILL_POLL_INTERVAL
        ):
            self._last_fill_check_tick = now
            asyncio.create_task(self._poll_gtc_fills())

    async def _on_window_close(self, window: WindowState) -> None:
        """Window closed — cancel any unfilled GTC orders, then await resolution."""
        logger.info(f"Window {window.window_slug} closed. Awaiting resolution...")
        self._dashboard.push_log("INFO", "clock", f"Window closed: {window.window_slug}")

        for symbol, lane in self._lanes.items():
            cancelled_orders = await lane.executor.cancel_all_open()
            for order in cancelled_orders:
                actual = _order_actual_cost(order)
                refund = order.size_usd - actual
                self._exposure.close_position(order.size_usd)
                self._balance += refund + order.fee_usd
                self._dashboard.push("trade_cancelled", {
                    "order_id": order.order_id,
                    "asset": symbol,
                })
            if cancelled_orders:
                refund_total = sum(
                    order.size_usd - _order_actual_cost(order) for order in cancelled_orders
                )
                logger.info(
                    f"{symbol}: Cancelled {len(cancelled_orders)} unfilled GTC order(s) "
                    f"at window close (${refund_total:.2f} refunded)"
                )

            # Process resolution after a short delay
            asyncio.create_task(self._process_lane_resolution(lane, window))

    # ── Strategy evaluation ───────────────────────────────────────────────────

    async def _evaluate_lane(self, lane: AssetLane, window: WindowState) -> None:
        """Run strategy evaluation for one asset lane."""
        symbol = lane.config.symbol
        try:
            cross_delta = self._cross_asset_delta(symbol)
            decision = await lane.strategy.evaluate(window, cross_asset_delta_pct=cross_delta)
            logger.debug(
                f"{symbol} T-30s evaluation: {decision.direction} "
                f"conf={decision.confidence.total:.0f}"
            )
            if decision.confidence.total > 0:
                self._dashboard.push("confidence", {
                    **decision.confidence.to_dict(),
                    "asset": symbol,
                })
            if lane.strategy.last_consensus:
                self._dashboard.push("agent_votes", {
                    **lane.strategy.last_consensus.to_dict(),
                    "asset": symbol,
                })
        except Exception as exc:
            logger.error(f"{symbol} strategy evaluation error: {exc}", exc_info=True)

    async def _maybe_trade_lane(
        self, lane: AssetLane, window: WindowState, is_deadline: bool,
    ) -> None:
        """Core trading decision logic for one asset lane."""
        symbol = lane.config.symbol

        # Check circuit breaker
        breaker = self._circuit.evaluate(
            daily_loss_usd=self._pnl.get_daily_loss(),
            drawdown_pct=self._drawdown.drawdown_pct,
            consecutive_losses=self._pnl.get_consecutive_losses(),
            balance=self._balance,
        )
        if not breaker.can_trade:
            if symbol == next(iter(self._lanes)):  # Only log once per tick
                logger.warning(f"Circuit breaker {breaker.tier.value}: {breaker.reason} — skip")
                self._dashboard.push("circuit_breaker", breaker.to_dict())
            return

        # Check exposure limits
        can_open, reason = self._exposure.can_open_position(cfg.trade_amount_usd)
        if not can_open:
            return

        # Check price data availability
        agg = lane.aggregator.get_aggregated()
        if agg.binance_price <= 0:
            logger.warning(f"{symbol} price unavailable (Binance feed down?) — skip trade")
            return
        if agg.oracle_latency_sec > 300:
            logger.warning(
                f"{symbol} Chainlink oracle stale: {agg.oracle_latency_sec:.0f}s since last update"
            )

        # Check for existing or already-attempted position in this window
        existing = lane.order_manager.get_active_for_window(window.window_ts)
        if existing:
            return
        attempted = [o for o in lane.order_manager.get_recent_history(10)
                     if o.window_ts == window.window_ts]
        if attempted:
            return

        # Run strategy
        try:
            cross_delta = self._cross_asset_delta(symbol)
            decision = await lane.strategy.evaluate(window, cross_asset_delta_pct=cross_delta)
        except Exception as exc:
            logger.error(f"{symbol} strategy error: {exc}", exc_info=True)
            return

        if decision.confidence.total > 0:
            self._dashboard.push("confidence", {
                **decision.confidence.to_dict(),
                "asset": symbol,
            })
        if lane.strategy.last_consensus:
            self._dashboard.push("agent_votes", {
                **lane.strategy.last_consensus.to_dict(),
                "asset": symbol,
            })

        if not decision.should_trade:
            if is_deadline:
                logger.info(f"{symbol} deadline skip: {decision.reason}")
            return

        # Resolve market
        market = await lane.token_resolver.resolve_current()
        if not market:
            logger.warning(f"{symbol}: Could not resolve current market — skip")
            return

        # Hard floor
        if decision.confidence.total < cfg.min_confidence_score:
            return

        # Calculate position size
        stats = self._pnl.get_stats()
        win_rate = stats.win_rate if stats.total_trades >= 10 else 0.55
        token_price = (
            market.yes_price if decision.direction == "UP" else market.no_price
        )
        token_price = max(cfg.min_token_price, min(cfg.max_token_price, token_price))

        sizing = self._sizer.calculate(
            balance=self._balance,
            confidence=decision.confidence.total,
            win_rate=win_rate,
            token_price=token_price,
            consecutive_losses=self._pnl.get_consecutive_losses(),
            drawdown_pct=self._drawdown.drawdown_pct,
        )
        size_usd = sizing.size_usd * breaker.size_multiplier

        if size_usd < 1.0:
            return

        # ── GTC vs FOK routing ────────────────────────────────────────────
        use_gtc = window.remaining_sec > cfg.gtc_window_sec

        if use_gtc:
            conf_bid = min(round(decision.confidence.total / 100.0, 2), cfg.max_token_price)
            logger.info(
                f"{symbol} early window ({window.remaining_sec:.0f}s remaining) → "
                f"GTC maker (conf cap={conf_bid:.2f})"
            )
            order = await lane.executor.execute_gtc(
                market=market,
                direction=decision.direction,
                confidence=decision.confidence.total,
                position_size_usd=size_usd,
                bid_price=conf_bid,
            )
        else:
            logger.info(
                f"{symbol} late window ({window.remaining_sec:.0f}s remaining) → FOK taker"
            )
            order = await lane.executor.execute(
                market=market,
                direction=decision.direction,
                confidence=decision.confidence.total,
                position_size_usd=size_usd,
            )

        if order:
            self._exposure.open_position(order.size_usd)
            self._balance -= (order.size_usd + order.fee_usd)
            self._drawdown.update(self._balance)
            lane.last_trade_votes = (
                lane.strategy.last_consensus.votes
                if lane.strategy.last_consensus else []
            )

            order_type_tag = "GTC" if use_gtc else "FOK"
            consensus = lane.strategy.last_consensus
            votes_payload = consensus.to_dict()["votes"] if consensus else []
            conf_payload = decision.confidence.to_dict()

            lane_delta = self._lane_delta_pct(lane)

            is_live_gtc = not order.is_paper and order_type_tag == "GTC"
            if not is_live_gtc:
                self._dashboard.push("trade_executed", {
                    "order_id": order.order_id,
                    "market": order.market_slug,
                    "asset": symbol,
                    "direction": order.direction,
                    "price": order.price,
                    "size_usd": order.size_usd,
                    "confidence": order.confidence,
                    "window_ts": order.window_ts,
                    "order_type": order_type_tag,
                    "agent_votes": votes_payload,
                    "confidence_breakdown": conf_payload,
                    "window_delta_pct": round(lane_delta, 4),
                })
            self._dashboard.push_log(
                "INFO" if is_live_gtc else "TRADE", "executor",
                f"{'[PAPER] ' if order.is_paper else ''}{symbol} "
                f"{'GTC PLACED' if is_live_gtc else f'TRADE {order.direction}'} "
                f"[{order_type_tag}] @ {order.price:.3f} × ${order.size_usd:.2f} "
                f"(conf={order.confidence:.0f})"
                + (" — awaiting fill" if is_live_gtc else "")
            )
            asyncio.create_task(trade_db.record_trade(
                order_id=order.order_id,
                asset=symbol,
                market=order.market_slug,
                direction=order.direction,
                entry_price=order.price,
                size_usd=order.size_usd,
                confidence=order.confidence,
                window_ts=order.window_ts,
                order_type=order_type_tag,
                window_delta_pct=round(lane_delta, 4),
                open_price=lane.strategy.window_open_price or None,
                agent_votes=votes_payload,
                confidence_breakdown=conf_payload,
            ))

    # ── GTC fill polling ──────────────────────────────────────────────────────

    async def _poll_gtc_fills(self) -> None:
        """Poll active live orders for fills every FILL_POLL_INTERVAL seconds."""
        try:
            for symbol, lane in self._lanes.items():
                if lane.executor is None:
                    continue
                filled, cancelled = await lane.executor.check_and_update_fills()

                for order_id in filled:
                    for order in lane.order_manager.get_recent_history(50):
                        if order.order_id == order_id:
                            self._exposure.close_position(order.size_usd)
                            self._dashboard.push("trade_executed", {
                                "order_id": order.order_id,
                                "market": order.market_slug,
                                "asset": symbol,
                                "direction": order.direction,
                                "price": order.filled_price,
                                "size_usd": order.size_usd,
                                "confidence": order.confidence,
                                "window_ts": order.window_ts,
                                "order_type": "GTC_FILLED",
                            })
                            self._dashboard.push_log(
                                "TRADE", "executor",
                                f"{symbol} GTC FILLED {order.direction} "
                                f"@ {order.filled_price:.3f} × ${order.size_usd:.2f} "
                                f"(conf={order.confidence:.0f})"
                            )
                            break

                for order_id in cancelled:
                    for order in lane.order_manager.get_recent_history(50):
                        if order.order_id == order_id:
                            self._exposure.close_position(order.size_usd)
                            self._balance += order.size_usd + order.fee_usd
                            self._dashboard.push("trade_cancelled", {
                                "order_id": order_id,
                                "asset": symbol,
                            })
                            break

        except Exception as exc:
            logger.debug(f"GTC fill poll error: {exc}")

    # ── Resolution ────────────────────────────────────────────────────────────

    async def _process_lane_resolution(self, lane: AssetLane, window: WindowState) -> None:
        """Wait for market resolution and process claims for one asset lane."""
        symbol = lane.config.symbol

        # Capture open price NOW before the new window overwrites it.
        open_at_close = lane.strategy.window_open_price

        # Query the exact close price of the completed kline from Binance.
        close_price_task = asyncio.create_task(
            get_window_close_price(lane.config.binance_symbol, window.window_ts),
            name=f"{symbol}_close_price",
        )
        await asyncio.sleep(15)
        close_price = await close_price_task
        if close_price <= 0:
            close_price = lane.aggregator.current_price
            logger.warning(f"{symbol} kline close unavailable — using live snapshot for fallback")

        actual_direction = await self._determine_resolution_for_lane(lane, window, open_at_close)

        if actual_direction is None:
            # Fallback: use close-time price (captured at T+15s) vs window open.
            if open_at_close > 0 and close_price > 0:
                actual_direction = "UP" if close_price >= open_at_close else "DOWN"
                logger.warning(
                    f"{symbol} resolution fallback to Binance close price: "
                    f"open={open_at_close:.2f} close={close_price:.2f} → {actual_direction}"
                )
            else:
                logger.error(
                    f"{symbol} resolution completely failed for {window.window_slug} — "
                    f"force-expiring filled orders"
                )
                for order in lane.order_manager.get_history_for_window(window.window_ts):
                    if order.status == OrderStatus.FILLED:
                        lane.order_manager.mark_cancelled(order.order_id, "Resolution failed")
                        self._dashboard.push("trade_cancelled", {
                            "order_id": order.order_id,
                            "asset": symbol,
                        })
                        self._exposure.close_position(order.size_usd)
                return

        logger.info(f"{symbol} resolution: {window.window_slug} → {actual_direction}")
        self._dashboard.push_log(
            "INFO", "resolution",
            f"{symbol} window resolved: {window.window_slug} → {actual_direction}"
        )

        window_orders = lane.order_manager.get_history_for_window(window.window_ts)

        for order in window_orders:
            if order.status in (
                OrderStatus.CANCELLED, OrderStatus.REJECTED,
                OrderStatus.EXPIRED, OrderStatus.PENDING,
            ):
                self._dashboard.push("trade_cancelled", {
                    "order_id": order.order_id,
                    "asset": symbol,
                })
                continue

            won = order.direction == actual_direction
            actual_cost = _order_actual_cost(order)
            pnl = (
                (order.filled_shares - actual_cost)
                if won
                else -(actual_cost + order.fee_usd)
            )

            lane.claimer.schedule_claim(order, actual_direction)
            self._pnl.record_trade(
                trade_id=order.order_id,
                direction=order.direction,
                won=won,
                pnl=pnl,
                entry_price=order.price,
                confidence=order.confidence,
                window_ts=order.window_ts,
            )

            if won:
                self._balance += order.size_shares
                self._dashboard.push_log(
                    "TRADE", "resolution",
                    f"WIN +${pnl:.2f} | {symbol} {order.direction} | "
                    f"size=${order.size_usd:.2f} | conf={order.confidence:.0f}"
                )
            else:
                self._dashboard.push_log(
                    "TRADE", "resolution",
                    f"LOSS -${abs(pnl):.2f} | {symbol} {order.direction} | "
                    f"size=${order.size_usd:.2f} | conf={order.confidence:.0f}"
                )
            self._exposure.close_position(_order_actual_cost(order))

            lane_delta = (
                (close_price - open_at_close) / open_at_close * 100
                if open_at_close > 0 else 0.0
            )
            self._dashboard.push("trade_resolved", {
                "order_id": order.order_id,
                "market": order.market_slug,
                "asset": symbol,
                "direction": order.direction,
                "actual_direction": actual_direction,
                "won": won,
                "pnl": round(pnl, 2),
                "window_ts": window.window_ts,
                "price": order.price,
                "size_usd": order.size_usd,
                "confidence": round(order.confidence, 1),
                "window_delta_pct": round(lane_delta, 4),
            })
            asyncio.create_task(trade_db.resolve_trade(
                order_id=order.order_id,
                won=won,
                actual_direction=actual_direction,
                pnl=round(pnl, 2),
            ))

        # Update portfolio stats
        self._drawdown.update(self._balance)
        stats = self._pnl.get_stats()
        self._dashboard.push("portfolio_update", {
            "balance": round(self._balance, 2),
            "paper_mode": cfg.paper_mode,
            **stats.to_dict(),
        })

        # Process claims
        await lane.claimer.process_pending_claims(self._wallet)

        # Update agent meta-learner
        if actual_direction and lane.last_trade_votes:
            lane.consensus.record_outcome(actual_direction, lane.last_trade_votes)

    async def _determine_resolution_for_lane(
        self,
        lane: AssetLane,
        window: WindowState,
        open_at_close: float,
    ) -> str | None:
        """
        Determine the actual outcome of a window for one asset.

        Live mode: polls Polymarket oracle every 30s for up to 5 minutes,
        then tries CLOB mid-price.  Returns None if inconclusive.
        Paper mode: compares current Binance price vs open_at_close.
        """
        symbol = lane.config.symbol

        if not cfg.paper_mode:
            market = await lane.token_resolver.resolve_window(window.window_ts)
            if market:
                async with PolymarketRestClient() as rest:
                    for attempt in range(10):
                        try:
                            result = await rest.get_market_winner(market.condition_id)
                            if result:
                                logger.info(
                                    f"{symbol} live resolution: winner={result} "
                                    f"(attempt {attempt + 1})"
                                )
                                return result
                            if attempt < 9:
                                await asyncio.sleep(30)
                        except Exception as exc:
                            logger.warning(f"{symbol} resolution poll failed: {exc}")
                            if attempt < 9:
                                await asyncio.sleep(30)

                    # Oracle not settled after 5 min — try CLOB mid-price.
                    clob_404 = False
                    try:
                        book = await rest.get_order_book(market.yes_token_id)
                        bids = book.get("bids", [])
                        asks = book.get("asks", [])
                        if bids and asks:
                            best_bid = max(float(b["price"]) for b in bids)
                            best_ask = min(float(a["price"]) for a in asks)
                            mid = (best_bid + best_ask) / 2.0
                            if mid > 0.90:
                                logger.info(f"{symbol} CLOB mid={mid:.3f} → UP (settled high)")
                                return "UP"
                            if mid < 0.10:
                                logger.info(f"{symbol} CLOB mid={mid:.3f} → DOWN (settled low)")
                                return "DOWN"
                            logger.info(
                                f"{symbol} CLOB mid={mid:.3f} ambiguous after 5 min "
                                f"— falling back to Binance price"
                            )
                    except aiohttp.ClientResponseError as exc:
                        if exc.status == 404:
                            clob_404 = True
                            logger.info(f"{symbol} CLOB book 404 — market likely just resolved, retrying")
                        else:
                            logger.warning(f"{symbol} CLOB mid-price check failed: {exc}")
                    except Exception as exc:
                        logger.warning(f"{symbol} CLOB mid-price check failed: {exc}")

                    if clob_404:
                        for retry in range(20):
                            try:
                                result = await rest.get_market_winner(market.condition_id)
                                if result:
                                    logger.info(f"{symbol} winner confirmed after CLOB 404: {result} (retry {retry + 1})")
                                    return result
                            except Exception as exc:
                                logger.warning(f"{symbol} winner retry {retry + 1} failed: {exc}")
                            await asyncio.sleep(30)

            logger.warning(f"{symbol}: all winner polls exhausted — returning None for close-price fallback")
            return None

        # ── Paper mode: resolve from current Binance price ────────────────
        if open_at_close <= 0:
            return None
        current = lane.aggregator.current_price
        if current <= 0:
            return None
        return "UP" if current >= open_at_close else "DOWN"

    # ── Market subscription ───────────────────────────────────────────────────

    async def _subscribe_market_for_lane(self, lane: AssetLane, window: WindowState) -> None:
        """Subscribe to order book for a lane's current window market."""
        symbol = lane.config.symbol
        try:
            market = await lane.token_resolver.resolve_current()
            if market:
                self._poly_ws.subscribe_token(market.yes_token_id)
                self._poly_ws.subscribe_token(market.no_token_id)
                lane.strategy.set_current_market_tokens(market.yes_token_id, market.no_token_id)
                logger.debug(f"Subscribed to {symbol} order books for {market.slug}")
        except Exception as exc:
            logger.warning(f"Could not subscribe to {symbol} market order book: {exc}")

    # ── Heartbeat ─────────────────────────────────────────────────────────────

    async def _heartbeat_loop(self) -> None:
        """Periodic health check and portfolio update push."""
        while self._running:
            await asyncio.sleep(30)

            # Reconcile exposure counter against ground-truth order state.
            all_open = []
            for lane in self._lanes.values():
                all_open.extend(lane.order_manager.get_active_orders())
                all_open.extend(
                    o for o in lane.order_manager.get_recent_history(200)
                    if o.status == OrderStatus.FILLED and o.pnl is None
                )
            self._exposure.reconcile(
                true_count=len(all_open),
                true_usd=sum(_order_actual_cost(o) for o in all_open),
            )

            # Update balance from chain if live
            if self._wallet and not cfg.paper_mode:
                try:
                    usdc = await self._wallet.get_usdc_balance()
                    if usdc > 0:
                        self._balance = usdc
                except Exception as exc:
                    logger.warning(f"Balance refresh failed: {exc}")

            stats = self._pnl.get_stats()
            self._dashboard.push("portfolio_update", {
                "balance": round(self._balance, 2),
                "paper_mode": cfg.paper_mode,
                **stats.to_dict(),
            })

            breaker = self._circuit.status
            self._dashboard.push("circuit_breaker", breaker.to_dict())

    # ── Command handler ───────────────────────────────────────────────────────

    def _push_updated_agent_votes(self) -> None:
        """Re-apply meta-learner weights to last consensus and push to dashboard."""
        for symbol, lane in self._lanes.items():
            if lane.strategy.last_consensus:
                lane.meta_learner.apply_to_votes(lane.strategy.last_consensus.votes)
                self._dashboard.push("agent_votes", {
                    **lane.strategy.last_consensus.to_dict(),
                    "asset": symbol,
                })

    async def _handle_command(self, cmd: dict) -> None:
        """Handle commands from the dashboard."""
        cmd_type = cmd.get("command", "")

        if cmd_type == "pause":
            self._circuit.manual_pause()
            logger.info("Bot paused via dashboard command")

        elif cmd_type == "resume":
            self._circuit.manual_resume()
            logger.info("Bot resumed via dashboard command")

        elif cmd_type == "emergency_stop":
            self._circuit.trigger_emergency_stop("Dashboard emergency stop")
            logger.critical("EMERGENCY STOP triggered from dashboard")

        elif cmd_type == "status":
            stats = self._pnl.get_stats()
            self._dashboard.push("portfolio_update", {
                "balance": round(self._balance, 2),
                "paper_mode": cfg.paper_mode,
                **stats.to_dict(),
            })

        elif cmd_type == "set_confidence":
            new_conf = int(cmd.get("value", cfg.min_confidence_score))
            cfg.min_confidence_score = max(0, min(100, new_conf))
            logger.info(f"Confidence threshold updated to {cfg.min_confidence_score}")

        elif cmd_type == "unmute_agent":
            agent_name = cmd.get("agent", "")
            for lane in self._lanes.values():
                lane.meta_learner.force_unmute(agent_name)
            logger.info(f"Agent '{agent_name}' unmuted via dashboard")
            self._push_updated_agent_votes()

        elif cmd_type == "mute_agent":
            agent_name = cmd.get("agent", "")
            for lane in self._lanes.values():
                lane.meta_learner.force_mute(agent_name)
            logger.info(f"Agent '{agent_name}' muted via dashboard")
            self._push_updated_agent_votes()

        elif cmd_type == "collect_claims":
            if cfg.paper_mode or not self._wallet:
                self._dashboard.push_log("INFO", "claimer", "Claim collection not available in paper mode")
            else:
                total_pending = sum(len(lane.claimer._pending) for lane in self._lanes.values())
                self._dashboard.push_log(
                    "INFO", "claimer",
                    f"Collecting claims: {total_pending} pending + scanning for ghost claims..."
                )
                if total_pending > 0:
                    for lane in self._lanes.values():
                        asyncio.create_task(lane.claimer.process_pending_claims(self._wallet))
                asyncio.create_task(
                    self._run_ghost_claim_recovery(startup=False),
                    name="ghost_claim_recovery_manual",
                )

        else:
            logger.warning(f"Unknown command: {cmd_type}")


# ── CLI entry point ───────────────────────────────────────────────────────────

@click.command()
@click.option("--paper/--live", default=None, help="Override paper/live mode from .env")
@click.option("--log-level", default=None, help="Override log level")
@click.option("--with-dashboard/--no-dashboard", default=True, help="Launch Next.js dashboard alongside the bot")
def cli_main(paper: bool | None, log_level: str | None, with_dashboard: bool) -> None:
    """PolyOracle — Autonomous Polymarket BTC prediction market bot."""
    import subprocess
    import os as _os

    # Setup logging
    level = log_level or cfg.log_level
    setup_logging(level=level, log_file=cfg.log_file)

    # Override paper mode if specified
    if paper is not None:
        _os.environ["PAPER_MODE"] = "true" if paper else "false"

    logger.info(
        f"PolyOracle starting | "
        f"mode={'PAPER' if cfg.paper_mode else 'LIVE'} | "
        f"confidence_threshold={cfg.min_confidence_score} | "
        f"assets={[a.symbol for a in cfg.assets]}"
    )

    # Launch dashboard as a child process
    dashboard_proc: subprocess.Popen | None = None
    if with_dashboard:
        dashboard_dir = Path(__file__).resolve().parent.parent / "dashboard"
        if (dashboard_dir / "package.json").exists():
            logger.info(f"Launching dashboard from {dashboard_dir} on port {cfg.dashboard_port}")
            dashboard_proc = subprocess.Popen(
                ["npm", "run", "dev", "--", "--port", str(cfg.dashboard_port)],
                cwd=str(dashboard_dir),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            logger.info(f"Dashboard UI: http://localhost:{cfg.dashboard_port}")
        else:
            logger.warning("dashboard/package.json not found — skipping dashboard launch")

    bot = PolyOracle(paper_mode=paper)

    # Handle Ctrl+C gracefully
    loop = asyncio.get_event_loop()

    def _shutdown_handler():
        logger.info("Shutdown signal received")
        for task in asyncio.all_tasks(loop):
            task.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _shutdown_handler)

    try:
        loop.run_until_complete(bot.run())
    except KeyboardInterrupt:
        pass
    finally:
        if dashboard_proc:
            logger.info("Stopping dashboard...")
            dashboard_proc.terminate()
            try:
                dashboard_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                dashboard_proc.kill()
        loop.close()
        logger.info("PolyOracle stopped")


if __name__ == "__main__":
    cli_main()
