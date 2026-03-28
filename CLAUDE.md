# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Run the bot
python -m core.main --paper       # paper trading (safe default)
python -m core.main --live        # live trading

# Dashboard (separate terminal)
cd dashboard && npm run dev       # http://localhost:3000

# Linting & type checking
ruff check . --fix
mypy .

# Utility scripts
python scripts/setup_wallet.py    # one-time CLOB credential generation (live mode)
python scripts/recover_claims.py  # recover unclaimed winning positions
python scripts/check_balance.py   # check USDC wallet balance

# Install
pip install -e .          # runtime
pip install -e ".[dev]"   # + ruff, mypy, pytest
```

There are no tests yet — `pytest` is configured (`asyncio_mode = "auto"`, `testpaths = ["tests"]`) but the `tests/` directory does not exist.

## Architecture

### Data flow overview

```
Binance WS ──┐
             ├──▶ PriceAggregator ──▶ CandleBuilder ──▶ LateWindowStrategy ──▶ PolymarketExecutor
Chainlink ───┘                                          ↑
                                              ConsensusEngine (5 agents)
                                              ConfidenceEngine (0-100 score)
Polymarket WS ──▶ order-book imbalance ──────────────────┘

PolymarketExecutor ──▶ OrderManager ──▶ Claimer (claims wins)
                                     ──▶ trade_db (SQLite)

All components ──▶ DashboardBroadcaster ──▶ WebSocket server (port 8765) ──▶ Next.js dashboard
```

### Window lifecycle

`WindowClock` (`core/clock.py`) drives everything via callbacks fired at phase transitions:

| Phase | Trigger | Action |
|-------|---------|--------|
| `MONITORING` | window start | subscribe order books, capture open price |
| `EVALUATING` | T-295s | `_maybe_trade` every 5s (GTC maker bid) |
| `TRADING` | T-120s | `_maybe_trade` every 5s, GTC → FOK switch |
| `DEADLINE` | T-10s | final FOK attempt |
| `RESOLVED` | T+0 | `_process_window_resolution` |

Both BTC and ETH windows run in parallel with separate executors, order managers, and strategies, but share the same circuit breaker, exposure manager, and P&L tracker.

### Order routing

- **GTC** (early window, >120s remaining): resting maker bid at `confidence/100`. Dynamically repriced every 5s via `_reprice_gtc_if_needed`. Checks for partial fills before repricing to avoid double-exposure.
- **FOK** (late window, ≤120s remaining): immediate taker sweep. Pre-checked with VWAP against book depth; retries once at 50% size if depth insufficient.

### Resolution chain (live mode only)

`_determine_resolution` / `_eth_determine_resolution` try in order:
1. `get_market_winner()` poll × 10 at 30s intervals (ground truth from Polymarket oracle)
2. CLOB order book mid-price (settled tokens trade near 0.00 or 1.00)
3. If CLOB returns 404 (book closed = market resolved): retry `get_market_winner` × 5
4. Return `None` → caller falls back to Binance **close-time kline** captured at T+15s

Never use the live aggregator price for resolution — it reflects the current moment, not the settlement moment.

### Confidence scoring (`strategy/confidence.py`)

Score 0–100 assembled from weighted components:

| Component | Max pts | Source |
|-----------|---------|--------|
| Signal | 35 | `SignalCombiner` (RSI, MACD, BB, VWAP) |
| Agents | 25 | weighted consensus vote |
| Delta | 25 | window % move (vol-adjusted) |
| Regime | 15 | `detect_regime()` trending bonus |
| Momentum | 5 | delta acceleration |
| Time decay | 5 | bonus for acting late in window |
| Persistence | 5 | consecutive same-direction signals |
| Cross-asset | ±10 | BTC↔ETH correlation |

Trade fires when total ≥ `MIN_CONFIDENCE_SCORE` (default 65).

### Agent system (`agents/`)

Five agents each return `AgentVote(vote, conviction, reasoning)`:
- `momentum_agent` — EMA crossover + MACD
- `mean_reversion_agent` — RSI extremes + Bollinger Bands
- `volatility_agent` — ATR gating (abstains in flat markets)
- `orderflow_agent` — Polymarket bid/ask imbalance
- `oracle_agent` — Chainlink vs Binance price latency

`MetaLearner` tracks rolling 50-trade accuracy per agent and adjusts weights. Agents below 45% accuracy are muted.

### Risk stack

Circuit breaker (`risk/circuit_breaker.py`) has 3 tiers that scale position size or halt trading:
- **YELLOW**: consecutive losses or mild drawdown → size ×0.5
- **ORANGE**: daily loss limit approaching → size ×0.25
- **RED**: daily loss limit hit or severe drawdown → halt

Position sizing uses fractional Kelly (default 0.25×) with confidence-scaled fraction and guardrails: consecutive-loss scaling, drawdown scaling, max 8% of balance per trade, and a USD exposure ceiling (`MAX_EXPOSURE_USD`, default $500 across all open positions).

### Dashboard ↔ bot communication

`DashboardBroadcaster` (`websocket_server/server.py`) holds a per-type cache so new clients get current state immediately on connect. Push types: `tick`, `window_state`, `agent_votes`, `confidence`, `trade_executed`, `trade_resolved`, `circuit_breaker`, `portfolio_update`, `log`. ETH equivalents prefixed with `eth_`.

`window_state` now includes `oracle_latency_sec`, `market_regime` (`TRENDING`/`VOLATILE`/`RANGING`), and `regime_trend_strength`.

Frontend (`dashboard/hooks/useBotState.ts`) is a single reducer over all WS messages into `BotState`.

### Trade persistence (`data/trade_db.py`)

SQLite at `logs/trades.db`. Written on execution, updated on resolution. The claimer overwrites `pnl` with the actual USDC received (`usdc_received - size_usd`) after on-chain redemption, which is authoritative over the estimated P&L written at resolution time.

Unresolved trades older than 10 minutes are re-queued for resolution on bot restart (`load_unresolved_trades`).

## Key configuration (`core/config.py`)

All settings come from `.env`. Important non-obvious ones:

| Key | Default | Notes |
|-----|---------|-------|
| `ENTRY_WINDOW_START_SEC` | 295 | Start evaluating at T+5s |
| `TRADING_WINDOW_START_SEC` | 270 | Switch to GTC at T+30s |
| `GTC_WINDOW_SEC` | 120 | Switch GTC→FOK below this threshold |
| `MIN_WINDOW_DELTA_PCT` | 0.005 | Minimum BTC move to consider trading |
| `MAX_EXPOSURE_USD` | 500 | Max total USD at risk across all positions |
| `KELLY_FRACTION` | 0.25 | Quarter-Kelly base (confidence-scaled dynamically) |
| `AGENT_MUTE_THRESHOLD` | 0.45 | Mute agents with rolling accuracy below this |

## Live trading prerequisites

1. Run `python scripts/setup_wallet.py` once to generate and write CLOB credentials to `.env`
2. Deposit USDC to the `FUNDER_ADDRESS` on Polygon
3. Set `PAPER_MODE=false` in `.env`
4. Bot requires `PRIVATE_KEY`, `CLOB_API_KEY`, `CLOB_SECRET`, `CLOB_PASS_PHRASE` to be set
