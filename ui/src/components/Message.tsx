import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { ChatItem } from "../types";
import { ThinkingIndicator } from "./ThinkingIndicator";
import { ToolCallBlock } from "./ToolCallBlock";

interface Props {
  item: ChatItem;
}

// User: right-aligned bubble with a subtle border and userBubble
// background, max 85% width, 60px left-padding on the container.
// Agent: no bubble — prose flows in the container with 40px right
// padding. This asymmetry is deliberate: user turns are finite
// utterances, agent turns are running commentary.
export function Message({ item }: Props) {
  const isUser = item.role === "user";
  if (isUser) {
    return (
      <div className="flex justify-end" style={{ paddingLeft: 60 }}>
        <div
          className="max-w-[85%] rounded border px-3 py-[7px] font-sans text-[13px] leading-[1.5]"
          style={{
            backgroundColor: "var(--color-user-bubble)",
            borderColor: "var(--color-user-border)",
            color: "var(--color-text)",
          }}
        >
          {item.fragments.map((fragment, i) => {
            if (fragment.kind === "text") {
              return (
                <div key={i} className="prose-signal">
                  <ReactMarkdown remarkPlugins={[remarkGfm]}>
                    {fragment.text}
                  </ReactMarkdown>
                </div>
              );
            }
            return <ToolCallBlock key={i} call={fragment.call} />;
          })}
        </div>
      </div>
    );
  }

  return (
    <div style={{ paddingRight: 40 }}>
      {item.fragments.map((fragment, i) => {
        if (fragment.kind === "text") {
          return (
            <div key={i} className="prose-signal">
              <ReactMarkdown remarkPlugins={[remarkGfm]}>
                {fragment.text}
              </ReactMarkdown>
            </div>
          );
        }
        return <ToolCallBlock key={i} call={fragment.call} />;
      })}
      {item.pending ? <ThinkingIndicator /> : null}
    </div>
  );
}
