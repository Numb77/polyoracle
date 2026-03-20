"use client";

import { useCallback, useReducer } from "react";
import { MAX_LOG_ENTRIES, MAX_TRADE_HISTORY } from "@/lib/constants";
import type {
  BotState,
  WsMessage,
  LogEntry,
  TradeResolved,
  TradeExecuted,
  ActivePosition,
  BtcTick,
} from "@/lib/types";

let logIdCounter = 0;

const initialState: BotState = {
  connected: false,
  lastTick: null,
  ticks: [],
  window: null,
  agents: null,
  confidence: null,
  ethLastTick: null,
  ethTicks: [],
  ethWindow: null,
  ethAgents: null,
  ethConfidence: null,
  ethActivePositions: [],
  activeTradeIds: [],
  activePositions: [],
  recentTrades: [],
  portfolio: null,
  circuit: null,
  logs: [],
};

type Action =
  | { type: "SET_CONNECTED"; payload: boolean }
  | { type: "WS_MESSAGE"; payload: WsMessage };

function botReducer(state: BotState, action: Action): BotState {
  switch (action.type) {
    case "SET_CONNECTED":
      return { ...state, connected: action.payload };

    case "WS_MESSAGE": {
      const { type, data } = action.payload;

      switch (type) {
        case "tick": {
          const tick = data as BtcTick;
          return {
            ...state,
            lastTick: tick,
            ticks: [...state.ticks.slice(-119), tick],
          };
        }

        case "window_state":
          return { ...state, window: data as BotState["window"] };

        case "agent_votes":
          return { ...state, agents: data as BotState["agents"] };

        case "confidence":
          return { ...state, confidence: data as BotState["confidence"] };

        case "trade_executed": {
          const trade = data as TradeExecuted;
          const pos: ActivePosition = { ...trade, opened_at: Date.now() };
          return {
            ...state,
            activeTradeIds: [...state.activeTradeIds, trade.order_id],
            activePositions: [...state.activePositions, pos],
          };
        }

        case "trade_resolved": {
          const resolved = data as TradeResolved;
          return {
            ...state,
            activeTradeIds: state.activeTradeIds.filter(
              (id) => id !== resolved.order_id
            ),
            activePositions: state.activePositions.filter(
              (p) => p.order_id !== resolved.order_id
            ),
            recentTrades: [resolved, ...state.recentTrades].slice(
              0,
              MAX_TRADE_HISTORY
            ),
          };
        }

        case "trade_cancelled": {
          const cancelled = data as { order_id: string };
          return {
            ...state,
            activeTradeIds: state.activeTradeIds.filter(
              (id) => id !== cancelled.order_id
            ),
            activePositions: state.activePositions.filter(
              (p) => p.order_id !== cancelled.order_id
            ),
          };
        }

        // ── ETH messages ─────────────────────────────────────────────────────

        case "eth_tick": {
          const tick = data as BtcTick;
          return {
            ...state,
            ethLastTick: tick,
            ethTicks: [...state.ethTicks.slice(-119), tick],
          };
        }

        case "eth_window_state":
          return { ...state, ethWindow: data as BotState["ethWindow"] };

        case "eth_agent_votes":
          return { ...state, ethAgents: data as BotState["ethAgents"] };

        case "eth_confidence":
          return { ...state, ethConfidence: data as BotState["ethConfidence"] };

        case "eth_trade_executed": {
          const trade = data as TradeExecuted;
          const pos: ActivePosition = { ...trade, opened_at: Date.now() };
          return {
            ...state,
            activeTradeIds: [...state.activeTradeIds, trade.order_id],
            ethActivePositions: [...state.ethActivePositions, pos],
          };
        }

        case "eth_trade_resolved": {
          const resolved = data as TradeResolved;
          return {
            ...state,
            activeTradeIds: state.activeTradeIds.filter(
              (id) => id !== resolved.order_id
            ),
            ethActivePositions: state.ethActivePositions.filter(
              (p) => p.order_id !== resolved.order_id
            ),
            recentTrades: [resolved, ...state.recentTrades].slice(
              0,
              MAX_TRADE_HISTORY
            ),
          };
        }

        case "eth_trade_cancelled": {
          const cancelled = data as { order_id: string };
          return {
            ...state,
            activeTradeIds: state.activeTradeIds.filter(
              (id) => id !== cancelled.order_id
            ),
            ethActivePositions: state.ethActivePositions.filter(
              (p) => p.order_id !== cancelled.order_id
            ),
          };
        }

        case "portfolio_update":
          return {
            ...state,
            portfolio: data as BotState["portfolio"],
          };

        case "circuit_breaker":
          return {
            ...state,
            circuit: data as BotState["circuit"],
          };

        case "log": {
          const logData = data as Omit<LogEntry, "id">;
          const entry: LogEntry = { ...logData, id: ++logIdCounter };
          return {
            ...state,
            logs: [...state.logs, entry].slice(-MAX_LOG_ENTRIES),
          };
        }

        default:
          return state;
      }
    }

    default:
      return state;
  }
}

export function useBotState() {
  const [state, dispatch] = useReducer(botReducer, initialState);

  const handleMessage = useCallback((msg: WsMessage) => {
    dispatch({ type: "WS_MESSAGE", payload: msg });
  }, []);

  const setConnected = useCallback((connected: boolean) => {
    dispatch({ type: "SET_CONNECTED", payload: connected });
  }, []);

  return { state, handleMessage, setConnected };
}
