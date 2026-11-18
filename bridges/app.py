"""One service for the screens that need data from outside: weather, fitness
(Strava), Polestar and home energy (Dominion).

Each screen lives in its own module and keeps its own routes, token and
refresh loop; a screen that fails never stops the others. LaraPaper still
reaches each screen by its old name (weather-bridge, strava-bridge, ...),
which are network aliases of this service in docker-compose.yml.

    GET /health   every screen's status ({"ok": ..., "screens": {...}})

Each screen module provides:
    NAME          the screen's key in /health
    router        its FastAPI routes
    start()       a list of coroutines to run in the background
    health()      a dict with at least "ok"
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI

import strava
import weather

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bridges")

SCREENS = [weather, strava]


@asynccontextmanager
async def lifespan(_: FastAPI):
    tasks = []
    for screen in SCREENS:
        for coro in screen.start():
            tasks.append(asyncio.create_task(coro, name=f"{screen.NAME}"))
    log.info("Started: %s", ", ".join(s.NAME for s in SCREENS))
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(title="bridges", lifespan=lifespan)
for _screen in SCREENS:
    app.include_router(_screen.router)


@app.get("/health")
async def health() -> dict[str, Any]:
    screens = {}
    for screen in SCREENS:
        try:
            screens[screen.NAME] = screen.health()
        except Exception as err:  # one screen's health never hides the others
            screens[screen.NAME] = {"ok": False, "error": f"{type(err).__name__}: {err}"}
    return {"ok": all(s.get("ok") for s in screens.values()), "screens": screens}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8765")))
