import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { ChatItem } from "../types";
import { ToolCallBlock } from "./ToolCallBlock";

interface Props {
  item: ChatItem;
}

export function Message({ item }: Props) {
  const isUser = item.role === "user";
  return (
    <div className={`flex ${isUser ? "justify-end" : "justify-start"}`}>
      <div
        className={`max-w-[80%] rounded-lg px-3 py-2 ${
          isUser
            ? "bg-indigo-600/20 border border-indigo-500/30"
            : "bg-elevated border border-border"
        }`}
      >
        {item.fragments.map((fragment, i) => {
          if (fragment.kind === "text") {
            return (
              <div key={i} className="prose prose-invert max-w-none text-sm">
                <ReactMarkdown remarkPlugins={[remarkGfm]}>
                  {fragment.text}
                </ReactMarkdown>
              </div>
            );
          }
          return <ToolCallBlock key={i} call={fragment.call} />;
        })}
        {item.pending ? (
          <div className="mt-1 text-xs text-mute">typing…</div>
        ) : null}
      </div>
    </div>
  );
}
