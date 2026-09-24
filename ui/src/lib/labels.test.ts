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
import { tierLabel, toolLabel } from "./labels";

describe("labels", () => {
  it("uses the table when present", () => {
    expect(toolLabel("query_entities", { query_entities: { label: "Looking at your devices", tier: 1 } })).toBe("Looking at your devices");
  });
  it("humanizes unknown names", () => {
    expect(toolLabel("query_entities")).toBe("Query entities");
    expect(toolLabel("apply_dashboard_plan", {})).toBe("Apply dashboard plan");
  });
  it("maps tiers from numbers and legacy strings", () => {
    expect(tierLabel(1)).toBe("Read-only");
    expect(tierLabel(2)).toBe("Changes config");
    expect(tierLabel(3)).toBe("Controls devices");
    expect(tierLabel("TIER-3")).toBe("Controls devices");
    expect(tierLabel("tier-2")).toBe("Changes config");
    expect(tierLabel("weird")).toBe("Changes config");
  });
});
