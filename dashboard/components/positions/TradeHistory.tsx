"use client";

import { useState, useEffect } from "react";
import { useBotContext } from "@/app/providers";
import { formatTimestamp, cn } from "@/lib/utils";
import { AGENT_META } from "@/lib/types";
import type { ActivePosition, TradeResolved, AgentVote } from "@/lib/types";

// ── Live countdown to window close ────────────────────────────────────────────

function useWindowCountdown(windowTs: number) {
  const windowCloseTs = (windowTs + 300) * 1000; // ms
  const calc = () => Math.max(0, Math.floor((windowCloseTs - Date.now()) / 1000));
  const [remaining, setRemaining] = useState(calc);
  useEffect(() => {
    const id = setInterval(() => setRemaining(calc), 1000);
    return () => clearInterval(id);
  }, [windowTs]);
  return remaining;
}

// ── Market label helper ────────────────────────────────────────────────────────

function AssetBadge({ asset, className }: { asset: "BTC" | "ETH"; className?: string }) {
  return (
    <span
      className={cn(
        "text-[10px] font-bold px-1.5 py-0.5 rounded font-mono",
        asset === "BTC"
          ? "bg-accent-green/10 text-accent-green border border-accent-green/30"
          : "bg-indigo-500/10 text-indigo-400 border border-indigo-400/30",
        className
      )}
    >
      {asset}
    </span>
  );
}

function marketLabel(market: string, asset?: "BTC" | "ETH"): string {
  // "btc-updown-5m-1774115100" → "BTC  5m · 17:45"
  // Prefer asset from payload; fall back to parsing the slug
  const a = asset ?? (market.startsWith("eth") ? "ETH" : "BTC");
  const ts = parseInt(market.split("-").pop() ?? "0", 10);
  if (!ts) return a;
  const d = new Date(ts * 1000);
  const time = d.toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", hour12: false });
  return `${a} · ${time}`;
}

// ── Trade detail modal ─────────────────────────────────────────────────────────

function AgentRow({ vote }: { vote: AgentVote }) {
  const meta = AGENT_META[vote.agent] || { emoji: "?", label: vote.agent };
  const dirColor =
    vote.vote === "UP" ? "text-accent-green" :
    vote.vote === "DOWN" ? "text-accent-red" : "text-zinc-500";
  return (
    <div className="flex items-center gap-2 py-1 border-b last:border-0"
         style={{ borderColor: "var(--border-color)" }}>
      <span className="text-sm w-5">{meta.emoji}</span>
      <span className="text-xs text-zinc-300 flex-1">{meta.label}</span>
      <span className={cn("text-xs font-bold w-14 text-center", dirColor)}>{vote.vote}</span>
      <span className="text-xs text-zinc-400 w-14 text-right">
        {(vote.conviction * 100).toFixed(0)}% conv
      </span>
      <span className="text-xs text-zinc-500 w-16 text-right">
        {(vote.accuracy * 100).toFixed(0)}% acc
      </span>
    </div>
  );
}

function TradeDetailModal({ trade, onClose }: { trade: TradeResolved; onClose: () => void }) {
  const asset = trade.asset ?? (trade.market.startsWith("eth") ? "ETH" : "BTC");
  const ts = new Date(trade.window_ts * 1000).toLocaleString();
  // Time into window: how far into the 5-min span the trade was placed
  const holdMs = trade.opened_at ? trade.opened_at - (trade.window_ts * 1000) : null;
  const holdSec = holdMs != null && holdMs >= 0 ? Math.floor(holdMs / 1000) : null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/70"
      onClick={(e) => e.target === e.currentTarget && onClose()}
    >
      <div className="w-full max-w-md mx-4 rounded-xl border bg-zinc-950 shadow-2xl overflow-hidden"
           style={{ borderColor: "var(--border-color)" }}>
        {/* Header */}
        <div className={cn(
          "px-4 py-3 flex items-center justify-between border-b",
          trade.won ? "bg-accent-green/10" : "bg-accent-red/10"
        )} style={{ borderColor: "var(--border-color)" }}>
          <div className="flex items-center gap-2">
            <AssetBadge asset={asset} />
            <span className={cn("font-bold text-sm", trade.won ? "text-accent-green" : "text-accent-red")}>
              {trade.won ? "WIN ✓" : "LOSS ✗"}
            </span>
            <span className={cn("font-bold text-sm", trade.pnl >= 0 ? "text-accent-green" : "text-accent-red")}>
              {trade.pnl >= 0 ? "+" : ""}${Math.abs(trade.pnl).toFixed(2)}
            </span>
          </div>
          <button onClick={onClose} className="text-zinc-500 hover:text-white text-lg leading-none">×</button>
        </div>

        <div className="p-4 space-y-4 max-h-[70vh] overflow-y-auto">
          {/* Core trade info */}
          <div className="grid grid-cols-2 gap-2 text-xs font-mono">
            <div className="bg-zinc-900 rounded p-2">
              <div className="text-zinc-500 mb-0.5">MARKET</div>
              <div className="text-white truncate">{marketLabel(trade.market, asset)}</div>
            </div>
            <div className="bg-zinc-900 rounded p-2">
              <div className="text-zinc-500 mb-0.5">WINDOW OPEN</div>
              <div className="text-white">{ts}</div>
            </div>
            <div className="bg-zinc-900 rounded p-2">
              <div className="text-zinc-500 mb-0.5">DIRECTION</div>
              <div className={cn("font-bold", trade.direction === "UP" ? "text-accent-green" : "text-accent-red")}>
                {trade.direction} / actual: {trade.actual_direction}
              </div>
            </div>
            <div className="bg-zinc-900 rounded p-2">
              <div className="text-zinc-500 mb-0.5">ORDER TYPE</div>
              <div className="text-white">{trade.order_type ?? "–"}</div>
            </div>
            {trade.price != null && (
              <div className="bg-zinc-900 rounded p-2">
                <div className="text-zinc-500 mb-0.5">ENTRY PRICE</div>
                <div className="text-white">{trade.price.toFixed(3)}</div>
              </div>
            )}
            {trade.size_usd != null && (
              <div className="bg-zinc-900 rounded p-2">
                <div className="text-zinc-500 mb-0.5">SIZE</div>
                <div className="text-white">${trade.size_usd.toFixed(2)}</div>
              </div>
            )}
            <div className="bg-zinc-900 rounded p-2">
              <div className="text-zinc-500 mb-0.5">CONFIDENCE</div>
              <div className="text-white">{trade.confidence?.toFixed(0) ?? "–"}</div>
            </div>
            {trade.window_delta_pct != null && (
              <div className="bg-zinc-900 rounded p-2">
                <div className="text-zinc-500 mb-0.5">ΔBTC AT ENTRY</div>
                <div className={cn(
                  "font-bold",
                  trade.window_delta_pct >= 0 ? "text-accent-green" : "text-accent-red"
                )}>
                  {trade.window_delta_pct >= 0 ? "+" : ""}{trade.window_delta_pct.toFixed(3)}%
                </div>
              </div>
            )}
            {holdSec != null && (
              <div className="bg-zinc-900 rounded p-2">
                <div className="text-zinc-500 mb-0.5">TIME INTO WINDOW</div>
                <div className="text-white">
                  {holdSec >= 60 ? `${Math.floor(holdSec/60)}m ` : ""}{holdSec % 60}s into window
                </div>
              </div>
            )}
          </div>

          {/* Confidence breakdown */}
          {trade.confidence_breakdown && (
            <div>
              <div className="text-xs text-zinc-500 mb-2 font-mono">CONFIDENCE BREAKDOWN</div>
              <div className="grid grid-cols-2 gap-1 text-xs font-mono">
                {Object.entries({
                  Signal: trade.confidence_breakdown.signal_contribution,
                  Agents: trade.confidence_breakdown.agent_contribution,
                  Delta: trade.confidence_breakdown.delta_contribution,
                  Regime: trade.confidence_breakdown.regime_contribution,
                }).map(([k, v]) => (
                  <div key={k} className="flex items-center justify-between bg-zinc-900 rounded px-2 py-1">
                    <span className="text-zinc-500">{k}</span>
                    <span className={v > 0 ? "text-accent-green" : v < 0 ? "text-accent-red" : "text-zinc-400"}>
                      {v > 0 ? "+" : ""}{v.toFixed(1)}
                    </span>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Agent votes */}
          {trade.agent_votes && trade.agent_votes.length > 0 && (
            <div>
              <div className="text-xs text-zinc-500 mb-2 font-mono">AGENT VOTES</div>
              <div className="rounded-lg overflow-hidden border" style={{ borderColor: "var(--border-color)" }}>
                {trade.agent_votes.map((v) => <AgentRow key={v.agent} vote={v} />)}
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

// ── Active position card ──────────────────────────────────────────────────────

function ActivePositionCard({ pos }: { pos: ActivePosition }) {
  const remaining = useWindowCountdown(pos.window_ts);
  const mins = Math.floor(remaining / 60);
  const secs = remaining % 60;
  const asset = pos.asset ?? (pos.market.startsWith("eth") ? "ETH" : "BTC");
  // Window has closed when countdown reaches 0
  const isStale = remaining === 0;

  return (
    <div
      className={cn(
        "slide-in flex items-center gap-3 p-3 rounded-lg border transition-all",
        isStale
          ? "border-zinc-600/50 bg-zinc-800/40 opacity-60"
          : pos.direction === "UP"
          ? "border-accent-green/30 bg-accent-green/5"
          : "border-accent-red/30 bg-accent-red/5"
      )}
    >
      {/* Pulse dot */}
      <div className={cn(
        "w-2.5 h-2.5 rounded-full shrink-0 pulse-ring",
        pos.direction === "UP" ? "bg-accent-green" : "bg-accent-red"
      )} />

      {/* Asset + Direction badge */}
      <div className="flex items-center gap-1.5 shrink-0">
        <AssetBadge asset={asset} />
        <div className={cn(
          "text-xs font-bold px-2 py-0.5 rounded border",
          pos.direction === "UP"
            ? "text-accent-green border-accent-green/40 bg-accent-green/10"
            : "text-accent-red border-accent-red/40 bg-accent-red/10"
        )}>
          {pos.direction} {pos.direction === "UP" ? "YES" : "NO"}
        </div>
      </div>

      {/* Details */}
      <div className="flex-1 min-w-0">
        <div className="text-xs font-mono text-white">
          {asset} 5min {pos.order_type ? `[${pos.order_type}]` : ""}
        </div>
        <div className="text-[10px] text-text-secondary mt-0.5">
          @ {pos.price.toFixed(3)} · ${pos.size_usd.toFixed(2)} · conf {pos.confidence.toFixed(0)}
        </div>
      </div>

      {/* Countdown to window close */}
      <div className="text-right shrink-0">
        {isStale ? (
          <div className="text-xs font-mono text-zinc-500">CLOSED</div>
        ) : (
          <div className={cn(
            "text-xs font-mono tabular-nums font-bold",
            remaining < 10 ? "text-accent-red animate-pulse" :
            remaining < 30 ? "text-yellow-400" : "text-white"
          )}>
            {mins > 0 ? `${mins}:${String(secs).padStart(2, "0")}` : `${secs}s`}
          </div>
        )}
        <div className="text-[10px] text-zinc-600 font-mono mt-0.5">
          {isStale ? pos.order_id.slice(0, 10) + "..." : "left"}
        </div>
      </div>
    </div>
  );
}

// ── Resolved trade row ────────────────────────────────────────────────────────

function TradeRow({ trade, onClick }: { trade: TradeResolved; onClick: () => void }) {
  const asset = trade.asset ?? (trade.market.startsWith("eth") ? "ETH" : "BTC");
  return (
    <tr
      className="border-b hover:bg-white/[0.04] transition-colors cursor-pointer"
      style={{ borderColor: "var(--border-color)" }}
      onClick={onClick}
      title="Click for trade details"
    >
      <td className="px-4 py-2 max-w-[140px]">
        <div className="flex items-center gap-1.5">
          <AssetBadge asset={asset} />
          <span className="text-zinc-400 font-mono text-xs truncate">
            {new Date(trade.window_ts * 1000).toLocaleTimeString("en-US", {
              hour: "2-digit", minute: "2-digit", hour12: false
            })}
          </span>
        </div>
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
  const { state, send } = useBotContext();
  const { recentTrades } = state;
  const activePositions = [...state.activePositions, ...state.ethActivePositions];

  const { lastClaimsRecovery } = state;

  const [selectedTrade, setSelectedTrade] = useState<TradeResolved | null>(null);
  const [collecting, setCollecting] = useState(false);

  // Reset "collecting" state when the bot sends back a recovery result
  useEffect(() => {
    if (lastClaimsRecovery !== null) {
      setCollecting(false);
    }
  }, [lastClaimsRecovery]);

  function handleCollectClaims() {
    setCollecting(true);
    send({ command: "collect_claims" });
  }

  function claimButtonLabel(): string {
    if (collecting) return "COLLECTING…";
    if (lastClaimsRecovery) {
      const { recovered_count, recovered_usd } = lastClaimsRecovery;
      if (recovered_count > 0) return `✓ +$${recovered_usd.toFixed(2)}`;
      return "✓ ALL CLAIMED";
    }
    return "COLLECT CLAIMS";
  }

  const wins = recentTrades.filter((t) => t.won).length;
  const losses = recentTrades.filter((t) => !t.won).length;
  const totalPnl = recentTrades.reduce((acc, t) => acc + t.pnl, 0);

  return (
    <div className="space-y-4">
      {selectedTrade && (
        <TradeDetailModal trade={selectedTrade} onClose={() => setSelectedTrade(null)} />
      )}

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

      {/* Session summary + Collect Claims */}
      <div className="card p-3 flex items-center gap-4 text-xs font-mono">
        {recentTrades.length > 0 ? (
          <>
            <span className="text-text-secondary">SESSION</span>
            <span className="text-accent-green">{wins}W</span>
            <span className="text-accent-red">{losses}L</span>
            <span className="text-text-secondary">·</span>
            <span className={totalPnl >= 0 ? "text-accent-green" : "text-accent-red"}>
              {totalPnl >= 0 ? "+" : ""}${totalPnl.toFixed(2)}
            </span>
            <span className="text-text-secondary">·</span>
            <span className="text-white">
              {((wins / recentTrades.length) * 100).toFixed(0)}% WR
            </span>
          </>
        ) : (
          <span className="text-text-secondary">SESSION</span>
        )}
        <div className="flex-1" />
        <button
          onClick={handleCollectClaims}
          disabled={collecting}
          className={cn(
            "px-3 py-1 rounded text-xs font-bold border transition-all",
            collecting
              ? "text-purple-300 border-purple-400/30 bg-purple-400/5 cursor-wait"
              : lastClaimsRecovery && lastClaimsRecovery.recovered_count > 0
              ? "text-accent-green border-accent-green/40 bg-accent-green/10 hover:bg-accent-green/20"
              : "text-purple-400 border-purple-400/40 bg-purple-400/10 hover:bg-purple-400/20"
          )}
        >
          {claimButtonLabel()}
        </button>
      </div>

      {/* Trade history table */}
      <div className="card overflow-hidden">
        <div
          className="px-4 py-2 border-b text-xs text-text-secondary flex items-center justify-between"
          style={{ borderColor: "var(--border-color)" }}
        >
          <span>TRADE HISTORY ({recentTrades.length})</span>
          <span className="text-zinc-600">click row for detail</span>
        </div>
        {recentTrades.length === 0 ? (
          <div className="p-8 text-center text-text-secondary text-xs">
            No resolved trades yet
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-xs font-mono">
              <thead>
                <tr className="border-b text-text-secondary"
                    style={{ borderColor: "var(--border-color)" }}>
                  <th className="text-left px-4 py-2">MARKET</th>
                  <th className="text-left px-4 py-2">DIR</th>
                  <th className="text-left px-4 py-2">ACTUAL</th>
                  <th className="text-right px-4 py-2">P&L</th>
                  <th className="text-right px-4 py-2">RESULT</th>
                </tr>
              </thead>
              <tbody>
                {recentTrades.map((trade, i) => (
                  <TradeRow
                    key={`${trade.order_id}-${i}`}
                    trade={trade}
                    onClick={() => setSelectedTrade(trade)}
                  />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}
