"""Shikimori OAuth2 authorization.

Obtains and refreshes an access token for the Shikimori API using the
OAuth2 authorization code flow with a local HTTP callback. Tokens are
cached in ``shikimori_tokens.json`` so subsequent runs don't need a
browser until the refresh token stops working.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import sys
import time
import urllib.parse
import webbrowser
from pathlib import Path

import aiohttp
from aiohttp import web


BASE_URL = "https://shikimori.io"
AUTHORIZE_URL = f"{BASE_URL}/oauth/authorize"
TOKEN_URL = f"{BASE_URL}/oauth/token"
WHOAMI_URL = f"{BASE_URL}/api/users/whoami"

REDIRECT_HOST = "127.0.0.1"
REDIRECT_PORT = 53682
REDIRECT_PATH = "/callback"
REDIRECT_URI = f"http://{REDIRECT_HOST}:{REDIRECT_PORT}{REDIRECT_PATH}"

SCOPE = "user_rates"
USER_AGENT = "sync_myanimelist_and_shikimori"

PROJECT_DIR = Path(__file__).resolve().parent
CREDS_PATH = PROJECT_DIR / "shikimori_app_oauth2_creds.json"
TOKENS_PATH = PROJECT_DIR / "shikimori_tokens.json"

EXPIRY_SAFETY_MARGIN_SEC = 60


def _load_creds() -> tuple[str, str]:
    data = json.loads(CREDS_PATH.read_text())
    return data["client_id"], data["client_secret"]


def _load_tokens() -> dict | None:
    if not TOKENS_PATH.exists():
        return None
    try:
        return json.loads(TOKENS_PATH.read_text())
    except json.JSONDecodeError:
        return None


def _save_tokens(tokens: dict) -> None:
    TOKENS_PATH.write_text(json.dumps(tokens, indent=2))
    try:
        os.chmod(TOKENS_PATH, 0o600)
    except OSError:
        pass


def _is_expired(tokens: dict) -> bool:
    created_at = tokens.get("created_at", 0)
    expires_in = tokens.get("expires_in", 0)
    return time.time() >= created_at + expires_in - EXPIRY_SAFETY_MARGIN_SEC


async def _run_browser_flow(
    session: aiohttp.ClientSession, client_id: str, client_secret: str
) -> dict:
    state = secrets.token_urlsafe(32)
    captured: dict = {}
    done = asyncio.Event()

    async def handle_callback(request: web.Request) -> web.Response:
        error = request.query.get("error")
        if error:
            captured["error"] = error
            done.set()
            return web.Response(
                status=400,
                content_type="text/html",
                text=f"<p>Shikimori returned an error: {error}</p>",
            )

        code = request.query.get("code")
        recv_state = request.query.get("state")
        if not code or recv_state != state:
            captured["error"] = "invalid_callback"
            done.set()
            return web.Response(
                status=400,
                content_type="text/html",
                text="<p>Invalid callback (missing code or bad state).</p>",
            )

        captured["code"] = code
        done.set()
        return web.Response(
            content_type="text/html",
            text="<p>Authorization complete. You can close this tab and return to the terminal.</p>",
        )

    app = web.Application()
    app.router.add_get(REDIRECT_PATH, handle_callback)

    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, REDIRECT_HOST, REDIRECT_PORT)
    await site.start()

    try:
        authorize_url = (
            AUTHORIZE_URL
            + "?"
            + urllib.parse.urlencode(
                {
                    "client_id": client_id,
                    "redirect_uri": REDIRECT_URI,
                    "response_type": "code",
                    "scope": SCOPE,
                    "state": state,
                }
            )
        )
        print(f"Opening browser for Shikimori authorization...\n  {authorize_url}")
        webbrowser.open(authorize_url)
        await done.wait()
    finally:
        await runner.cleanup()

    if "error" in captured:
        raise RuntimeError(f"Authorization failed: {captured['error']}")

    return await _exchange_code(session, client_id, client_secret, captured["code"])


async def _exchange_code(
    session: aiohttp.ClientSession, client_id: str, client_secret: str, code: str
) -> dict:
    async with session.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "redirect_uri": REDIRECT_URI,
        },
    ) as resp:
        resp.raise_for_status()
        return await resp.json()


async def _refresh(
    session: aiohttp.ClientSession,
    client_id: str,
    client_secret: str,
    refresh_token: str,
) -> dict:
    async with session.post(
        TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
        },
    ) as resp:
        resp.raise_for_status()
        return await resp.json()


async def get_access_token(session: aiohttp.ClientSession) -> str:
    """Return a valid Shikimori access token, refreshing or re-authorizing as needed.

    The caller provides an ``aiohttp.ClientSession`` (it should be created with
    ``headers={"User-Agent": USER_AGENT}`` — Shikimori rejects generic UAs).
    """
    client_id, client_secret = _load_creds()
    tokens = _load_tokens()

    if tokens and not _is_expired(tokens):
        return tokens["access_token"]

    if tokens and tokens.get("refresh_token"):
        try:
            refreshed = await _refresh(
                session, client_id, client_secret, tokens["refresh_token"]
            )
            # If Shikimori ever stops returning a new refresh_token on refresh,
            # keep the old one — otherwise the next run has no way back.
            refreshed.setdefault("refresh_token", tokens["refresh_token"])
            _save_tokens(refreshed)
            return refreshed["access_token"]
        except aiohttp.ClientResponseError as e:
            print(
                f"Refresh failed ({e}); falling back to browser flow.",
                file=sys.stderr,
            )

    tokens = await _run_browser_flow(session, client_id, client_secret)
    _save_tokens(tokens)
    return tokens["access_token"]


async def _whoami(session: aiohttp.ClientSession, access_token: str) -> dict:
    async with session.get(
        WHOAMI_URL,
        headers={"Authorization": f"Bearer {access_token}"},
    ) as resp:
        resp.raise_for_status()
        return await resp.json()


async def _main() -> None:
    async with aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}) as session:
        token = await get_access_token(session)
        me = await _whoami(session, token)
        print(json.dumps(me, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(_main())
