import { useCallback, useEffect, useState } from "react";
import {
  applyPrune,
  deleteMemoryItem,
  fetchMemoryFull,
  fetchScratchpad,
  resolveConflict,
  syncMemory,
} from "../api";
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
    return <div className="p-4 text-sm text-mute">Loading memory…</div>;
  }
  if (error && !memory) {
    return <div className="p-4 text-sm text-rose-400">Error: {error}</div>;
  }
  if (!memory) return null;

  const pendingConflicts = memory.conflicts.filter((c) => c.status === "pending_review");

  return (
    <div className="flex h-full flex-col overflow-hidden">
      <header className="flex items-center justify-between border-b border-border bg-elevated px-4 py-3">
        <div>
          <div className="text-sm font-medium">Memory</div>
          <div className="text-xs text-mute">
            Last sync: {memory.last_sync ? formatDate(memory.last_sync) : "never"}
            {" · "}
            {counts(memory)}
          </div>
        </div>
        <button
          type="button"
          onClick={() => void handleSync()}
          disabled={syncing}
          className="rounded bg-indigo-600 px-3 py-1 text-xs font-medium hover:bg-indigo-500 disabled:opacity-40"
        >
          {syncing ? "Syncing…" : "Sync now"}
        </button>
      </header>

      {error ? (
        <div className="border-b border-rose-900/40 bg-rose-950/40 px-4 py-2 text-xs text-rose-300">
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
          <Section title={`Conflicts (${pendingConflicts.length} pending)`} accent="amber">
            {pendingConflicts.map((c) => (
              <ConflictRow
                key={c.id}
                conflict={c}
                busy={busyItem === `conflict/${c.id}`}
                onResolve={handleResolve}
              />
            ))}
          </Section>
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
    <div className="rounded border border-indigo-500/30 bg-indigo-950/30 p-3 text-sm">
      <div className="font-medium text-indigo-200">Sync result</div>
      <div className="mt-1 text-xs text-indigo-100">{result.summary}</div>
      {result.conflicts_added > 0 ? (
        <div className="mt-1 text-xs text-amber-300">
          {result.conflicts_added} new conflict(s) — review below
        </div>
      ) : null}
      {result.prune_candidates.length > 0 ? (
        <div className="mt-2">
          <div className="text-xs text-indigo-100">
            Prune candidates ({result.prune_candidates.length}):
          </div>
          <ul className="mt-1 ml-4 list-disc text-xs text-mute">
            {result.prune_candidates.slice(0, 5).map((c) => (
              <li key={`${c.section}/${c.id}`}>
                <span className="text-gray-300">{c.section}/{c.id}</span>
                <span className="ml-2 text-mute">{c.reason}</span>
              </li>
            ))}
            {result.prune_candidates.length > 5 ? (
              <li>… and {result.prune_candidates.length - 5} more</li>
            ) : null}
          </ul>
          <button
            type="button"
            onClick={onApplyPrune}
            className="mt-2 rounded bg-indigo-600 px-3 py-1 text-xs font-medium hover:bg-indigo-500"
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
  const border =
    accent === "amber"
      ? "border-amber-500/40"
      : accent === "indigo"
        ? "border-indigo-500/40"
        : "border-border";
  return (
    <section className={`rounded border ${border} bg-elevated`}>
      <div className="border-b border-border px-3 py-2 text-xs font-semibold text-gray-200">
        {title}
      </div>
      <div className="divide-y divide-border px-3">{children}</div>
    </section>
  );
}

function ScratchpadRow({ entry }: { entry: ScratchpadEntry }) {
  const scope =
    (entry.scope.entity as string | undefined) ||
    (entry.scope.area as string | undefined) ||
    (entry.scope.general ? "general" : undefined);
  return (
    <div className="py-2">
      <div className="text-sm text-gray-100">{entry.content}</div>
      <div className="mt-0.5 text-xs text-mute">
        <span className="rounded bg-indigo-900/50 px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-indigo-200">
          {entry.type}
        </span>
        {scope ? <span className="ml-2">[{scope}]</span> : null}
        {entry.recorded ? <span className="ml-2">{entry.recorded}</span> : null}
      </div>
    </div>
  );
}

function Empty({ text }: { text: string }) {
  return <div className="py-3 text-xs text-mute">{text}</div>;
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
    <div className="flex items-start justify-between gap-3 py-2">
      <div className="min-w-0 flex-1">
        <div className="text-sm text-gray-100">{note.content}</div>
        <div className="mt-0.5 text-xs text-mute">
          <code className="text-mute">{note.id}</code>
          {scope ? <span className="ml-2">[{scope}]</span> : null}
          {protectedFlag ? <span className="ml-2 text-emerald-400">protected</span> : null}
          {note.metadata?.reference_count ? (
            <span className="ml-2">refs={note.metadata.reference_count}</span>
          ) : null}
        </div>
      </div>
      <DeleteButton onClick={onDelete} busy={busy} />
    </div>
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
  return (
    <div className="flex items-start justify-between gap-3 py-2">
      <div className="min-w-0 flex-1">
        <div className="text-sm text-gray-100">{issue.description}</div>
        <div className="mt-0.5 text-xs text-mute">
          <code>{issue.id}</code>
          <span className="ml-2">{issue.status}</span>
          {issue.suggested_fix ? (
            <span className="ml-2">fix: {issue.suggested_fix}</span>
          ) : null}
        </div>
      </div>
      <DeleteButton onClick={onDelete} busy={busy} />
    </div>
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
    <div className="flex items-start justify-between gap-3 py-2">
      <div className="min-w-0 flex-1">
        <div className="text-sm text-gray-100">{pattern.description}</div>
        <div className="mt-0.5 text-xs text-mute">
          <code>{pattern.id}</code>
          <span className="ml-2">confidence={pattern.confidence.toFixed(2)}</span>
        </div>
      </div>
      <DeleteButton onClick={onDelete} busy={busy} />
    </div>
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
    <div className="flex items-start justify-between gap-3 py-2">
      <div className="min-w-0 flex-1">
        <div className="text-sm text-gray-100">{rejection.suggestion}</div>
        <div className="mt-0.5 text-xs text-mute">
          <code>{rejection.id}</code>
          {rejection.reason ? <span className="ml-2">reason: {rejection.reason}</span> : null}
        </div>
      </div>
      <DeleteButton onClick={onDelete} busy={busy} />
    </div>
  );
}

function ConflictRow({
  conflict,
  busy,
  onResolve,
}: {
  conflict: MemoryConflict;
  busy: boolean;
  onResolve: (id: string, choice: "a" | "b" | "dismiss") => void;
}) {
  return (
    <div className="py-3">
      <div className="text-xs text-amber-300">
        <code>{conflict.id}</code>
        <span className="ml-2">{conflict.type}</span>
      </div>
      <div className="mt-1 space-y-1 text-sm">
        {conflict.claim_a ? (
          <div>
            <span className="text-emerald-300">A</span>
            <span className="ml-2 text-mute">({conflict.claim_a.source})</span>:
            <span className="ml-2">{conflict.claim_a.content}</span>
          </div>
        ) : null}
        {conflict.claim_b ? (
          <div>
            <span className="text-sky-300">B</span>
            <span className="ml-2 text-mute">({conflict.claim_b.source})</span>:
            <span className="ml-2">{conflict.claim_b.content}</span>
          </div>
        ) : null}
      </div>
      <div className="mt-2 flex gap-2">
        <button
          type="button"
          disabled={busy}
          onClick={() => onResolve(conflict.id, "a")}
          className="rounded bg-emerald-700 px-2 py-1 text-xs hover:bg-emerald-600 disabled:opacity-40"
        >
          Keep A
        </button>
        <button
          type="button"
          disabled={busy}
          onClick={() => onResolve(conflict.id, "b")}
          className="rounded bg-sky-700 px-2 py-1 text-xs hover:bg-sky-600 disabled:opacity-40"
        >
          Keep B
        </button>
        <button
          type="button"
          disabled={busy}
          onClick={() => onResolve(conflict.id, "dismiss")}
          className="rounded border border-border px-2 py-1 text-xs text-mute hover:text-gray-200 disabled:opacity-40"
        >
          Dismiss
        </button>
      </div>
    </div>
  );
}

function DeleteButton({ onClick, busy }: { onClick: () => void; busy: boolean }) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={busy}
      className="shrink-0 rounded border border-border px-2 py-1 text-xs text-mute hover:text-rose-400 disabled:opacity-40"
    >
      {busy ? "…" : "Delete"}
    </button>
  );
}

// ─── Helpers ──────────────────────────────────────────────────────────────

function counts(memory: MemoryFull): string {
  const parts = [
    `${memory.notes.length} notes`,
    `${memory.known_issues.length} issues`,
    `${memory.patterns.length} patterns`,
    `${memory.conflicts.filter((c) => c.status === "pending_review").length} open conflicts`,
  ];
  return parts.join(", ");
}

function formatDate(iso: string): string {
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}
