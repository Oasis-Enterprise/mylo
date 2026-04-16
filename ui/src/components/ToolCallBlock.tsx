import { useState } from "react";
import { formatDuration } from "../lib/format";
import type { ToolCallRecord } from "../types";
import { StatusDot, type DotTone } from "./StatusDot";

interface Props {
  call: ToolCallRecord;
}

// Collapsed row: dot · name (accent bold mono) · summary (dim mono).
// Right side: duration · rotating chevron. Click to reveal params
// block in mono 10px. Matches the "rename_entities dry run" treatment
// in the Signal screenshot.
export function ToolCallBlock({ call }: Props) {
  const [open, setOpen] = useState(false);

  const isAwaitingApproval =
    call.state === "error" && call.errorCode === "confirmation_required";

  const tone: DotTone = call.state === "pending"
    ? "warning"
    : isAwaitingApproval
      ? "warning"
      : call.state === "ok"
        ? "accent"
        : "error";

  const summary = buildSummary(call, isAwaitingApproval);
  const durationLabel = call.durationMs !== undefined
    ? formatDuration(call.durationMs)
    : call.state === "pending"
      ? "…"
      : "";

  return (
    <div
      className="my-1 rounded border bg-surface-raised"
      style={{ borderColor: "var(--color-border)" }}
    >
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center gap-2 px-2.5 py-1.5 text-left"
      >
        <StatusDot tone={tone} pulse={call.state === "pending"} />
        <span
          className="font-mono font-bold text-[11px]"
          style={{ color: tone === "error" ? "var(--color-error)" : "var(--color-accent)" }}
        >
          {call.name}
        </span>
        {summary ? (
          <span
            className="font-mono text-[10px]"
            style={{ color: "var(--color-text-dim)" }}
          >
            {summary}
          </span>
        ) : null}
        <span className="ml-auto flex items-center gap-2">
          {durationLabel ? (
            <span
              className="font-mono text-[10px]"
              style={{ color: "var(--color-text-muted)" }}
            >
              {durationLabel}
            </span>
          ) : null}
          <Chevron open={open} />
        </span>
      </button>
      {open ? (
        <pre
          className="overflow-auto border-t px-3 py-2 font-mono text-[10px] leading-[1.55]"
          style={{
            borderColor: "var(--color-border)",
            color: "var(--color-text-muted)",
            backgroundColor: "var(--color-surface)",
          }}
        >
          {JSON.stringify(call.input, null, 2)}
        </pre>
      ) : null}
    </div>
  );
}

function buildSummary(call: ToolCallRecord, awaiting: boolean): string {
  if (awaiting) return "awaiting approval";
  if (call.errorCode && call.state === "error") return call.errorCode;
  if (call.summary) return call.summary;
  if (call.state === "pending") return "running";
  return "";
}

function Chevron({ open }: { open: boolean }) {
  return (
    <svg
      width="8"
      height="8"
      viewBox="0 0 10 10"
      fill="none"
      style={{
        transform: open ? "rotate(180deg)" : "none",
        transition: "transform 120ms ease",
        color: "var(--color-text-muted)",
      }}
    >
      <path
        d="M2 3.5L5 6.5L8 3.5"
        stroke="currentColor"
        strokeWidth="1.3"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}
