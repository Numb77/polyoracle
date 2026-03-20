"use client";

import { useEffect, useRef, useState } from "react";
import { useBotContext } from "@/app/providers";
import { formatPnl } from "@/lib/utils";
import type { TradeResolved } from "@/lib/types";

interface EquityPoint {
  trade: number;
  pnl: number;
  cumPnl: number;
  rollingWinRate: number;  // Rolling 20-trade win rate (0-1)
}

export function EquityCurve() {
  const { state } = useBotContext();
  const { recentTrades, portfolio } = state;
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [points, setPoints] = useState<EquityPoint[]>([]);

  // Build equity curve from trade history
  useEffect(() => {
    let cum = 0;
    const ROLLING_WINDOW = 20;
    const chronological = recentTrades.slice().reverse();
    const newPoints: EquityPoint[] = chronological.map((t, i) => {
      cum += t.pnl;
      // Rolling win rate: last N trades up to and including this one
      const window = chronological.slice(Math.max(0, i - ROLLING_WINDOW + 1), i + 1);
      const wins = window.filter((w) => w.won).length;
      const rollingWinRate = window.length > 0 ? wins / window.length : 0.5;
      return { trade: i + 1, pnl: t.pnl, cumPnl: cum, rollingWinRate };
    });
    setPoints(newPoints);
  }, [recentTrades]);

  // Draw on canvas
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || points.length < 2) return;

    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    const W = canvas.width;
    const H = canvas.height;
    const PAD = { top: 20, right: 20, bottom: 30, left: 60 };

    ctx.clearRect(0, 0, W, H);

    const pnls = points.map((p) => p.cumPnl);
    const minPnl = Math.min(0, ...pnls);
    const maxPnl = Math.max(0, ...pnls);
    const range = maxPnl - minPnl || 1;

    const toX = (i: number) =>
      PAD.left + (i / (points.length - 1)) * (W - PAD.left - PAD.right);
    const toY = (v: number) =>
      PAD.top + (1 - (v - minPnl) / range) * (H - PAD.top - PAD.bottom);

    const zeroY = toY(0);

    // Grid lines
    ctx.strokeStyle = "#1E1E2E";
    ctx.lineWidth = 1;
    for (let i = 0; i <= 4; i++) {
      const y = PAD.top + (i / 4) * (H - PAD.top - PAD.bottom);
      ctx.beginPath();
      ctx.moveTo(PAD.left, y);
      ctx.lineTo(W - PAD.right, y);
      ctx.stroke();
    }

    // Zero line
    ctx.strokeStyle = "#3f3f5a";
    ctx.lineWidth = 1;
    ctx.setLineDash([4, 4]);
    ctx.beginPath();
    ctx.moveTo(PAD.left, zeroY);
    ctx.lineTo(W - PAD.right, zeroY);
    ctx.stroke();
    ctx.setLineDash([]);

    // Fill area
    const gradient = ctx.createLinearGradient(0, PAD.top, 0, H - PAD.bottom);
    const lastPnl = pnls[pnls.length - 1];
    if (lastPnl >= 0) {
      gradient.addColorStop(0, "rgba(0, 255, 136, 0.2)");
      gradient.addColorStop(1, "rgba(0, 255, 136, 0)");
    } else {
      gradient.addColorStop(0, "rgba(255, 51, 102, 0)");
      gradient.addColorStop(1, "rgba(255, 51, 102, 0.2)");
    }

    ctx.beginPath();
    points.forEach((p, i) => {
      const x = toX(i);
      const y = toY(p.cumPnl);
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.lineTo(toX(points.length - 1), zeroY);
    ctx.lineTo(toX(0), zeroY);
    ctx.closePath();
    ctx.fillStyle = gradient;
    ctx.fill();

    // Line
    ctx.beginPath();
    points.forEach((p, i) => {
      const x = toX(i);
      const y = toY(p.cumPnl);
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.strokeStyle = lastPnl >= 0 ? "#00FF88" : "#FF3366";
    ctx.lineWidth = 2;
    ctx.stroke();

    // Y-axis labels
    ctx.fillStyle = "#71717A";
    ctx.font = "10px IBM Plex Mono";
    ctx.textAlign = "right";
    for (let i = 0; i <= 4; i++) {
      const v = minPnl + (i / 4) * range;
      const y = toY(v);
      ctx.fillText(`$${v.toFixed(1)}`, PAD.left - 4, y + 3);
    }

    // ── Rolling win rate line (secondary axis, right side) ──────────────────
    if (points.length >= 5) {
      // Map win rate 0-1 to canvas Y space
      const toYWin = (wr: number) =>
        PAD.top + (1 - wr) * (H - PAD.top - PAD.bottom);

      // 50% reference dashed line
      ctx.strokeStyle = "rgba(250, 204, 21, 0.15)";
      ctx.lineWidth = 1;
      ctx.setLineDash([3, 3]);
      const y50 = toYWin(0.5);
      ctx.beginPath();
      ctx.moveTo(PAD.left, y50);
      ctx.lineTo(W - PAD.right, y50);
      ctx.stroke();
      ctx.setLineDash([]);

      // Win rate line
      ctx.beginPath();
      points.forEach((p, i) => {
        const x = toX(i);
        const y = toYWin(p.rollingWinRate);
        if (i === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      });
      ctx.strokeStyle = "rgba(250, 204, 21, 0.6)";
      ctx.lineWidth = 1.5;
      ctx.stroke();

      // Right-axis label
      ctx.fillStyle = "rgba(250, 204, 21, 0.7)";
      ctx.font = "9px IBM Plex Mono";
      ctx.textAlign = "left";
      const lastWr = points[points.length - 1].rollingWinRate;
      ctx.fillText(`${(lastWr * 100).toFixed(0)}%`, W - PAD.right + 3, toYWin(lastWr) + 3);
    }
  }, [points]);

  if (points.length < 2) {
    return (
      <div
        className="card p-4 flex items-center justify-center text-text-secondary"
        style={{ height: 200 }}
      >
        No trade history yet
      </div>
    );
  }

  const totalPnl = points[points.length - 1]?.cumPnl ?? 0;

  return (
    <div className="card p-4">
      <div className="flex justify-between items-center mb-3">
        <div className="flex items-center gap-3">
          <span className="text-xs text-text-secondary">EQUITY CURVE</span>
          <div className="flex items-center gap-1">
            <div className="w-3 h-0.5 bg-yellow-400/60" />
            <span className="text-[10px] text-yellow-400/70">20-trade win rate</span>
          </div>
        </div>
        <span
          className={`text-sm font-bold ${
            totalPnl >= 0 ? "text-accent-green" : "text-accent-red"
          }`}
        >
          {formatPnl(totalPnl)}
        </span>
      </div>
      <canvas
        ref={canvasRef}
        width={600}
        height={200}
        className="w-full"
        style={{ height: 200 }}
      />
    </div>
  );
}
