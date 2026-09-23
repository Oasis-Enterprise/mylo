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

import { describe, expect, it } from "vitest";
import type { ChatItem, ToolCallRecord } from "../types";
import { deriveTurnState } from "./turnState";

function tool(partial: Partial<ToolCallRecord> & { name: string }): ToolCallRecord {
  return { id: partial.id ?? "t1", input: {}, state: "ok", ...partial };
}

function assistant(calls: ToolCallRecord[], text = ""): ChatItem {
  return {
    id: Math.random().toString(36).slice(2),
    role: "assistant",
    pending: false,
    fragments: [
      ...(text ? [{ kind: "text" as const, text }] : []),
      ...calls.map((call) => ({ kind: "tool" as const, call })),
    ],
  };
}

function user(text: string): ChatItem {
  return { id: "u", role: "user", pending: false, fragments: [{ kind: "text", text }] };
}

describe("deriveTurnState", () => {
  it("is empty for no items", () => {
    expect(deriveTurnState([])).toEqual({
      pendingApproval: null,
      approvalContexts: [],
      pendingQuestion: null,
    });
  });

  it("finds a preview in the last assistant turn", () => {
    const items = [user("rename it"), assistant([tool({ name: "rename_entities", data: { preview: true } })])];
    const st = deriveTurnState(items);
    expect(st.pendingApproval).toEqual({ planIds: [] });
    expect(st.approvalContexts).toHaveLength(1);
    expect(st.approvalContexts[0].tierLabel).toBe("TIER-2");
  });

  it("clears once the user has replied", () => {
    const items = [
      assistant([tool({ name: "rename_entities", data: { preview: true } })]),
      user("no thanks"),
    ];
    expect(deriveTurnState(items).pendingApproval).toBeNull();
    expect(deriveTurnState(items).approvalContexts).toEqual([]);
  });

  it("treats confirmation_required as pending", () => {
    const items = [assistant([tool({ name: "call_service", state: "error", errorCode: "confirmation_required" })])];
    expect(deriveTurnState(items).pendingApproval).not.toBeNull();
  });

  it("collects plan and card ids", () => {
    const items = [
      assistant([
        tool({ id: "a", name: "plan_dashboard", data: { preview: true, plan_id: "p1", plan: { summary: "s" } } }),
        tool({ id: "b", name: "stage_custom_card", data: { preview: true, card_id: "c1", element: "mylo-x" } }),
      ]),
    ];
    expect(deriveTurnState(items).pendingApproval).toEqual({ planIds: ["p1", "c1"] });
  });

  it("derives a pending question from ask_user", () => {
    const items = [
      assistant([
        tool({
          name: "ask_user",
          data: { await_user_input: true, question: "Which room?", options: [{ label: "Kitchen" }, { label: "Den" }] },
        }),
      ]),
    ];
    expect(deriveTurnState(items).pendingQuestion).toEqual({
      question: "Which room?",
      options: [{ label: "Kitchen", value: undefined, description: undefined }, { label: "Den", value: undefined, description: undefined }],
      allowFreeText: true,
    });
  });

  it("clears the question once answered", () => {
    const items = [
      assistant([tool({ name: "ask_user", data: { await_user_input: true, question: "Q", options: [] } })]),
      user("Kitchen"),
    ];
    expect(deriveTurnState(items).pendingQuestion).toBeNull();
  });
});
