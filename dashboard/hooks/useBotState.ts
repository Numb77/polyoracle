"use client";

import { useCallback, useReducer } from "react";
import { MAX_LOG_ENTRIES, MAX_TRADE_HISTORY } from "@/lib/constants";
import type {
	ActivePosition,
	AssetState,
	BotState,
	ClaimsRecoveryResult,
	LogEntry,
	PriceTick,
	TradeExecuted,
	TradeResolved,
	WsMessage,
} from "@/lib/types";

let logIdCounter = 0;

const emptyAssetState: AssetState = {
	lastTick: null,
	ticks: [],
	window: null,
	agents: null,
	confidence: null,
	activePositions: [],
};

const initialState: BotState = {
	connected: false,
	assets: {},
	// Legacy BTC aliases
	lastTick: null,
	ticks: [],
	window: null,
	agents: null,
	confidence: null,
	// Legacy ETH aliases
	ethLastTick: null,
	ethTicks: [],
	ethWindow: null,
	ethAgents: null,
	ethConfidence: null,
	ethActivePositions: [],
	// Shared
	activeTradeIds: [],
	activePositions: [],
	recentTrades: [],
	portfolio: null,
	circuit: null,
	logs: [],
	lastClaimsRecovery: null,
};

type Action =
	| { type: "SET_CONNECTED"; payload: boolean }
	| { type: "WS_MESSAGE"; payload: WsMessage };

/** Helper: get or create asset slice */
function getAsset(state: BotState, asset: string): AssetState {
	return state.assets[asset] ?? { ...emptyAssetState };
}

/** Helper: update one asset slice and sync legacy aliases */
function withAsset(
	state: BotState,
	asset: string,
	patch: Partial<AssetState>,
): BotState {
	const prev = getAsset(state, asset);
	const updated = { ...prev, ...patch };
	const newAssets = { ...state.assets, [asset]: updated };
	// Sync legacy aliases for BTC and ETH so existing components keep working
	const btc = newAssets.BTC ?? emptyAssetState;
	const eth = newAssets.ETH ?? emptyAssetState;
	return {
		...state,
		assets: newAssets,
		lastTick: btc.lastTick,
		ticks: btc.ticks,
		window: btc.window,
		agents: btc.agents,
		confidence: btc.confidence,
		ethLastTick: eth.lastTick,
		ethTicks: eth.ticks,
		ethWindow: eth.window,
		ethAgents: eth.agents,
		ethConfidence: eth.confidence,
		ethActivePositions: eth.activePositions,
		// Also merge activePositions from all asset lanes
		activePositions: Object.values(newAssets).flatMap((a) => a.activePositions),
	};
}

function botReducer(state: BotState, action: Action): BotState {
	switch (action.type) {
		case "SET_CONNECTED":
			return { ...state, connected: action.payload };

		case "WS_MESSAGE": {
			const { type, data } = action.payload;

			switch (type) {
				case "connection_init":
					return { ...state, logs: [], recentTrades: [] };

				case "tick": {
					const tick = data as PriceTick;
					const asset = tick.asset || "BTC";
					const prev = getAsset(state, asset);
					return withAsset(state, asset, {
						lastTick: tick,
						ticks: [...prev.ticks.slice(-119), tick],
					});
				}

				case "window_state": {
					const ws = data as BotState["window"];
					const asset = (ws as any)?.asset || "BTC";
					return withAsset(state, asset, { window: ws });
				}

				case "agent_votes": {
					const agents = data as BotState["agents"];
					const asset = (agents as any)?.asset || "BTC";
					return withAsset(state, asset, { agents });
				}

				case "confidence": {
					const confidence = data as BotState["confidence"];
					const asset = (confidence as any)?.asset || "BTC";
					return withAsset(state, asset, { confidence });
				}

				case "trade_executed": {
					const trade = data as TradeExecuted;
					const asset = trade.asset || "BTC";
					const assetState = getAsset(state, asset);
					// Dedup: skip if this order_id or same asset+window_ts already tracked
					const alreadyExists =
						assetState.activePositions.some(
							(p) => p.order_id === trade.order_id,
						) ||
						assetState.activePositions.some(
							(p) => p.asset === trade.asset && p.window_ts === trade.window_ts,
						);
					if (alreadyExists) return state;
					const pos: ActivePosition = { ...trade, opened_at: Date.now() };
					const newState = withAsset(state, asset, {
						activePositions: [...assetState.activePositions, pos],
					});
					return {
						...newState,
						activeTradeIds: [...state.activeTradeIds, trade.order_id],
					};
				}

				case "trade_resolved": {
					const resolved = data as TradeResolved;
					const asset = resolved.asset || "BTC";
					const assetState = getAsset(state, asset);
					// Enrich resolved trade with snapshot data from active position
					const activePos = assetState.activePositions.find(
						(p) => p.order_id === resolved.order_id,
					);
					const enriched: TradeResolved = {
						...resolved,
						price: activePos?.price,
						size_usd: activePos?.size_usd,
						confidence: resolved.confidence ?? activePos?.confidence,
						order_type: activePos?.order_type,
						agent_votes: activePos?.agent_votes,
						confidence_breakdown: activePos?.confidence_breakdown,
						window_delta_pct: activePos?.window_delta_pct,
						opened_at: activePos?.opened_at,
					};
					const newState = withAsset(state, asset, {
						activePositions: assetState.activePositions.filter(
							(p) => p.order_id !== resolved.order_id,
						),
					});
					return {
						...newState,
						activeTradeIds: state.activeTradeIds.filter(
							(id) => id !== resolved.order_id,
						),
						recentTrades: [
							enriched,
							...state.recentTrades.filter(
								(t) => t.order_id !== resolved.order_id,
							),
						].slice(0, MAX_TRADE_HISTORY),
					};
				}

				case "trade_cancelled": {
					const cancelled = data as { order_id: string; asset?: string };
					const asset = cancelled.asset || "BTC";
					const assetState = getAsset(state, asset);
					const newState = withAsset(state, asset, {
						activePositions: assetState.activePositions.filter(
							(p) => p.order_id !== cancelled.order_id,
						),
					});
					return {
						...newState,
						activeTradeIds: state.activeTradeIds.filter(
							(id) => id !== cancelled.order_id,
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

				case "claims_recovery_complete":
					return {
						...state,
						lastClaimsRecovery: data as ClaimsRecoveryResult,
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
