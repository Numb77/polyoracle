import type { Config } from "tailwindcss";

const config: Config = {
  darkMode: ["class"],
  content: [
    "./pages/**/*.{js,ts,jsx,tsx,mdx}",
    "./components/**/*.{js,ts,jsx,tsx,mdx}",
    "./app/**/*.{js,ts,jsx,tsx,mdx}",
  ],
  theme: {
    extend: {
      colors: {
        background: "var(--background)",
        surface: "var(--surface)",
        border: "var(--border-color)",
        accent: {
          green: "#00FF88",
          red: "#FF3366",
          indigo: "#6366F1",
        },
        text: {
          primary: "#E4E4E7",
          secondary: "#71717A",
        },
        chart: {
          green: "#22C55E",
          red: "#EF4444",
        },
      },
      fontFamily: {
        mono: ["IBM Plex Mono", "Fira Code", "monospace"],
        sans: ["Satoshi", "Inter", "system-ui", "sans-serif"],
      },
      animation: {
        "pulse-green": "pulseGreen 2s ease-in-out infinite",
        "count-up": "countUp 0.3s ease-out",
        scanline: "scanline 8s linear infinite",
        blink: "blink 1s step-end infinite",
      },
      keyframes: {
        pulseGreen: {
          "0%, 100%": { boxShadow: "0 0 0 0 rgba(0, 255, 136, 0.4)" },
          "50%": { boxShadow: "0 0 0 8px rgba(0, 255, 136, 0)" },
        },
        countUp: {
          from: { opacity: "0", transform: "translateY(4px)" },
          to: { opacity: "1", transform: "translateY(0)" },
        },
        scanline: {
          "0%": { transform: "translateY(-100%)" },
          "100%": { transform: "translateY(100vh)" },
        },
        blink: {
          "0%, 100%": { opacity: "1" },
          "50%": { opacity: "0" },
        },
      },
      backgroundImage: {
        "grid-pattern":
          "linear-gradient(rgba(0,255,136,0.03) 1px, transparent 1px), linear-gradient(90deg, rgba(0,255,136,0.03) 1px, transparent 1px)",
      },
      backgroundSize: {
        grid: "40px 40px",
      },
    },
  },
  plugins: [],
};

export default config;
