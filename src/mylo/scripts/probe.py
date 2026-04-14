"""Connect to a live Home Assistant and dump registry counts.

Usage::

    python -m mylo.scripts.probe             # one-shot: print counts, exit
    python -m mylo.scripts.probe --watch     # stay connected; useful to test
                                             # that reconnect works when HA
                                             # is restarted.

Reads ``HA_URL`` and ``HA_TOKEN`` from the environment, with ``.env`` loaded
automatically if present in the current working directory.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections import Counter

from mylo.ha.registries import Registries
from mylo.ha.ws_client import AuthFailed, HaWsClient
from mylo.logging_setup import configure_logging, get_logger
from mylo.util.env import load_dotenv


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Probe a Home Assistant instance.")
    parser.add_argument(
        "--watch",
        action="store_true",
        help="Stay connected and log reconnects until Ctrl+C.",
    )
    parser.add_argument(
        "--url",
        default=None,
        help="HA base URL. Defaults to $HA_URL.",
    )
    parser.add_argument(
        "--token",
        default=None,
        help="HA long-lived access token. Defaults to $HA_TOKEN.",
    )
    return parser.parse_args(argv)


def _print_summary(reg: Registries) -> None:
    print()
    print(f"Entities: {len(reg.entities)}")
    print(f"Devices:  {len(reg.devices)}")
    print(f"Areas:    {len(reg.areas)}")
    print(f"Labels:   {len(reg.labels)}")
    print()

    if reg.areas:
        print("By area:")
        area_counts: Counter[str] = Counter()
        for e in reg.entities.values():
            if e.area_id:
                area_counts[e.area_id] += 1
        name_for = {a.area_id: a.name for a in reg.areas.values()}
        for area_id, count in sorted(area_counts.items(), key=lambda kv: -kv[1]):
            print(f"  {name_for.get(area_id, area_id):<24} {count:>4}")
        unassigned = len(reg.unassigned_entities())
        if unassigned:
            print(f"  {'(unassigned)':<24} {unassigned:>4}")
        print()

    top_domains = sorted(reg.domain_counts().items(), key=lambda kv: -kv[1])[:10]
    if top_domains:
        print("Top domains:")
        for domain, count in top_domains:
            print(f"  {domain:<24} {count:>4}")
        print()


async def _run(url: str, token: str, watch: bool) -> int:
    log = get_logger(__name__)
    client = HaWsClient(url, token)

    try:
        await client.start()
        try:
            await client.wait_ready(timeout=15.0)
        except TimeoutError:
            log.error("probe.timeout_waiting_for_ready")
            return 1

        reg = await Registries.attach(client)
        await reg.wait_loaded(timeout=15.0)
        _print_summary(reg)

        if not watch:
            return 0

        print("Watching for reconnects. Restart your HA to test — Ctrl+C to exit.")
        stop = asyncio.Event()
        try:
            await stop.wait()  # blocks until cancelled (Ctrl+C raises)
        except (KeyboardInterrupt, asyncio.CancelledError):
            print("\nStopping.")
        return 0
    except AuthFailed as exc:
        log.error("probe.auth_failed", error=str(exc))
        return 2
    finally:
        await client.close()


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    configure_logging()
    args = _parse_args(argv)

    url = args.url or os.environ.get("HA_URL")
    token = args.token or os.environ.get("HA_TOKEN")

    if not url:
        print("error: HA_URL not set (export HA_URL or pass --url).", file=sys.stderr)
        return 2
    if not token:
        print("error: HA_TOKEN not set (export HA_TOKEN or pass --token).", file=sys.stderr)
        return 2

    try:
        return asyncio.run(_run(url, token, watch=args.watch))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
