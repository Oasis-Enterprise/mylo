// Tailwind utilities map to the CSS variables set in src/index.css,
// which are themselves driven by src/styles/theme.ts. This lets us
// keep Tailwind ergonomics without duplicating the Signal palette.
/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        bg: "var(--color-bg)",
        surface: "var(--color-surface)",
        "surface-raised": "var(--color-surface-raised)",
        border: "var(--color-border)",
        "border-accent": "var(--color-border-accent)",
        text: "var(--color-text)",
        muted: "var(--color-text-muted)",
        dim: "var(--color-text-dim)",
        accent: "var(--color-accent)",
        "accent-dim": "var(--color-accent-dim)",
        "accent-soft": "var(--color-accent-soft)",
        "accent-glow": "var(--color-accent-glow)",
        success: "var(--color-success)",
        "success-soft": "var(--color-success-soft)",
        warning: "var(--color-warning)",
        "warning-soft": "var(--color-warning-soft)",
        error: "var(--color-error)",
        "error-soft": "var(--color-error-soft)",
        info: "var(--color-info)",
        "info-soft": "var(--color-info-soft)",
        "user-bubble": "var(--color-user-bubble)",
        "user-border": "var(--color-user-border)",
        // Legacy names still referenced by a few older components —
        // aliased to the new tokens so the refresh is incremental.
        elevated: "var(--color-surface-raised)",
        mute: "var(--color-text-muted)",
      },
      fontFamily: {
        mono: ["'JetBrains Mono'", "'Fira Code'", "'SF Mono'", "monospace"],
        sans: ["'Inter'", "-apple-system", "BlinkMacSystemFont", "sans-serif"],
      },
      borderRadius: {
        DEFAULT: "var(--radius)",
        tag: "var(--radius-tag)",
      },
      letterSpacing: {
        label: "0.04em",
        wordmark: "0.06em",
      },
    },
  },
  plugins: [],
};
