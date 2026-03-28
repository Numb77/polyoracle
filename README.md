# PolyOracle

An autonomous trading bot for Polymarket's 5-minute BTC and ETH Up/Down prediction markets. It streams live price data from Binance and Chainlink, runs a multi-agent AI consensus engine, scores trade confidence, and places orders on the Polymarket CLOB (Central Limit Order Book) — all with a real-time Next.js dashboard.

---

## Table of Contents

- [How It Works](#how-it-works)
- [System Architecture](#system-architecture)
- [Project Structure](#project-structure)
- [Requirements](#requirements)
- [Installation](#installation)
- [Environment Setup (.env)](#environment-setup-env)
- [Running the Bot](#running-the-bot)
- [Dashboard](#dashboard)
- [Live Trading Setup](#live-trading-setup)
- [Strategy Deep Dive](#strategy-deep-dive)
- [Risk Management](#risk-management)
- [Utility Scripts](#utility-scripts)
- [Docker](#docker)

---

## How It Works

Polymarket hosts binary Up/Down markets for BTC and ETH on 5-minute windows. Each market asks: *"Will BTC be higher or lower at the end of this 5-minute window than it was at the start?"*

**The strategy:**

1. A new 5-minute window opens every 5 minutes (on the clock: 00:00, 05:00, 10:00, ...).
2. The bot captures the **opening price** at window start.
3. For the first ~30 seconds the bot watches passively — order books are not yet meaningful.
4. Starting at **T+5s** (295s remaining), the bot evaluates every 5 seconds:
   - Computes BTC/ETH delta from window open
   - Runs 5 AI agents (momentum, mean reversion, volatility, order flow, oracle arbitrage)
   - Scores a composite confidence (0–100) from signals + agents + regime + delta
5. If confidence ≥ threshold and remaining > 120s → place a **GTC maker bid** (resting limit order). This captures the early mispricing before market makers reprice from ~0.50 to 0.85+.
6. If confidence ≥ threshold and remaining ≤ 120s → switch to **FOK taker sweep** (immediate fill or cancel).
7. At window close, the Polymarket oracle (Chainlink) settles the outcome.
8. The auto-claimer redeems any winning positions from the smart contract on Polygon.

**Why it has edge:** Market makers initially price both outcomes near 0.50. Within the final 90 seconds, BTC's direction is largely locked in and the true probability is >85%, but the book often still shows 0.55–0.65 ask prices. The bot captures this repricing gap.

---

## System Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        Data Sources                             │
│                                                                 │
│  Binance WS (BTC/ETH) ──┐                                       │
│  Chainlink (on-chain) ───┼──▶ PriceAggregator ──▶ CandleBuilder │
│  Polymarket WS (books) ─┘                                       │
└────────────────────────────────┬────────────────────────────────┘
                                 │
                    ┌────────────▼────────────┐
                    │   WindowClock (5-min)   │
                    │  MONITORING → EVALUATING│
                    │  → TRADING → DEADLINE   │
                    │  → RESOLVED             │
                    └────────────┬────────────┘
                                 │ phase callbacks
          ┌──────────────────────▼──────────────────────┐
          │              LateWindowStrategy              │
          │                                             │
          │  ┌─────────────┐   ┌─────────────────────┐ │
          │  │ SignalCombiner│  │  ConsensusEngine    │ │
          │  │ RSI/MACD/BB/ │  │  5 Agents + weights │ │
          │  │ VWAP / candles│  │  MetaLearner (acc.) │ │
          │  └──────┬──────┘   └────────┬────────────┘ │
          │         └────────┬──────────┘              │
          │          ConfidenceEngine (0-100)           │
          └────────────────┬─────────────────────────── ┘
                           │ if score ≥ threshold
          ┌────────────────▼────────────────────────────┐
          │            PolymarketExecutor               │
          │                                             │
          │  GTC (>120s left): resting maker bid        │
          │  FOK (≤120s left): immediate taker sweep    │
          │  RepricingEngine: adjust GTC every 5s       │
          └──────────────┬──────────────────────────────┘
                         │
          ┌──────────────▼──────────────┐
          │        OrderManager         │◀── CircuitBreaker
          │   SQLite (logs/trades.db)   │◀── ExposureManager
          │   PositionSizer (Kelly)     │◀── PnLTracker
          └──────────────┬──────────────┘
                         │
          ┌──────────────▼──────────────┐
          │    Claimer (on-chain)       │  auto-redeems winning
          │    Polygon / ERC-1155       │  positions after settle
          └─────────────────────────────┘
                         │
          ┌──────────────▼──────────────┐
          │   DashboardBroadcaster      │  WebSocket port 8765
          │   (websocket_server/)       │──▶ Next.js dashboard
          └─────────────────────────────┘
```

Both **BTC and ETH** windows run in parallel with separate executors, strategies, and order managers, but share the same circuit breaker, exposure manager, and P&L tracker.

---

## Project Structure

```
polyoracle/
├── core/
│   ├── main.py          # Main orchestrator — wires everything together
│   ├── clock.py         # WindowClock — drives 5-min phase transitions
│   ├── config.py        # All settings loaded from .env (pydantic-settings)
│   └── logger.py        # Structured JSON logger with TRADE/CLAIM levels
│
├── data/
│   ├── binance_ws.py    # Binance trade stream WebSocket (BTC + ETH)
│   ├── chainlink.py     # On-chain Chainlink oracle price reader (Polygon)
│   ├── aggregator.py    # Merges Binance + Chainlink → AggregatedPrice
│   ├── candles.py       # 1-minute OHLCV candle builder from tick stream
│   ├── polymarket_ws.py # Polymarket order book WebSocket
│   └── polymarket_rest.py # CLOB + Gamma REST API client
│
├── strategy/
│   ├── signals.py       # Technical indicators: RSI, MACD, Bollinger, VWAP
│   ├── confidence.py    # Composite confidence scorer (0-100)
│   ├── late_window.py   # Main evaluate() — runs agents + scores confidence
│   └── regime.py        # Market regime detection: TRENDING/VOLATILE/RANGING
│
├── agents/
│   ├── momentum_agent.py      # EMA crossover + MACD
│   ├── mean_reversion_agent.py # RSI extremes + Bollinger Bands
│   ├── volatility_agent.py    # ATR gating — abstains in flat markets
│   ├── orderflow_agent.py     # Polymarket bid/ask imbalance
│   ├── oracle_agent.py        # Chainlink vs Binance latency arbitrage
│   ├── consensus.py           # Weighted vote aggregation
│   └── meta_learner.py        # Rolling accuracy tracking, weight adjustment
│
├── execution/
│   ├── polymarket_executor.py # Order placement: GTC + FOK + repricing
│   ├── order_manager.py       # Order lifecycle tracking
│   ├── claimer.py             # On-chain winning position redemption
│   ├── wallet.py              # Polygon wallet (private key never leaves here)
│   └── token_resolver.py      # Resolves condition_id → YES/NO token_ids
│
├── risk/
│   ├── position_sizer.py  # Fractional Kelly with confidence scaling
│   ├── circuit_breaker.py # 3-tier halting: YELLOW/ORANGE/RED
│   ├── exposure_manager.py # USD exposure ceiling across all positions
│   └── pnl_tracker.py     # Session P&L, drawdown, win rate
│
├── websocket_server/
│   └── server.py          # Broadcasts state to dashboard (port 8765)
│
├── dashboard/             # Next.js 14 real-time dashboard
│   ├── app/               # App router, providers, root layout
│   ├── components/        # Header, MarketCard, AgentPanel, TradeHistory, ...
│   ├── hooks/useBotState.ts # Single reducer over all WS messages
│   └── lib/types.ts       # Shared TypeScript types
│
├── scripts/
│   ├── setup_wallet.py    # One-time CLOB credential generation
│   ├── recover_claims.py  # Recover unclaimed winning positions
│   ├── check_balance.py   # Check USDC balance on Polygon
│   └── backtest.py        # Historical strategy backtesting
│
├── logs/
│   ├── polyoracle.log     # Structured JSON log file
│   └── trades.db          # SQLite trade history
│
├── pyproject.toml         # Python deps + tool config
├── docker-compose.yml     # Redis + bot + dashboard
└── .env                   # Your config — never commit this
```

---

## Requirements

**Python:**
- Python 3.12+
- See `pyproject.toml` for all dependencies

**Node.js:**
- Node.js 18+
- npm or yarn

**Infrastructure:**
- Internet access (Binance WS, Polymarket API, Polygon RPC)
- For live trading: a Polygon wallet with USDC deposited

**Optional:**
- Redis (only needed if running via Docker)

---

## Installation

```bash
# 1. Clone the repo
git clone <repo-url>
cd polyoracle

# 2. Create and activate a Python virtualenv
python3.12 -m venv .venv
source .venv/bin/activate        # macOS/Linux
# .\.venv\Scripts\activate       # Windows

# 3. Install Python dependencies
pip install -e .                 # runtime deps only
pip install -e ".[dev]"          # + ruff, mypy, pytest

# 4. Install dashboard dependencies
cd dashboard && npm install && cd ..

# 5. Create your .env file (see next section)
cp .env.example .env             # if .env.example exists
# OR create .env manually — see below
```

---

## Environment Setup (.env)

Create a `.env` file in the project root. **Never commit this file.**

```dotenv
# ─────────────────────────────────────────────
#  PAPER TRADING (safe default — start here)
# ─────────────────────────────────────────────
PAPER_MODE=true
PAPER_INITIAL_BALANCE=1000.0


# ─────────────────────────────────────────────
#  WALLET (required for live trading only)
# ─────────────────────────────────────────────
# Your Polygon wallet private key (with or without 0x prefix)
PRIVATE_KEY=

# The wallet address that holds USDC on Polygon
# (same address derived from PRIVATE_KEY)
FUNDER_ADDRESS=

# Signature type: 0 = EOA (standard wallet), 1 = Magic/Email wallet
SIGNATURE_TYPE=0


# ─────────────────────────────────────────────
#  POLYMARKET CLOB API CREDENTIALS
#  Generated automatically by: python scripts/setup_wallet.py
#  Leave blank in paper mode — not needed
# ─────────────────────────────────────────────
CLOB_API_KEY=
CLOB_SECRET=
CLOB_PASS_PHRASE=


# ─────────────────────────────────────────────
#  STRATEGY PARAMETERS (optional — these are the defaults)
# ─────────────────────────────────────────────

# Minimum confidence score (0-100) required to place a trade
MIN_CONFIDENCE_SCORE=65

# Base trade size in USD
TRADE_AMOUNT_USD=100.0

# Maximum trade size in USD (Kelly can scale up to this)
MAX_TRADE_AMOUNT_USD=100.0

# Min/max token price range to consider (0.0-1.0)
# Tokens below 0.30 (< 30% implied probability) are too cheap / noisy
# Tokens above 0.80 are too expensive — no edge after fees
MIN_TOKEN_PRICE=0.30
MAX_TOKEN_PRICE=0.80

# Minimum BTC/ETH move (% from window open) to even consider a trade
MIN_WINDOW_DELTA_PCT=0.005

# Minimum expected edge over the ask price
MIN_TRADE_EDGE=0.02

# Start evaluating at T+5s (295s remaining)
ENTRY_WINDOW_START_SEC=295

# Start placing GTC orders at T+30s (270s remaining)
TRADING_WINDOW_START_SEC=270

# Stop trying to enter after this many seconds remaining
ENTRY_DEADLINE_SEC=90

# Switch from GTC (maker) to FOK (taker) when this many seconds remain
GTC_WINDOW_SEC=120

# Maximum concurrent open positions
MAX_CONCURRENT_POSITIONS=2

# Maximum total USD at risk across all open positions
MAX_EXPOSURE_USD=500.0


# ─────────────────────────────────────────────
#  RISK MANAGEMENT (optional — these are the defaults)
# ─────────────────────────────────────────────

# Daily loss limit in USD — hits RED circuit breaker
MAX_DAILY_LOSS_USD=1000.0

# Maximum drawdown % from peak balance — hits RED circuit breaker
MAX_DRAWDOWN_PCT=75.0

# Consecutive losses before circuit breaker activates
MAX_CONSECUTIVE_LOSSES=6

# Fractional Kelly multiplier (0.25 = quarter Kelly — recommended)
KELLY_FRACTION=0.25

# Minimum USDC balance required to keep trading
MIN_USDC_BALANCE=20.0


# ─────────────────────────────────────────────
#  INFRASTRUCTURE (optional — these are the defaults)
# ─────────────────────────────────────────────

# WebSocket server port (dashboard connects here)
WS_SERVER_PORT=8765

# Dashboard port (Next.js dev server)
DASHBOARD_PORT=3000

# Logging level: DEBUG | INFO | WARNING | ERROR
LOG_LEVEL=INFO

# Log file path
LOG_FILE=logs/polyoracle.log
```

> **Minimum required for paper trading:** Just set `PAPER_MODE=true`. Everything else has sane defaults.
>
> **Minimum required for live trading:** `PAPER_MODE=false`, `PRIVATE_KEY`, `FUNDER_ADDRESS`, and the three CLOB credentials (generated by `setup_wallet.py`).

---

## Running the Bot

Always run from the project root with your virtualenv active.

### Paper trading (recommended first step)

```bash
# Terminal 1 — start the bot
source .venv/bin/activate
python -m core.main --paper

# Terminal 2 — start the dashboard
cd dashboard && npm run dev
```

Open **http://localhost:3000** to see the live dashboard.

### Live trading

```bash
# Ensure PAPER_MODE=false and credentials are set in .env
python -m core.main --live
```

### Other run options

```bash
# Force paper mode regardless of .env setting
python -m core.main --paper

# Force live mode (overrides PAPER_MODE=true in .env)
python -m core.main --live

# Increase log verbosity
LOG_LEVEL=DEBUG python -m core.main --paper
```

---

## Dashboard

The dashboard at **http://localhost:3000** has six tabs:

| Tab | What you see |
|-----|--------------|
| **Terminal** | Live log feed with color-coded levels; send commands to the bot |
| **Markets** | BTC + ETH market cards with sparkline, AI confidence gauge, agent consensus, regime badge, per-asset session P&L |
| **Positions** | Active open positions with countdown to window close; resolved trade history with clickable detail modal |
| **Portfolio** | Equity curve over time + session statistics (win rate, Sharpe ratio, avg win/loss, EV) |
| **Agents** | Full agent cards with vote, conviction bars, accuracy trend, per-session accuracy pills; mute/unmute individual agents |
| **Risk** | Circuit breaker status, current drawdown vs threshold, exposure meters |

The header shows live **BTC and ETH prices** with window delta %, the current window countdown, and session P&L.

**Dashboard → Bot commands** (type in Terminal tab or send via WebSocket):

```
mute_agent <agent_name>      # Silence a specific agent
unmute_agent <agent_name>    # Re-enable a muted agent
collect_claims               # Trigger manual claims collection
```

---

## Live Trading Setup

> ⚠️ Run paper trading for at least 48 hours and achieve > 55% win rate over 50+ trades before going live.

```bash
# Step 1: Add your private key and funder address to .env
# PRIVATE_KEY=0x...
# FUNDER_ADDRESS=0x...

# Step 2: Generate CLOB API credentials (one-time)
python scripts/setup_wallet.py
# This writes CLOB_API_KEY, CLOB_SECRET, CLOB_PASS_PHRASE to your .env

# Step 3: Check your USDC balance on Polygon
python scripts/check_balance.py
# You need USDC in your FUNDER_ADDRESS wallet on Polygon mainnet
# Deposit via bridge: https://www.polymarket.com/deposit

# Step 4: Set live mode
# Edit .env: PAPER_MODE=false

# Step 5: Start with small size to validate
# Edit .env: TRADE_AMOUNT_USD=10.0

# Step 6: Run
python -m core.main --live
```

---

## Strategy Deep Dive

### Window lifecycle

Each 5-minute window goes through 5 phases:

```
T+0s    MONITORING  — subscribe order books, capture open price
T+5s    EVALUATING  — run agents + confidence every 5s, place GTC if score ≥ threshold
T+180s  TRADING     — same as evaluating; GTC orders already placed may still reprice
T+180s  (≤120s left) — switch to FOK for any new entries
T+290s  DEADLINE    — final FOK attempt
T+300s  RESOLVED    — fetch winner from oracle, record P&L, queue claims
```

### Confidence score breakdown

| Component | Max pts | What it measures |
|-----------|---------|------------------|
| Signal | 35 | RSI, MACD, Bollinger Band alignment |
| Agents | 25 | Weighted consensus from all 5 agents |
| Delta | 25 | Size of BTC/ETH move from window open |
| Regime | 15 | TRENDING market bonus |
| Momentum | 5 | Delta acceleration (direction getting stronger?) |
| Time decay | 5 | Bonus for acting later in window (more certainty) |
| Persistence | 5 | Consecutive same-direction signals |
| Cross-asset | ±10 | BTC↔ETH correlation confirmation |

A score ≥ 65 triggers a trade (configurable via `MIN_CONFIDENCE_SCORE`).

### Order types

**GTC (Good Till Cancelled)** — used when > 120s remain in the window:
- Places a resting limit bid at `confidence / 100` (e.g. score 72 → bid at 0.72)
- Repriced every 5 seconds if the market moves away
- Before repricing, checks for any partial fills to avoid double-exposure
- Cancelled automatically when the window enters the FOK phase

**FOK (Fill or Kill)** — used when ≤ 120s remain:
- Sweeps the ask up to a max fair price
- Pre-checked against order book depth (VWAP)
- Retried once at 50% size if the book is too thin

### Resolution chain

After each window closes, the bot determines the outcome in this order:

1. `get_market_winner()` polls the CLOB API every 30s for up to **5 minutes** (Polymarket's Chainlink oracle takes 7–12 min to settle)
2. If CLOB order book returns a mid-price near 0.00 or 1.00 → infer outcome
3. If CLOB returns HTTP 404 (book closed = market resolved) → retry `get_market_winner()` up to 10 more times over 5 min
4. If all polls exhausted → fall back to Binance **close-time kline** captured at T+15s

---

## Risk Management

### Position sizing (Kelly criterion)

```
kelly_fraction = base_kelly × confidence_scale × consecutive_loss_scale × drawdown_scale
size_usd       = balance × kelly_fraction × max_position_pct (capped at 8%)
```

Additional guardrails:
- Max `MAX_EXPOSURE_USD` across all open positions simultaneously (default $500)
- Effective token price must be `< 1.0` after fees (guaranteed loss otherwise)
- Minimum price `MIN_TOKEN_PRICE` = 0.30 (avoids garbage markets)

### Circuit breaker tiers

| Tier | Trigger | Effect |
|------|---------|--------|
| GREEN | Normal | Full size |
| YELLOW | 3+ consecutive losses or mild drawdown | Size × 0.5 |
| ORANGE | Daily loss limit approaching (75%) | Size × 0.25 |
| RED | Daily loss limit hit or severe drawdown | Trading halted |

### Meta-learner

Each agent tracks a rolling 50-trade accuracy. Agents that drop below 45% accuracy are automatically muted and excluded from the consensus. Weights are rebalanced continuously toward agents with higher recent accuracy.

---

## Utility Scripts

```bash
# Generate Polymarket CLOB API credentials from your private key
python scripts/setup_wallet.py

# Check USDC balance on Polygon (no private key needed)
python scripts/check_balance.py

# Recover and claim any unclaimed winning positions
python scripts/recover_claims.py

# Run a backtest over historical data
python scripts/backtest.py --days 30 --confidence 65
```

---

## Linting and Type Checking

```bash
# Lint and auto-fix
ruff check . --fix

# Type checking
mypy .
```

---

## Docker

```bash
# Start Redis + bot + dashboard together
docker-compose up

# Bot only (if Redis already running locally)
docker-compose up bot

# Rebuild after code changes
docker-compose up --build
```

The `docker-compose.yml` mounts `./logs` into the container so trade history and logs persist between restarts.

---

## Security Notes

- `PRIVATE_KEY` is accessed **only** in `execution/wallet.py` — it is never logged, never printed, never sent over the network
- `.env` must be added to `.gitignore` — never commit it
- All sensitive fields in `Config` are excluded from `__repr__`
- Paper mode is the default — `PAPER_MODE=true` unless you explicitly set it to `false`
