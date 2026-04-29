# headless-scraping-subsystem

A small FastAPI + Playwright service that solves Imperva (Incapsula) JS
challenges on behalf of a scraper that *cannot* run a browser itself —
typically Scrapy Cloud spiders for `bger.ch` / `entscheidsuche.ch`.

The trick: the headless browser does **not** make outbound HTTP itself. It
intercepts every request the page would issue, hands it to the calling
spider, waits for the response over the spider's egress IP, and only then
lets the page continue. That way the IP/cookie pair Imperva pins to never
splits across egress points.

## Architecture

```
Scrapy Cloud spider                Subsystem (this repo)        bger.ch + Imperva
-------------------                ---------------------        -----------------
GET /entscheid/...        ------>                               returns 403 + JS challenge
                          <------  detects challenge in response

POST /headless/challenge/start
  { url, cookies, headers }  --->  open BrowserContext
                                   navigate(url)
                          <------  200 NeedFetch { req_id, url, headers, ... }

(spider performs the fetch ----->  bger.ch/imperva
 over its own IP)         <------  upstream response

POST /headless/challenge/feed/{req_id}?session_id=...
  { status, headers, body_b64 }
                          <------  200 NeedFetch (next subresource)
                                   ... loop ...
                          <------  200 ChallengeDone { html, cookies, ... }
```

The spider then re-issues its original request with the new cookies — Imperva
now treats it as a solved session and returns content.

## Endpoints

All under the prefix `/headless` so the production URL is
`https://files.entscheidsuche.ch/headless/...`.

| Method | Path | Purpose |
| ------ | ---- | ------- |
| POST | `/headless/challenge/start` | Start a solve; returns first event |
| POST | `/headless/challenge/feed/{req_id}?session_id=…` | Feed an upstream response, get the next event |
| POST | `/headless/challenge/cancel/{session_id}` | Tear down a stuck session |
| GET  | `/headless/health` | Liveness probe |

Auth: every endpoint expects `Authorization: Bearer $BEARER_TOKEN` once the
token is set in env (it is empty by default for local dev).

## Local development

```bash
# 1. Python venv + deps
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
playwright install chromium
playwright install-deps chromium    # Linux: pulls system libs

# 2. Run the service
uvicorn app.main:app --host 127.0.0.1 --port 8088 --reload

# 3. Sanity check
curl -s http://127.0.0.1:8088/headless/health
```

## Deployment (Debian)

The bundled installer assumes Debian 12+ with systemd:

```bash
sudo bash deploy/install.sh
# then edit:
sudo $EDITOR /etc/headless-scraping-subsystem.env
sudo systemctl enable --now headless-scraping-subsystem
```

The systemd unit binds the app to `127.0.0.1:8088`. nginx terminates TLS for
`files.entscheidsuche.ch` and reverse-proxies `/headless/` — see
`deploy/nginx/files.entscheidsuche.ch.conf` for the location block to splice
into the existing vhost.

## Calling from a Scrapy spider

Pseudo-code for the new client we will add to `CH_BGE.py`:

```python
def parse(self, response):
    if self._is_imperva_challenge(response):
        cookies = response.headers.getlist(b'Set-Cookie')
        return self._solve_challenge(response.url, cookies)
    ...

def _solve_challenge(self, url, cookies):
    payload = {'url': url, 'cookies': _normalize(cookies),
               'user_agent': self._chosen_ua,
               'correlation_id': self._req_id()}
    r = requests.post('https://files.entscheidsuche.ch/headless/challenge/start',
                      json=payload,
                      headers={'Authorization': f'Bearer {TOKEN}'})
    while r.json()['state'] == 'need_fetch':
        ev = r.json()
        upstream = self._fetch_via_zyte(ev['url'], ev['method'],
                                        ev['headers'], ev.get('body_b64'))
        r = requests.post(
            f'https://files.entscheidsuche.ch/headless/challenge/feed/{ev["req_id"]}',
            params={'session_id': ev['session_id']},
            headers={'Authorization': f'Bearer {TOKEN}'},
            json=upstream)
    if r.json()['state'] == 'done':
        # Re-issue the original request with the new cookies; we are through.
        return self._reissue(url, r.json()['cookies'])
```

Adding this path to `CH_BGE.py` is **task 2** of the integration; this
repository is task 1 (the service itself).

## Operational notes

- **Memory**: every concurrent challenge holds its own BrowserContext
  (~150-250 MB). `BROWSER_POOL_SIZE` caps that.
- **Stealth**: see `STEALTH_INIT_SCRIPT` in `app/browser.py`. Imperva's
  detector evolves; if false-positives spike, expand the script.
- **Logging**: all events go to stdout/journalctl. Tail with
  `journalctl -u headless-scraping-subsystem -f`.
- **Security**: never expose the service publicly without the bearer token.
  The provided nginx snippet does **not** do its own auth; that is up to the
  upstream Bearer token, which is constant-time-checked in `app/auth.py`.

## Tests

```bash
pytest -q tests/
```

`tests/test_challenge_solve.py` covers the protocol contract end-to-end with
a mocked target. A live integration test is documented inline but skipped by
default — set `HEADLESS_E2E=1` and supply a known-challenged URL via
`HEADLESS_E2E_URL` to run it.
