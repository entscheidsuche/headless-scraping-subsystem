#!/usr/bin/env python3
"""End-to-end smoke test of the challenge protocol.

Reads the bearer token from /etc/headless-scraping-subsystem.env, opens a
challenge for the URL given on the command line (default: example.com),
plays the need_fetch loop by performing real HTTP fetches via httpx, and
prints the final state.

Usage:
    sudo /opt/headless-scraping-subsystem/.venv/bin/python \
        /opt/src/headless-scraping-subsystem/scripts/smoke_loop.py
    # or with a custom URL:
    sudo /opt/headless-scraping-subsystem/.venv/bin/python \
        /opt/src/headless-scraping-subsystem/scripts/smoke_loop.py \
        https://www.bger.ch/
"""

from __future__ import annotations

import base64
import json
import sys

import httpx


ENV_FILE = "/etc/headless-scraping-subsystem.env"
BASE = "http://127.0.0.1:8088/headless"


def load_token(path: str = ENV_FILE) -> str:
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line.startswith("BEARER_TOKEN="):
                return line.split("=", 1)[1]
    raise RuntimeError(f"BEARER_TOKEN not found in {path}")


def main() -> int:
    url = sys.argv[1] if len(sys.argv) > 1 else "https://example.com/"
    token = load_token()
    headers = {"Authorization": f"Bearer {token}",
               "Content-Type": "application/json"}

    # 1) Open the challenge.
    r = httpx.post(
        f"{BASE}/challenge/start",
        headers=headers,
        timeout=60,
        json={"url": url, "correlation_id": "smoke-loop"},
    ).json()
    print(">>> opened:", r.get("state"), r.get("url") or r.get("final_url"))

    # 2) need_fetch loop.
    fetches = 0
    while r.get("state") == "need_fetch":
        fetches += 1
        body = base64.b64decode(r["body_b64"]) if r.get("body_b64") else None
        try:
            upstream = httpx.request(
                r["method"], r["url"],
                headers=r["headers"],
                content=body,
                follow_redirects=False,
                timeout=30,
            )
            feed = {
                "status": upstream.status_code,
                "headers": dict(upstream.headers),
                "body_b64": base64.b64encode(upstream.content).decode("ascii"),
            }
        except Exception as e:
            feed = {"status": 0, "headers": {}, "body_b64": "",
                    "error": f"{type(e).__name__}: {e}"}

        next_r = httpx.post(
            f"{BASE}/challenge/feed/{r['req_id']}",
            params={"session_id": r["session_id"]},
            headers=headers,
            timeout=60,
            json=feed,
        ).json()
        print(f"   #{fetches}: fed {feed['status']} for {r['url'][:80]}"
              f" -> next state={next_r.get('state')}")
        r = next_r

    # 3) Final result.
    print("=== final:", r.get("state"))
    if r.get("state") == "done":
        print(f"status: {r['status']}  fetches: {r['fetch_count']}"
              f"  duration: {round(r['duration_s'], 2)}s")
        print(f"html-len: {len(r['html'])}")
        snippet = r["html"][:240].replace("\n", " ").replace("\r", " ")
        print(f"first 240 chars: {snippet}")
        # Selected cookies
        names = [c.get("name") for c in r.get("cookies", [])]
        if names:
            print(f"cookies: {names}")
    else:
        print(json.dumps(r, indent=2))
    return 0 if r.get("state") == "done" else 1


if __name__ == "__main__":
    sys.exit(main())
