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

// Maps a raw error type + message (as caught from a fetch/SSE failure,
// or emitted by the server's `error` event) to a plain-language prose
// sentence for the error strip. The raw text is still shown verbatim
// under a "Details" disclosure — this is just the human-readable summary.

export function describeError(errorType: string, message: string): string {
  const m = message.toLowerCase();
  if (m.includes("turn_in_progress")) {
    return "Mylo is still finishing the previous request — this message wasn't sent. Try again in a moment.";
  }
  if (errorType === "TypeError" || m.includes("failed to fetch") || m.includes("networkerror")) {
    return "Lost the connection to Mylo. It will reconnect on its own.";
  }
  if (/returned 5\d\d/.test(m)) return "Mylo hit a problem on the server. Try again in a moment.";
  return "Something went wrong.";
}
