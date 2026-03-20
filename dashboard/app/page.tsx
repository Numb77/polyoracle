"use client";

import { useState } from "react";
import { Header } from "@/components/layout/Header";
import { Terminal } from "@/components/terminal/Terminal";
import { MarketCard } from "@/components/markets/MarketCard";
import { AgentPanel } from "@/components/agents/AgentPanel";
import { Statistics } from "@/components/portfolio/Statistics";
import { EquityCurve } from "@/components/portfolio/EquityCurve";
import { RiskDashboard } from "@/components/risk/RiskDashboard";
import { TradeHistory } from "@/components/positions/TradeHistory";
import { cn } from "@/lib/utils";
import { useBotContext } from "./providers";

const TABS = [
  { id: "terminal", label: "TERMINAL" },
  { id: "markets", label: "MARKETS" },
  { id: "positions", label: "POSITIONS" },
  { id: "portfolio", label: "PORTFOLIO" },
  { id: "agents", label: "AGENTS" },
  { id: "risk", label: "RISK" },
] as const;

type TabId = (typeof TABS)[number]["id"];

export default function Home() {
  const [activeTab, setActiveTab] = useState<TabId>("terminal");
  const { state } = useBotContext();

  return (
    <div className="flex flex-col h-screen overflow-hidden">
      <Header />

      {/* Tab bar */}
      <nav
        className="flex border-b shrink-0"
        style={{ background: "var(--surface)", borderColor: "var(--border-color)" }}
      >
        {TABS.map((tab) => (
          <button
            key={tab.id}
            onClick={() => setActiveTab(tab.id)}
            className={cn(
              "px-4 py-3 text-xs font-mono tracking-wider transition-colors relative",
              activeTab === tab.id
                ? "text-accent-green"
                : "text-text-secondary hover:text-white"
            )}
          >
            {tab.label}
            {/* Notification dots */}
            {tab.id === "positions" && state.activeTradeIds.length > 0 && (
              <span className="absolute top-2 right-1 w-1.5 h-1.5 rounded-full bg-yellow-400" />
            )}
            {tab.id === "risk" &&
              state.circuit &&
              state.circuit.tier !== "GREEN" && (
                <span className="absolute top-2 right-1 w-1.5 h-1.5 rounded-full bg-accent-red animate-pulse" />
              )}
            {activeTab === tab.id && (
              <div className="absolute bottom-0 left-0 right-0 h-0.5 bg-accent-green" />
            )}
          </button>
        ))}
      </nav>

      {/* Tab content */}
      <main className="flex-1 overflow-y-auto p-4" style={{ minHeight: 0 }}>
        {activeTab === "terminal" && (
          <div className="h-full" style={{ minHeight: "calc(100vh - 120px)" }}>
            <Terminal />
          </div>
        )}

        {activeTab === "markets" && (
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
            <MarketCard asset="BTC" />
            <MarketCard asset="ETH" />
          </div>
        )}

        {activeTab === "positions" && <TradeHistory />}

        {activeTab === "portfolio" && (
          <div className="space-y-4">
            <EquityCurve />
            <Statistics />
          </div>
        )}

        {activeTab === "agents" && <AgentPanel />}

        {activeTab === "risk" && <RiskDashboard />}
      </main>
    </div>
  );
}
