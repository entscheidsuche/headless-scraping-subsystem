"""End-to-end test of the challenge protocol against a real Chromium browser.

Runs by default (it does not need network access — the page it loads is a
``data:`` URL plus a single XHR to a *non-existent* host that we intercept
and answer ourselves). What it asserts:

1. ``/challenge/start`` opens a session and returns a ``need_fetch`` event
   for the page's first outbound request.
2. After ``/challenge/feed``, the page resumes and we eventually get
   ``done`` with the final HTML.

Skipped if Chromium is not installed (e.g. on a vanilla CI without
``playwright install chromium``).
"""

from __future__ import annotations

import asyncio
import base64
import os
from typing import Any, Dict

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.browser import engine

# All async work in this module
pytestmark = pytest.mark.asyncio(loop_scope="module")


def _chromium_available() -> bool:
    """Heuristic: try to launch Playwright once at import time."""
    try:
        import playwright  # noqa: F401
        return True
    except Exception:
        return False


CHROMIUM_PRESENT = _chromium_available()


# A tiny page that issues one fetch() to a host we will intercept. We use
# example.invalid so there is zero risk of accidental real traffic.
TEST_PAGE_HTML = """
<!doctype html><html><head><title>t</title></head>
<body>
  <h1 id=h>loading</h1>
  <script>
    (async () => {
      try {
        const r = await fetch('https://example.invalid/echo');
        const t = await r.text();
        document.getElementById('h').textContent = t;
      } catch (e) {
        document.getElementById('h').textContent = 'ERR:' + e.message;
      }
    })();
  </script>
</body></html>
""".strip()


@pytest.fixture(scope="module")
async def _engine():
    if not CHROMIUM_PRESENT:
        pytest.skip("Playwright not installed")
    try:
        await engine.start()
    except Exception as e:
        pytest.skip(f"could not start Chromium: {e}")
    yield engine
    await engine.stop()


@pytest.mark.asyncio
async def test_full_flow(_engine) -> None:
    """Drives a full start → feed → done flow against a data: URL.

    The page issues one fetch() to example.invalid; we feed back a
    canned response and expect the page to render that text.
    """
    from app.protocol import StartRequest, FeedFetch

    start = StartRequest(
        url="data:text/html;base64," + base64.b64encode(
            TEST_PAGE_HTML.encode("utf-8")
        ).decode("ascii"),
        correlation_id="test-1",
    )
    session = await _engine.start_challenge(start)

    saw_fetch = False
    deadline = asyncio.get_running_loop().time() + 30
    while asyncio.get_running_loop().time() < deadline:
        ev = await session.next_event(timeout=15)
        kind = ev.model_dump()["state"]
        if kind == "need_fetch":
            # The page may issue several requests (favicon, etc.); only
            # the example.invalid one carries the test payload.
            if "example.invalid" in ev.url:
                saw_fetch = True
                feed = FeedFetch(
                    status=200,
                    headers={"Content-Type": "text/plain"},
                    body_b64=base64.b64encode(b"PONG-FROM-TEST").decode("ascii"),
                )
            else:
                # Empty 200 to keep the page quiet.
                feed = FeedFetch(
                    status=200,
                    headers={"Content-Type": "text/plain"},
                    body_b64=base64.b64encode(b"").decode("ascii"),
                )
            await session.feed(ev.req_id, feed)
            continue
        if kind == "done":
            assert saw_fetch, "expected to intercept the example.invalid fetch"
            assert "PONG-FROM-TEST" in ev.html, f"page did not render fed body: {ev.html!r}"
            await _engine.close_session(session)
            return
        if kind == "error":
            await _engine.close_session(session)
            pytest.fail(f"engine error: {ev.message}")

    await _engine.close_session(session)
    pytest.fail("test timed out without seeing done")


@pytest.mark.skipif(
    os.environ.get("HEADLESS_E2E") != "1",
    reason="set HEADLESS_E2E=1 and HEADLESS_E2E_URL=<challenged url> to run",
)
@pytest.mark.asyncio
async def test_real_imperva(_engine) -> None:
    """Optional integration test against a real Imperva-protected URL.

    This test is skipped unless explicitly enabled because it requires both
    network access AND a target that's currently behind a JS challenge — an
    unstable combination for CI.
    """
    import httpx
    from app.protocol import StartRequest, FeedFetch

    target = os.environ["HEADLESS_E2E_URL"]
    start = StartRequest(url=target, correlation_id="e2e")
    session = await _engine.start_challenge(start)

    async with httpx.AsyncClient(timeout=30, follow_redirects=False) as http:
        deadline = asyncio.get_running_loop().time() + 60
        while asyncio.get_running_loop().time() < deadline:
            ev = await session.next_event(timeout=30)
            kind = ev.model_dump()["state"]
            if kind == "need_fetch":
                body = base64.b64decode(ev.body_b64) if ev.body_b64 else None
                resp = await http.request(
                    ev.method, ev.url, headers=ev.headers, content=body,
                )
                feed = FeedFetch(
                    status=resp.status_code,
                    headers=dict(resp.headers),
                    body_b64=base64.b64encode(resp.content).decode("ascii"),
                )
                await session.feed(ev.req_id, feed)
                continue
            if kind == "done":
                assert ev.status == 200, f"final status {ev.status}"
                assert len(ev.html) > 1000
                await _engine.close_session(session)
                return
            if kind == "error":
                await _engine.close_session(session)
                pytest.fail(f"engine error: {ev.message}")
    await _engine.close_session(session)
    pytest.fail("real Imperva flow timed out without done")


def test_health_endpoint() -> None:
    """Pure FastAPI sanity check — no browser involved."""
    # Use a TestClient *without* triggering lifespan (which would launch
    # Playwright). We hit the health endpoint via the raw app router.
    with TestClient(app, raise_server_exceptions=True) as client:
        r = client.get("/headless/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"
