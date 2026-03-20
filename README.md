# PolyOracle

Autonomous Polymarket 5-minute BTC prediction market trading bot.

Uses the **Late-Window Directional Strategy** — waits until the final 10-30 seconds of each 5-minute window when BTC's direction is largely locked in, then places high-confidence bets on the confirmed outcome.

## Architecture

```
polyoracle/
├── core/          Window clock, config, logger, main orchestrator
├── data/          Binance WS, Polymarket WS/REST, Chainlink oracle, candles
├── strategy/      Indicators, signals, confidence engine, regime detection
├── agents/        5 specialized agents + consensus engine + meta-learner
├── execution/     Wallet, order placement, token resolver, auto-claimer
├── risk/          Kelly sizing, circuit breaker, drawdown, P&L tracker
├── websocket_server/  Backend → frontend bridge
├── dashboard/     Next.js real-time dashboard
└── scripts/       Setup, paper trade, backtest
```

## Setup

### Prerequisites

- Python 3.9+
- Node.js 18+
- Redis (optional, for Docker mode)

### Install

```bash
# 1. Create virtual environment
python3 -m venv .venv
source .venv/bin/activate

# 2. Install Python deps
pip install python-dotenv pydantic pydantic-settings websockets aiohttp \
    pandas numpy web3 python-json-logger rich click ta scipy orjson

# Optional: Polymarket SDK (required for live trading)
pip install py-clob-client

# 3. Setup .env
cp .env.example .env
# Edit .env — at minimum set PAPER_MODE=true

# 4. Install dashboard
cd dashboard && npm install && cd ..
```

### First run (paper trading)

```bash
# Terminal 1: Start the bot
source .venv/bin/activate
python -m core.main --paper

# Terminal 2: Start the dashboard
cd dashboard && npm run dev
```

Open http://localhost:3000

### Backtest

```bash
python scripts/backtest.py --days 30 --confidence 65
```

### Live trading setup

> ⚠️ Run paper trading for at least 48 hours with win rate > 60% over 100+ trades before going live.

```bash
# 1. Set PAPER_MODE=false and add PRIVATE_KEY to .env
# 2. Run wallet setup
python scripts/setup_wallet.py

# 3. Start live bot
python -m core.main --live
```

## Strategy

### Late-Window Directional

Each 5-minute BTC Up/Down market resolves based on the Chainlink oracle price at open vs close. The strategy:

1. **Monitors** passively for the first 4:30 (T-30s)
2. **Evaluates** at T-30s: runs all 5 agents + confidence engine
3. **Decides** at T-10s: if confidence ≥ threshold → execute
4. **Hard deadline** at T-5s: fire or skip

### Primary Signal: Window Delta

```
window_delta = (current_price - window_open_price) / window_open_price × 100
```

This directly answers the market question. A BTC move of +0.05% at T-30s has an ~85% chance of staying positive at T+0s.

### Five Agents

| Agent | Strategy | Strength |
|-------|----------|----------|
| 🏄 Momentum | EMA crossover + MACD | Trending markets |
| 🔄 Contrarian | RSI extremes + Bollinger | Overextended moves |
| 🌊 Volatility | ATR-based gating | Avoids flat markets |
| 📊 Order Flow | Polymarket bid/ask imbalance | Sentiment edge |
| 🔮 Oracle | CEX vs Chainlink latency | Structural edge |

### Confidence Score (0-100)

- Signal magnitude: 0-40 pts
- Agent consensus: 0-25 pts
- Window delta size: 0-20 pts
- Market regime: 0-15 pts

Trade fires when confidence ≥ `MIN_CONFIDENCE_SCORE` (default: 65).

### Risk Management

- **Quarter-Kelly** position sizing
- **3-tier circuit breaker**: YELLOW (reduce size) → ORANGE (30min pause) → RED (emergency stop)
- **Drawdown scaling**: positions scale down as drawdown increases
- **Auto-claim**: winning positions redeemed automatically

## Dashboard

7 tabs:
1. **Terminal** — live log feed with command input
2. **Markets** — current window with AI confidence overlay
3. **Positions** — active + historical trades
4. **Portfolio** — equity curve + statistics
5. **Agents** — live agent votes + consensus gauge
6. **Risk** — circuit breaker + exposure meters
7. **Settings** — live parameter tuning

## Security

- Private key is accessed **only** in `execution/wallet.py`
- Never logged, never transmitted, never stored outside `.env`
- `.env` is gitignored
- Paper mode is the default

## Critical Warnings

- **Start with paper trading** — run 48+ hours before going live
- **Start small** ($10-20 bets) — validate win rate > 60% over 100+ trades first
- **Query fee rates before every trade** — Polymarket fees change
- **Never commit `.env`** — your private key must stay local
