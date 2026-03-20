"use client";

import { useState, useEffect, useRef } from "react";
import { useBotContext } from "@/app/providers";
import { formatBtcPrice, getCircuitColor, cn } from "@/lib/utils";

export function Header() {
  const { state } = useBotContext();
  const { connected, lastTick, circuit, window: win, portfolio } = state;
  const [clock, setClock] = useState("");
  const [flashClass, setFlashClass] = useState("");
  const prevPrice = useRef<number | null>(null);

  useEffect(() => {
    const fmt = () =>
      setClock(new Date().toLocaleTimeString("en-US", { hour12: false }));
    fmt();
    const id = setInterval(fmt, 1000);
    return () => clearInterval(id);
  }, []);

  // Flash price on direction change
  useEffect(() => {
    if (!lastTick) return;
    if (prevPrice.current !== null && prevPrice.current !== lastTick.price) {
      const dir = lastTick.price > prevPrice.current ? "flash-green" : "flash-red";
      setFlashClass(dir);
      const t = setTimeout(() => setFlashClass(""), 500);
      return () => clearTimeout(t);
    }
    prevPrice.current = lastTick.price;
  }, [lastTick?.price]);

  const remaining = win?.remaining_sec ?? 0;
  const progress = win ? ((300 - remaining) / 300) * 100 : 0;

  return (
    <header
      className="relative flex items-center justify-between px-6 py-3 border-b shrink-0"
      style={{ borderColor: "var(--border-color)", background: "var(--surface)" }}
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
      <div className="flex items-center gap-3">
        <div className="text-lg font-bold tracking-widest text-accent-green glow-text-green">
          POLY<span className="text-white">ORACLE</span>
        </div>
        <div className="text-xs text-text-secondary font-mono">v0.1.0</div>
      </div>

      {/* Center: BTC Price + window */}
      <div className="flex items-center gap-6 font-mono">
        {lastTick && (
          <div className="flex items-center gap-2">
            <span className="text-xs text-text-secondary">BTC</span>
            <span className={cn("text-xl font-semibold price-display", flashClass)}>
              {formatBtcPrice(lastTick.price)}
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
        )}

        {/* Window countdown */}
        {win && (
          <div className="flex items-center gap-2 text-xs">
            <span className="text-text-secondary uppercase">{win.phase}</span>
            <span
              className={cn(
                "font-mono font-bold tabular-nums",
                remaining < 10
                  ? "text-accent-red animate-pulse"
                  : remaining < 30
                  ? "text-yellow-400"
                  : "text-white"
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
                : "text-accent-red border-accent-red/30 bg-accent-red/5"
            )}
          >
            <span className="text-text-secondary font-normal">P&L</span>
            {portfolio.total_pnl >= 0 ? "+" : ""}${portfolio.total_pnl.toFixed(2)}
          </div>
        )}
      </div>

      {/* Right: Status indicators */}
      <div className="flex items-center gap-4 text-xs font-mono">
        {circuit && (
          <div className={`flex items-center gap-1.5 ${getCircuitColor(circuit.tier)}`}>
            <div
              className={cn(
                "w-2 h-2 rounded-full",
                circuit.tier === "GREEN"   ? "bg-accent-green active-dot" :
                circuit.tier === "YELLOW"  ? "bg-yellow-400" :
                circuit.tier === "ORANGE"  ? "bg-orange-400" :
                "bg-accent-red animate-pulse"
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
