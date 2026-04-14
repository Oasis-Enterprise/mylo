"""Mylo entrypoint — start the HTTP server.

Prints the loaded configuration for diagnostics, then hands off to
:func:`mylo.server.app.run` which builds the aiohttp application and
blocks on ``web.run_app``.
"""

from __future__ import annotations

import sys

from mylo import __version__
from mylo.config import load_config
from mylo.logging_setup import configure_logging, get_logger
from mylo.server.app import run as run_server
from mylo.util.env import load_dotenv


def main() -> int:
    load_dotenv()  # no-op outside dev
    configure_logging()
    log = get_logger(__name__)

    log.info("mylo.startup", version=__version__)

    try:
        config = load_config()
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

    run_server()
    return 0


if __name__ == "__main__":
    sys.exit(main())
