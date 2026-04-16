import type { ReactNode } from "react";

export type TagTone = "default" | "success" | "warning" | "error" | "info" | "muted";

interface Props {
  children: ReactNode;
  tone?: TagTone;
  className?: string;
}

// Inline uppercase pill. Monospace, tight letter-spacing. Used for
// tier labels (TIER-2), severity tags (CRIT / HIGH / MED), build
// versions (V0.8), and tab-like affordances.
export function Tag({ children, tone = "default", className = "" }: Props) {
  const style = toneStyles(tone);
  return (
    <span
      className={
        "inline-flex items-center font-mono font-semibold uppercase " +
        "tracking-label text-[10px] leading-none " +
        "rounded-tag border px-[7px] py-[3px] " +
        className
      }
      style={style}
    >
      {children}
    </span>
  );
}

function toneStyles(tone: TagTone) {
  switch (tone) {
    case "success":
      return {
        backgroundColor: "var(--color-success-soft)",
        borderColor: "var(--color-border-accent)",
        color: "var(--color-success)",
      };
    case "warning":
      return {
        backgroundColor: "var(--color-warning-soft)",
        borderColor: "rgba(229, 161, 14, 0.33)",
        color: "var(--color-warning)",
      };
    case "error":
      return {
        backgroundColor: "var(--color-error-soft)",
        borderColor: "rgba(229, 72, 77, 0.33)",
        color: "var(--color-error)",
      };
    case "info":
      return {
        backgroundColor: "var(--color-info-soft)",
        borderColor: "rgba(59, 130, 246, 0.33)",
        color: "var(--color-info)",
      };
    case "muted":
      return {
        backgroundColor: "transparent",
        borderColor: "var(--color-border)",
        color: "var(--color-text-muted)",
      };
    default:
      return {
        backgroundColor: "var(--color-accent-soft)",
        borderColor: "var(--color-border-accent)",
        color: "var(--color-accent)",
      };
  }
}
