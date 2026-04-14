"""Mylo entrypoint.

Milestone 0 scope: load config, log it, exit. The real event loop lands in
later milestones once the HA websocket client and web server exist.
"""

from __future__ import annotations

import sys

from mylo import __version__
from mylo.config import AppConfig, load_config
from mylo.logging_setup import configure_logging, get_logger


def main() -> int:
    configure_logging()
    log = get_logger(__name__)

    log.info("mylo.startup", version=__version__)

    try:
        config: AppConfig = load_config()
    except Exception as exc:
        log.error("mylo.config_load_failed", error=str(exc))
        return 1

    log.info(
        "mylo.config_loaded",
        llm_provider=config.llm_provider,
        model=config.model,
        sync_frequency=config.sync_frequency,
        memory_token_limit=config.memory_token_limit,
        proactive_notifications=config.proactive_notifications,
        has_api_key=bool(config.api_key),
        supervisor_token_present=config.supervisor_token_present,
    )

    # M0: we do not start the server or scheduler yet — that lands in later milestones.
    log.info("mylo.m0_complete", note="scaffold-only; exiting")
    return 0


if __name__ == "__main__":
    sys.exit(main())
