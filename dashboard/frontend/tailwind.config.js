/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,jsx}"],
  theme: {
    extend: {
      fontFamily: {
        sans: ["Inter", "system-ui", "-apple-system", "sans-serif"],
        mono: ["JetBrains Mono", "Fira Code", "Menlo", "monospace"],
      },
      colors: {
        surface: {
          0: "#0a0a0f",
          1: "#111118",
          2: "#1a1a24",
          3: "#232330",
          4: "#2c2c3a",
        },
        border: {
          subtle: "rgba(255,255,255,0.06)",
          DEFAULT: "rgba(255,255,255,0.10)",
          strong: "rgba(255,255,255,0.16)",
        },
        accent: {
          DEFAULT: "#818cf8",
          hover: "#a5b4fc",
          muted: "rgba(129,140,248,0.15)",
        },
      },
      animation: {
        "pulse-slow": "pulse 3s ease-in-out infinite",
      },
    },
  },
  plugins: [],
};
