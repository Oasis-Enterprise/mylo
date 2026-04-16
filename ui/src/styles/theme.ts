// Signal theme — tactical green-on-black with high data density.
//
// These are the canonical tokens. They're mirrored into CSS variables
// in index.css so Tailwind utilities (bg-surface, text-accent, etc.)
// map back to the same values. Import `theme` directly when a
// component needs a token inline (gradient stops, computed shadows).

export const theme = {
  bg: "#08090a",
  surface: "#0f1114",
  surfaceRaised: "#151820",
  border: "#1e222a",
  borderAccent: "#10b98133",
  text: "#c8cdd5",
  textMuted: "#5c6370",
  textDim: "#333842",
  accent: "#10b981",
  accentDim: "#0d9668",
  accentSoft: "#10b98115",
  accentGlow: "#10b98140",
  success: "#10b981",
  successSoft: "#10b98118",
  warning: "#e5a10e",
  warningSoft: "#e5a10e18",
  error: "#e5484d",
  errorSoft: "#e5484d18",
  info: "#3b82f6",
  infoSoft: "#3b82f618",
  userBubble: "#0d1118",
  userBorder: "#1a2030",
  radius: "4px",
  tagRadius: "3px",
} as const;

export const fonts = {
  mono: "'JetBrains Mono', 'Fira Code', 'SF Mono', monospace",
  sans: "'Inter', -apple-system, BlinkMacSystemFont, sans-serif",
} as const;

export type ThemeToken = keyof typeof theme;
