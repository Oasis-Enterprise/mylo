# Copyright 2026 Maxwell Monson / Oasis Enterprise LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Minimal system-prompt loader.

M4a. Reads the shipped ``data/system_prompt.txt``, parses the
``# version: X.Y.Z`` header for changelog correlation, strips comment
lines before handing the prompt to the model.

M4b replaces this with the full :mod:`mylo.context.assembler` that
composes all four layers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_VERSION_RE = re.compile(r"^#\s*version:\s*(\S+)", re.MULTILINE)


@dataclass(slots=True, frozen=True)
class LoadedPrompt:
    version: str
    text: str


def _find_prompt_path() -> Path:
    # src/mylo/context/basic_prompt.py  →  src/mylo/data/system_prompt.txt
    # Ships with the package via pyproject.toml force-include so the same
    # lookup works in a source checkout AND in a pip install inside the
    # add-on container.
    here = Path(__file__).resolve()
    return here.parent.parent / "data" / "system_prompt.txt"


def load_system_prompt(path: Path | None = None) -> LoadedPrompt:
    src = path or _find_prompt_path()
    raw = src.read_text(encoding="utf-8")

    match = _VERSION_RE.search(raw)
    version = match.group(1) if match else "unknown"

    # Strip comment-only lines (start with '#') and blank leading/trailing.
    body_lines = [line for line in raw.splitlines() if not line.lstrip().startswith("#")]
    body = "\n".join(body_lines).strip()

    return LoadedPrompt(version=version, text=body)
