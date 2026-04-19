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
