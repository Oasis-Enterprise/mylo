import { useState } from "react";
import type { ToolCallRecord } from "../types";

interface Props {
  call: ToolCallRecord;
}

export function ToolCallBlock({ call }: Props) {
  const [open, setOpen] = useState(false);

  const indicator =
    call.state === "pending" ? "·" : call.state === "ok" ? "✓" : "✗";
  const color =
    call.state === "pending"
      ? "text-mute"
      : call.state === "ok"
        ? "text-emerald-400"
        : "text-rose-400";

  return (
    <div className="my-1 font-mono text-xs">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center gap-2 text-left text-mute hover:text-gray-200"
      >
        <span className={color}>{indicator}</span>
        <span>{call.name}</span>
        {call.summary ? <span className="text-mute">— {call.summary}</span> : null}
        {call.errorCode ? (
          <span className="text-rose-400">— {call.errorCode}</span>
        ) : null}
        <span className="ml-auto text-mute">{open ? "hide" : "details"}</span>
      </button>
      {open ? (
        <pre className="mt-1 overflow-auto rounded border border-border bg-elevated p-2 text-mute">
          {JSON.stringify(call.input, null, 2)}
        </pre>
      ) : null}
    </div>
  );
}
