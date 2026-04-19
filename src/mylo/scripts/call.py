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

"""Invoke a tool from the command line against a live Home Assistant.

Examples::

    python -m mylo.scripts.call query_entities --filter.area kitchen
    python -m mylo.scripts.call query_entities --filter.domain light --include-attributes
    python -m mylo.scripts.call query_entities --limit 5 --params '{"filter":{"pattern":"kitchen"}}'

Two ways to pass parameters:

* ``--key value`` flags: each flag name becomes a dotted path into the
  params JSON. ``--filter.area kitchen`` → ``{"filter":{"area":"kitchen"}}``.
  Booleans are flags (``--include-attributes`` → True).
* ``--params '{...}'``: pass a raw JSON blob. Flags overlay on top of it.

Output is the tool's ToolResult rendered as pretty JSON.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any

from mylo.config import load_config
from mylo.ha.registries import Registries
from mylo.ha.ws_client import AuthFailed, HaWsClient
from mylo.logging_setup import configure_logging, get_logger
from mylo.safety.audit import AuditLogger
from mylo.safety.permissions import default_permissions
from mylo.tools import registry as tool_registry
from mylo.tools.context import ToolContext
from mylo.tools.executor import execute
from mylo.util.env import load_dotenv


def _parse_args(argv: list[str]) -> tuple[argparse.Namespace, list[str]]:
    parser = argparse.ArgumentParser(
        description="Invoke a Mylo tool against a live HA instance.",
    )
    parser.add_argument("tool", nargs="?", help="Tool name (omit to list all).")
    parser.add_argument("--url", default=None, help="HA base URL. Defaults to $HA_URL.")
    parser.add_argument("--token", default=None, help="HA token. Defaults to $HA_TOKEN.")
    parser.add_argument(
        "--params",
        default=None,
        help="Raw JSON params. Flags below merge on top of this.",
    )
    parser.add_argument(
        "--pretty/--compact",
        dest="pretty",
        default=True,
        action=argparse.BooleanOptionalAction,
        help="Pretty-print result (default on).",
    )
    parser.add_argument(
        "--approve",
        action="store_true",
        help="Mark this invocation as user-approved (required for tier-2/3 tools).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Request a dry-run; honored by tier-2 write tools.",
    )
    return parser.parse_known_args(argv)


def _set_by_path(obj: dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    cur = obj
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value


def _parse_flag_overlay(extra: list[str]) -> dict[str, Any]:
    """Turn ``['--filter.area', 'kitchen', '--include-attributes']`` into a dict.

    Booleans: a flag with no following value (or followed by another ``--``
    flag) is treated as ``True``. Values are json-parsed when possible so
    numbers and ``true``/``false`` work; otherwise kept as strings.
    """
    overlay: dict[str, Any] = {}
    i = 0
    while i < len(extra):
        tok = extra[i]
        if not tok.startswith("--"):
            raise SystemExit(f"unexpected positional arg: {tok!r}")
        key = tok[2:].replace("-", "_")
        # Support --no-foo to set false.
        if key.startswith("no_"):
            _set_by_path(overlay, key[3:], False)
            i += 1
            continue
        if i + 1 >= len(extra) or extra[i + 1].startswith("--"):
            _set_by_path(overlay, key, True)
            i += 1
            continue
        raw = extra[i + 1]
        try:
            value: Any = json.loads(raw)
        except json.JSONDecodeError:
            value = raw
        _set_by_path(overlay, key, value)
        i += 2
    return overlay


def _merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in overlay.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


async def _run(ns: argparse.Namespace, params: dict[str, Any]) -> int:
    url = ns.url or os.environ.get("HA_URL")
    token = ns.token or os.environ.get("HA_TOKEN")
    if not url or not token:
        print("error: HA_URL and HA_TOKEN must be set.", file=sys.stderr)
        return 2

    log = get_logger(__name__)
    config = load_config()

    client = HaWsClient(url, token)
    try:
        await client.start()
        try:
            await client.wait_ready(timeout=15.0)
        except TimeoutError:
            log.error("call.timeout_waiting_for_ready")
            return 1
        registries = await Registries.attach(client)
        await registries.wait_loaded(timeout=15.0)

        ctx = ToolContext(
            ws_client=client,
            registries=registries,
            config=config,
            permissions=default_permissions(),
            audit=AuditLogger(config.mylo_data_dir),
            user_approved=ns.approve,
            dry_run=ns.dry_run,
        )
        result = await execute(ns.tool, params, ctx)
        payload = result.to_dict()
        if ns.pretty:
            print(json.dumps(payload, indent=2, default=str))
        else:
            print(json.dumps(payload, default=str))
        return 0 if result.status.value == "ok" else 1
    except AuthFailed as exc:
        log.error("call.auth_failed", error=str(exc))
        return 2
    finally:
        await client.close()


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    configure_logging()
    ns, extra = _parse_args(sys.argv[1:] if argv is None else argv)

    tool_registry.load_all()

    if not ns.tool:
        print("Available tools:")
        for t in sorted(tool_registry.all_tools(), key=lambda t: t.name):
            print(f"  {t.name:<24} tier={t.tier.value}  {t.description[:60]}")
        return 0

    base: dict[str, Any] = {}
    if ns.params:
        try:
            parsed = json.loads(ns.params)
        except json.JSONDecodeError as exc:
            print(f"error: --params is not valid JSON: {exc}", file=sys.stderr)
            return 2
        if not isinstance(parsed, dict):
            print("error: --params must decode to a JSON object.", file=sys.stderr)
            return 2
        base = parsed

    overlay = _parse_flag_overlay(extra)
    params = _merge(base, overlay)

    try:
        return asyncio.run(_run(ns, params))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
