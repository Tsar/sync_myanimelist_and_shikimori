"""Shikimori API — thin wrappers for the endpoints sync.py needs.

All requests go through an aiohttp.ClientSession that the caller creates
with ``User-Agent: sync_myanimelist_and_shikimori`` — Shikimori rejects
generic User-Agents.
"""

from __future__ import annotations

import asyncio
import sys

import aiohttp


API_BASE = "https://shikimori.io/api"
API_V2 = f"{API_BASE}/v2"

# Delay between sequential public API calls (e.g. title lookups). Shikimori's
# documented global limit is 90 rpm (1.5 rps) — 0.8 s gives ~75 rpm, with a
# small margin under the cap.
_POLITE_DELAY_SEC = 0.8


def _auth(access_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {access_token}"}


async def get_user_id(
    session: aiohttp.ClientSession, access_token: str
) -> tuple[int, str]:
    """Return (id, nickname) for the authenticated Shikimori user."""
    async with session.get(
        f"{API_BASE}/users/whoami", headers=_auth(access_token)
    ) as resp:
        resp.raise_for_status()
        data = await resp.json()
        return data["id"], data["nickname"]


async def get_anime_list(
    session: aiohttp.ClientSession, access_token: str, user_id: int
) -> list[dict]:
    """Return the full list of anime user_rates for ``user_id``.

    Per Shikimori's docs, passing ``user_id`` disables pagination — the
    whole list comes back in one response.
    """
    async with session.get(
        f"{API_V2}/user_rates",
        params={"user_id": str(user_id), "target_type": "Anime"},
        headers=_auth(access_token),
    ) as resp:
        resp.raise_for_status()
        return await resp.json()


async def get_anime_title(session: aiohttp.ClientSession, anime_id: int) -> str | None:
    """Title lookup for a Shikimori (== MAL) anime id.

    Returns ``russian`` if set, else ``name``, else ``None`` on any error or
    empty payload. The caller is responsible for any fallback or caching —
    returning ``None`` on error lets the caller avoid caching failures.

    No auth required for this public endpoint, but the session's User-Agent
    header is still applied. Includes a small delay to stay polite.
    """
    await asyncio.sleep(_POLITE_DELAY_SEC)
    try:
        async with session.get(f"{API_BASE}/animes/{anime_id}") as resp:
            resp.raise_for_status()
            data = await resp.json()
    except aiohttp.ClientError as e:
        print(f"    ! fetch error for anime #{anime_id}: {e}", file=sys.stderr)
        return None
    title = data.get("russian") or data.get("name")
    if not title:
        print(f"    ! no title in response for anime #{anime_id}", file=sys.stderr)
        return None
    return title


async def create_list_entry(
    session: aiohttp.ClientSession,
    access_token: str,
    user_id: int,
    anime_id: int,
    *,
    status: str,
    score: int,
    episodes: int,
) -> dict:
    """Create a Shikimori user_rate for ``anime_id``."""
    payload = {
        "user_rate": {
            "user_id": user_id,
            "target_id": anime_id,
            "target_type": "Anime",
            "status": status,
            "score": score,
            "episodes": episodes,
        }
    }
    async with session.post(
        f"{API_V2}/user_rates",
        json=payload,
        headers={
            **_auth(access_token),
            "Content-Type": "application/json",
        },
    ) as resp:
        resp.raise_for_status()
        return await resp.json()


async def update_list_entry(
    session: aiohttp.ClientSession,
    access_token: str,
    user_rate_id: int,
    *,
    status: str,
    score: int,
    episodes: int,
) -> dict:
    """Update an existing Shikimori user_rate by its ``user_rate_id``.

    ``user_rate_id`` is the rate row's own id (returned in the
    ``user_rates`` list response), distinct from the anime's ``target_id``.
    PATCH is partial — fields not sent are left untouched — but we always
    send the three canonical fields the sync model knows about.
    """
    payload = {
        "user_rate": {
            "status": status,
            "score": score,
            "episodes": episodes,
        }
    }
    async with session.patch(
        f"{API_V2}/user_rates/{user_rate_id}",
        json=payload,
        headers={
            **_auth(access_token),
            "Content-Type": "application/json",
        },
    ) as resp:
        resp.raise_for_status()
        return await resp.json()
