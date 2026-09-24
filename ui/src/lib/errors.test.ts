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
import { describeError } from "./errors";

describe("describeError", () => {
  it("maps network failures", () => {
    expect(describeError("TypeError", "Failed to fetch")).toBe("Lost the connection to Mylo. It will reconnect on its own.");
  });
  it("maps Safari's network failure type even when the message doesn't say so", () => {
    expect(describeError("TypeError", "Load failed")).toBe("Lost the connection to Mylo. It will reconnect on its own.");
  });
  it("maps a busy server", () => {
    expect(describeError("Error", "chat endpoint returned 409: turn_in_progress")).toBe(
      "Mylo is still finishing the previous request — this message wasn't sent. Try again in a moment.",
    );
  });
  it("maps 5xx", () => {
    expect(describeError("Error", "chat endpoint returned 502: bad gateway")).toBe("Mylo hit a problem on the server. Try again in a moment.");
  });
  it("falls back", () => {
    expect(describeError("ValueError", "x")).toBe("Something went wrong.");
  });
});
