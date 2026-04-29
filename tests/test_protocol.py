"""Lightweight contract tests for the protocol models — no browser needed."""

from __future__ import annotations

import base64

from app.protocol import (
    ChallengeDone,
    ChallengeError,
    FeedFetch,
    NeedFetch,
    StartRequest,
)


def test_start_request_minimum() -> None:
    sr = StartRequest(url="https://example.com/")
    assert sr.method == "GET"
    assert sr.headers == {}
    assert sr.cookies == []


def test_need_fetch_state_literal() -> None:
    nf = NeedFetch(
        req_id="r1",
        url="https://example.com/_Incapsula_Resource",
        method="GET",
        headers={"User-Agent": "test"},
        session_id="s1",
    )
    payload = nf.model_dump()
    assert payload["state"] == "need_fetch"
    assert payload["req_id"] == "r1"


def test_done_round_trip() -> None:
    cd = ChallengeDone(
        session_id="s1",
        final_url="https://example.com/",
        status=200,
        html="<html></html>",
        cookies=[{"name": "incap_ses_x", "value": "abc"}],
        headers={"Content-Type": "text/html"},
        fetch_count=3,
        duration_s=4.2,
    )
    payload = cd.model_dump()
    assert payload["state"] == "done"
    assert payload["fetch_count"] == 3
    # round-trip
    cd2 = ChallengeDone.model_validate(payload)
    assert cd2.html == "<html></html>"


def test_feed_fetch_b64() -> None:
    body = b"<html>imperva</html>"
    feed = FeedFetch(status=200, headers={"Content-Type": "text/html"},
                     body_b64=base64.b64encode(body).decode("ascii"))
    assert base64.b64decode(feed.body_b64) == body


def test_error_payload() -> None:
    err = ChallengeError(code="drive_failed", message="boom")
    assert err.state == "error"
    payload = err.model_dump()
    assert payload["code"] == "drive_failed"
