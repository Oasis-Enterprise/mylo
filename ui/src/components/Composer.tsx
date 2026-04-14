import { useRef, useState } from "react";

interface Props {
  disabled?: boolean;
  onSubmit: (message: string) => void | Promise<void>;
}

export function Composer({ disabled, onSubmit }: Props) {
  const [text, setText] = useState("");
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  async function submit() {
    const trimmed = text.trim();
    if (!trimmed || disabled) return;
    setText("");
    await onSubmit(trimmed);
    // Refocus for fast iteration.
    textareaRef.current?.focus();
  }

  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        void submit();
      }}
      className="border-t border-border bg-elevated p-3"
    >
      <div className="flex items-end gap-2">
        <textarea
          ref={textareaRef}
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            // Enter to send, Shift+Enter for newline — standard.
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              void submit();
            }
          }}
          placeholder={
            disabled ? "Waiting for response…" : "Ask Mylo about your home…"
          }
          disabled={disabled}
          rows={2}
          className="flex-1 resize-none rounded bg-surface border border-border px-3 py-2 text-sm outline-none focus:border-indigo-500/60 disabled:opacity-60"
        />
        <button
          type="submit"
          disabled={disabled || !text.trim()}
          className="rounded bg-indigo-600 px-3 py-2 text-sm font-medium hover:bg-indigo-500 disabled:opacity-40"
        >
          Send
        </button>
      </div>
    </form>
  );
}
