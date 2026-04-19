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

"""File-access policy enforcement.

Spec §4.13 + §5. The agent may read YAML files under ``/config/`` but must
never see ``secrets.yaml`` (even via read) or files outside the config
tree. Writes have the same path rules as reads plus a small extra block
list (``configuration.yaml`` and the log files are read-only even for
write tools — editing the root config is outside the v1 scope).
"""

from __future__ import annotations

from pathlib import Path

# Files whose contents must never flow into LLM context.
NEVER_READ: frozenset[str] = frozenset(
    {
        "secrets.yaml",
        "secrets.yml",
        "home-assistant.log",
        "home-assistant.log.1",
    }
)

# Directories (relative to /config/) we refuse to read from.
NEVER_READ_DIRS: frozenset[str] = frozenset(
    {
        ".storage",
        ".cloud",
    }
)

# Extensions we'll allow for *content* reads. Binary/opaque files are refused.
ALLOWED_EXTENSIONS: frozenset[str] = frozenset({".yaml", ".yml", ".md", ".txt", ".json"})

# Files that are readable but NEVER writable. configuration.yaml is the
# big one — editing a user's core HA config is v2 scope at best, and the
# blast radius of a typo is too large. Users who want to edit it should
# do it manually.
NEVER_WRITE: frozenset[str] = frozenset(
    {
        "configuration.yaml",
        "configuration.yml",
    }
)

# Write-only extensions — the agent should only write config YAML. Text
# / JSON / Markdown are allowed to READ (for docs, integrations) but not
# WRITE (the agent shouldn't be editing user's README.md).
WRITE_ALLOWED_EXTENSIONS: frozenset[str] = frozenset({".yaml", ".yml"})


class FileAccessError(ValueError):
    """Raised when a file access request violates policy."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def resolve_under_config(config_dir: Path, rel_path: str) -> Path:
    """Resolve ``rel_path`` under ``config_dir`` and enforce the read policy.

    Raises :class:`FileAccessError` for any policy violation. Returns the
    absolute, symlink-resolved path on success. Existence is NOT checked —
    the caller does that next so it can map a missing file to its own error
    code.
    """
    if rel_path.startswith("/"):
        raise FileAccessError("path_absolute", "path must be relative to /config/")
    if ".." in Path(rel_path).parts:
        raise FileAccessError("path_traversal", "'..' is not allowed in paths")

    base = config_dir.resolve()
    candidate = (base / rel_path).resolve()

    try:
        candidate.relative_to(base)
    except ValueError as exc:
        raise FileAccessError(
            "path_outside_config", f"{rel_path!r} escapes the config directory"
        ) from exc

    rel = candidate.relative_to(base)

    # Deny reads from sensitive directories.
    for part in rel.parts:
        if part in NEVER_READ_DIRS:
            raise FileAccessError("denied_directory", f"reads from {part!r} are not allowed")

    # Deny sensitive filenames regardless of directory.
    if candidate.name in NEVER_READ:
        raise FileAccessError("denied_file", f"{candidate.name!r} is on the never-read list")

    if candidate.suffix.lower() not in ALLOWED_EXTENSIONS:
        raise FileAccessError(
            "unsupported_extension",
            f"extension {candidate.suffix!r} not in {sorted(ALLOWED_EXTENSIONS)}",
        )

    return candidate


def resolve_under_config_writable(config_dir: Path, rel_path: str) -> Path:
    """Resolve + enforce the **write** policy.

    Applies every read-time rule plus:
    * refuses :data:`NEVER_WRITE` filenames (configuration.yaml);
    * extension must be in :data:`WRITE_ALLOWED_EXTENSIONS` (yaml/yml).
    """
    candidate = resolve_under_config(config_dir, rel_path)

    if candidate.name in NEVER_WRITE:
        raise FileAccessError(
            "denied_write",
            f"{candidate.name!r} is read-only — edit it manually",
        )
    if candidate.suffix.lower() not in WRITE_ALLOWED_EXTENSIONS:
        raise FileAccessError(
            "unsupported_write_extension",
            f"write target must be YAML (.yaml or .yml), got {candidate.suffix!r}",
        )
    return candidate
