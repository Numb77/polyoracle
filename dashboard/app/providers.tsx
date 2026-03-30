"use client";

import { createContext, useContext, useEffect, useRef } from "react";
import { useBotState } from "@/hooks/useBotState";
import { useWebSocket } from "@/hooks/useWebSocket";
import type { BotState } from "@/lib/types";

interface BotContextValue {
	state: BotState;
	send: (data: unknown) => void;
}

const BotContext = createContext<BotContextValue>({
	state: {
		connected: false,
		assets: {},
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
		lastClaimsRecovery: null,
	},
	send: () => {},
});

export function useBotContext() {
	return useContext(BotContext);
}

export function Providers({ children }: { children: React.ReactNode }) {
	const { state, handleMessage, setConnected } = useBotState();

	const { connected, send } = useWebSocket((msg) => {
		handleMessage(msg);
	});

	// Sync connected state into bot state
	const prevConnected = useRef(connected);
	useEffect(() => {
		if (prevConnected.current !== connected) {
			prevConnected.current = connected;
			setConnected(connected);
		}
	}, [connected, setConnected]);

	return (
		<BotContext.Provider value={{ state: { ...state, connected }, send }}>
			{children}
		</BotContext.Provider>
	);
}
