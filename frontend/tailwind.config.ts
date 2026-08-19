import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        base: {
          DEFAULT: "#0A0C10",
          raised: "#12151B",
          panel: "#171B22",
          hairline: "#242A33",
        },
        ink: {
          DEFAULT: "#E7EAEE",
          muted: "#8B93A1",
          faint: "#5B6472",
        },
        signal: {
          amber: "#E8A33D",
          amberDim: "#8A6528",
          teal: "#3FC7B6",
          tealDim: "#215F58",
          rose: "#E8637A",
          roseDim: "#7A2E3A",
          violet: "#9B87E8",
          slate: "#5B6472",
        },
      },
      fontFamily: {
        sans: [
          "Inter",
          "-apple-system",
          "BlinkMacSystemFont",
          "Segoe UI",
          "Roboto",
          "Helvetica Neue",
          "Arial",
          "sans-serif",
        ],
        mono: [
          "IBM Plex Mono",
          "ui-monospace",
          "SFMono-Regular",
          "Menlo",
          "Consolas",
          "monospace",
        ],
      },
      boxShadow: {
        panel: "0 1px 0 0 rgba(255,255,255,0.03) inset, 0 8px 24px -12px rgba(0,0,0,0.6)",
      },
      keyframes: {
        pulseDot: {
          "0%, 100%": { opacity: "1", transform: "scale(1)" },
          "50%": { opacity: "0.4", transform: "scale(0.75)" },
        },
        dashFlow: {
          to: { strokeDashoffset: "-24" },
        },
      },
      animation: {
        pulseDot: "pulseDot 1.6s ease-in-out infinite",
        dashFlow: "dashFlow 1s linear infinite",
      },
    },
  },
  plugins: [],
};

export default config;
