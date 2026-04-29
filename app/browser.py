"""Playwright-driven challenge solver.

Public surface
--------------
- ``BrowserEngine`` — singleton, lifecycle managed by FastAPI.
- ``ChallengeSession`` — one solve in flight, exposes ``next_event()`` and
  ``feed(req_id, FeedFetch)``.

The browser is launched once per process; each challenge gets its own
``BrowserContext`` so cookies and storage are isolated. Every outbound
request from the page is intercepted via ``page.route("**/*")``, queued for
the caller, and only fulfilled once the caller posts the upstream response
back via ``feed()``.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Union

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    Request,
    Route,
    async_playwright,
)

from .config import settings
from .protocol import (
    ChallengeDone,
    ChallengeError,
    FeedFetch,
    NeedFetch,
    StartRequest,
)

log = logging.getLogger(__name__)


# Default UA used when the caller doesn't supply one. Matches a recent stable
# Chrome on Linux x86_64 — keep close to real to minimize fingerprint drift.
DEFAULT_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
)


# Stealth patches are baked into an init script applied to every new context.
# They blunt the most common ``navigator.webdriver`` / plugins / languages
# checks that Imperva is known to run. This is intentionally modest — we lean
# on the actual Chromium runtime for the rest. If you find Imperva
# fingerprinting more aggressively, expand the script (see README).
STEALTH_INIT_SCRIPT = r"""
// 1) navigator.webdriver -> false
Object.defineProperty(navigator, 'webdriver', { get: () => false });

// 2) navigator.languages
Object.defineProperty(navigator, 'languages', {
  get: () => ['de-CH', 'de', 'en-US', 'en'],
});

// 3) navigator.plugins (non-empty list)
Object.defineProperty(navigator, 'plugins', {
  get: () => [
    { name: 'PDF Viewer' },
    { name: 'Chrome PDF Viewer' },
    { name: 'Chromium PDF Viewer' },
    { name: 'Microsoft Edge PDF Viewer' },
    { name: 'WebKit built-in PDF' },
  ],
});

// 4) WebGL vendor / renderer
const _gp = WebGLRenderingContext.prototype.getParameter;
WebGLRenderingContext.prototype.getParameter = function(p) {
  if (p === 37445) return 'Intel Inc.';      // UNMASKED_VENDOR_WEBGL
  if (p === 37446) return 'Intel Iris OpenGL Engine'; // UNMASKED_RENDERER_WEBGL
  return _gp.call(this, p);
};

// 5) chrome runtime stub (Imperva looks for window.chrome)
window.chrome = window.chrome || { runtime: {} };

// 6) permissions.query: notifications -> 'default' (instead of 'denied')
const _origQuery = window.navigator.permissions && window.navigator.permissions.query;
if (_origQuery) {
  window.navigator.permissions.query = (params) =>
    params && params.name === 'notifications'
      ? Promise.resolve({ state: Notification.permission })
      : _origQuery(params);
}
"""


# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------


@dataclass
class _PendingFetch:
    """A request the page issued and is now blocked on."""

    req_id: str
    route: Route
    request: Request
    future: asyncio.Future


@dataclass
class ChallengeSession:
    """Holds all state for one in-flight challenge solve."""

    session_id: str
    correlation_id: Optional[str]
    context: BrowserContext
    page: Page
    started_at: float = field(default_factory=time.monotonic)
    fetch_count: int = 0

    # Queue of events the caller (FastAPI) consumes via next_event(). Each
    # element is either a NeedFetch the caller must answer, or a ChallengeDone
    # signalling the page is fully loaded, or a ChallengeError.
    events: asyncio.Queue = field(default_factory=asyncio.Queue)

    # Pending fetches awaiting a /feed call, keyed by req_id.
    pending: Dict[str, _PendingFetch] = field(default_factory=dict)

    # Final result captured once navigation completes. Stored so that late
    # /feed calls can still retrieve it without redoing the work.
    result: Optional[Union[ChallengeDone, ChallengeError]] = None

    closed: bool = False

    async def next_event(
        self, timeout: float
    ) -> Union[NeedFetch, ChallengeDone, ChallengeError]:
        """Wait for the next protocol event from the browser side."""
        return await asyncio.wait_for(self.events.get(), timeout=timeout)

    async def feed(self, req_id: str, feed: FeedFetch) -> None:
        """Hand the upstream response for ``req_id`` back to the page."""
        pending = self.pending.pop(req_id, None)
        if pending is None:
            raise KeyError(f"unknown req_id {req_id!r}")
        if not pending.future.done():
            pending.future.set_result(feed)


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class BrowserEngine:
    """Process-wide browser lifecycle wrapper."""

    def __init__(self) -> None:
        self._pw: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        # Limits the number of *concurrent* challenge contexts to keep memory
        # usage predictable.
        self._sem = asyncio.Semaphore(settings.browser_pool_size)
        self._sessions: Dict[str, ChallengeSession] = {}
        self._lock = asyncio.Lock()

    # -- lifecycle -------------------------------------------------------
    async def start(self) -> None:
        log.info("starting Playwright (headless=%s)", settings.headless)
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(
            headless=settings.headless,
            args=[
                "--no-sandbox",  # systemd unit drops privileges, sandbox is unhelpful here
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ],
        )

    async def stop(self) -> None:
        # Close any sessions still open.
        async with self._lock:
            sessions = list(self._sessions.values())
        for s in sessions:
            await self._cleanup(s)
        if self._browser:
            await self._browser.close()
            self._browser = None
        if self._pw:
            await self._pw.stop()
            self._pw = None

    # -- public API ------------------------------------------------------
    async def start_challenge(self, req: StartRequest) -> ChallengeSession:
        """Begin a new challenge solve. Returns once the session exists; the
        browser may already have queued the first NEED_FETCH."""
        if self._browser is None:
            raise RuntimeError("BrowserEngine not started")

        # Reserve a slot from the pool. NB: we *acquire* without a timeout
        # here; FastAPI sets its own request timeout. If you want a hard
        # back-pressure ceiling, wrap this in asyncio.wait_for upstream.
        await self._sem.acquire()

        try:
            ua = req.user_agent or DEFAULT_UA
            context = await self._browser.new_context(
                user_agent=ua,
                locale="de-CH",
                timezone_id="Europe/Zurich",
                viewport={"width": 1366, "height": 768},
                ignore_https_errors=False,
                java_script_enabled=True,
            )
            await context.add_init_script(STEALTH_INIT_SCRIPT)

            # Seed cookies the spider already had (e.g. set by the first GET).
            if req.cookies:
                await context.add_cookies(_cookies_for_playwright(req.cookies))

            page = await context.new_page()
        except Exception:
            self._sem.release()
            raise

        session = ChallengeSession(
            session_id=str(uuid.uuid4()),
            correlation_id=req.correlation_id,
            context=context,
            page=page,
        )
        async with self._lock:
            self._sessions[session.session_id] = session

        # Hook routing — every outbound request goes through us.
        await page.route("**/*", lambda route, request: asyncio.create_task(
            self._on_route(session, route, request)
        ))

        # Kick off navigation in the background. The route handler will fire
        # for the very first request; the FastAPI handler will pick that up
        # via session.next_event().
        asyncio.create_task(self._drive(session, req))
        return session

    async def get(self, session_id: str) -> Optional[ChallengeSession]:
        async with self._lock:
            return self._sessions.get(session_id)

    async def close_session(self, session: ChallengeSession) -> None:
        await self._cleanup(session)

    # -- internals -------------------------------------------------------
    async def _drive(self, session: ChallengeSession, req: StartRequest) -> None:
        """Navigate the page; on completion, capture the result."""
        try:
            # Apply request-specific extra headers (UA is set on the context
            # already; everything else passes through here).
            extra = {k: v for k, v in (req.headers or {}).items()
                     if k.lower() not in ("user-agent", "host", "content-length")}
            if extra:
                await session.page.set_extra_http_headers(extra)

            method = (req.method or "GET").upper()
            if method != "GET":
                # Playwright's goto() is GET-only. For non-GET top-level
                # navigations we emulate via fetch() inside the page so the
                # interception path still applies. The result HTML is then the
                # response body.
                # Most challenge URLs are GET, so this is a rare fallback.
                body = ""
                if req.body_b64:
                    body = base64.b64decode(req.body_b64).decode("latin1", "replace")
                await session.page.goto("about:blank")
                resp_text = await session.page.evaluate(
                    """async ({url, method, body, headers}) => {
                        const r = await fetch(url, {
                            method, headers,
                            body: body ? body : undefined,
                            credentials: 'include',
                        });
                        return { status: r.status, headers: Object.fromEntries(r.headers.entries()), body: await r.text(), final_url: r.url };
                    }""",
                    {"url": req.url, "method": method, "body": body, "headers": req.headers or {}},
                )
                # Treat the fetch result as the final document.
                cookies = await session.context.cookies()
                await session.events.put(ChallengeDone(
                    session_id=session.session_id,
                    correlation_id=session.correlation_id,
                    final_url=resp_text["final_url"],
                    status=int(resp_text["status"]),
                    html=resp_text["body"],
                    cookies=cookies,
                    headers={k.title(): v for k, v in (resp_text["headers"] or {}).items()},
                    fetch_count=session.fetch_count,
                    duration_s=time.monotonic() - session.started_at,
                ))
                return

            response = await session.page.goto(
                req.url,
                wait_until="domcontentloaded",
                timeout=int(settings.challenge_timeout_s * 1000),
            )

            # Imperva pages reload themselves once the challenge cookie is set.
            # Wait briefly for the network to quiet down before we snapshot.
            try:
                await session.page.wait_for_load_state(
                    "networkidle", timeout=10_000
                )
            except Exception:
                # networkidle is best-effort; fall through with what we have.
                pass

            html = await session.page.content()
            cookies = await session.context.cookies()
            status_code = response.status if response else 0
            headers = {k.title(): v for k, v in
                       (await response.all_headers()).items()} if response else {}

            await session.events.put(ChallengeDone(
                session_id=session.session_id,
                correlation_id=session.correlation_id,
                final_url=session.page.url,
                status=status_code,
                html=html,
                cookies=cookies,
                headers=headers,
                fetch_count=session.fetch_count,
                duration_s=time.monotonic() - session.started_at,
            ))
        except Exception as e:  # pragma: no cover - defensive
            log.exception("challenge drive failed: %s", e)
            await session.events.put(ChallengeError(
                session_id=session.session_id,
                correlation_id=session.correlation_id,
                code="drive_failed",
                message=f"{type(e).__name__}: {e}",
            ))

    async def _on_route(
        self, session: ChallengeSession, route: Route, request: Request
    ) -> None:
        """Per-page request handler. Suspends the page, asks the caller to
        execute the fetch, then fulfils the route with the response."""
        if session.closed:
            try:
                await route.abort()
            except Exception:
                pass
            return

        if session.fetch_count >= settings.max_fetches_per_challenge:
            log.warning(
                "session=%s exceeded max_fetches_per_challenge=%d",
                session.session_id, settings.max_fetches_per_challenge,
            )
            try:
                await route.abort()
            except Exception:
                pass
            return

        req_id = str(uuid.uuid4())
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        pending = _PendingFetch(req_id=req_id, route=route, request=request,
                                future=future)
        session.pending[req_id] = pending
        session.fetch_count += 1

        # Body — Playwright exposes post_data_buffer().
        body_b64: Optional[str] = None
        try:
            buf = request.post_data_buffer
            if buf:
                body_b64 = base64.b64encode(buf).decode("ascii")
        except Exception:
            pass

        try:
            req_headers = await request.all_headers()
        except Exception:
            req_headers = dict(request.headers or {})

        await session.events.put(NeedFetch(
            req_id=req_id,
            url=request.url,
            method=request.method,
            headers=req_headers,
            body_b64=body_b64,
            session_id=session.session_id,
        ))

        # Wait for /feed to deliver the upstream response.
        try:
            feed: FeedFetch = await asyncio.wait_for(
                future, timeout=settings.feed_wait_timeout_s
            )
        except asyncio.TimeoutError:
            log.warning("session=%s req=%s feed timeout", session.session_id, req_id)
            try:
                await route.abort()
            except Exception:
                pass
            return

        # Hand the response back to the page.
        if feed.error:
            try:
                await route.abort()
            except Exception:
                pass
            return

        try:
            body = base64.b64decode(feed.body_b64) if feed.body_b64 else b""
            # Playwright wants headers as a flat dict; multi-value headers
            # are uncommon for our case.
            await route.fulfill(
                status=feed.status,
                headers=feed.headers or {},
                body=body,
            )
        except Exception as e:
            log.warning("session=%s req=%s fulfill failed: %s",
                        session.session_id, req_id, e)
            try:
                await route.abort()
            except Exception:
                pass

    async def _cleanup(self, session: ChallengeSession) -> None:
        if session.closed:
            return
        session.closed = True
        # Abort any still-pending routes to release the page.
        for p in list(session.pending.values()):
            if not p.future.done():
                p.future.set_exception(RuntimeError("session closed"))
        try:
            await session.page.close()
        except Exception:
            pass
        try:
            await session.context.close()
        except Exception:
            pass
        async with self._lock:
            self._sessions.pop(session.session_id, None)
        try:
            self._sem.release()
        except ValueError:
            pass


def _cookies_for_playwright(cookies: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Normalise input cookies into Playwright's ``add_cookies`` format.

    Playwright requires either ``url`` OR (``domain`` AND ``path``) per cookie;
    most upstream callers send just name/value/domain — fill in the gaps.
    """
    out: List[Dict[str, Any]] = []
    for c in cookies:
        if not c.get("name") or "value" not in c:
            continue
        item = {"name": c["name"], "value": c["value"]}
        if "url" in c:
            item["url"] = c["url"]
        else:
            item["domain"] = c.get("domain", "")
            item["path"] = c.get("path", "/")
        for k in ("expires", "httpOnly", "secure", "sameSite"):
            if k in c:
                item[k] = c[k]
        out.append(item)
    return out


# Engine singleton (constructed by FastAPI lifespan).
engine = BrowserEngine()
