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
import { contextWindowFor, estimateCost } from "./cost";

describe("cost", () => {
  it("prices a known model", () => {
    expect(estimateCost({ input_tokens: 1_000_000 }, "claude-sonnet-4-6")).toBeCloseTo(3);
  });
  it("prices unknown ollama models at zero", () => {
    expect(estimateCost({ input_tokens: 1_000_000 }, "gemma3:27b", "ollama")).toBe(0);
  });
  it("falls back to sonnet for unknown cloud models", () => {
    expect(estimateCost({ input_tokens: 1_000_000 }, "mystery", "anthropic")).toBeCloseTo(3);
  });
  it("knows context windows", () => {
    expect(contextWindowFor("claude-sonnet-4-6")).toBe(200_000);
    expect(contextWindowFor("gpt-4o")).toBe(128_000);
    expect(contextWindowFor("gemini-2.5-pro")).toBe(1_000_000);
    expect(contextWindowFor("anything")).toBe(128_000);
  });
});
