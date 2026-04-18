import type { CatchupData } from "../api";

interface Props {
  data: CatchupData;
  onDismiss: () => void;
}

// Injected as a divider between old messages and the input area.
// Not a message from Mylo — a lightweight status block that gives
// context without pretending the old conversation didn't happen.
export function CatchupBanner({ data, onDismiss }: Props) {
  if (!data.show_banner) return null;

  return (
    <div className="my-4">
      <div
        className="flex items-center gap-3 text-center"
        style={{ color: "var(--color-text-dim)" }}
      >
        <div
          className="flex-1 h-px"
          style={{ backgroundColor: "var(--color-border)" }}
        />
        <span className="font-mono text-[9px] uppercase tracking-label shrink-0">
          {data.gap_label}
        </span>
        <div
          className="flex-1 h-px"
          style={{ backgroundColor: "var(--color-border)" }}
        />
      </div>
      <div
        className="mt-3 rounded border px-4 py-3"
        style={{
          borderColor: "var(--color-border)",
          backgroundColor: "var(--color-surface)",
        }}
      >
        <div
          className="font-mono text-[10px] uppercase tracking-label mb-2"
          style={{ color: "var(--color-text-dim)" }}
        >
          Since we last talked
        </div>
        <ul className="space-y-1">
          {(data.lines || []).map((line, i) => (
            <li
              key={i}
              className="flex items-start gap-2 font-sans text-[12.5px]"
              style={{ color: "var(--color-text-muted)" }}
            >
              <span
                className="mt-1.5 shrink-0 h-[4px] w-[4px] rounded-full"
                style={{ backgroundColor: "var(--color-accent)" }}
              />
              {line}
            </li>
          ))}
        </ul>
        <div className="mt-3 flex items-center justify-between">
          <span
            className="font-sans text-[12.5px]"
            style={{ color: "var(--color-text)" }}
          >
            What are you working on?
          </span>
          <button
            type="button"
            onClick={onDismiss}
            className="font-mono text-[9px] uppercase tracking-label"
            style={{ color: "var(--color-text-dim)" }}
          >
            dismiss
          </button>
        </div>
      </div>
    </div>
  );
}
