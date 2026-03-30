"use client";

import { useEffect, useRef, useState } from "react";
import { useBotContext } from "@/app/providers";
import { cn, formatBtcPrice, getCircuitColor } from "@/lib/utils";

const ASSET_COLOR: Record<string, string> = {
	BTC: "text-accent-green",
	ETH: "text-indigo-400",
	SOL: "text-purple-400",
	DOGE: "text-yellow-400",
	XRP: "text-sky-400",
};

function formatPrice(symbol: string, price: number): string {
	if (symbol === "BTC") return formatBtcPrice(price);
	return price.toLocaleString("en-US", {
		minimumFractionDigits: 2,
		maximumFractionDigits: price >= 100 ? 2 : 4,
	});
}

function AssetTicker({ symbol }: { symbol: string }) {
	const { state } = useBotContext();
	const assetState = state.assets[symbol];
	const tick = assetState?.lastTick ?? null;
	const win = assetState?.window ?? null;
	const prevPrice = useRef<number | null>(null);
	const [flash, setFlash] = useState("");

	useEffect(() => {
		if (!tick) return;
		if (prevPrice.current !== null && prevPrice.current !== tick.price) {
			setFlash(tick.price > prevPrice.current ? "flash-green" : "flash-red");
			const t = setTimeout(() => setFlash(""), 500);
			return () => clearTimeout(t);
		}
		prevPrice.current = tick.price;
	// eslint-disable-next-line react-hooks/exhaustive-deps
	}, [tick?.price]);

	if (!tick) return null;

	const color = ASSET_COLOR[symbol] ?? "text-zinc-400";

	return (
		<div className="flex items-center gap-2">
			<span className={`text-xs font-bold ${color}`}>{symbol}</span>
			<span className={cn("text-lg font-semibold price-display", flash)}>
				{formatPrice(symbol, tick.price)}
			</span>
			{win && (
				<span
					className={`text-xs font-mono ${
						win.delta_pct >= 0 ? "text-accent-green" : "text-accent-red"
					}`}
				>
					{win.delta_pct >= 0 ? "+" : ""}
					{win.delta_pct.toFixed(3)}%
				</span>
			)}
		</div>
	);
}

export function Header() {
	const { state } = useBotContext();
	const { connected, circuit, portfolio } = state;
	const [clock, setClock] = useState("");

	const assetSymbols = Object.keys(state.assets);
	// Use BTC window for the global countdown (first asset fallback)
	const win = state.assets["BTC"]?.window ?? state.assets[assetSymbols[0]]?.window ?? null;
	const remaining = win?.remaining_sec ?? 0;
	const progress = win ? ((300 - remaining) / 300) * 100 : 0;

	useEffect(() => {
		const fmt = () =>
			setClock(new Date().toLocaleTimeString("en-US", { hour12: false }));
		fmt();
		const id = setInterval(fmt, 1000);
		return () => clearInterval(id);
	}, []);

	return (
		<header
			className="relative flex items-center justify-between px-6 py-3 border-b shrink-0"
			style={{
				borderColor: "var(--border-color)",
				background: "var(--surface)",
			}}
		>
			{/* Window progress bar along bottom of header */}
			{win && (
				<div className="absolute bottom-0 left-0 right-0 h-0.5 bg-zinc-800">
					<div
						className="h-full transition-all duration-1000"
						style={{
							width: `${progress}%`,
							background:
								remaining < 10
									? "var(--accent-red)"
									: remaining < 30
										? "#FACC15"
										: "var(--accent-green)",
						}}
					/>
				</div>
			)}

			{/* Logo */}
			<div className="flex items-center gap-3 shrink-0">
				<div className="text-lg font-bold tracking-widest text-accent-green glow-text-green">
					POLY<span className="text-white">ORACLE</span>
				</div>
				<div className="text-xs text-text-secondary font-mono">v1.0.0</div>
				{portfolio?.paper_mode && (
					<span className="px-2 py-0.5 rounded text-[10px] font-bold font-mono bg-yellow-400/10 text-yellow-400 border border-yellow-400/30 tracking-wider">
						PAPER MODE
					</span>
				)}
			</div>

			{/* Center: all asset prices + window countdown */}
			<div className="flex items-center gap-4 font-mono flex-wrap">
				{assetSymbols.map((sym, i) => (
					<div key={sym} className="flex items-center gap-4">
						{i > 0 && <div className="w-px h-5 bg-zinc-700" />}
						<AssetTicker symbol={sym} />
					</div>
				))}

				{/* Window countdown */}
				{win && (
					<div className="flex items-center gap-2 text-xs border-l border-zinc-700 pl-4 ml-1">
						<span className="text-text-secondary uppercase">{win.phase}</span>
						<span
							className={cn(
								"font-mono font-bold tabular-nums",
								remaining < 10
									? "text-accent-red animate-pulse"
									: remaining < 30
										? "text-yellow-400"
										: "text-white",
							)}
						>
							{Math.floor(remaining / 60)}:
							{String(Math.floor(remaining % 60)).padStart(2, "0")}
						</span>
					</div>
				)}

				{/* Live P&L pill */}
				{portfolio && (
					<div
						className={cn(
							"hidden md:flex items-center gap-1.5 px-2 py-1 rounded text-xs font-bold border",
							portfolio.total_pnl >= 0
								? "text-accent-green border-accent-green/30 bg-accent-green/5"
								: "text-accent-red border-accent-red/30 bg-accent-red/5",
						)}
					>
						<span className="text-text-secondary font-normal">P&L</span>
						{portfolio.total_pnl >= 0 ? "+" : ""}$
						{portfolio.total_pnl.toFixed(2)}
					</div>
				)}
			</div>

			{/* Right: Status indicators */}
			<div className="flex items-center gap-4 text-xs font-mono">
				{circuit && (
					<div
						className={`flex items-center gap-1.5 ${getCircuitColor(circuit.tier)}`}
					>
						<div
							className={cn(
								"w-2 h-2 rounded-full",
								circuit.tier === "GREEN"
									? "bg-accent-green active-dot"
									: circuit.tier === "YELLOW"
										? "bg-yellow-400"
										: circuit.tier === "ORANGE"
											? "bg-orange-400"
											: "bg-accent-red animate-pulse",
							)}
						/>
						{circuit.tier}
					</div>
				)}

				<div className="flex items-center gap-1.5">
					<div
						className={`w-2 h-2 rounded-full ${
							connected ? "bg-accent-green active-dot" : "bg-accent-red"
						}`}
					/>
					<span className={connected ? "text-accent-green" : "text-accent-red"}>
						{connected ? "LIVE" : "OFFLINE"}
					</span>
				</div>

				<span className="text-text-secondary hidden sm:block">{clock}</span>
			</div>
		</header>
	);
}
