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

import type { ReactNode } from "react";
import { GhostButton, PrimaryButton } from "./Button";

export interface ActionBarProps {
  primary: { label: string; onClick: () => void; disabled?: boolean; tone?: "accent" | "error" };
  secondary?: Array<{ label: string; onClick: () => void }>;
  note?: ReactNode;
}

// One footer for every review card: optional note line, then a wrapping,
// right-aligned row of ghost buttons followed by the primary action.
export function ActionBar({ primary, secondary = [], note }: ActionBarProps) {
  return (
    <div className="border-t" style={{ borderColor: "var(--color-border)" }}>
      {note ? <div className="px-3 pt-2 font-mono text-[10px]" style={{ color: "var(--color-error)" }}>{note}</div> : null}
      <div className="flex flex-wrap items-center justify-end gap-2 px-3 py-2">
        {secondary.map((s) => <GhostButton key={s.label} onClick={s.onClick}>{s.label}</GhostButton>)}
        <PrimaryButton tone={primary.tone} onClick={primary.onClick} disabled={primary.disabled}>{primary.label}</PrimaryButton>
      </div>
    </div>
  );
}
