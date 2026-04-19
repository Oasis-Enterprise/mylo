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

"""Layer 4 — task reference loader (spec §6.6).

When :mod:`context.task_detector` classifies a turn as
``automation`` / ``dashboard`` / ``troubleshoot`` /
``entity_management``, the assembler pulls the matching reference
file(s) so the model has few-shot examples to crib from.

Reference files live at ``src/mylo/data/references/*.yaml`` (shipped
with the package) and optionally at
``{mylo_data_dir}/references/*.yaml`` (user-extended). User files
override package defaults on filename collision — this is how power
users can teach Mylo their house's conventions without forking.

Spec §6.6 notes: "Reference files are few-shot examples, not
documentation." We ship minimal starters and expect the ``references``
corpus to grow over time.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from mylo.logging_setup import get_logger

log = get_logger(__name__)

TaskType = str  # avoid circular import with task_detector

# Which file(s) each task type loads. Keep this tight — big reference
# dumps spike token cost per turn.
_TASK_FILES: dict[TaskType, tuple[str, ...]] = {
    "automation": ("automation_examples.yaml",),
    "dashboard": ("dashboard_examples.yaml",),
    "troubleshoot": ("common_issues.yaml",),
    "entity_management": ("naming_conventions.yaml",),
}


def _package_references_dir() -> Path:
    # src/mylo/context/references.py → src/mylo/data/references
    return Path(__file__).resolve().parent.parent / "data" / "references"


def load_task_context(
    task_type: TaskType,
    *,
    mylo_data_dir: Path | None = None,
) -> str:
    """Return the concatenated text of the reference file(s) for a task.

    Returns empty string when nothing matches — caller should skip
    the REFERENCE block entirely rather than emit an empty header.
    """
    filenames = _TASK_FILES.get(task_type)
    if not filenames:
        return ""

    blocks: list[str] = []
    for name in filenames:
        text = _read_with_overlay(name, mylo_data_dir=mylo_data_dir)
        if text:
            blocks.append(f"# {name}\n{text.rstrip()}")

    if not blocks:
        return ""
    return "\n\n".join(blocks)


def _read_with_overlay(name: str, *, mylo_data_dir: Path | None) -> str:
    """User override wins; fall back to shipped package file."""
    if mylo_data_dir is not None:
        user_path = mylo_data_dir / "references" / name
        if user_path.exists():
            try:
                return user_path.read_text(encoding="utf-8")
            except OSError as exc:
                log.warning("references.read_failed", path=str(user_path), error=str(exc))

    package_path = _package_references_dir() / name
    if package_path.exists():
        try:
            return package_path.read_text(encoding="utf-8")
        except OSError as exc:
            log.warning("references.read_failed", path=str(package_path), error=str(exc))
    return ""


def available_references(mylo_data_dir: Path | None = None) -> dict[str, list[str]]:
    """Introspection helper — returns {task_type: [resolved_paths]}."""
    out: dict[str, list[str]] = {}
    for task, filenames in _TASK_FILES.items():
        paths: list[str] = []
        for name in filenames:
            if mylo_data_dir is not None:
                user = mylo_data_dir / "references" / name
                if user.exists():
                    paths.append(str(user))
                    continue
            pkg = _package_references_dir() / name
            if pkg.exists():
                paths.append(str(pkg))
        out[task] = paths
    return out


# Keep a module-level handle for ergonomic access during debug.
_ = Any
