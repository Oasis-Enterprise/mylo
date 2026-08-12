// Copyright 2026 Maxwell Monson / Oasis Enterprise LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

import { useEffect, useState } from "react";
import { fetchStatus, type ServerStatus } from "../api";
import { formatRelative, formatTokens } from "../lib/format";
import { useSession } from "../store";
import { StatusDot } from "./StatusDot";
import { Tag } from "./Tag";

export type Tab = "chat" | "memory" | "activity";

interface Props {
  tab: Tab;
  onChange: (tab: Tab) => void;
  onNewConversation?: () => void;
  // Toggles the inline findings panel in the chat view.
  onToggleFindings?: () => void;
  // Bump to force an immediate status re-poll (e.g. after a dismissal
  // changed the findings count).
  refreshSignal?: number;
}

// Top row: status dot, MYLO wordmark, version, right-aligned tabs.
// Bottom row: mono 9px status bar with entity/automation counts,
// memory sync age, and session-level token + turn counters. The
// status endpoint is polled every 30s and right after mount so the
// memory sync string stays fresh without a WebSocket.
export function Header({
  tab,
  onChange,
  onNewConversation,
  onToggleFindings,
  refreshSignal = 0,
}: Props) {
  const [status, setStatus] = useState<ServerStatus | null>(null);
  const turns = useSession((s) => s.turns);
  const inputTokens = useSession((s) => s.inputTokens);
  const outputTokens = useSession((s) => s.outputTokens);

  useEffect(() => {
    let cancelled = false;
    async function tick() {
      try {
        const s = await fetchStatus();
        if (!cancelled) setStatus(s);
      } catch {
        // Offline or server restarting — keep last-known state.
      }
    }
    void tick();
    const interval = window.setInterval(tick, 30_000);
    return () => {
      cancelled = true;
      window.clearInterval(interval);
    };
  }, [refreshSignal]);

  const totalSessionTokens = inputTokens + outputTokens;

  return (
    <header className="border-b border-border bg-surface">
      <div className="flex items-center justify-between px-4 pt-3 pb-2">
        <div className="flex items-center gap-2.5">
          <StatusDot tone="accent" pulse={status !== null} />
          <span
            className="font-mono font-extrabold text-[13px] tracking-wordmark"
            style={{ color: "var(--color-text)" }}
          >
            MYLO
          </span>
          <Tag tone="muted">V{status?.version ?? "..."}</Tag>
        </div>
        <nav className="flex items-center gap-1">
          <TabButton active={tab === "chat"} onClick={() => onChange("chat")}>
            Chat
          </TabButton>
          <TabButton active={tab === "memory"} onClick={() => onChange("memory")}>
            Memory
          </TabButton>
          <TabButton active={tab === "activity"} onClick={() => onChange("activity")}>
            Activity
          </TabButton>
          {onNewConversation ? (
            <>
              <span
                className="mx-1 h-3 w-px"
                style={{ backgroundColor: "var(--color-border)" }}
              />
              <button
                type="button"
                onClick={onNewConversation}
                className="font-mono text-[10px] tracking-label px-2 py-1"
                style={{ color: "var(--color-text-dim)" }}
                title="Archive this conversation and start fresh"
              >
                + New
              </button>
            </>
          ) : null}
        </nav>
      </div>
      <div className="px-4 pb-2">
        <div
          className="font-mono text-[9px] leading-none flex items-center gap-2 flex-wrap"
          style={{ color: "var(--color-text-muted)" }}
        >
          <StatusField label="entities" value={status ? String(status.entities) : "—"} />
          <Sep />
          <StatusField
            label="automations"
            value={status ? String(status.automations) : "—"}
          />
          <Sep />
          <StatusField
            label="memory"
            value={
              status ? (
                <span style={{ color: "var(--color-accent)" }}>
                  synced {formatRelative(status.memory.last_sync)}
                </span>
              ) : (
                "—"
              )
            }
          />
          {status && status.memory.pending_conflicts > 0 ? (
            <>
              <Sep />
              <StatusField
                label=""
                value={
                  <span style={{ color: "var(--color-warning)" }}>
                    {status.memory.pending_conflicts} conflict
                    {status.memory.pending_conflicts === 1 ? "" : "s"}
                  </span>
                }
              />
            </>
          ) : null}
          {status && (status.memory.findings ?? 0) > 0 ? (
            <>
              <Sep />
              <button
                type="button"
                onClick={onToggleFindings}
                title="Show findings"
                className="font-mono text-[9px] leading-none"
                style={{ color: "var(--color-accent)" }}
              >
                {status.memory.findings} finding
                {status.memory.findings === 1 ? "" : "s"}
              </button>
            </>
          ) : null}
          <Sep />
          <StatusField
            label="session"
            value={`${turns} turns · ${formatTokens(totalSessionTokens)} tok`}
          />
        </div>
      </div>
    </header>
  );
}

function TabButton({
  active,
  children,
  onClick,
}: {
  active: boolean;
  children: React.ReactNode;
  onClick: () => void;
}) {
  const base =
    "font-sans text-[12px] font-medium px-3 py-1 rounded-tag border transition-colors";
  const activeStyles =
    "text-accent bg-accent-soft";
  const inactiveStyles =
    "text-muted hover:text-text border-transparent";
  return (
    <button
      type="button"
      onClick={onClick}
      className={`${base} ${active ? activeStyles : inactiveStyles}`}
      style={
        active
          ? { borderColor: "var(--color-border-accent)" }
          : undefined
      }
    >
      {children}
    </button>
  );
}

function StatusField({
  label,
  value,
}: {
  label: string;
  value: React.ReactNode;
}) {
  return (
    <span className="inline-flex items-center gap-1.5">
      {label ? (
        <span style={{ color: "var(--color-text-dim)" }}>{label}</span>
      ) : null}
      <span style={{ color: "var(--color-text)" }}>{value}</span>
    </span>
  );
}

function Sep() {
  return <span style={{ color: "var(--color-text-dim)" }}>·</span>;
}
