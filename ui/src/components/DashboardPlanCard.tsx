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

import { useMemo, useState } from "react";
import type { ReactNode } from "react";
import { lineDiff } from "../lib/lineDiff";
import type {
  DashboardPlanData,
  PlanFingerprint,
  PlanOpData,
  PlanSectionData,
  StagedCardData,
} from "../types";
import { StatusDot } from "./StatusDot";
import { Tag } from "./Tag";

export interface DashboardPlanCardProps {
  plans: DashboardPlanData[];
  // Staged custom cards in the same turn — rendered after the plan's
  // operations, before the assumptions block.
  cards?: StagedCardData[];
  // Non-dashboard previews in the same turn, described in one line each.
  otherChanges: string[];
  onApprove: () => void;
  onReject: () => void;
  onModify: () => void;
  applying?: boolean;
}

// Wireframe preview of a dashboard plan: one block per operation, each
// section drawn as a box with its heading and card chips, then the
// assumptions the model made and any lint warnings. Apply sends the
// plan ids back; Modify pre-fills the composer.
export function DashboardPlanCard({
  plans,
  cards = [],
  otherChanges,
  onApprove,
  onReject,
  onModify,
  applying = false,
}: DashboardPlanCardProps) {
  const [showYaml, setShowYaml] = useState(false);
  const assumptions = plans.flatMap((p) => p.assumptions);
  const warnings = plans.flatMap((p) => p.issues.filter((i) => i.severity === "warning"));

  return (
    <div
      className="rounded border"
      style={{ borderColor: "var(--color-border-accent)", borderWidth: 1 }}
    >
      <div
        className="flex items-center gap-2 px-3 py-2 border-b"
        style={{ borderColor: "var(--color-border)" }}
      >
        <StatusDot tone="accent" pulse />
        <span
          className="font-mono text-[10px] font-bold uppercase tracking-label"
          style={{ color: "var(--color-accent)" }}
        >
          {plans.length === 0 ? "Custom card" : "Dashboard plan"}
        </span>
        <span className="font-sans text-[12px]" style={{ color: "var(--color-text)" }}>
          {[...plans.map((p) => p.summary), ...cards.map((c) => c.description)].join(" · ")}
        </span>
      </div>

      {plans.map((plan) => (
        <div key={plan.plan_id}>
          {plan.operations.map((op, i) => (
            <OpBlock
              key={`${plan.plan_id}-${i}`}
              op={op}
              index={i}
              fingerprint={plan.resolved.find((r) => r.op_index === i)?.fingerprint ?? null}
              dashboardId={plan.dashboard_id}
            />
          ))}
        </div>
      ))}

      {otherChanges.map((text, i) => (
        <div
          key={`other-${i}`}
          className="px-3 py-2 font-sans text-[12px] border-t"
          style={{ borderColor: "var(--color-border)", color: "var(--color-text)" }}
        >
          <Tag tone="muted">TIER-2</Tag> <span className="ml-2">{text}</span>
        </div>
      ))}

      {cards.map((card) => (
        <StagedCardBlock key={card.card_id} card={card} />
      ))}

      {assumptions.length > 0 ? (
        <div className="px-3 py-2 border-t" style={{ borderColor: "var(--color-border)" }}>
          <div
            className="font-mono text-[10px] uppercase tracking-label mb-1"
            style={{ color: "var(--color-text-dim)" }}
          >
            Mylo assumed
          </div>
          <ul
            className="font-sans text-[12px] list-disc pl-4"
            style={{ color: "var(--color-text)" }}
          >
            {assumptions.map((a, i) => (
              <li key={i}>{a}</li>
            ))}
          </ul>
        </div>
      ) : null}

      {warnings.length > 0 ? (
        <div className="px-3 py-2 border-t" style={{ borderColor: "var(--color-border)" }}>
          <div
            className="font-mono text-[10px] uppercase tracking-label mb-1"
            style={{ color: "var(--color-warning)" }}
          >
            Warnings
          </div>
          {warnings.map((w, i) => (
            <div
              key={i}
              className="font-mono text-[10px]"
              style={{ color: "var(--color-text-muted)" }}
            >
              {w.code}: {w.message}
            </div>
          ))}
        </div>
      ) : null}

      {showYaml ? (
        <pre
          className="px-3 py-2 border-t font-mono text-[10px] overflow-x-auto"
          style={{ borderColor: "var(--color-border)", color: "var(--color-text-muted)" }}
        >
          {plans.map((p) => toYaml(p.operations)).join("\n---\n")}
        </pre>
      ) : null}

      <div
        className="flex items-center justify-end gap-2 px-3 py-2 border-t"
        style={{ borderColor: "var(--color-border)" }}
      >
        <button type="button" onClick={onReject} className={ghostBtn} style={ghostStyle}>
          Reject
        </button>
        <button type="button" onClick={onModify} className={ghostBtn} style={ghostStyle}>
          Modify
        </button>
        {plans.length > 0 ? (
          <button
            type="button"
            onClick={() => setShowYaml((v) => !v)}
            className={ghostBtn}
            style={ghostStyle}
          >
            {showYaml ? "Hide YAML" : "Show YAML"}
          </button>
        ) : null}
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

const ghostBtn =
  "rounded border px-3 py-1 font-mono text-[11px] font-bold uppercase tracking-label hover:opacity-80";
const ghostStyle = {
  borderColor: "var(--color-border)",
  color: "var(--color-text-muted)",
  background: "transparent",
} as const;

// ─── Custom cards ────────────────────────────────────────────────────────────

function StagedCardBlock({ card }: { card: StagedCardData }) {
  const [showSource, setShowSource] = useState(false);
  const { diff, added, removed } = useMemo(() => {
    const d =
      card.action === "update" && card.previous_source !== null
        ? lineDiff(card.previous_source, card.source)
        : null;
    return {
      diff: d,
      added: d ? d.filter((l) => l.kind === "add").length : card.line_count,
      removed: d ? d.filter((l) => l.kind === "del").length : 0,
    };
  }, [card.action, card.previous_source, card.source, card.line_count]);
  return (
    <div className="px-3 py-3 space-y-2 border-t" style={{ borderColor: "var(--color-border)" }}>
      <div className="flex items-center gap-2">
        <Tag tone="muted">CUSTOM CARD</Tag>
        <span className="font-mono text-[11px] font-bold" style={{ color: "var(--color-accent)" }}>
          custom:{card.element}
        </span>
        <span className="font-mono text-[10px]" style={{ color: "var(--color-text-dim)" }}>
          {card.action === "update" ? `update · +${added} −${removed}` : `new · ${card.line_count} lines`}
        </span>
      </div>
      <div className="font-sans text-[12px]" style={{ color: "var(--color-text)" }}>{card.description}</div>
      {card.config_example ? (
        <pre className="font-mono text-[10px] overflow-x-auto" style={{ color: "var(--color-text-muted)" }}>
          {toYaml(card.config_example)}
        </pre>
      ) : null}
      {card.warnings.map((w, i) => (
        <div key={i} className="font-mono text-[10px]" style={{ color: "var(--color-warning)" }}>
          {w.code}: {w.message}
        </div>
      ))}
      <button type="button" onClick={() => setShowSource((v) => !v)} className={ghostBtn} style={ghostStyle}>
        {showSource ? "Hide source" : "Show source"}
      </button>
      {showSource ? (
        <pre className="font-mono text-[10px] overflow-x-auto rounded border px-2 py-1.5"
             style={{ borderColor: "var(--color-border)", backgroundColor: "var(--color-surface)" }}>
          {diff
            ? diff.map((l, i) => (
                <div key={i} style={{ color: l.kind === "add" ? "var(--color-accent)" : l.kind === "del" ? "var(--color-warning)" : "var(--color-text-muted)" }}>
                  {l.kind === "add" ? "+ " : l.kind === "del" ? "− " : "  "}{l.text}
                </div>
              ))
            : card.source}
        </pre>
      ) : null}
    </div>
  );
}

// ─── Operation blocks ────────────────────────────────────────────────────────

function OpBlock({
  op,
  index,
  fingerprint,
  dashboardId,
}: {
  op: PlanOpData;
  index: number;
  fingerprint: PlanFingerprint | null;
  dashboardId: string | null;
}) {
  const view = str(op.view_path ?? op.path);
  const where = dashboardId ? `${dashboardId}/${view}` : view;
  const sectionOf = (key: string) => {
    const v = op[key];
    return v === undefined || v === null
      ? null
      : typeof v === "number"
        ? `section ${v}`
        : `"${str(v)}"`;
  };
  const target = fingerprint
    ? `${fingerprint.type}${fingerprint.entity ? ` · ${fingerprint.entity}` : ""}`
    : `card #${str(op.card_index)}`;
  const destructive =
    op.op === "remove_card" || op.op === "remove_section" || op.op === "delete_view";

  let title: string;
  let body: ReactNode = null;
  switch (op.op) {
    case "create_view": {
      const sections = (op.sections as PlanSectionData[] | undefined) ?? [];
      const layout = str(op.layout ?? "sections");
      const sectionsSuffix =
        layout === "sections"
          ? ` · ${sections.length} section${sections.length === 1 ? "" : "s"}`
          : "";
      title =
        `Create view "${str(op.title)}" (/${view}) · ${layout}${sectionsSuffix}` +
        positionSuffix(op.position, "view");
      body =
        layout === "sections" ? (
          <div className="space-y-1.5">
            {sections.map((s, i) => (
              <SectionBox key={i} section={s} />
            ))}
          </div>
        ) : (
          <CardChips cards={(op.cards as Record<string, unknown>[] | undefined) ?? []} />
        );
      break;
    }
    case "add_section": {
      const section = op.section as PlanSectionData;
      title =
        `Add section "${section.heading}" to ${where}` + positionSuffix(op.position, "section");
      body = <SectionBox section={section} />;
      break;
    }
    case "add_cards": {
      const cards = (op.cards as Record<string, unknown>[] | undefined) ?? [];
      const sectionSuffix = sectionOf("section") ? ` › ${sectionOf("section")}` : "";
      title =
        `Add ${cards.length} card${cards.length === 1 ? "" : "s"} to ${where}${sectionSuffix}` +
        positionSuffix(op.position, "card");
      body = <CardChips cards={cards} />;
      break;
    }
    case "replace_card": {
      const card = op.card as Record<string, unknown>;
      const sectionSuffix = sectionOf("section") ? ` › ${sectionOf("section")}` : "";
      title = `Replace ${target} in ${where}${sectionSuffix} with ${chipLabel(card)}`;
      break;
    }
    case "remove_card": {
      const sectionSuffix = sectionOf("section") ? ` › ${sectionOf("section")}` : "";
      title = `Remove ${target} from ${where}${sectionSuffix}`;
      break;
    }
    case "move_card": {
      const fromSuffix = sectionOf("from_section") ? ` › ${sectionOf("from_section")}` : "";
      const toSection = sectionOf("to_section") ?? sectionOf("from_section") ?? "same section";
      title =
        `Move ${target} from ${where}${fromSuffix} to ${toSection}` +
        positionSuffix(op.position, "card");
      break;
    }
    case "update_view_meta": {
      const changes = ["title", "icon", "theme", "max_columns", "new_path"]
        .filter((k) => op[k] !== undefined && op[k] !== null)
        .map((k) => `${k} → ${str(op[k])}`);
      title = `Update view ${where}: ${changes.join(", ") || "no changes"}`;
      break;
    }
    case "remove_section":
      title = `Remove section ${sectionOf("section")} from ${where}`;
      break;
    case "delete_view":
      title = `Delete view ${where}`;
      break;
    default:
      title = `${op.op} on ${where}`;
  }

  return (
    <div
      className="px-3 py-3 space-y-2"
      style={index > 0 ? { borderTop: "1px solid var(--color-border)" } : undefined}
    >
      <div className="flex items-start gap-2">
        <span className="font-mono text-[10px] pt-0.5" style={{ color: "var(--color-text-dim)" }}>
          {index + 1}.
        </span>
        <span
          className="font-sans text-[13px]"
          style={{ color: destructive ? "var(--color-warning)" : "var(--color-text)" }}
        >
          {title}
        </span>
      </div>
      {body ? <div className="pl-5">{body}</div> : null}
    </div>
  );
}

function SectionBox({ section }: { section: PlanSectionData }) {
  return (
    <div
      className="rounded border px-2 py-1.5"
      style={{ borderColor: "var(--color-border)", backgroundColor: "var(--color-surface)" }}
    >
      <div
        className="font-mono text-[10px] font-bold uppercase tracking-label mb-1"
        style={{ color: "var(--color-text-muted)" }}
      >
        {section.heading}
        {section.column_span ? ` · spans ${section.column_span}` : ""}
      </div>
      <CardChips cards={section.cards} />
    </div>
  );
}

function CardChips({ cards }: { cards: Record<string, unknown>[] }) {
  if (cards.length === 0) {
    return (
      <div className="font-mono text-[10px]" style={{ color: "var(--color-text-dim)" }}>
        (no cards)
      </div>
    );
  }
  return (
    <div className="flex flex-wrap gap-1">
      {cards.map((card, i) => (
        <span
          key={i}
          className="rounded border px-1.5 py-0.5 font-mono text-[10px]"
          style={{
            borderColor: "var(--color-border)",
            color: "var(--color-text)",
            flexBasis: isWide(card) ? "100%" : undefined,
          }}
        >
          {chipLabel(card)}
        </span>
      ))}
    </div>
  );
}

// ─── Helpers ─────────────────────────────────────────────────────────────────

function str(v: unknown): string {
  return v === undefined || v === null ? "" : String(v);
}

function positionSuffix(position: unknown, noun: string): string {
  if (position === undefined || position === null || position === "end") return "";
  if (position === "start") return ` at the start`;
  return ` at ${noun} position ${str(position)}`;
}

function chipLabel(card: Record<string, unknown>): string {
  const type = str(card.type) || "?";
  if (type === "heading") return `heading · ${str(card.heading)}`;
  const entity = card.entity;
  if (typeof entity === "string") return `${type} · ${entity}`;
  const entities = card.entities;
  if (Array.isArray(entities) && entities.length > 0) {
    const first = entities[0];
    const id = typeof first === "string" ? first : str((first as Record<string, unknown>)?.entity);
    return `${type} · ${id}${entities.length > 1 ? ` +${entities.length - 1}` : ""}`;
  }
  if (type === "markdown") return `markdown · ${str(card.content).slice(0, 24)}`;
  return type;
}

function isWide(card: Record<string, unknown>): boolean {
  const opts = card.grid_options as Record<string, unknown> | undefined;
  if (!opts) return false;
  const cols = opts.columns;
  return cols === "full" || (typeof cols === "number" && cols >= 7);
}

// Minimal YAML for the "Show YAML" toggle — plain objects, arrays,
// strings, numbers, booleans, null. Enough for plan operations.
export function toYaml(value: unknown, indent = 0): string {
  const pad = "  ".repeat(indent);
  if (Array.isArray(value)) {
    if (value.length === 0) return `${pad}[]`;
    return value
      .map((item) => {
        if (item !== null && typeof item === "object") {
          const inner = toYaml(item, indent + 1).trimStart();
          return `${pad}- ${inner}`;
        }
        return `${pad}- ${scalar(item)}`;
      })
      .join("\n");
  }
  if (value !== null && typeof value === "object") {
    const entries = Object.entries(value as Record<string, unknown>);
    if (entries.length === 0) return `${pad}{}`;
    return entries
      .map(([k, v]) => {
        if (v !== null && typeof v === "object") {
          const isEmpty = Array.isArray(v) ? v.length === 0 : Object.keys(v).length === 0;
          if (isEmpty) return `${pad}${k}: ${Array.isArray(v) ? "[]" : "{}"}`;
          return `${pad}${k}:\n${toYaml(v, indent + 1)}`;
        }
        return `${pad}${k}: ${scalar(v)}`;
      })
      .join("\n");
  }
  return `${pad}${scalar(value)}`;
}

function scalar(v: unknown): string {
  if (v === null || v === undefined) return "null";
  if (typeof v === "string") return /^[A-Za-z0-9_./:-]+$/.test(v) && v !== "" ? v : JSON.stringify(v);
  return String(v);
}
