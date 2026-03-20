"use client";

import { useBotContext } from "@/app/providers";
import { getCircuitBg, getCircuitColor, cn } from "@/lib/utils";
import { AGENT_META } from "@/lib/types";

export function RiskDashboard() {
  const { state, send } = useBotContext();
  const { circuit, portfolio, agents } = state;

  const handlePause = () => send({ command: "pause" });
  const handleResume = () => send({ command: "resume" });
  const handleEmergencyStop = () => {
    if (confirm("Trigger emergency stop? This will cancel all orders and halt trading.")) {
      send({ command: "emergency_stop" });
    }
  };

  return (
    <div className="space-y-4">
      {/* Circuit breaker */}
      {circuit && (
        <div className={cn("card p-6 border-2", getCircuitBg(circuit.tier))}>
          <div className="flex items-start justify-between">
            <div>
              <div className="text-xs text-text-secondary mb-1">CIRCUIT BREAKER</div>
              <div className={cn("text-3xl font-bold mb-1", getCircuitColor(circuit.tier))}>
                {circuit.tier}
              </div>
              <div className="text-sm text-text-secondary">{circuit.reason}</div>
              {circuit.resume_at && circuit.resume_at > Date.now() / 1000 && (
                <div className="text-xs text-orange-400 mt-1">
                  Resumes in {Math.ceil(circuit.resume_at - Date.now() / 1000)}s
                </div>
              )}
            </div>
            <div className="flex flex-col items-end gap-2">
              <div
                className={cn(
                  "w-6 h-6 rounded-full",
                  circuit.tier === "GREEN"
                    ? "bg-accent-green active-dot"
                    : circuit.tier === "YELLOW"
                    ? "bg-yellow-400"
                    : circuit.tier === "ORANGE"
                    ? "bg-orange-400"
                    : "bg-accent-red animate-blink"
                )}
              />
              <div className="text-xs text-text-secondary">
                Size: {(circuit.size_multiplier * 100).toFixed(0)}%
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Portfolio risk metrics */}
      {portfolio && (
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          <div className="card p-4">
            <div className="text-xs text-text-secondary mb-1">DAILY P&L</div>
            <div
              className={cn(
                "text-xl font-bold",
                portfolio.daily_pnl >= 0 ? "text-accent-green" : "text-accent-red"
              )}
            >
              {portfolio.daily_pnl >= 0 ? "+" : ""}${Math.abs(portfolio.daily_pnl).toFixed(2)}
            </div>
          </div>
          <div className="card p-4">
            <div className="text-xs text-text-secondary mb-1">BALANCE</div>
            <div className="text-xl font-bold text-white">
              ${portfolio.balance.toFixed(2)}
            </div>
          </div>
          <div className="card p-4">
            <div className="text-xs text-text-secondary mb-1">LOSS STREAK</div>
            <div
              className={cn(
                "text-xl font-bold",
                portfolio.consecutive_losses >= 3 ? "text-accent-red" : "text-white"
              )}
            >
              {portfolio.consecutive_losses}
            </div>
          </div>
          <div className="card p-4">
            <div className="text-xs text-text-secondary mb-1">ACTIVE TRADES</div>
            <div className="text-xl font-bold text-white">
              {state.activeTradeIds.length}
            </div>
          </div>
        </div>
      )}

      {/* Agent meta-learner weights */}
      {agents && agents.votes.length > 0 && (
        <div className="card p-4">
          <div className="text-xs text-text-secondary mb-3">AGENT ACCURACY (META-LEARNER)</div>
          <div className="space-y-2">
            {agents.votes.map((vote) => {
              const meta = AGENT_META[vote.agent] || { emoji: "?", label: vote.agent, description: "" };
              const accPct = (vote.accuracy * 100).toFixed(1);
              return (
                <div key={vote.agent} className="flex items-center gap-3">
                  <span className="text-base w-6">{meta.emoji}</span>
                  <span className="text-xs text-text-secondary w-24 truncate">{meta.label}</span>
                  {/* Accuracy bar */}
                  <div className="flex-1 h-1.5 bg-zinc-800 rounded-full overflow-hidden">
                    <div
                      className="h-full rounded-full transition-all duration-500"
                      style={{
                        width: `${vote.accuracy * 100}%`,
                        background: vote.accuracy >= 0.6 ? "#00FF88" : vote.accuracy >= 0.5 ? "#FACC15" : "#FF3366",
                      }}
                    />
                  </div>
                  <span className={cn(
                    "text-xs font-mono w-10 text-right",
                    vote.accuracy >= 0.6 ? "text-accent-green" : vote.accuracy >= 0.5 ? "text-yellow-400" : "text-accent-red"
                  )}>{accPct}%</span>
                  <span className="text-xs font-mono w-10 text-right text-zinc-400">{vote.weight.toFixed(2)}x</span>
                  <span className={cn(
                    "text-xs w-4",
                    (vote.trend === "↑") ? "text-accent-green" : (vote.trend === "↓") ? "text-accent-red" : "text-zinc-600"
                  )}>{vote.trend ?? "→"}</span>
                  {vote.is_muted && (
                    <span className="text-[10px] text-accent-red border border-accent-red/30 px-1 rounded">MUTED</span>
                  )}
                </div>
              );
            })}
          </div>
        </div>
      )}

      {/* Manual controls */}
      <div className="card p-4">
        <div className="text-xs text-text-secondary mb-3">MANUAL CONTROLS</div>
        <div className="flex gap-3">
          <button
            onClick={handlePause}
            className="flex-1 py-2 px-4 rounded border border-yellow-400/30 text-yellow-400 text-sm hover:bg-yellow-400/10 transition-colors"
          >
            ⏸ PAUSE
          </button>
          <button
            onClick={handleResume}
            className="flex-1 py-2 px-4 rounded border border-accent-green/30 text-accent-green text-sm hover:bg-accent-green/10 transition-colors"
          >
            ▶ RESUME
          </button>
          <button
            onClick={handleEmergencyStop}
            className="flex-1 py-2 px-4 rounded border border-accent-red/30 text-accent-red text-sm hover:bg-accent-red/10 transition-colors font-bold"
          >
            ■ EMERGENCY STOP
          </button>
        </div>
      </div>
    </div>
  );
}
