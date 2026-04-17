import { useCallback, useEffect, useState } from "react";
import { fetchActivity } from "../api";
import type { AuditEntry } from "../types";
import { StatusDot, type DotTone } from "./StatusDot";
import { Tag, type TagTone } from "./Tag";

type ResultFilter = "" | "success" | "failure" | "rolled_back" | "denied";

export function ActivityTab() {
  const [entries, setEntries] = useState<AuditEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<ResultFilter>("");

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const data = await fetchActivity({
        limit: 200,
        result: filter || undefined,
      });
      setEntries(data);
      setError(null);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setLoading(false);
    }
  }, [filter]);

  useEffect(() => {
    void load();
  }, [load]);

  if (loading && entries.length === 0) {
    return (
      <div
        className="p-4 font-mono text-[11px]"
        style={{ color: "var(--color-text-muted)" }}
      >
        loading activity…
      </div>
    );
  }

  const grouped = groupByDay(entries);

  return (
    <div className="flex h-full flex-col overflow-hidden">
      <div
        className="flex items-center justify-between border-b px-4 py-2.5"
        style={{ borderColor: "var(--color-border)" }}
      >
        <div
          className="font-mono text-[10px]"
          style={{ color: "var(--color-text-muted)" }}
        >
          <span style={{ color: "var(--color-text)" }}>{entries.length}</span>
          <span style={{ color: "var(--color-text-dim)" }}> actions logged</span>
        </div>
        <div className="flex items-center gap-1">
          <FilterButton active={filter === ""} onClick={() => setFilter("")}>
            All
          </FilterButton>
          <FilterButton
            active={filter === "success"}
            onClick={() => setFilter("success")}
          >
            Success
          </FilterButton>
          <FilterButton
            active={filter === "failure"}
            onClick={() => setFilter("failure")}
          >
            Failures
          </FilterButton>
        </div>
      </div>

      {error ? (
        <div
          className="border-b px-4 py-2 font-mono text-[10px]"
          style={{
            borderColor: "var(--color-border)",
            color: "var(--color-error)",
            backgroundColor: "var(--color-error-soft)",
          }}
        >
          {error}
        </div>
      ) : null}

      <main className="flex-1 overflow-y-auto px-4 py-3 space-y-5">
        {entries.length === 0 ? (
          <div
            className="flex h-full items-center justify-center"
            style={{ color: "var(--color-text-muted)" }}
          >
            <div className="text-center">
              <div
                className="font-mono text-[11px] uppercase tracking-label"
                style={{ color: "var(--color-text-dim)" }}
              >
                No activity yet
              </div>
              <div className="mt-2 font-sans text-[13px]">
                Actions will appear here as you use Mylo.
              </div>
            </div>
          </div>
        ) : (
          grouped.map(([day, dayEntries]) => (
            <DayGroup key={day} day={day} entries={dayEntries} />
          ))
        )}
      </main>
    </div>
  );
}

function DayGroup({ day, entries }: { day: string; entries: AuditEntry[] }) {
  return (
    <div>
      <div
        className="mb-2 font-mono text-[9px] uppercase tracking-label"
        style={{ color: "var(--color-text-dim)" }}
      >
        {formatDay(day)}
      </div>
      <div className="space-y-1">
        {entries.map((entry, i) => (
          <ActivityRow key={`${entry.timestamp}-${i}`} entry={entry} />
        ))}
      </div>
    </div>
  );
}

function ActivityRow({ entry }: { entry: AuditEntry }) {
  const [open, setOpen] = useState(false);
  const tone = resultTone(entry.result);
  const dotTone = resultDotTone(entry.result);

  return (
    <div
      className="rounded border"
      style={{ borderColor: "var(--color-border)" }}
    >
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center gap-2 px-2.5 py-1.5 text-left"
      >
        <StatusDot
          tone={dotTone}
          glow={entry.result === "failure"}
        />
        <span
          className="font-mono text-[11px] font-bold"
          style={{ color: "var(--color-accent)" }}
        >
          {entry.tool_name}
        </span>
        <Tag tone={tone}>{entry.result}</Tag>
        {entry.dry_run ? (
          <Tag tone="muted">dry-run</Tag>
        ) : null}
        <span className="ml-auto flex items-center gap-2">
          <span
            className="font-mono text-[10px]"
            style={{ color: "var(--color-text-muted)" }}
          >
            {formatTime(entry.timestamp)}
          </span>
          <span
            className="font-mono text-[10px]"
            style={{ color: "var(--color-text-dim)" }}
          >
            T{entry.tier}
          </span>
          <Chevron open={open} />
        </span>
      </button>
      {open ? (
        <div
          className="border-t px-3 py-2 space-y-2"
          style={{ borderColor: "var(--color-border)" }}
        >
          <DetailRow label="timestamp" value={entry.timestamp} />
          <DetailRow label="approved" value={entry.user_approved ? "yes" : "no"} />
          {entry.rollback_performed ? (
            <DetailRow label="rollback" value="yes" />
          ) : null}
          <div
            className="font-mono text-[10px]"
            style={{ color: "var(--color-text-dim)" }}
          >
            params:
          </div>
          <pre
            className="overflow-auto rounded border px-2 py-1.5 font-mono text-[10px] leading-[1.55]"
            style={{
              borderColor: "var(--color-border)",
              color: "var(--color-text-muted)",
              backgroundColor: "var(--color-surface)",
            }}
          >
            {JSON.stringify(entry.params, null, 2)}
          </pre>
          {Object.keys(entry.details).length > 0 ? (
            <>
              <div
                className="font-mono text-[10px]"
                style={{ color: "var(--color-text-dim)" }}
              >
                details:
              </div>
              <pre
                className="overflow-auto rounded border px-2 py-1.5 font-mono text-[10px] leading-[1.55]"
                style={{
                  borderColor: "var(--color-border)",
                  color: "var(--color-text-muted)",
                  backgroundColor: "var(--color-surface)",
                }}
              >
                {JSON.stringify(entry.details, null, 2)}
              </pre>
            </>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

function DetailRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="font-mono text-[10px]">
      <span style={{ color: "var(--color-text-dim)" }}>{label}: </span>
      <span style={{ color: "var(--color-text)" }}>{value}</span>
    </div>
  );
}

function FilterButton({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="rounded-tag border px-2.5 py-1 font-mono text-[10px] font-medium transition-colors"
      style={
        active
          ? {
              borderColor: "var(--color-border-accent)",
              backgroundColor: "var(--color-accent-soft)",
              color: "var(--color-accent)",
            }
          : {
              borderColor: "transparent",
              color: "var(--color-text-muted)",
            }
      }
    >
      {children}
    </button>
  );
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

// ─── Helpers ──────────────────────────────────────────────────────────────

function resultTone(result: string): TagTone {
  switch (result) {
    case "success":
      return "success";
    case "failure":
      return "error";
    case "rolled_back":
      return "warning";
    case "denied":
      return "muted";
    default:
      return "default";
  }
}

function resultDotTone(result: string): DotTone {
  switch (result) {
    case "success":
      return "accent";
    case "failure":
      return "error";
    case "rolled_back":
      return "warning";
    case "denied":
      return "muted";
    default:
      return "accent";
  }
}

function groupByDay(entries: AuditEntry[]): [string, AuditEntry[]][] {
  const groups = new Map<string, AuditEntry[]>();
  for (const entry of entries) {
    const day = entry.timestamp.slice(0, 10); // YYYY-MM-DD
    if (!groups.has(day)) groups.set(day, []);
    groups.get(day)!.push(entry);
  }
  return Array.from(groups.entries());
}

function formatDay(isoDay: string): string {
  const today = new Date().toISOString().slice(0, 10);
  const yesterday = new Date(Date.now() - 86_400_000).toISOString().slice(0, 10);
  if (isoDay === today) return "today";
  if (isoDay === yesterday) return "yesterday";
  try {
    return new Date(isoDay + "T00:00:00").toLocaleDateString(undefined, {
      weekday: "short",
      month: "short",
      day: "numeric",
    });
  } catch {
    return isoDay;
  }
}

function formatTime(iso: string): string {
  try {
    return new Date(iso).toLocaleTimeString(undefined, {
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return iso.slice(11, 16);
  }
}
