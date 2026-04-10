"""Shikimori API — thin wrappers for the endpoints sync.py needs.

All requests go through an aiohttp.ClientSession that the caller creates
with ``User-Agent: sync_myanimelist_and_shikimori`` — Shikimori rejects
generic User-Agents.
"""

from __future__ import annotations

import asyncio

import aiohttp


API_BASE = "https://shikimori.io/api"
API_V2 = f"{API_BASE}/v2"

# Small delay between sequential public API calls (e.g. title lookups) to
# stay well under Shikimori's rate limits.
_POLITE_DELAY_SEC = 0.25


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


async def get_anime_title(session: aiohttp.ClientSession, anime_id: int) -> str:
    """Best-effort title lookup for a Shikimori (== MAL) anime id.

    Returns ``russian`` if set, else ``name``, else a ``#<id>`` placeholder
    on any error. No auth required for this public endpoint, but the
    session's User-Agent header is still applied.
    """
    await asyncio.sleep(_POLITE_DELAY_SEC)
    try:
        async with session.get(f"{API_BASE}/animes/{anime_id}") as resp:
            resp.raise_for_status()
            data = await resp.json()
    except aiohttp.ClientError:
        return f"(anime #{anime_id})"
    return data.get("russian") or data.get("name") or f"(anime #{anime_id})"


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
