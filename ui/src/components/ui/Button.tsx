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

import type { ButtonHTMLAttributes } from "react";

const base = "tap rounded px-3 py-1 font-mono text-[11px] font-bold uppercase tracking-label disabled:opacity-40";

type Tone = "accent" | "error";
const toneStyle: Record<Tone, React.CSSProperties> = {
  accent: { backgroundColor: "var(--color-accent-soft)", border: "1px solid var(--color-accent)", color: "var(--color-accent)" },
  error: { backgroundColor: "var(--color-error-soft)", border: "1px solid var(--color-error)", color: "var(--color-error)" },
};

export function PrimaryButton({ tone = "accent", glow = tone === "accent", className = "", style, ...rest }: ButtonHTMLAttributes<HTMLButtonElement> & { tone?: Tone; glow?: boolean }) {
  return <button type="button" className={`${base} ${glow ? "btn-glow" : ""} hover:brightness-110 ${className}`} style={{ ...toneStyle[tone], ...style }} {...rest} />;
}

export function GhostButton({ className = "", style, ...rest }: ButtonHTMLAttributes<HTMLButtonElement>) {
  return <button type="button" className={`${base} border hover:opacity-80 ${className}`} style={{ borderColor: "var(--color-border)", color: "var(--color-text-muted)", background: "transparent", ...style }} {...rest} />;
}

export function IconButton({ className = "", style, ...rest }: ButtonHTMLAttributes<HTMLButtonElement> & { "aria-label": string }) {
  return <button type="button" className={`tap rounded px-1 font-mono text-[10px] ${className}`} style={{ color: "var(--color-text-dim)", ...style }} {...rest} />;
}
