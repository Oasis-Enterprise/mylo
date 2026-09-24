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

interface Props {
  label: string;
}

// Live status beside the pulsing dots. The label comes from the server's
// `status` events ("Reading your automations"), so it describes what is
// actually happening rather than cycling decorative phrases.
export function ThinkingIndicator({ label }: Props) {
  return (
    <div role="status" aria-live="polite" className="flex items-center gap-2 py-1">
      <PulsingDots />
      <span className="font-mono text-[10px]" style={{ color: "var(--color-text-muted)" }}>
        {label}
      </span>
    </div>
  );
}

function PulsingDots() {
  return (
    <span className="inline-flex items-center gap-[3px]">
      {[0, 1, 2].map((i) => (
        <span
          key={i}
          className="inline-block h-[4px] w-[4px] rounded-full"
          style={{
            backgroundColor: "var(--color-accent)",
            animation: `pulse-dot 1.4s ease-in-out ${i * 0.2}s infinite`,
          }}
        />
      ))}
    </span>
  );
}
