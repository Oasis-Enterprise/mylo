"""Tiny .env loader for local development.

Not used in production — the add-on receives its config via
``/data/options.json`` and the Supervisor token via environment variable.
This is only to make ``python -m mylo.scripts.probe`` nice to run from a
dev machine without exporting vars manually.

Intentionally minimal: no interpolation, no multiline values, no exports.
"""

from __future__ import annotations

import os
from pathlib import Path


def load_dotenv(path: str | Path = ".env", *, override: bool = False) -> dict[str, str]:
    """Load key/value pairs from a .env file into ``os.environ``.

    Returns a dict of the keys actually loaded (useful for logging/debug).
    Missing file is not an error — we just return an empty dict.
    """
    p = Path(path)
    if not p.exists():
        return {}

    loaded: dict[str, str] = {}
    for raw_line in p.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        # Strip surrounding quotes if paired.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if not key:
            continue
        if not override and key in os.environ:
            continue
        os.environ[key] = value
        loaded[key] = value
    return loaded
