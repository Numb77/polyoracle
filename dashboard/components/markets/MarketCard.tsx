"use client";

import { useRef, useEffect, useState } from "react";
import { useBotContext } from "@/app/providers";
import {
  formatCountdown,
  formatDeltaPct,
  getConfidenceColor,
  getDeltaColor,
  getPhaseColor,
  getPhaseLabel,
  getVoteDot,
  cn,
} from "@/lib/utils";
import { AGENT_META } from "@/lib/types";
import type { BtcTick, WindowState, Consensus, ConfidenceBreakdown } from "@/lib/types";

// ── BTC/ETH price sparkline ───────────────────────────────────────────────────

function Sparkline({ ticks }: { ticks: BtcTick[] }) {
  if (ticks.length < 2) return <div className="h-10 w-full" />;

  const W = 260;
  const H = 40;
  const prices = ticks.map((t) => t.price);
  const min = Math.min(...prices);
  const max = Math.max(...prices);
  const range = max - min || 1;

  const pts = prices
    .map((p, i) => {
      const x = (i / (prices.length - 1)) * W;
      const y = H - ((p - min) / range) * (H - 4) - 2;
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");

  const isUp = prices[prices.length - 1] >= prices[0];
  const color = isUp ? "#00FF88" : "#FF3366";
  const gradId = `spark-${isUp ? "g" : "r"}`;

  return (
    <svg
      viewBox={`0 0 ${W} ${H}`}
      width="100%"
      height={H}
      preserveAspectRatio="none"
      className="opacity-70"
    >
      <defs>
        <linearGradient id={gradId} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor={color} stopOpacity="0.3" />
          <stop offset="100%" stopColor={color} stopOpacity="0" />
        </linearGradient>
      </defs>
      {/* Fill area */}
      <polygon
        points={`0,${H} ${pts} ${W},${H}`}
        fill={`url(#${gradId})`}
      />
      {/* Line */}
      <polyline
        points={pts}
        fill="none"
        stroke={color}
        strokeWidth="1.5"
        strokeLinejoin="round"
      />
      {/* Latest dot */}
      {(() => {
        const last = pts.split(" ").pop()!.split(",");
        return (
          <circle
            cx={last[0]}
            cy={last[1]}
            r="2.5"
            fill={color}
            className="animate-pulse"
          />
        );
      })()}
    </svg>
  );
}

// ── Confidence arc gauge ──────────────────────────────────────────────────────

function ConfidenceGauge({ score, shouldTrade }: { score: number; shouldTrade: boolean }) {
  const prevScore = useRef(score);
  const [glowing, setGlowing] = useState(false);

  useEffect(() => {
    if (score !== prevScore.current) {
      setGlowing(true);
      const t = setTimeout(() => setGlowing(false), 600);
      prevScore.current = score;
      return () => clearTimeout(t);
    }
  }, [score]);

  const R = 28;
  const circ = 2 * Math.PI * R;
  const dash = (score / 100) * circ;
  const color =
    score >= 65 ? "#00FF88" : score >= 48 ? "#FACC15" : "#FF3366";

  return (
    <div className={cn("flex items-center gap-3", glowing && "conf-glow")}>
      <svg width="72" height="72" viewBox="0 0 72 72">
        {/* Track */}
        <circle cx="36" cy="36" r={R} fill="none" stroke="#1e1e2e" strokeWidth="6" />
        {/* Arc */}
        <circle
          cx="36"
          cy="36"
          r={R}
          fill="none"
          stroke={color}
          strokeWidth="6"
          strokeDasharray={`${dash} ${circ - dash}`}
          strokeLinecap="round"
          transform="rotate(-90 36 36)"
          style={{ transition: "stroke-dasharray 0.5s ease, stroke 0.3s ease" }}
        />
        {/* Score label */}
        <text
          x="36"
          y="40"
          textAnchor="middle"
          fill={color}
          fontSize="14"
          fontWeight="bold"
          fontFamily="IBM Plex Mono, monospace"
        >
          {score.toFixed(0)}
        </text>
      </svg>
      <div>
        <div className="text-xs text-text-secondary mb-0.5">AI CONFIDENCE</div>
        {shouldTrade ? (
          <div className="text-xs text-accent-green font-bold animate-pulse">
            ▶ TRADE SIGNAL
          </div>
        ) : (
          <div className="text-xs text-zinc-500">WATCHING</div>
        )}
      </div>
    </div>
  );
}

// ── Main MarketCard ───────────────────────────────────────────────────────────

interface MarketCardProps {
  asset?: "BTC" | "ETH";
}

export function MarketCard({ asset = "BTC" }: MarketCardProps) {
  const { state } = useBotContext();

  const win: WindowState | null = asset === "ETH" ? state.ethWindow : state.window;
  const ticks: BtcTick[] = asset === "ETH" ? state.ethTicks : state.ticks;
  const confidence: ConfidenceBreakdown | null = asset === "ETH" ? state.ethConfidence : state.confidence;
  const agents: Consensus | null = asset === "ETH" ? state.ethAgents : state.agents;

  const assetLabel = asset === "ETH" ? "Ethereum" : "Bitcoin";
  const assetColor = asset === "ETH" ? "text-indigo-400" : "text-accent-green";

  if (!win) {
    return (
      <div className="card p-6 flex flex-col items-center justify-center text-text-secondary gap-2">
        <div className={`text-xs font-bold font-mono ${assetColor}`}>{asset}</div>
        <span className="animate-pulse text-xs">Waiting for market data...</span>
      </div>
    );
  }

  const confScore = confidence?.total ?? 0;
  const isLive =
    win.phase === "evaluating" || win.phase === "trading" || win.phase === "deadline";
  const remaining = win.remaining_sec;

  return (
    <div
      className={cn(
        "card relative overflow-hidden transition-all duration-300",
        isLive && "border-yellow-400/40"
      )}
    >
      {/* Phase progress bar at top */}
      <div className="h-1 w-full bg-zinc-800">
        <div
          className="h-full transition-all duration-1000"
          style={{
            width: `${((300 - remaining) / 300) * 100}%`,
            background:
              remaining < 10 ? "var(--accent-red)" :
              remaining < 30 ? "#FACC15" : "var(--accent-green)",
          }}
        />
      </div>

      <div className="p-4">
        {/* Header row */}
        <div className="flex items-start justify-between mb-2">
          <div>
            <div className="flex items-center gap-2">
              <span className={`text-xs font-bold font-mono ${assetColor}`}>{asset}</span>
              <span className="text-xs text-text-secondary">{win.window_slug}</span>
            </div>
            <div className="text-sm font-semibold">{assetLabel} Up/Down — 5 Min</div>
          </div>
          {isLive && (
            <div className="flex items-center gap-1.5">
              <div className="w-1.5 h-1.5 rounded-full bg-accent-green active-dot" />
              <span className="text-xs text-accent-green font-bold">LIVE</span>
            </div>
          )}
        </div>

        {/* Sparkline */}
        <div className="mb-3 -mx-1">
          <Sparkline ticks={ticks} />
        </div>

        {/* Countdown + phase */}
        <div className="flex items-baseline gap-3 mb-4">
          <span
            className={cn(
              "text-3xl font-bold font-mono tabular-nums",
              remaining < 10
                ? "text-accent-red animate-pulse"
                : remaining < 30
                ? "text-yellow-400"
                : "text-white"
            )}
          >
            {formatCountdown(remaining)}
          </span>
          <span className={`text-xs ${getPhaseColor(win.phase)}`}>
            {getPhaseLabel(win.phase)}
          </span>
        </div>

        {/* Open / Delta / Now */}
        <div className="flex items-center gap-4 mb-4">
          <div>
            <div className="text-xs text-text-secondary mb-0.5">OPEN</div>
            <div className="font-mono text-sm">
              ${win.open_price.toLocaleString("en-US", { minimumFractionDigits: 2 })}
            </div>
          </div>
          <div className="flex-1 text-center">
            <div className={cn("text-xl font-bold font-mono", getDeltaColor(win.delta_pct))}>
              {formatDeltaPct(win.delta_pct)}
            </div>
            <div className="text-xs text-text-secondary">Δ from open</div>
          </div>
          <div>
            <div className="text-xs text-text-secondary mb-0.5">NOW</div>
            <div className="font-mono text-sm">
              ${win.current_price.toLocaleString("en-US", { minimumFractionDigits: 2 })}
            </div>
          </div>
        </div>

        {/* Confidence gauge */}
        {confidence && (
          <div className="mb-4 p-3 rounded-lg bg-zinc-900/60 border border-zinc-800">
            <ConfidenceGauge score={confScore} shouldTrade={confidence.should_trade} />
          </div>
        )}

        {/* Agent consensus dots */}
        {agents && (
          <div>
            <div className="text-xs text-text-secondary mb-2">AGENT CONSENSUS</div>
            <div className="flex items-center gap-3">
              <div className="flex gap-2">
                {agents.votes.map((vote) => {
                  const meta = AGENT_META[vote.agent] || { emoji: "?", label: vote.agent };
                  return (
                    <div
                      key={vote.agent}
                      className="flex flex-col items-center gap-1"
                      title={`${meta.label}: ${vote.vote}`}
                    >
                      <div
                        className={cn(
                          "w-2.5 h-2.5 rounded-full",
                          getVoteDot(vote.vote as "UP" | "DOWN" | "ABSTAIN")
                        )}
                      />
                      <span className="text-xs">{meta.emoji}</span>
                    </div>
                  );
                })}
              </div>
              <div className="ml-auto text-xs">
                <span
                  className={
                    agents.direction === "UP"
                      ? "text-accent-green font-bold"
                      : agents.direction === "DOWN"
                      ? "text-accent-red font-bold"
                      : "text-text-secondary"
                  }
                >
                  {agents.direction}
                </span>
                <span className="text-text-secondary ml-1">
                  {(agents.agreement_ratio * 100).toFixed(0)}%
                </span>
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
