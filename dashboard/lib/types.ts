// ── WebSocket message types ───────────────────────────────────────────────────

export type WsMessageType =
  | "tick"
  | "window_state"
  | "agent_votes"
  | "confidence"
  | "trade_executed"
  | "trade_resolved"
  | "trade_cancelled"
  | "circuit_breaker"
  | "portfolio_update"
  | "log"
  // ETH variants
  | "eth_tick"
  | "eth_window_state"
  | "eth_agent_votes"
  | "eth_confidence"
  | "eth_trade_executed"
  | "eth_trade_resolved"
  | "eth_trade_cancelled";

export interface WsMessage<T = unknown> {
  type: WsMessageType;
  data: T;
  ts: number;
}

// ── Price / Market data ───────────────────────────────────────────────────────

export interface BtcTick {
  price: number;
  timestamp: number;
}

export type WindowPhase = "monitoring" | "evaluating" | "trading" | "deadline" | "resolved";

export interface WindowState {
  window_ts: number;
  open_price: number;
  current_price: number;
  delta_pct: number;
  phase: WindowPhase;
  elapsed_sec: number;
  remaining_sec: number;
  window_slug: string;
}

// ── Agent system ──────────────────────────────────────────────────────────────

export type VoteDirection = "UP" | "DOWN" | "ABSTAIN";

export interface AgentVote {
  agent: string;
  vote: VoteDirection;
  conviction: number;       // 0-1
  reasoning: string;
  accuracy: number;         // Rolling accuracy 0-1
  weight: number;           // Meta-learner weight
  is_muted: boolean;
  effective_conviction: number;
  session_accuracy?: Record<string, number>;  // Per-session accuracy (optional)
  trend?: string;           // "↑" | "↓" | "→"
}

export interface Consensus {
  direction: "UP" | "DOWN" | "NEUTRAL";
  strength: number;
  agreement_ratio: number;
  up_weight: number;
  down_weight: number;
  abstain_count: number;
  votes: AgentVote[];
}

// ── Confidence ────────────────────────────────────────────────────────────────

export interface ConfidenceBreakdown {
  signal_contribution: number;
  agent_contribution: number;
  delta_contribution: number;
  regime_contribution: number;
  momentum_contribution: number;
  total: number;
  should_trade: boolean;
}

// ── Trades ────────────────────────────────────────────────────────────────────

export interface TradeExecuted {
  order_id: string;
  market: string;
  direction: "UP" | "DOWN";
  price: number;
  size_usd: number;
  confidence: number;
  window_ts: number;
}

export interface TradeResolved {
  order_id: string;
  market: string;
  direction: "UP" | "DOWN";
  actual_direction: "UP" | "DOWN";
  won: boolean;
  pnl: number;
  window_ts: number;
}

// ── Portfolio ─────────────────────────────────────────────────────────────────

export interface PortfolioUpdate {
  balance: number;
  total_pnl: number;
  win_rate: number;
  total_trades: number;
  winning_trades: number;
  losing_trades: number;
  avg_win: number;
  avg_loss: number;
  best_trade: number;
  worst_trade: number;
  sharpe_ratio: number;
  daily_pnl: number;
  consecutive_wins: number;
  consecutive_losses: number;
  expected_value: number;
  avg_confidence_wins: number;
  avg_confidence_losses: number;
}

// ── Risk ──────────────────────────────────────────────────────────────────────

export type CircuitTier = "GREEN" | "YELLOW" | "ORANGE" | "RED";

export interface CircuitBreakerStatus {
  tier: CircuitTier;
  reason: string;
  triggered_at: number | null;
  resume_at: number | null;
  size_multiplier: number;
  can_trade: boolean;
}

// ── Log ───────────────────────────────────────────────────────────────────────

export type LogLevel = "DEBUG" | "INFO" | "TRADE" | "WARNING" | "ERROR" | "CRITICAL";

export interface LogEntry {
  level: LogLevel;
  module: string;
  message: string;
  timestamp: string;
  id: number;   // Client-side ID for React keys
}

// ── Active position (richer than just an ID) ──────────────────────────────────

export interface ActivePosition extends TradeExecuted {
  opened_at: number;  // client-side timestamp (Date.now()) for elapsed timer
}

// ── Bot state (aggregated) ────────────────────────────────────────────────────

export interface BotState {
  connected: boolean;
  // BTC
  lastTick: BtcTick | null;
  ticks: BtcTick[];              // last 120 price ticks for sparkline
  window: WindowState | null;
  agents: Consensus | null;
  confidence: ConfidenceBreakdown | null;
  // ETH
  ethLastTick: BtcTick | null;
  ethTicks: BtcTick[];
  ethWindow: WindowState | null;
  ethAgents: Consensus | null;
  ethConfidence: ConfidenceBreakdown | null;
  ethActivePositions: ActivePosition[];
  // Shared
  activeTradeIds: string[];       // kept for page.tsx notification dot
  activePositions: ActivePosition[];
  recentTrades: TradeResolved[];
  portfolio: PortfolioUpdate | null;
  circuit: CircuitBreakerStatus | null;
  logs: LogEntry[];
}

// ── Agent metadata ────────────────────────────────────────────────────────────

export const AGENT_META: Record<string, { emoji: string; label: string; description: string }> = {
  momentum: {
    emoji: "🏄",
    label: "Momentum",
    description: "The Trend Rider — follows short-term momentum",
  },
  mean_reversion: {
    emoji: "🔄",
    label: "Contrarian",
    description: "The Contrarian — fades overextended moves",
  },
  volatility: {
    emoji: "🌊",
    label: "Volatility",
    description: "The Risk Sentinel — avoids flat markets",
  },
  orderflow: {
    emoji: "📊",
    label: "Order Flow",
    description: "The Book Reader — reads Polymarket order book",
  },
  oracle: {
    emoji: "🔮",
    label: "Oracle",
    description: "The Arbitrageur — exploits CEX vs Chainlink latency",
  },
};
