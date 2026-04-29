"""Wire protocol between Scrapy Cloud and the headless subsystem.

State machine
=============

The browser opens the user-supplied URL.  As Imperva injects its challenge
script, the page issues a number of subsidiary fetches (e.g.
``/_Incapsula_Resource?...``) which the browser intercepts and *delegates*
to Scrapy Cloud — the only party that owns the upstream IP/cookie context.

Conceptually:

    Scrapy Cloud                Subsystem (browser)            Imperva server
    -----------                 -------------------            --------------
    POST /challenge/start  ---> create session, navigate URL
                                page.route() intercepts every
                                outbound request
    <--- 200 NEED_FETCH (req#1)  page suspends
    upstream HTTP for req#1                                    real fetch
    POST /challenge/feed/req#1 ---> response handed to browser
                                page resumes
                                ... possibly more fetches ...
    <--- 200 NEED_FETCH (req#N)
    POST /challenge/feed/req#N --->
                                page resolves; cookies + final
                                HTML extracted
    <--- 200 DONE (cookies, html, status)


Every NEED_FETCH instructs Scrapy Cloud which URL to fetch with which
method, headers and body, all of which the subsystem replays from the
intercepted request. The feed body returns the upstream response verbatim
(status, headers, base64-encoded body).
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Start request
# ---------------------------------------------------------------------------


class StartRequest(BaseModel):
    """Scrapy Cloud → subsystem: please solve a challenge for this URL."""

    url: str = Field(..., description="Target URL the spider was trying to load")
    method: str = Field("GET")
    headers: Dict[str, str] = Field(default_factory=dict)
    body_b64: Optional[str] = Field(
        None, description="Base64-encoded request body, if any"
    )
    # Cookies the spider already collected (e.g. from the very first GET that
    # returned the challenge page). They are seeded into the browser context.
    cookies: List[Dict[str, Any]] = Field(default_factory=list)
    user_agent: Optional[str] = Field(
        None, description="UA the browser should spoof. Falls back to a default."
    )
    # Free-form key for Scrapy Cloud's own bookkeeping. Echoed in DONE.
    correlation_id: Optional[str] = None


# ---------------------------------------------------------------------------
# Subsystem responses
# ---------------------------------------------------------------------------


class NeedFetch(BaseModel):
    """Subsystem → Scrapy Cloud: the page needs *this* HTTP fetch executed
    over the spider's egress IP. POST the answer back to /feed/{req_id}."""

    state: Literal["need_fetch"] = "need_fetch"
    req_id: str
    url: str
    method: str
    headers: Dict[str, str]
    body_b64: Optional[str] = None
    # The subsystem also returns the running session id so the spider can
    # confirm or, on retry, reuse the same browser context.
    session_id: str


class ChallengeDone(BaseModel):
    """Subsystem → Scrapy Cloud: challenge complete, here are the goodies."""

    state: Literal["done"] = "done"
    session_id: str
    correlation_id: Optional[str] = None
    final_url: str
    status: int
    html: str
    cookies: List[Dict[str, Any]]
    # Raw response headers of the final document for debugging.
    headers: Dict[str, str]
    # How many delegated fetches were performed.
    fetch_count: int
    # Wall-clock duration of the solve in seconds.
    duration_s: float


class ChallengeError(BaseModel):
    state: Literal["error"] = "error"
    session_id: Optional[str] = None
    correlation_id: Optional[str] = None
    code: str
    message: str


# ---------------------------------------------------------------------------
# Feed request (Scrapy Cloud → subsystem)
# ---------------------------------------------------------------------------


class FeedFetch(BaseModel):
    """Scrapy Cloud → subsystem: the upstream response for a NEED_FETCH."""

    status: int
    headers: Dict[str, str] = Field(default_factory=dict)
    body_b64: str = ""
    # If the upstream call itself failed (DNS, TCP, 5xx after retries), the
    # spider can mark it so the subsystem can fail the challenge gracefully.
    error: Optional[str] = None
