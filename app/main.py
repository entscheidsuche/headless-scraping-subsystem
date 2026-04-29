"""FastAPI app exposing /headless/challenge/start and /feed/{req_id}.

Run locally with:
    uvicorn app.main:app --host 127.0.0.1 --port 8088

In production this binds to 127.0.0.1 and is reverse-proxied at
``https://files.entscheidsuche.ch/headless/`` by nginx (see deploy/nginx/).
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Union

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.responses import JSONResponse

from .auth import require_bearer
from .browser import engine
from .config import settings
from .protocol import (
    ChallengeDone,
    ChallengeError,
    FeedFetch,
    NeedFetch,
    StartRequest,
)


logging.basicConfig(
    level=getattr(logging, settings.log_level, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("headless")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await engine.start()
    log.info("subsystem ready: pool=%d, headless=%s",
             settings.browser_pool_size, settings.headless)
    try:
        yield
    finally:
        log.info("shutting down")
        await engine.stop()


app = FastAPI(
    title="headless-scraping-subsystem",
    version="0.1.0",
    description=(
        "Solves Imperva (Incapsula) JS challenges by driving a Chromium "
        "browser whose outbound HTTP is delegated to the calling Scrapy "
        "spider over the spider's egress IP."
    ),
    lifespan=lifespan,
)


# All real work lives under /headless/ so the same nginx prefix that handles
# the public URL doesn't need URL rewriting.
PREFIX = "/headless"


@app.get(f"{PREFIX}/health")
async def health() -> dict:
    return {"status": "ok", "version": app.version}


@app.post(
    f"{PREFIX}/challenge/start",
    response_model=Union[NeedFetch, ChallengeDone, ChallengeError],
    dependencies=[Depends(require_bearer)],
)
async def challenge_start(req: StartRequest) -> JSONResponse:
    """Begin a challenge solve.

    Returns the first protocol event:
    - ``NeedFetch`` — the spider must execute the upstream fetch and POST
      the response to ``/headless/challenge/feed/{req_id}``.
    - ``ChallengeDone`` — the page loaded without further intervention.
    - ``ChallengeError`` — the engine failed to start the solve.
    """
    session = await engine.start_challenge(req)
    try:
        event = await session.next_event(timeout=settings.challenge_timeout_s)
    except Exception as e:
        await engine.close_session(session)
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail=f"engine timeout waiting for first event: {e!r}",
        )

    if isinstance(event, (ChallengeDone, ChallengeError)):
        await engine.close_session(session)
    return JSONResponse(content=event.model_dump(mode="json"))


@app.post(
    f"{PREFIX}/challenge/feed/{{req_id}}",
    response_model=Union[NeedFetch, ChallengeDone, ChallengeError],
    dependencies=[Depends(require_bearer)],
)
async def challenge_feed(req_id: str, feed: FeedFetch,
                         session_id: str) -> JSONResponse:
    """Hand back the upstream response for ``req_id`` and wait for the next
    protocol event.

    ``session_id`` is required (as a query parameter) so we can route the feed
    to the correct in-flight session.
    """
    session = await engine.get(session_id)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"unknown session_id {session_id!r}",
        )

    try:
        await session.feed(req_id, feed)
    except KeyError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"unknown req_id {req_id!r} for session {session_id!r}",
        )

    try:
        event = await session.next_event(timeout=settings.challenge_timeout_s)
    except Exception as e:
        await engine.close_session(session)
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail=f"engine timeout waiting for next event: {e!r}",
        )

    if isinstance(event, (ChallengeDone, ChallengeError)):
        await engine.close_session(session)
    return JSONResponse(content=event.model_dump(mode="json"))


@app.post(
    f"{PREFIX}/challenge/cancel/{{session_id}}",
    dependencies=[Depends(require_bearer)],
)
async def challenge_cancel(session_id: str) -> dict:
    session = await engine.get(session_id)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"unknown session_id {session_id!r}",
        )
    await engine.close_session(session)
    return {"cancelled": session_id}
