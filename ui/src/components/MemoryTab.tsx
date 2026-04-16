import { useCallback, useEffect, useState } from "react";
import {
  applyPrune,
  deleteMemoryItem,
  fetchMemoryFull,
  fetchScratchpad,
  resolveConflict,
  syncMemory,
} from "../api";
import { formatRelative } from "../lib/format";
import type {
  MemoryConflict,
  MemoryFull,
  MemoryIssue,
  MemoryNote,
  MemoryPattern,
  MemoryRejection,
  ScratchpadEntry,
  SyncResult,
} from "../types";
import { SeverityCard } from "./SeverityCard";
import { StatusDot } from "./StatusDot";
import { Tag } from "./Tag";

export function MemoryTab() {
  const [memory, setMemory] = useState<MemoryFull | null>(null);
  const [scratchpad, setScratchpad] = useState<ScratchpadEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [syncing, setSyncing] = useState(false);
  const [lastSync, setLastSync] = useState<SyncResult | null>(null);
  const [busyItem, setBusyItem] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [mem, pending] = await Promise.all([fetchMemoryFull(), fetchScratchpad()]);
      setMemory(mem);
      setScratchpad(pending);
      setError(null);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const handleSync = useCallback(async () => {
    setSyncing(true);
    try {
      const result = await syncMemory();
      setLastSync(result);
      await load();
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setSyncing(false);
    }
  }, [load]);

  const handleApplyPrune = useCallback(async () => {
    setSyncing(true);
    try {
      const ids = lastSync?.prune_candidates.map((c) => c.id) ?? [];
      const result = await applyPrune({ ids });
      setLastSync({
        ok: result.ok,
        changed: true,
        applied: true,
        summary: `pruned ${result.applied} items`,
        conflicts_added: 0,
        prune_candidates: [],
      });
      await load();
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setSyncing(false);
    }
  }, [lastSync, load]);

  const handleDelete = useCallback(
    async (section: string, id: string) => {
      setBusyItem(`${section}/${id}`);
      try {
        await deleteMemoryItem(section, id);
        await load();
      } catch (exc) {
        setError(exc instanceof Error ? exc.message : String(exc));
      } finally {
        setBusyItem(null);
      }
    },
    [load],
  );

  const handleResolve = useCallback(
    async (conflictId: string, choice: "a" | "b" | "dismiss") => {
      setBusyItem(`conflict/${conflictId}`);
      try {
        await resolveConflict(conflictId, choice);
        await load();
      } catch (exc) {
        setError(exc instanceof Error ? exc.message : String(exc));
      } finally {
        setBusyItem(null);
      }
    },
    [load],
  );

  if (loading && !memory) {
    return (
      <div
        className="p-4 font-mono text-[11px]"
        style={{ color: "var(--color-text-muted)" }}
      >
        loading memory…
      </div>
    );
  }
  if (error && !memory) {
    return (
      <div
        className="p-4 font-mono text-[11px]"
        style={{ color: "var(--color-error)" }}
      >
        error: {error}
      </div>
    );
  }
  if (!memory) return null;

  const pendingConflicts = memory.conflicts.filter((c) => c.status === "pending_review");

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
          <span style={{ color: "var(--color-text-dim)" }}>last sync: </span>
          <span style={{ color: "var(--color-text)" }}>
            {formatRelative(memory.last_sync)}
          </span>
          <span className="mx-2" style={{ color: "var(--color-text-dim)" }}>·</span>
          {counts(memory)}
        </div>
        <button
          type="button"
          onClick={() => void handleSync()}
          disabled={syncing}
          className="rounded px-3 py-1 font-mono text-[10px] font-bold uppercase tracking-label disabled:opacity-40"
          style={{
            backgroundColor: "var(--color-accent-soft)",
            border: "1px solid rgba(16, 185, 129, 0.55)",
            color: "var(--color-accent)",
          }}
        >
          {syncing ? "Syncing…" : "Sync now"}
        </button>
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

      <main className="flex-1 overflow-y-auto p-4 space-y-6">
        {lastSync ? <SyncResultCard result={lastSync} onApplyPrune={handleApplyPrune} /> : null}

        {scratchpad.length > 0 ? (
          <Section title={`Pending — not yet synced (${scratchpad.length})`} accent="indigo">
            <div className="py-2 text-xs text-mute">
              These notes were captured in chat and are already used in conversations.
              Hit "Sync now" to fold them into the sections below.
            </div>
            {scratchpad.map((e, i) => (
              <ScratchpadRow key={i} entry={e} />
            ))}
          </Section>
        ) : null}

        {pendingConflicts.length > 0 ? (
          <div className="space-y-2">
            <div
              className="font-mono text-[10px] font-bold uppercase tracking-label"
              style={{ color: "var(--color-warning)" }}
            >
              Conflicts ({pendingConflicts.length} pending)
            </div>
            {pendingConflicts.map((c) => (
              <ConflictCard
                key={c.id}
                conflict={c}
                busy={busyItem === `conflict/${c.id}`}
                onResolve={handleResolve}
              />
            ))}
          </div>
        ) : null}

        <Section title={`Household (${memory.household.members.length})`}>
          {memory.household.members.length === 0 ? (
            <Empty text="No household members recorded yet." />
          ) : (
            memory.household.members.map((m) => (
              <div key={m.name} className="py-2">
                <div className="text-sm">
                  <span className="font-medium">{m.name}</span>
                  <span className="ml-2 text-xs text-mute">{m.role}</span>
                </div>
                {m.notes.length > 0 ? (
                  <ul className="mt-1 ml-4 list-disc text-xs text-gray-300">
                    {m.notes.map((note, i) => (
                      <li key={i}>{note}</li>
                    ))}
                  </ul>
                ) : null}
              </div>
            ))
          )}
        </Section>

        <Section title={`Notes (${memory.notes.length})`}>
          {memory.notes.length === 0 ? (
            <Empty text="No notes yet. Ask Mylo to remember something." />
          ) : (
            memory.notes.map((n) => (
              <NoteRow
                key={n.id}
                note={n}
                busy={busyItem === `notes/${n.id}`}
                onDelete={() => handleDelete("notes", n.id)}
              />
            ))
          )}
        </Section>

        <Section title={`Known issues (${memory.known_issues.length})`}>
          {memory.known_issues.length === 0 ? (
            <Empty text="No issues tracked." />
          ) : (
            memory.known_issues.map((issue) => (
              <IssueRow
                key={issue.id}
                issue={issue}
                busy={busyItem === `known_issues/${issue.id}`}
                onDelete={() => handleDelete("known_issues", issue.id)}
              />
            ))
          )}
        </Section>

        <Section title={`Patterns (${memory.patterns.length})`}>
          {memory.patterns.length === 0 ? (
            <Empty text="No patterns observed yet." />
          ) : (
            memory.patterns.map((p) => (
              <PatternRow
                key={p.id}
                pattern={p}
                busy={busyItem === `patterns/${p.id}`}
                onDelete={() => handleDelete("patterns", p.id)}
              />
            ))
          )}
        </Section>

        <Section title={`Rejected suggestions (${memory.rejected.length})`}>
          {memory.rejected.length === 0 ? (
            <Empty text="No rejected suggestions." />
          ) : (
            memory.rejected.map((r) => (
              <RejectionRow
                key={r.id}
                rejection={r}
                busy={busyItem === `rejected/${r.id}`}
                onDelete={() => handleDelete("rejected", r.id)}
              />
            ))
          )}
        </Section>
      </main>
    </div>
  );
}

// ─── Sub-components ────────────────────────────────────────────────────────

function SyncResultCard({
  result,
  onApplyPrune,
}: {
  result: SyncResult;
  onApplyPrune: () => void;
}) {
  return (
    <div
      className="rounded border p-3"
      style={{
        borderColor: "var(--color-border-accent)",
        backgroundColor: "var(--color-accent-soft)",
      }}
    >
      <div className="flex items-center gap-2">
        <StatusDot tone="accent" />
        <span
          className="font-mono text-[10px] font-bold uppercase tracking-label"
          style={{ color: "var(--color-accent)" }}
        >
          Sync result
        </span>
      </div>
      <div
        className="mt-1.5 font-sans text-[12px]"
        style={{ color: "var(--color-text)" }}
      >
        {result.summary}
      </div>
      {result.conflicts_added > 0 ? (
        <div
          className="mt-1 font-mono text-[10px]"
          style={{ color: "var(--color-warning)" }}
        >
          {result.conflicts_added} new conflict{result.conflicts_added === 1 ? "" : "s"} — review below
        </div>
      ) : null}
      {result.prune_candidates.length > 0 ? (
        <div className="mt-3">
          <div
            className="font-mono text-[10px] uppercase tracking-label"
            style={{ color: "var(--color-text-muted)" }}
          >
            Prune candidates ({result.prune_candidates.length})
          </div>
          <ul
            className="mt-1 ml-4 list-disc font-mono text-[10px] leading-[1.55]"
            style={{ color: "var(--color-text-muted)" }}
          >
            {result.prune_candidates.slice(0, 5).map((c) => (
              <li key={`${c.section}/${c.id}`}>
                <span style={{ color: "var(--color-text)" }}>
                  {c.section}/{c.id}
                </span>
                <span className="ml-2">{c.reason}</span>
              </li>
            ))}
            {result.prune_candidates.length > 5 ? (
              <li>… and {result.prune_candidates.length - 5} more</li>
            ) : null}
          </ul>
          <button
            type="button"
            onClick={onApplyPrune}
            className="mt-2 rounded px-3 py-1 font-mono text-[10px] font-bold uppercase tracking-label"
            style={{
              backgroundColor: "var(--color-accent-soft)",
              border: "1px solid var(--color-accent)",
              color: "var(--color-accent)",
            }}
          >
            Apply prune ({result.prune_candidates.length})
          </button>
        </div>
      ) : null}
    </div>
  );
}

function Section({
  title,
  accent,
  children,
}: {
  title: string;
  accent?: "amber" | "indigo";
  children: React.ReactNode;
}) {
  const borderColor =
    accent === "amber"
      ? "rgba(229, 161, 14, 0.33)"
      : accent === "indigo"
        ? "var(--color-border-accent)"
        : "var(--color-border)";
  return (
    <section
      className="rounded border bg-surface"
      style={{ borderColor }}
    >
      <div
        className="border-b px-3 py-2 font-mono text-[10px] font-bold uppercase tracking-label"
        style={{
          borderColor: "var(--color-border)",
          color: "var(--color-text-muted)",
        }}
      >
        {title}
      </div>
      <div
        className="divide-y px-3"
        style={{
          // @ts-expect-error CSS custom prop for `divide-y` child borders.
          "--tw-divide-opacity": 1,
          borderColor: "var(--color-border)",
        }}
      >
        {children}
      </div>
    </section>
  );
}

function ScratchpadRow({ entry }: { entry: ScratchpadEntry }) {
  const scope =
    (entry.scope.entity as string | undefined) ||
    (entry.scope.area as string | undefined) ||
    (entry.scope.general ? "general" : undefined);
  return (
    <div
      className="py-2 border-b last:border-b-0"
      style={{ borderColor: "var(--color-border)" }}
    >
      <div
        className="font-sans text-[13px]"
        style={{ color: "var(--color-text)" }}
      >
        {entry.content}
      </div>
      <div
        className="mt-1 flex items-center gap-2 font-mono text-[10px]"
        style={{ color: "var(--color-text-muted)" }}
      >
        <Tag tone="default">{entry.type}</Tag>
        {scope ? <span>[{scope}]</span> : null}
        {entry.recorded ? (
          <span style={{ color: "var(--color-text-dim)" }}>
            {formatRelative(entry.recorded)}
          </span>
        ) : null}
      </div>
    </div>
  );
}

function Empty({ text }: { text: string }) {
  return (
    <div
      className="py-3 font-mono text-[11px]"
      style={{ color: "var(--color-text-muted)" }}
    >
      {text}
    </div>
  );
}

function NoteRow({
  note,
  busy,
  onDelete,
}: {
  note: MemoryNote;
  busy: boolean;
  onDelete: () => void;
}) {
  const scope = note.entity || note.area || note.scope;
  const protectedFlag =
    note.source === "user_confirmed" || note.metadata?.priority === "critical";
  return (
    <MemoryRow
      body={note.content}
      meta={
        <>
          <code style={{ color: "var(--color-text-dim)" }}>{note.id}</code>
          {scope ? <span>[{scope}]</span> : null}
          {protectedFlag ? <Tag tone="success">protected</Tag> : null}
          {note.metadata?.reference_count ? (
            <span>refs={note.metadata.reference_count}</span>
          ) : null}
        </>
      }
      busy={busy}
      onDelete={onDelete}
    />
  );
}

function IssueRow({
  issue,
  busy,
  onDelete,
}: {
  issue: MemoryIssue;
  busy: boolean;
  onDelete: () => void;
}) {
  const tone = issue.status === "active" ? "warning" : "muted";
  return (
    <MemoryRow
      body={issue.description}
      meta={
        <>
          <code style={{ color: "var(--color-text-dim)" }}>{issue.id}</code>
          <Tag tone={tone}>{issue.status}</Tag>
          {issue.suggested_fix ? (
            <span>
              <span style={{ color: "var(--color-text-dim)" }}>fix: </span>
              {issue.suggested_fix}
            </span>
          ) : null}
        </>
      }
      busy={busy}
      onDelete={onDelete}
    />
  );
}

function PatternRow({
  pattern,
  busy,
  onDelete,
}: {
  pattern: MemoryPattern;
  busy: boolean;
  onDelete: () => void;
}) {
  return (
    <MemoryRow
      body={pattern.description}
      meta={
        <>
          <code style={{ color: "var(--color-text-dim)" }}>{pattern.id}</code>
          <span>confidence={pattern.confidence.toFixed(2)}</span>
        </>
      }
      busy={busy}
      onDelete={onDelete}
    />
  );
}

function RejectionRow({
  rejection,
  busy,
  onDelete,
}: {
  rejection: MemoryRejection;
  busy: boolean;
  onDelete: () => void;
}) {
  return (
    <MemoryRow
      body={rejection.suggestion}
      meta={
        <>
          <code style={{ color: "var(--color-text-dim)" }}>{rejection.id}</code>
          {rejection.reason ? (
            <span>
              <span style={{ color: "var(--color-text-dim)" }}>reason: </span>
              {rejection.reason}
            </span>
          ) : null}
        </>
      }
      busy={busy}
      onDelete={onDelete}
    />
  );
}

function MemoryRow({
  body,
  meta,
  busy,
  onDelete,
}: {
  body: string;
  meta: React.ReactNode;
  busy: boolean;
  onDelete: () => void;
}) {
  return (
    <div
      className="flex items-start justify-between gap-3 py-2 border-b last:border-b-0"
      style={{ borderColor: "var(--color-border)" }}
    >
      <div className="min-w-0 flex-1">
        <div
          className="font-sans text-[13px]"
          style={{ color: "var(--color-text)" }}
        >
          {body}
        </div>
        <div
          className="mt-1 flex flex-wrap items-center gap-2 font-mono text-[10px]"
          style={{ color: "var(--color-text-muted)" }}
        >
          {meta}
        </div>
      </div>
      <DeleteButton onClick={onDelete} busy={busy} />
    </div>
  );
}

function ConflictCard({
  conflict,
  busy,
  onResolve,
}: {
  conflict: MemoryConflict;
  busy: boolean;
  onResolve: (id: string, choice: "a" | "b" | "dismiss") => void;
}) {
  return (
    <SeverityCard
      severity="high"
      title={conflict.type || "contradiction"}
      action={
        <code
          className="font-mono text-[10px]"
          style={{ color: "var(--color-text-dim)" }}
        >
          {conflict.id}
        </code>
      }
    >
      <div className="space-y-1.5">
        {conflict.claim_a ? (
          <div>
            <span
              className="font-mono text-[10px] font-bold mr-2"
              style={{ color: "var(--color-accent)" }}
            >
              A
            </span>
            <span
              className="font-mono text-[10px] mr-2"
              style={{ color: "var(--color-text-dim)" }}
            >
              ({conflict.claim_a.source})
            </span>
            <span style={{ color: "var(--color-text)" }}>
              {conflict.claim_a.content}
            </span>
          </div>
        ) : null}
        {conflict.claim_b ? (
          <div>
            <span
              className="font-mono text-[10px] font-bold mr-2"
              style={{ color: "var(--color-info)" }}
            >
              B
            </span>
            <span
              className="font-mono text-[10px] mr-2"
              style={{ color: "var(--color-text-dim)" }}
            >
              ({conflict.claim_b.source})
            </span>
            <span style={{ color: "var(--color-text)" }}>
              {conflict.claim_b.content}
            </span>
          </div>
        ) : null}
      </div>
      <div className="mt-3 flex gap-2">
        <ConflictButton
          disabled={busy}
          onClick={() => onResolve(conflict.id, "a")}
          tone="accent"
        >
          Keep A
        </ConflictButton>
        <ConflictButton
          disabled={busy}
          onClick={() => onResolve(conflict.id, "b")}
          tone="info"
        >
          Keep B
        </ConflictButton>
        <ConflictButton
          disabled={busy}
          onClick={() => onResolve(conflict.id, "dismiss")}
          tone="muted"
        >
          Dismiss
        </ConflictButton>
      </div>
    </SeverityCard>
  );
}

function ConflictButton({
  children,
  disabled,
  onClick,
  tone,
}: {
  children: React.ReactNode;
  disabled: boolean;
  onClick: () => void;
  tone: "accent" | "info" | "muted";
}) {
  const styles =
    tone === "accent"
      ? {
          backgroundColor: "var(--color-accent-soft)",
          border: "1px solid var(--color-accent)",
          color: "var(--color-accent)",
        }
      : tone === "info"
        ? {
            backgroundColor: "var(--color-info-soft)",
            border: "1px solid rgba(59, 130, 246, 0.55)",
            color: "var(--color-info)",
          }
        : {
            backgroundColor: "transparent",
            border: "1px solid var(--color-border)",
            color: "var(--color-text-muted)",
          };
  return (
    <button
      type="button"
      disabled={disabled}
      onClick={onClick}
      className="rounded px-3 py-1 font-mono text-[10px] font-bold uppercase tracking-label disabled:opacity-40"
      style={styles}
    >
      {children}
    </button>
  );
}

function DeleteButton({ onClick, busy }: { onClick: () => void; busy: boolean }) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={busy}
      className="shrink-0 rounded border px-2 py-1 font-mono text-[10px] font-bold uppercase tracking-label disabled:opacity-40"
      style={{
        borderColor: "var(--color-border)",
        color: "var(--color-text-muted)",
        background: "transparent",
      }}
    >
      {busy ? "…" : "Delete"}
    </button>
  );
}

// ─── Helpers ──────────────────────────────────────────────────────────────

function counts(memory: MemoryFull): React.ReactNode {
  const open = memory.conflicts.filter((c) => c.status === "pending_review").length;
  return (
    <>
      <span style={{ color: "var(--color-text)" }}>{memory.notes.length}</span>
      <span style={{ color: "var(--color-text-dim)" }}> notes · </span>
      <span style={{ color: "var(--color-text)" }}>{memory.known_issues.length}</span>
      <span style={{ color: "var(--color-text-dim)" }}> issues · </span>
      <span style={{ color: "var(--color-text)" }}>{memory.patterns.length}</span>
      <span style={{ color: "var(--color-text-dim)" }}> patterns · </span>
      <span style={{ color: open > 0 ? "var(--color-warning)" : "var(--color-text)" }}>
        {open}
      </span>
      <span style={{ color: "var(--color-text-dim)" }}> open conflicts</span>
    </>
  );
}
