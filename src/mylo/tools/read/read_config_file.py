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

"""``read_config_file`` — read a YAML configuration file from ``/config/``.

Applies :mod:`mylo.safety.file_access` to enforce path policy, then sanitizes
``!secret`` references (:mod:`mylo.safety.secret_filter`). Files over the
size limit are refused — the LLM will blow its context on a 1MB YAML file
and there's nothing useful to do with it.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from mylo.safety.file_access import FileAccessError, resolve_under_config
from mylo.safety.secret_filter import sanitize_yaml_secrets
from mylo.tools.base import Tier, ToolDefinition, ToolResult
from mylo.tools.context import ToolContext
from mylo.tools.registry import register


class ReadConfigFileParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(
        description=(
            "Path relative to /config/ (e.g. 'automations.yaml', "
            "'packages/agent/kitchen/automations.yaml'). No absolute paths "
            "or .. segments."
        ),
    )
    max_bytes: int = Field(
        default=128 * 1024,
        ge=1024,
        le=1024 * 1024,
        description="Refuse to read files larger than this many bytes.",
    )


async def handler(params: ReadConfigFileParams, ctx: ToolContext) -> ToolResult:
    try:
        resolved = resolve_under_config(ctx.config.ha_config_dir, params.path)
    except FileAccessError as exc:
        return ToolResult.error(exc.code, exc.message)

    if not resolved.exists():
        return ToolResult.error("not_found", f"no file at {params.path!r}")
    if not resolved.is_file():
        return ToolResult.error("not_a_file", f"{params.path!r} is not a regular file")

    size = resolved.stat().st_size
    if size > params.max_bytes:
        return ToolResult.error(
            "file_too_large",
            f"{params.path!r} is {size} bytes (limit {params.max_bytes})",
            data={"size_bytes": size},
        )

    try:
        raw = resolved.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return ToolResult.error("binary_file", f"{params.path!r} is not valid UTF-8")

    sanitized = sanitize_yaml_secrets(raw)

    return ToolResult.ok(
        {
            "path": params.path,
            "size_bytes": size,
            "content": sanitized,
            "secrets_masked": sanitized != raw,
        }
    )


TOOL = ToolDefinition(
    name="read_config_file",
    description=(
        "Read a YAML/text configuration file from the HA config directory. "
        "Paths must be relative to /config/. secrets.yaml, .storage/, and "
        "log files are denied; !secret references in returned content are "
        "replaced with [SECRET:name] placeholders."
    ),
    params_model=ReadConfigFileParams,
    tier=Tier.READ,
    handler=handler,
)
register(TOOL)
