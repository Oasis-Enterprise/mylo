"""Serve the built UI from ``src/mylo/server/static/``.

In dev the UI runs under Vite on port 5173 with HMR; Python doesn't serve
anything then. In production the Dockerfile runs ``pnpm build`` (or npm)
against ``ui/`` and copies ``ui/dist`` here. ``index.html`` and the hashed
assets all live under this directory.
"""

from __future__ import annotations

from pathlib import Path

from aiohttp import web

_STATIC_DIR = Path(__file__).parent / "static"


def register_static_routes(app: web.Application) -> None:
    if not _STATIC_DIR.exists():
        # Running from a source checkout without having built the UI.
        # Fall back to a minimal index so curl-style smoke tests work.
        app.router.add_get("/", _placeholder_index)
        return

    # Catch-all for client-side routing: any non-/api/ path falls back to
    # index.html so React Router / Vite routes resolve.
    app.router.add_get("/", _serve_index)
    app.router.add_static("/assets", _STATIC_DIR / "assets")
    app.router.add_get("/{tail:.*}", _serve_index)


async def _serve_index(_request: web.Request) -> web.Response:
    index = _STATIC_DIR / "index.html"
    return web.Response(
        body=index.read_bytes(),
        content_type="text/html",
    )


async def _placeholder_index(_request: web.Request) -> web.Response:
    return web.Response(
        body=(
            "<!doctype html><meta charset=utf-8><title>Mylo</title>"
            "<h1>Mylo</h1>"
            "<p>UI not built. Run <code>pnpm --dir ui build</code> or "
            "use the dev server on port 5173.</p>"
        ),
        content_type="text/html",
    )
