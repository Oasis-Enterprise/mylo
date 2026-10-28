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
import { displayUserText } from "./userText";

describe("displayUserText", () => {
  it("strips the turn context and the time stamp", () => {
    const raw =
      "<turn_context>\nWORKING SET:\n- light.kitchen\n</turn_context>\n\n" +
      "[CURRENT TIME: Monday, October 27, 2026 — 9:15 PM PDT (ISO: 2026-10-27T21:15:00-07:00)] " +
      "turn off the kitchen light";
    expect(displayUserText(raw)).toBe("turn off the kitchen light");
  });
  it("strips a bare time stamp from older messages", () => {
    expect(displayUserText("[CURRENT TIME: Monday (ISO: x)] hi")).toBe("hi");
  });
  it("leaves plain text alone, including brackets the user typed", () => {
    expect(displayUserText("[note] keep this")).toBe("[note] keep this");
    expect(displayUserText("what is <turn_context>?")).toBe("what is <turn_context>?");
  });
});
