"use client";

import { useEffect, useRef, useState } from "react";
import { useBotContext } from "@/app/providers";
import { getLogColor } from "@/lib/utils";
import type { LogEntry } from "@/lib/types";

export function Terminal() {
  const { state, send } = useBotContext();
  const { logs } = state;
  const scrollRef = useRef<HTMLDivElement>(null);
  const [autoScroll, setAutoScroll] = useState(true);
  const [command, setCommand] = useState("");
  const [filter, setFilter] = useState<string>("ALL");

  // Auto-scroll
  useEffect(() => {
    if (autoScroll && scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [logs, autoScroll]);

  const handleScroll = () => {
    const el = scrollRef.current;
    if (!el) return;
    const isAtBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 50;
    setAutoScroll(isAtBottom);
  };

  const handleCommand = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key !== "Enter" || !command.trim()) return;
    const parts = command.trim().split(" ");
    const cmd = parts[0].toLowerCase();

    if (cmd === "pause") send({ command: "pause" });
    else if (cmd === "resume") send({ command: "resume" });
    else if (cmd === "status") send({ command: "status" });
    else if (cmd === "stop") send({ command: "emergency_stop" });
    else if (cmd === "confidence" && parts[1]) {
      send({ command: "set_confidence", value: parseInt(parts[1]) });
    } else if (cmd === "help") {
      // Handled client-side
    }

    setCommand("");
  };

  const LEVELS = ["ALL", "TRADE", "CLAIM", "INFO", "WARNING", "ERROR"];

  const filtered: LogEntry[] =
    filter === "ALL"
      ? logs
      : logs.filter((l) => l.level === filter);

  return (
    <div
      className="flex flex-col h-full rounded-lg overflow-hidden"
      style={{ background: "#0A0A0F", border: "1px solid var(--border-color)" }}
    >
      {/* Toolbar */}
      <div
        className="flex items-center justify-between px-4 py-2 border-b text-xs"
        style={{ borderColor: "var(--border-color)", background: "var(--surface)" }}
      >
        <div className="flex items-center gap-2">
          <span className="text-accent-green">■</span>
          <span className="text-text-secondary">TERMINAL</span>
          <span className="text-text-secondary">|</span>
          <span className="text-zinc-400">{logs.length} messages</span>
        </div>
        <div className="flex gap-1">
          {LEVELS.map((l) => (
            <button
              key={l}
              onClick={() => setFilter(l)}
              className={`px-2 py-0.5 rounded text-xs transition-colors ${
                filter === l
                  ? "bg-accent-green/20 text-accent-green"
                  : "text-text-secondary hover:text-white"
              }`}
            >
              {l}
            </button>
          ))}
        </div>
        <button
          onClick={() => setAutoScroll(!autoScroll)}
          className={`text-xs ${autoScroll ? "text-accent-green" : "text-text-secondary"}`}
        >
          {autoScroll ? "AUTO-SCROLL ON" : "PAUSED"}
        </button>
      </div>

      {/* Log content */}
      <div
        ref={scrollRef}
        onScroll={handleScroll}
        className="flex-1 overflow-y-auto p-4 terminal"
        style={{ minHeight: 0 }}
      >
        {filtered.length === 0 && (
          <div className="text-text-secondary text-center mt-8">
            Waiting for bot connection...
          </div>
        )}
        {filtered.map((entry) => (
          <div key={entry.id} className="flex gap-3 mb-0.5 hover:bg-white/[0.02]">
            <span className="text-text-secondary shrink-0 w-20">{entry.timestamp}</span>
            <span
              className={`shrink-0 w-16 text-right ${
                entry.level === "TRADE"
                  ? "text-accent-green"
                  : entry.level === "CLAIM"
                  ? "text-purple-400"
                  : entry.level === "WARNING"
                  ? "text-yellow-400"
                  : entry.level === "ERROR" || entry.level === "CRITICAL"
                  ? "text-accent-red"
                  : "text-zinc-500"
              }`}
            >
              {entry.level}
            </span>
            <span className="text-zinc-500 shrink-0 w-20 truncate">{entry.module}</span>
            <span className={`flex-1 break-all ${getLogColor(entry.level)}`}>
              {entry.message}
            </span>
          </div>
        ))}
      </div>

      {/* Command input */}
      <div
        className="flex items-center gap-2 px-4 py-2 border-t"
        style={{ borderColor: "var(--border-color)" }}
      >
        <span className="text-accent-green shrink-0">$</span>
        <input
          type="text"
          value={command}
          onChange={(e) => setCommand(e.target.value)}
          onKeyDown={handleCommand}
          placeholder="pause | resume | status | confidence 70 | stop"
          className="flex-1 bg-transparent text-white text-xs outline-none placeholder-zinc-600 font-mono"
        />
      </div>
    </div>
  );
}
