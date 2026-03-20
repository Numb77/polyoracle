"use client";

import { useBotContext } from "@/app/providers";
import { formatPct, formatPnl, cn } from "@/lib/utils";

function StatCard({
  label,
  value,
  sub,
  color,
}: {
  label: string;
  value: string;
  sub?: string;
  color?: string;
}) {
  return (
    <div className="card p-4">
      <div className="text-xs text-text-secondary mb-1">{label}</div>
      <div className={cn("text-xl font-bold font-mono", color || "text-white")}>
        {value}
      </div>
      {sub && <div className="text-xs text-text-secondary mt-0.5">{sub}</div>}
    </div>
  );
}

export function Statistics() {
  const { state } = useBotContext();
  const { portfolio } = state;

  if (!portfolio) {
    return (
      <div className="text-center text-text-secondary py-8">
        No portfolio data yet...
      </div>
    );
  }

  const p = portfolio;

  return (
    <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
      <StatCard
        label="TOTAL P&L"
        value={formatPnl(p.total_pnl)}
        color={p.total_pnl >= 0 ? "text-accent-green" : "text-accent-red"}
        sub={`Balance: $${p.balance.toFixed(2)}`}
      />
      <StatCard
        label="WIN RATE"
        value={formatPct(p.win_rate)}
        color={p.win_rate >= 0.6 ? "text-accent-green" : p.win_rate >= 0.5 ? "text-yellow-400" : "text-accent-red"}
        sub={`${p.winning_trades}W / ${p.losing_trades}L`}
      />
      <StatCard
        label="TOTAL TRADES"
        value={String(p.total_trades)}
        sub={`Daily P&L: ${formatPnl(p.daily_pnl)}`}
      />
      <StatCard
        label="SHARPE RATIO"
        value={p.sharpe_ratio.toFixed(2)}
        color={p.sharpe_ratio > 1 ? "text-accent-green" : "text-text-primary"}
        sub="Annualized"
      />
      <StatCard
        label="AVG WIN"
        value={formatPnl(p.avg_win)}
        color="text-accent-green"
      />
      <StatCard
        label="AVG LOSS"
        value={formatPnl(p.avg_loss)}
        color="text-accent-red"
      />
      <StatCard
        label="BEST TRADE"
        value={formatPnl(p.best_trade)}
        color="text-accent-green"
      />
      <StatCard
        label="WORST TRADE"
        value={formatPnl(p.worst_trade)}
        color="text-accent-red"
      />
      <StatCard
        label="EXPECTED VALUE"
        value={`${(p.expected_value * 100).toFixed(2)}¢`}
        color={p.expected_value > 0 ? "text-accent-green" : "text-accent-red"}
        sub="Per trade"
      />
      <StatCard
        label="STREAK"
        value={
          p.consecutive_wins > 0
            ? `+${p.consecutive_wins} wins`
            : p.consecutive_losses > 0
            ? `-${p.consecutive_losses} losses`
            : "—"
        }
        color={
          p.consecutive_wins > 2
            ? "text-accent-green"
            : p.consecutive_losses > 2
            ? "text-accent-red"
            : "text-white"
        }
      />
      <StatCard
        label="AVG CONF (WIN)"
        value={`${p.avg_confidence_wins.toFixed(0)}`}
        sub="Avg confidence on wins"
      />
      <StatCard
        label="AVG CONF (LOSS)"
        value={`${p.avg_confidence_losses.toFixed(0)}`}
        sub="Avg confidence on losses"
      />
    </div>
  );
}
