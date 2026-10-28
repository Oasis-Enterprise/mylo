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

// The server stores each user message with what it prefixed for the
// model: a <turn_context> block and a "[CURRENT TIME: …]" stamp. Strip
// both so a rehydrated chat shows only what the user typed.

const TURN_CONTEXT = /^<turn_context>[\s\S]*?<\/turn_context>\s*/;
const TIME_STAMP = /^\[CURRENT TIME: [^\]]*\]\s?/;

export function displayUserText(raw: string): string {
  return raw.replace(TURN_CONTEXT, "").replace(TIME_STAMP, "");
}
