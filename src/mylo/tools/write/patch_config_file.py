"""``patch_config_file`` — surgical edits within a YAML file.

Reads the file, applies a single add/update/remove at a dotted path, and
writes back through the same rollback pipeline as ``write_config_file``.
Safer than round-tripping the whole file when only one nested value is
changing.

The dotted path mini-language accepts list indices as ``[N]``:

* ``automation[0].alias`` — the ``alias`` key on the first automation
* ``homeassistant.customize`` — a second-level dict key

For add/update the LLM supplies a ``content`` fragment (parsed as YAML).
Remove drops the target key entirely.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from mylo.files.diff import diff_structs
from mylo.files.manager import read_text
from mylo.files.rollback import RELOAD_SERVICES, apply_with_rollback
from mylo.safety.file_access import FileAccessError, resolve_under_config_writable
from mylo.tools.base import Tier, ToolDefinition, ToolResult
from mylo.tools.context import ToolContext
from mylo.tools.registry import register
from mylo.validators.yaml_parser import dump_yaml, load_yaml

Operation = Literal["add", "update", "remove"]


class PatchConfigFileParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(description="Relative path under /config/ (YAML only).")
    operation: Operation = Field(
        description=(
            "'add' inserts a new key/list item; 'update' replaces an existing "
            "value; 'remove' deletes a key/item."
        ),
    )
    yaml_path: str = Field(
        description=(
            "Dotted path into the YAML structure. List indices as [N] "
            "(positive) or [-N] (from end). For append-to-list with "
            "operation='add', use [+] as the trailing segment — e.g. "
            "'automation[+]' appends a new automation to the end. "
            "Examples: 'automation[0].alias', 'sensor[+]', 'homeassistant.customize'."
        ),
    )
    content: str | None = Field(
        default=None,
        description=(
            "YAML fragment used by 'add' and 'update'. Ignored for 'remove'. "
            "Parsed before applying."
        ),
    )
    domain: str = Field(default="all", description="See write_config_file.")
    dry_run: bool = Field(default=True)


async def handler(params: PatchConfigFileParams, ctx: ToolContext) -> ToolResult:
    try:
        resolved = resolve_under_config_writable(ctx.config.ha_config_dir, params.path)
    except FileAccessError as exc:
        return ToolResult.error(exc.code, exc.message)

    if params.domain not in RELOAD_SERVICES:
        return ToolResult.error(
            "unknown_domain",
            f"domain {params.domain!r} not in {sorted(RELOAD_SERVICES)}",
        )

    if params.operation in ("add", "update") and params.content is None:
        return ToolResult.error(
            "missing_content",
            f"operation {params.operation!r} requires 'content'",
        )

    if not resolved.exists():
        return ToolResult.error("not_found", f"file {params.path!r} does not exist")

    old_text = read_text(resolved)
    try:
        old_struct = load_yaml(old_text)
    except Exception as exc:
        return ToolResult.error(
            "invalid_yaml_in_target",
            f"target file failed to parse: {type(exc).__name__}: {exc}",
        )

    fragment: Any = None
    if params.content is not None:
        try:
            fragment = load_yaml(params.content)
        except Exception as exc:
            return ToolResult.error(
                "invalid_yaml_in_content",
                f"content fragment failed to parse: {type(exc).__name__}: {exc}",
            )

    try:
        new_struct = _apply_patch(old_struct, params.yaml_path, params.operation, fragment)
    except PatchError as exc:
        return ToolResult.error(exc.code, exc.message)

    new_text = dump_yaml(new_struct) if new_struct is not None else ""
    diff = diff_structs(old_struct, new_struct)

    preview: dict[str, Any] = {
        "path": params.path,
        "operation": params.operation,
        "yaml_path": params.yaml_path,
        "domain": params.domain,
        "diff": diff.to_dict(),
    }

    if params.dry_run:
        preview["preview"] = True
        return ToolResult.ok(preview)

    rollback_result = await apply_with_rollback(
        client=ctx.ws_client,
        path=resolved,
        content=new_text,
        domain=params.domain,
        config_dir=ctx.config.ha_config_dir,
        mylo_data_dir=ctx.config.mylo_data_dir,
        verify=None,
    )
    envelope: dict[str, Any] = {
        **preview,
        "preview": False,
        "apply": rollback_result.to_dict(),
    }
    if not rollback_result.ok:
        return ToolResult.error(
            "apply_failed",
            "patch applied but reload/verify failed — see apply.steps",
            data=envelope,
        )
    return ToolResult.ok(envelope)


# ─── Path mini-language ─────────────────────────────────────────────────────


class PatchError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


_SEGMENT = re.compile(r"(?P<key>[^.\[\]]+)|\[(?P<idx>-?\d+|\+|-)\]")


# Sentinel for "append to end of list" — yields from `[+]` or `[-]`.
APPEND = object()


def _parse_path(path: str) -> list[str | int | object]:
    """Parse ``'a.b[0].c'`` into ``['a', 'b', 0, 'c']``.

    Supports:
    * ``[N]`` — positive index
    * ``[-N]`` — negative index (from end)
    * ``[+]`` or ``[-]`` — append sentinel, only valid as the last
      segment of an ``add`` operation
    """
    parts: list[str | int | object] = []
    remainder = path
    while remainder:
        match = _SEGMENT.match(remainder)
        if not match:
            raise PatchError("invalid_path", f"can't parse path at {remainder!r}")
        if match.group("key") is not None:
            parts.append(match.group("key"))
        else:
            token = match.group("idx")
            if token in ("+", "-"):
                parts.append(APPEND)
            else:
                parts.append(int(token))
        remainder = remainder[match.end() :].lstrip(".")
    return parts


def _apply_patch(struct: Any, yaml_path: str, operation: Operation, content: Any) -> Any:
    parts = _parse_path(yaml_path)
    if not parts:
        raise PatchError("invalid_path", "path is empty")

    # Descend to the parent of the final segment.
    parent: Any = struct
    last = parts[-1]
    for seg in parts[:-1]:
        parent = _descend(parent, seg)

    if operation == "remove":
        _remove_at(parent, last)
    elif operation == "add":
        _add_at(parent, last, content)
    elif operation == "update":
        _update_at(parent, last, content)
    return struct


def _descend(value: Any, seg: Any) -> Any:
    if isinstance(seg, int):
        if not isinstance(value, list):
            raise PatchError(
                "path_type_mismatch",
                f"expected list at segment {seg}, got {type(value).__name__}",
            )
        if seg >= len(value):
            raise PatchError("path_not_found", f"list index {seg} out of range")
        return value[seg]
    if not isinstance(value, dict):
        raise PatchError(
            "path_type_mismatch",
            f"expected dict at segment {seg!r}, got {type(value).__name__}",
        )
    if seg not in value:
        raise PatchError("path_not_found", f"key {seg!r} not found")
    return value[seg]


def _remove_at(parent: Any, key: Any) -> None:
    if isinstance(key, int):
        if not isinstance(parent, list) or key >= len(parent):
            raise PatchError("path_not_found", f"list index {key} out of range")
        parent.pop(key)
        return
    if not isinstance(parent, dict) or key not in parent:
        raise PatchError("path_not_found", f"key {key!r} not found")
    del parent[key]


def _update_at(parent: Any, key: Any, content: Any) -> None:
    if isinstance(key, int):
        if not isinstance(parent, list) or key >= len(parent):
            raise PatchError("path_not_found", f"list index {key} out of range")
        parent[key] = content
        return
    if not isinstance(parent, dict):
        raise PatchError("path_type_mismatch", f"cannot update key on {type(parent).__name__}")
    parent[key] = content


def _add_at(parent: Any, key: Any, content: Any) -> None:
    if key is APPEND:
        if not isinstance(parent, list):
            raise PatchError(
                "path_type_mismatch",
                f"[+]/[-] append only works on lists, got {type(parent).__name__}",
            )
        parent.append(content)
        return
    if isinstance(key, int):
        if not isinstance(parent, list):
            raise PatchError("path_type_mismatch", f"cannot add indexed at {type(parent).__name__}")
        # Negative indices use Python's list-index semantics: [-1]
        # inserts BEFORE the current last element.
        if key < 0:
            key = len(parent) + key
        parent.insert(key, content)
        return
    if not isinstance(parent, dict):
        raise PatchError("path_type_mismatch", f"cannot add key to {type(parent).__name__}")
    if key in parent:
        raise PatchError(
            "already_exists",
            f"key {key!r} already present — use 'update' to replace",
        )
    parent[key] = content


TOOL = ToolDefinition(
    name="patch_config_file",
    description=(
        "Surgical add/update/remove on a single location inside a YAML "
        "config file. Use when only one nested value is changing — safer "
        "than rewriting the whole file. dry_run=true returns a structural "
        "diff preview; only retry with dry_run=false after user approval."
    ),
    params_model=PatchConfigFileParams,
    tier=Tier.MODIFY,
    handler=handler,
)
register(TOOL)
