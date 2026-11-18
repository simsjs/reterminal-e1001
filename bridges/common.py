"""Pieces every screen in the bridges service shares.

Each screen module (weather.py, strava.py, ...) reads its own settings with a
prefix (WEATHER_BRIDGE_TOKEN, STRAVA_REFRESH_MINUTES, ...) so they never clash
inside the one process.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
from typing import Protocol

from fastapi import HTTPException, Request


def setting(prefix: str, name: str, default: str = "") -> str:
    """The environment variable <PREFIX>_<NAME>, e.g. setting("WEATHER", "DEMO")."""
    return os.getenv(f"{prefix}_{name}", default)


def flag(prefix: str, name: str, default: str = "0") -> bool:
    return setting(prefix, name, default).strip().lower() in ("1", "true", "yes", "on")


def check_token(request: Request, token: str) -> None:
    """If the screen has a token, require it as `Authorization: Bearer ...` or ?token=."""
    if not token:
        return
    got = request.headers.get("authorization", "").removeprefix("Bearer ").strip() or request.query_params.get("token", "")
    if not secrets.compare_digest(got, token):
        raise HTTPException(401, "bad token")


def page_info(screen: str) -> dict[str, int]:
    """This screen's header dot and the total, from SCREEN_ORDER (playlist order)."""
    order = [s.strip().lower() for s in os.environ.get("SCREEN_ORDER", "").split(",") if s.strip()]
    if screen not in order:
        return {}
    return {"page_number": order.index(screen) + 1, "page_count": len(order)}


class Refreshable(Protocol):
    error: str | None

    async def refresh(self) -> None: ...


async def refresh_forever(source: Refreshable, minutes: float, log: logging.Logger) -> None:
    """Refresh every few minutes. A failed refresh never stops the loop, and the
    screen keeps serving the last good data with the error noted."""
    while True:
        try:
            await source.refresh()
        except asyncio.CancelledError:
            raise
        except Exception as err:
            source.error = f"{type(err).__name__}: {err}"
            log.warning("Refresh failed: %s", source.error)
        await asyncio.sleep(minutes * 60)

