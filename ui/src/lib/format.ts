// Tiny formatting helpers used across the Signal-themed header,
// status bar, and tool-call blocks. Kept runtime-free — no i18n
// for now; if the panel ever needs it, swap to Intl.RelativeTimeFormat.

export function formatTokens(count: number): string {
  if (count < 1_000) return `${count}`;
  if (count < 10_000) return `${(count / 1_000).toFixed(1)}k`;
  if (count < 1_000_000) return `${Math.round(count / 1_000)}k`;
  return `${(count / 1_000_000).toFixed(1)}M`;
}

export function formatDollars(amount: number): string {
  if (amount < 0.01) return "$0.00";
  if (amount < 1) return `$${amount.toFixed(2)}`;
  if (amount < 100) return `$${amount.toFixed(2)}`;
  return `$${Math.round(amount)}`;
}

export function formatRelative(iso: string | null | undefined): string {
  if (!iso) return "never";
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return iso;
  const now = Date.now();
  const delta = Math.max(0, Math.round((now - then) / 1000)); // seconds
  if (delta < 45) return "just now";
  if (delta < 90) return "1m ago";
  const minutes = Math.round(delta / 60);
  if (minutes < 45) return `${minutes}m ago`;
  if (minutes < 90) return "1h ago";
  const hours = Math.round(delta / 3600);
  if (hours < 24) return `${hours}h ago`;
  if (hours < 36) return "1d ago";
  const days = Math.round(delta / 86_400);
  if (days < 30) return `${days}d ago`;
  const months = Math.round(days / 30);
  if (months < 12) return `${months}mo ago`;
  return `${Math.round(months / 12)}y ago`;
}

export function formatDuration(ms: number): string {
  if (ms < 1_000) return `${Math.round(ms)}ms`;
  if (ms < 60_000) return `${(ms / 1_000).toFixed(1)}s`;
  return `${Math.round(ms / 1_000)}s`;
}
