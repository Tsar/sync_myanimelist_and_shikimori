"""MyAnimeList API v2 — thin wrappers for the endpoints sync.py needs.

Only the read and write calls required for the current sync iteration are
here; more will land when update/delete support arrives.
"""

from __future__ import annotations

import aiohttp


API_BASE = "https://api.myanimelist.net/v2"


def _auth(access_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {access_token}"}


async def get_user_id(
    session: aiohttp.ClientSession, access_token: str
) -> tuple[int, str]:
    """Return (id, name) for the authenticated MAL user."""
    async with session.get(f"{API_BASE}/users/@me", headers=_auth(access_token)) as resp:
        resp.raise_for_status()
        data = await resp.json()
        return data["id"], data["name"]


async def get_anime_list(
    session: aiohttp.ClientSession, access_token: str
) -> list[dict]:
    """Return the full anime list for @me as a list of raw {node, list_status} dicts.

    Follows paging.next until there are no more pages.
    """
    url: str | None = (
        f"{API_BASE}/users/@me/animelist?fields=list_status&limit=1000"
    )
    entries: list[dict] = []
    while url:
        async with session.get(url, headers=_auth(access_token)) as resp:
            resp.raise_for_status()
            page = await resp.json()
        entries.extend(page.get("data", []))
        url = page.get("paging", {}).get("next")
    return entries


async def create_or_update_list_entry(
    session: aiohttp.ClientSession,
    access_token: str,
    anime_id: int,
    *,
    status: str,
    score: int,
    num_watched_episodes: int,
    is_rewatching: bool,
) -> dict:
    """Create or update a MAL list entry for ``anime_id``.

    MAL's update-my-list-status endpoint is a single call that creates the
    entry if missing or updates it otherwise. The HTTP verb is ``PUT`` per the
    MAL docs' own curl example (the endpoint badge says PATCH — MAL's docs
    are inconsistent, PUT works).
    """
    form = {
        "status": status,
        "score": str(score),
        "num_watched_episodes": str(num_watched_episodes),
        "is_rewatching": "true" if is_rewatching else "false",
    }
    async with session.put(
        f"{API_BASE}/anime/{anime_id}/my_list_status",
        headers={
            **_auth(access_token),
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data=form,
    ) as resp:
        resp.raise_for_status()
        return await resp.json()
