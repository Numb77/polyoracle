"use client";

import { useState } from "react";
import { useBotContext } from "@/app/providers";
import { getVoteColor, getVoteDot, cn } from "@/lib/utils";
import { AGENT_META, type AgentVote } from "@/lib/types";

function AgentCard({ vote, onUnmute, onMute }: { vote: AgentVote; onUnmute: (agent: string) => void; onMute: (agent: string) => void }) {
  const meta = AGENT_META[vote.agent] || {
    emoji: "?",
    label: vote.agent,
    description: "",
  };

  const accuracyPct = (vote.accuracy * 100).toFixed(1);
  const convictionPct = (vote.conviction * 100).toFixed(0);

  return (
    <div
      className={cn(
        "card p-4 transition-all group",
        vote.is_muted && "opacity-40"
      )}
    >
      {/* Header */}
      <div className="flex items-start justify-between mb-3">
        <div className="flex items-center gap-2">
          <span className="text-2xl">{meta.emoji}</span>
          <div>
            <div className="font-semibold text-sm">{meta.label}</div>
            <div className="text-xs text-text-secondary">{meta.description}</div>
          </div>
        </div>
        {vote.is_muted ? (
          <button
            onClick={() => onUnmute(vote.agent)}
            title="Click to unmute this agent"
            className="text-xs text-accent-red border border-accent-red/30 px-1.5 py-0.5 rounded hover:bg-accent-red/10 hover:border-accent-red/60 transition-colors cursor-pointer"
          >
            MUTED — click to unmute
          </button>
        ) : (
          <button
            onClick={() => onMute(vote.agent)}
            title="Force-mute this agent"
            className="text-xs text-zinc-600 border border-zinc-700 px-1.5 py-0.5 rounded hover:text-accent-red hover:border-accent-red/40 transition-colors cursor-pointer opacity-0 group-hover:opacity-100"
          >
            mute
          </button>
        )}
      </div>

      {/* Vote */}
      <div className="flex items-center gap-3 mb-3">
        <div
          className={cn(
            "flex items-center gap-1.5 px-3 py-1.5 rounded text-sm font-bold",
            vote.vote === "UP"
              ? "bg-accent-green/10 border border-accent-green/30 text-accent-green"
              : vote.vote === "DOWN"
              ? "bg-accent-red/10 border border-accent-red/30 text-accent-red"
              : "bg-zinc-800 border border-zinc-700 text-zinc-400"
          )}
        >
          <div className={cn("w-2 h-2 rounded-full", getVoteDot(vote.vote as "UP" | "DOWN" | "ABSTAIN"))} />
          {vote.vote}
        </div>
        <div className="flex-1">
          <div className="text-xs text-text-secondary mb-1">
            CONVICTION {convictionPct}%
          </div>
          <div className="h-1 rounded-full bg-zinc-800">
            <div
              className="h-full rounded-full"
              style={{
                width: `${convictionPct}%`,
                background:
                  vote.vote === "UP"
                    ? "var(--accent-green)"
                    : vote.vote === "DOWN"
                    ? "var(--accent-red)"
                    : "#3f3f5a",
              }}
            />
          </div>
        </div>
      </div>

      {/* Accuracy + weight + trend */}
      <div className="flex gap-4 text-xs mb-2">
        <div>
          <div className="text-text-secondary mb-0.5">ACCURACY</div>
          <div
            className={
              vote.accuracy >= 0.6
                ? "text-accent-green"
                : vote.accuracy >= 0.5
                ? "text-yellow-400"
                : "text-accent-red"
            }
          >
            {accuracyPct}%{" "}
            {vote.trend && (
              <span className={
                vote.trend === "↑" ? "text-accent-green" :
                vote.trend === "↓" ? "text-accent-red" :
                "text-zinc-500"
              }>{vote.trend}</span>
            )}
          </div>
        </div>
        <div>
          <div className="text-text-secondary mb-0.5">WEIGHT</div>
          <div className="text-white">{vote.weight.toFixed(2)}x</div>
        </div>
        <div className="flex-1">
          <div className="text-text-secondary mb-0.5">REASONING</div>
          <div className="text-zinc-400 truncate">{vote.reasoning || "—"}</div>
        </div>
      </div>

      {/* Session accuracy pills */}
      {vote.session_accuracy && Object.keys(vote.session_accuracy).length > 0 && (
        <div className="flex gap-1 flex-wrap">
          {Object.entries(vote.session_accuracy).map(([session, acc]) => (
            <span
              key={session}
              className={cn(
                "text-[10px] px-1.5 py-0.5 rounded border",
                acc >= 0.6
                  ? "border-accent-green/30 text-accent-green"
                  : acc >= 0.5
                  ? "border-yellow-400/30 text-yellow-400"
                  : "border-accent-red/30 text-accent-red"
              )}
            >
              {session.replace("_", " ")} {(acc * 100).toFixed(0)}%
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

function ConsensusGauge({
  direction,
  strength,
  upWeight,
  downWeight,
}: {
  direction: string;
  strength: number;
  upWeight: number;
  downWeight: number;
}) {
  const total = upWeight + downWeight || 1;
  const upPct = (upWeight / total) * 100;
  const downPct = (downWeight / total) * 100;

  return (
    <div className="card p-4 mb-4">
      <div className="text-xs text-text-secondary mb-3">CONSENSUS GAUGE</div>
      <div className="flex items-center gap-4">
        <div className="text-xs text-accent-red w-8 text-right">DOWN</div>
        <div className="flex-1 h-4 bg-zinc-800 rounded-full overflow-hidden flex">
          <div
            className="h-full bg-accent-red transition-all duration-500"
            style={{ width: `${downPct}%` }}
          />
          <div
            className="h-full bg-accent-green transition-all duration-500"
            style={{ width: `${upPct}%` }}
          />
        </div>
        <div className="text-xs text-accent-green w-8">UP</div>
      </div>
      <div className="flex justify-between mt-2 text-xs">
        <span className="text-accent-red">{downPct.toFixed(0)}%</span>
        <span
          className={cn(
            "font-bold",
            direction === "UP"
              ? "text-accent-green"
              : direction === "DOWN"
              ? "text-accent-red"
              : "text-text-secondary"
          )}
        >
          {direction} ({(strength * 100).toFixed(0)}% strength)
        </span>
        <span className="text-accent-green">{upPct.toFixed(0)}%</span>
      </div>
    </div>
  );
}


export function AgentPanel() {
  const { state, send } = useBotContext();
  const assetSymbols = Object.keys(state.assets);
  const [selectedAsset, setSelectedAsset] = useState<string>("BTC");

  const activeAsset = assetSymbols.includes(selectedAsset)
    ? selectedAsset
    : assetSymbols[0] ?? "BTC";
  const agents = state.assets[activeAsset]?.agents ?? null;

  const handleUnmute = (agentName: string) => {
    send({ command: "unmute_agent", agent: agentName });
  };

  const handleMute = (agentName: string) => {
    send({ command: "mute_agent", agent: agentName });
  };

  return (
    <div className="space-y-4">
      {/* Asset selector */}
      {assetSymbols.length > 1 && (
        <div className="flex gap-2">
          {assetSymbols.map((sym) => (
            <button
              key={sym}
              onClick={() => setSelectedAsset(sym)}
              className={cn(
                "px-3 py-1 rounded text-xs font-bold font-mono border transition-colors",
                activeAsset === sym
                  ? "border-accent-green text-accent-green bg-accent-green/10"
                  : "border-zinc-700 text-text-secondary hover:text-white hover:border-zinc-500"
              )}
            >
              {sym}
            </button>
          ))}
        </div>
      )}

      {!agents ? (
        <div className="text-center text-text-secondary py-12">
          No agent data yet for {activeAsset}. Waiting for evaluation window...
        </div>
      ) : (
        <div>
          <ConsensusGauge
            direction={agents.direction}
            strength={agents.strength}
            upWeight={agents.up_weight}
            downWeight={agents.down_weight}
          />
          <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3">
            {agents.votes.map((vote) => (
              <AgentCard key={vote.agent} vote={vote} onUnmute={handleUnmute} onMute={handleMute} />
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
