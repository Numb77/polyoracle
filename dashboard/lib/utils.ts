import { type ClassValue, clsx } from "clsx";
import { twMerge } from "tailwind-merge";
import type { CircuitTier, LogLevel, VoteDirection, WindowPhase } from "./types";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

// ── Number formatters ─────────────────────────────────────────────────────────

export function formatPrice(price: number, decimals = 2): string {
  return new Intl.NumberFormat("en-US", {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  }).format(price);
}

export function formatPnl(pnl: number): string {
  const sign = pnl >= 0 ? "+" : "";
  return `${sign}$${Math.abs(pnl).toFixed(2)}`;
}

export function formatPct(value: number, decimals = 1): string {
  return `${(value * 100).toFixed(decimals)}%`;
}

export function formatDeltaPct(delta: number): string {
  const sign = delta >= 0 ? "+" : "";
  return `${sign}${delta.toFixed(3)}%`;
}

export function formatBtcPrice(price: number): string {
  return `$${price.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

// ── Time helpers ──────────────────────────────────────────────────────────────

export function formatCountdown(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  const m = Math.floor(s / 60);
  const rem = s % 60;
  return `${m}:${rem.toString().padStart(2, "0")}`;
}

export function formatTimestamp(ts: number): string {
  return new Date(ts * 1000).toLocaleTimeString("en-US", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });
}

// ── Color helpers ─────────────────────────────────────────────────────────────

export function getPnlColor(pnl: number): string {
  if (pnl > 0) return "text-accent-green";
  if (pnl < 0) return "text-accent-red";
  return "text-text-secondary";
}

export function getDeltaColor(delta: number): string {
  if (delta > 0.02) return "text-accent-green";
  if (delta < -0.02) return "text-accent-red";
  return "text-text-secondary";
}

export function getConfidenceColor(score: number): string {
  if (score >= 75) return "text-accent-green";
  if (score >= 55) return "text-yellow-400";
  return "text-accent-red";
}

export function getConfidenceBg(score: number): string {
  if (score >= 75) return "bg-accent-green/10 border-accent-green/30";
  if (score >= 55) return "bg-yellow-400/10 border-yellow-400/30";
  return "bg-accent-red/10 border-accent-red/30";
}

export function getCircuitColor(tier: CircuitTier): string {
  switch (tier) {
    case "GREEN": return "text-accent-green";
    case "YELLOW": return "text-yellow-400";
    case "ORANGE": return "text-orange-400";
    case "RED": return "text-accent-red";
  }
}

export function getCircuitBg(tier: CircuitTier): string {
  switch (tier) {
    case "GREEN": return "bg-accent-green/10 border-accent-green/30";
    case "YELLOW": return "bg-yellow-400/10 border-yellow-400/30";
    case "ORANGE": return "bg-orange-400/10 border-orange-400/30";
    case "RED": return "bg-accent-red/10 border-accent-red/30";
  }
}

export function getLogColor(level: LogLevel): string {
  switch (level) {
    case "DEBUG": return "text-zinc-500";
    case "INFO": return "text-zinc-300";
    case "TRADE": return "text-accent-green font-bold";
    case "CLAIM": return "text-purple-400 font-bold";
    case "WARNING": return "text-yellow-400";
    case "ERROR": return "text-accent-red";
    case "CRITICAL": return "text-accent-red font-bold animate-blink";
  }
}

export function getVoteColor(vote: VoteDirection): string {
  switch (vote) {
    case "UP": return "text-accent-green";
    case "DOWN": return "text-accent-red";
    case "ABSTAIN": return "text-zinc-500";
  }
}

export function getVoteDot(vote: VoteDirection): string {
  switch (vote) {
    case "UP": return "bg-accent-green";
    case "DOWN": return "bg-accent-red";
    case "ABSTAIN": return "bg-zinc-600";
  }
}

export function getPhaseColor(phase: WindowPhase): string {
  switch (phase) {
    case "monitoring": return "text-zinc-400";
    case "evaluating": return "text-yellow-400";
    case "trading": return "text-orange-400";
    case "deadline": return "text-accent-red";
    case "resolved": return "text-zinc-500";
  }
}

export function getPhaseLabel(phase: WindowPhase): string {
  switch (phase) {
    case "monitoring": return "MONITORING";
    case "evaluating": return "EVALUATING";
    case "trading": return "DECISION";
    case "deadline": return "DEADLINE";
    case "resolved": return "RESOLVED";
  }
}
