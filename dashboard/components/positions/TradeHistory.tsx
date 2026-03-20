"use client";

import { useState, useEffect } from "react";
import { useBotContext } from "@/app/providers";
import { formatTimestamp, cn } from "@/lib/utils";
import type { ActivePosition, TradeResolved } from "@/lib/types";

// ── Live elapsed timer ────────────────────────────────────────────────────────

function useElapsed(openedAt: number) {
  const [elapsed, setElapsed] = useState(
    Math.floor((Date.now() - openedAt) / 1000)
  );
  useEffect(() => {
    const id = setInterval(
      () => setElapsed(Math.floor((Date.now() - openedAt) / 1000)),
      1000
    );
    return () => clearInterval(id);
  }, [openedAt]);
  return elapsed;
}

// ── Active position card ──────────────────────────────────────────────────────

function ActivePositionCard({ pos }: { pos: ActivePosition }) {
  const elapsed = useElapsed(pos.opened_at);
  const mins = Math.floor(elapsed / 60);
  const secs = elapsed % 60;

  return (
    <div
      className={cn(
        "slide-in flex items-center gap-3 p-3 rounded-lg border transition-all",
        pos.direction === "UP"
          ? "border-accent-green/30 bg-accent-green/5"
          : "border-accent-red/30 bg-accent-red/5"
      )}
    >
      {/* Pulse dot */}
      <div
        className={cn(
          "w-2.5 h-2.5 rounded-full shrink-0 pulse-ring",
          pos.direction === "UP" ? "bg-accent-green" : "bg-accent-red"
        )}
      />

      {/* Direction badge */}
      <div
        className={cn(
          "text-xs font-bold px-2 py-0.5 rounded border shrink-0",
          pos.direction === "UP"
            ? "text-accent-green border-accent-green/40 bg-accent-green/10"
            : "text-accent-red border-accent-red/40 bg-accent-red/10"
        )}
      >
        {pos.direction} {pos.direction === "UP" ? "YES" : "NO"}
      </div>

      {/* Details */}
      <div className="flex-1 min-w-0">
        <div className="text-xs font-mono text-white truncate">
          {pos.market.split("-").slice(-2).join("-")}
        </div>
        <div className="text-[10px] text-text-secondary mt-0.5">
          @ {pos.price.toFixed(3)} · ${pos.size_usd.toFixed(2)} · conf {pos.confidence.toFixed(0)}
        </div>
      </div>

      {/* Elapsed + order ID */}
      <div className="text-right shrink-0">
        <div className="text-xs font-mono text-yellow-400">
          {mins > 0 ? `${mins}m ` : ""}{secs}s
        </div>
        <div className="text-[10px] text-zinc-600 font-mono">
          {pos.order_id.slice(0, 10)}...
        </div>
      </div>
    </div>
  );
}

// ── Resolved trade row ────────────────────────────────────────────────────────

function TradeRow({ trade }: { trade: TradeResolved }) {
  return (
    <tr className="border-b hover:bg-white/[0.02] transition-colors"
        style={{ borderColor: "var(--border-color)" }}>
      <td className="px-4 py-2 text-zinc-400 max-w-[160px] truncate font-mono text-xs">
        {trade.market.split("-").slice(-2).join("-")}
      </td>
      <td className={cn(
        "px-4 py-2 font-bold text-xs",
        trade.direction === "UP" ? "text-accent-green" : "text-accent-red"
      )}>
        {trade.direction}
      </td>
      <td className={cn(
        "px-4 py-2 text-xs",
        trade.actual_direction === "UP" ? "text-accent-green" : "text-accent-red"
      )}>
        {trade.actual_direction}
      </td>
      <td className={cn(
        "px-4 py-2 text-right font-bold text-xs",
        trade.pnl >= 0 ? "text-accent-green" : "text-accent-red"
      )}>
        {trade.pnl >= 0 ? "+" : ""}${Math.abs(trade.pnl).toFixed(2)}
      </td>
      <td className="px-4 py-2 text-right text-xs">
        {trade.won ? (
          <span className="text-accent-green font-bold">WIN ✓</span>
        ) : (
          <span className="text-accent-red">LOSS ✗</span>
        )}
      </td>
    </tr>
  );
}

// ── Main component ────────────────────────────────────────────────────────────

export function TradeHistory() {
  const { state } = useBotContext();
  const { recentTrades } = state;
  const activePositions = [...state.activePositions, ...state.ethActivePositions];

  const wins = recentTrades.filter((t) => t.won).length;
  const losses = recentTrades.filter((t) => !t.won).length;
  const totalPnl = recentTrades.reduce((acc, t) => acc + t.pnl, 0);

  return (
    <div className="space-y-4">
      {/* Active positions */}
      {activePositions.length > 0 && (
        <div className="card p-4">
          <div className="flex items-center justify-between mb-3">
            <div className="text-xs text-text-secondary">ACTIVE POSITIONS</div>
            <div className="flex items-center gap-1.5">
              <div className="w-1.5 h-1.5 rounded-full bg-yellow-400 animate-pulse" />
              <span className="text-xs text-yellow-400">{activePositions.length} open</span>
            </div>
          </div>
          <div className="space-y-2">
            {activePositions.map((pos) => (
              <ActivePositionCard key={pos.order_id} pos={pos} />
            ))}
          </div>
        </div>
      )}

      {/* Session summary bar */}
      {recentTrades.length > 0 && (
        <div className="card p-3 flex items-center gap-4 text-xs font-mono">
          <span className="text-text-secondary">SESSION</span>
          <span className="text-accent-green">{wins}W</span>
          <span className="text-accent-red">{losses}L</span>
          <span className="text-text-secondary">·</span>
          <span className={totalPnl >= 0 ? "text-accent-green" : "text-accent-red"}>
            {totalPnl >= 0 ? "+" : ""}${totalPnl.toFixed(2)}
          </span>
          {recentTrades.length > 0 && (
            <>
              <span className="text-text-secondary">·</span>
              <span className="text-white">
                {((wins / recentTrades.length) * 100).toFixed(0)}% WR
              </span>
            </>
          )}
        </div>
      )}

      {/* Trade history table */}
      <div className="card overflow-hidden">
        <div
          className="px-4 py-2 border-b text-xs text-text-secondary"
          style={{ borderColor: "var(--border-color)" }}
        >
          TRADE HISTORY ({recentTrades.length})
        </div>
        {recentTrades.length === 0 ? (
          <div className="p-8 text-center text-text-secondary text-xs">
            No resolved trades yet
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-xs font-mono">
              <thead>
                <tr
                  className="border-b text-text-secondary"
                  style={{ borderColor: "var(--border-color)" }}
                >
                  <th className="text-left px-4 py-2">MARKET</th>
                  <th className="text-left px-4 py-2">DIR</th>
                  <th className="text-left px-4 py-2">ACTUAL</th>
                  <th className="text-right px-4 py-2">P&L</th>
                  <th className="text-right px-4 py-2">RESULT</th>
                </tr>
              </thead>
              <tbody>
                {recentTrades.map((trade, i) => (
                  <TradeRow key={`${trade.order_id}-${i}`} trade={trade} />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}
