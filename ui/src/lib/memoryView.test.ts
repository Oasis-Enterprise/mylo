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
import { entriesOf, humanizeKey, sortNotesNewestFirst, stringifyValue } from "./memoryView";

describe("memoryView", () => {
  it("humanizes keys", () => {
    expect(humanizeKey("default_theme")).toBe("Default theme");
    expect(humanizeKey("wakeTime")).toBe("Wake time");
  });
  it("stringifies values", () => {
    expect(stringifyValue("x")).toBe("x");
    expect(stringifyValue(3)).toBe("3");
    expect(stringifyValue(true)).toBe("true");
    expect(stringifyValue(null)).toBe("—");
    expect(stringifyValue(["a", "b"])).toBe("a, b");
    expect(stringifyValue({ a: 1 })).toBe('{"a":1}');
  });
  it("lists entries sorted by label", () => {
    expect(entriesOf({ zeta: 1, alpha: "x" }).map((e) => e.label)).toEqual(["Alpha", "Zeta"]);
  });
  it("sorts notes newest first, stable", () => {
    const notes = [
      { id: "a", content: "old", metadata: { created: "2026-01-01T00:00:00Z" } },
      { id: "b", content: "new", added: "2026-09-01T00:00:00Z" },
      { id: "c", content: "none" },
    ];
    expect(sortNotesNewestFirst(notes as never).map((n) => n.id)).toEqual(["b", "a", "c"]);
  });
});
