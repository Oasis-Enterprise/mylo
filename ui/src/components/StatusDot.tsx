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

import type { CSSProperties } from "react";

export type DotTone = "accent" | "success" | "warning" | "error" | "muted" | "info";

interface Props {
  tone?: DotTone;
  size?: number;
  glow?: boolean;
  pulse?: boolean;
  className?: string;
  style?: CSSProperties;
}

// Solid circular indicator. Optional glow + pulse. Used everywhere a
// row needs a state marker (tool call status, header presence,
// approval card, severity card).
export function StatusDot({
  tone = "accent",
  size = 7,
  glow = true,
  pulse = false,
  className = "",
  style,
}: Props) {
  const bg = toneToBg(tone);
  const glowClass = glow ? toneToGlowClass(tone) : "";
  const pulseClass = pulse ? "dot-pulse" : "";
  return (
    <span
      className={`inline-block rounded-full ${glowClass} ${pulseClass} ${className}`.trim()}
      style={{
        width: size,
        height: size,
        backgroundColor: bg,
        ...style,
      }}
    />
  );
}

function toneToBg(tone: DotTone): string {
  switch (tone) {
    case "success":
    case "accent":
      return "var(--color-accent)";
    case "warning":
      return "var(--color-warning)";
    case "error":
      return "var(--color-error)";
    case "info":
      return "var(--color-info)";
    case "muted":
      return "var(--color-text-muted)";
  }
}

function toneToGlowClass(tone: DotTone): string {
  switch (tone) {
    case "success":
    case "accent":
      return "dot-glow-accent";
    case "warning":
      return "dot-glow-warning";
    case "error":
      return "dot-glow-error";
    default:
      return "";
  }
}
