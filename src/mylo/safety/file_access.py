"""File-access policy enforcement.

Spec §4.13 + §5. The agent may read YAML files under ``/config/`` but must
never see ``secrets.yaml`` (even via read) or files outside the config tree.
Write rules are enforced elsewhere; this module only concerns read.
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
