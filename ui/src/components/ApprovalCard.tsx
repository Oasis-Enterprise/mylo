import { StatusDot } from "./StatusDot";
import { Tag } from "./Tag";

interface Props {
  description?: string;
  // Optional diff block. When present, rendered as a red minus /
  // green plus pair in mono. Both sides are single lines — wrap at
  // the caller if you need multi-line diffs.
  diff?: { before: string; after: string };
  // Metadata line under the diff — "references: 0 automations · 0 dashboards · 0 scripts".
  meta?: string;
  tierLabel?: string;
  onApprove: () => void;
  onReject: () => void;
  // True when an apply has been queued but not yet submitted (prior
  // turn's SSE stream still closing). Disables APPLY to avoid double-
  // send and swaps the label to "Applying…" so the click feels live.
  applying?: boolean;
}

// Inline approval card rendered in the chat stream, not as a bottom
// bar. Pulsing dot + "AWAITING APPROVAL" label in accent mono, tier
// tag, optional description + diff + reference metadata, Reject /
// Apply buttons.
export function ApprovalCard({
  description,
  diff,
  meta,
  tierLabel = "TIER-2",
  onApprove,
  onReject,
  applying = false,
}: Props) {
  return (
    <div
      className="rounded border"
      style={{
        borderColor: "var(--color-border-accent)",
        borderWidth: 1,
      }}
    >
      <div className="flex items-center gap-2 px-3 py-2 border-b"
        style={{ borderColor: "var(--color-border)" }}
      >
        <StatusDot tone="accent" pulse />
        <span
          className="font-mono text-[10px] font-bold uppercase tracking-label"
          style={{ color: "var(--color-accent)" }}
        >
          Awaiting approval
        </span>
        <span className="ml-auto">
          <Tag tone="muted">{tierLabel}</Tag>
        </span>
      </div>

      {(description || diff || meta) ? (
        <div className="px-3 py-3 space-y-2.5">
          {description ? (
            <div
              className="font-sans text-[13px]"
              style={{ color: "var(--color-text)" }}
            >
              {description}
            </div>
          ) : null}
          {diff ? <DiffBlock before={diff.before} after={diff.after} /> : null}
          {meta ? (
            <div
              className="font-mono text-[10px]"
              style={{ color: "var(--color-text-dim)" }}
            >
              {meta}
            </div>
          ) : null}
        </div>
      ) : null}

      <div className="flex items-center justify-end gap-2 px-3 py-2 border-t"
        style={{ borderColor: "var(--color-border)" }}
      >
        <button
          type="button"
          onClick={onReject}
          className="rounded border px-3 py-1 font-mono text-[11px] font-bold uppercase tracking-label hover:opacity-80"
          style={{
            borderColor: "var(--color-border)",
            color: "var(--color-text-muted)",
            background: "transparent",
          }}
        >
          Reject
        </button>
        <button
          type="button"
          onClick={onApprove}
          disabled={applying}
          className="btn-glow rounded px-3 py-1 font-mono text-[11px] font-bold uppercase tracking-label hover:brightness-110 disabled:opacity-60"
          style={{
            backgroundColor: "var(--color-accent-soft)",
            border: "1px solid var(--color-accent)",
            color: "var(--color-accent)",
          }}
        >
          {applying ? "Applying…" : "Apply"}
        </button>
      </div>
    </div>
  );
}

function DiffBlock({ before, after }: { before: string; after: string }) {
  return (
    <div
      className="rounded border font-mono text-[11px] leading-[1.55]"
      style={{
        borderColor: "var(--color-border)",
        backgroundColor: "var(--color-surface)",
      }}
    >
      <div
        className="px-3 py-1.5 flex items-start gap-2"
        style={{ color: "var(--color-text-dim)" }}
      >
        <span style={{ color: "var(--color-error)" }}>-</span>
        <span className="line-through break-all">{before}</span>
      </div>
      <div
        className="px-3 py-1.5 flex items-start gap-2 border-t"
        style={{
          borderColor: "var(--color-border)",
          color: "var(--color-text)",
        }}
      >
        <span style={{ color: "var(--color-accent)" }}>+</span>
        <span className="break-all" style={{ color: "var(--color-accent)" }}>
          {after}
        </span>
      </div>
    </div>
  );
}
