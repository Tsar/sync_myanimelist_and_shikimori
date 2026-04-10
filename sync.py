#!/usr/bin/env python3
"""Sync MyAnimeList ↔ Shikimori anime lists.

First iteration: find anime that are present on one service but missing on
the other and create the missing entries on the target service. By default
each create is confirmed interactively; pass ``--autosync`` to skip the
prompts and create everything. Updates and deletions are out of scope for
this pass.

Run:
    python3 sync.py            # interactive per-entry confirmation
    python3 sync.py --autosync # create everything without prompting
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import aiohttp

import myanimelist_api
import myanimelist_auth
import shikimori_api
import shikimori_auth


USER_AGENT = "sync_myanimelist_and_shikimori"

# Seconds to wait between successful write requests in --autosync mode.
# In interactive mode the confirmation prompt already provides pacing, so no
# extra sleep is needed there.
_WRITE_DELAY_SEC = 1.0

PROJECT_DIR = Path(__file__).resolve().parent
# Disk cache for Shikimori anime titles so re-runs don't re-fetch everything.
TITLE_CACHE_PATH = PROJECT_DIR / "shikimori_titles_cache.json"


def _load_title_cache() -> dict[int, str]:
    if not TITLE_CACHE_PATH.exists():
        return {}
    try:
        raw = json.loads(TITLE_CACHE_PATH.read_text())
    except json.JSONDecodeError:
        return {}
    return {int(k): v for k, v in raw.items()}


def _save_title_cache(cache: dict[int, str]) -> None:
    # Atomic save: write to a sibling temp file then rename, so a Ctrl+C
    # mid-write can't leave a truncated cache behind.
    tmp = TITLE_CACHE_PATH.with_suffix(TITLE_CACHE_PATH.suffix + ".tmp")
    tmp.write_text(json.dumps(cache, ensure_ascii=False, indent=2))
    os.replace(tmp, TITLE_CACHE_PATH)


# ---------------------------------------------------------------------------
# Canonical model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ListEntry:
    anime_id: int
    title: str
    status: str  # canonical: planned | watching | rewatching | completed | on_hold | dropped
    score: int
    episodes: int


# MAL <-> canonical -----------------------------------------------------------

_MAL_TO_CANONICAL = {
    "plan_to_watch": "planned",
    "watching": "watching",
    "completed": "completed",
    "on_hold": "on_hold",
    "dropped": "dropped",
}

# canonical -> (mal_status, is_rewatching)
_CANONICAL_TO_MAL = {
    "planned": ("plan_to_watch", False),
    "watching": ("watching", False),
    "rewatching": ("watching", True),
    "completed": ("completed", False),
    "on_hold": ("on_hold", False),
    "dropped": ("dropped", False),
}


def _mal_to_canonical(list_status: dict) -> str:
    mal_status = list_status["status"]
    if list_status.get("is_rewatching") and mal_status == "watching":
        return "rewatching"
    try:
        return _MAL_TO_CANONICAL[mal_status]
    except KeyError:
        raise ValueError(f"Unknown MAL status: {mal_status!r}") from None


# Shikimori <-> canonical -----------------------------------------------------

_SHIKI_TO_CANONICAL = {
    "planned": "planned",
    "watching": "watching",
    "rewatching": "rewatching",
    "completed": "completed",
    "on_hold": "on_hold",
    "dropped": "dropped",
}

_CANONICAL_TO_SHIKI = {v: k for k, v in _SHIKI_TO_CANONICAL.items()}


def _shiki_to_canonical(rate: dict) -> str:
    try:
        return _SHIKI_TO_CANONICAL[rate["status"]]
    except KeyError:
        raise ValueError(f"Unknown Shikimori status: {rate['status']!r}") from None


# ---------------------------------------------------------------------------
# Raw -> ListEntry converters
# ---------------------------------------------------------------------------


def _mal_entry_to_listentry(raw: dict) -> ListEntry:
    node = raw["node"]
    ls = raw["list_status"]
    # MAL's read/write field names for episodes are asymmetric; be defensive.
    episodes = ls.get("num_episodes_watched", ls.get("num_watched_episodes", 0))
    return ListEntry(
        anime_id=node["id"],
        title=node["title"],
        status=_mal_to_canonical(ls),
        score=ls.get("score", 0),
        episodes=episodes,
    )


def _shiki_entry_to_listentry(raw: dict, title: str) -> ListEntry:
    return ListEntry(
        anime_id=raw["target_id"],
        title=title,
        status=_shiki_to_canonical(raw),
        score=raw.get("score", 0),
        episodes=raw.get("episodes", 0),
    )


# ---------------------------------------------------------------------------
# User interaction
# ---------------------------------------------------------------------------


async def _prompt(message: str) -> str:
    return (await asyncio.to_thread(input, message)).strip().lower()


def _print_entry(direction: str, entry: ListEntry) -> None:
    print(f"[{direction}] #{entry.anime_id} {entry.title!r}")
    print(f"  status:   {entry.status}")
    print(f"  score:    {entry.score}")
    print(f"  episodes: {entry.episodes}")


async def _confirm_sync(direction: str, entry: ListEntry) -> str:
    """Prompt the user about one planned create. Returns 'y', 'n' or 'q'.

    Enter (empty input) defaults to 'y'.
    """
    _print_entry(direction, entry)
    while True:
        answer = await _prompt("  Sync? [Y/n/q] ")
        if answer in ("", "y"):
            return "y"
        if answer == "n":
            return "n"
        if answer == "q":
            return "q"
        print("  Please answer y, n, or q.")


# ---------------------------------------------------------------------------
# Sync loops
# ---------------------------------------------------------------------------


async def _push_to_shikimori(
    shiki_session: aiohttp.ClientSession,
    shiki_token: str,
    shiki_user_id: int,
    entries: list[ListEntry],
    tally: dict,
    *,
    autosync: bool,
) -> bool:
    """Create each entry on Shikimori. Returns False if user quit."""
    for entry in entries:
        if autosync:
            _print_entry("MAL → Shiki", entry)
        else:
            choice = await _confirm_sync("MAL → Shiki", entry)
            if choice == "q":
                return False
            if choice == "n":
                tally["skipped"] += 1
                print("  skipped")
                continue
        try:
            await shikimori_api.create_list_entry(
                shiki_session,
                shiki_token,
                shiki_user_id,
                entry.anime_id,
                status=_CANONICAL_TO_SHIKI[entry.status],
                score=entry.score,
                episodes=entry.episodes,
            )
            tally["created"] += 1
            print("  ✓ created on Shikimori")
            if autosync:
                await asyncio.sleep(_WRITE_DELAY_SEC)
        except aiohttp.ClientResponseError as e:
            tally["failed"] += 1
            print(f"  ✗ failed: {e.status} {e.message}")
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            tally["failed"] += 1
            print(f"  ✗ failed: {type(e).__name__}: {e}")
    return True


async def _push_to_mal(
    mal_session: aiohttp.ClientSession,
    mal_token: str,
    entries: list[ListEntry],
    tally: dict,
    *,
    autosync: bool,
) -> bool:
    """Create each entry on MAL. Returns False if user quit."""
    for entry in entries:
        if autosync:
            _print_entry("Shiki → MAL", entry)
        else:
            choice = await _confirm_sync("Shiki → MAL", entry)
            if choice == "q":
                return False
            if choice == "n":
                tally["skipped"] += 1
                print("  skipped")
                continue
        mal_status, is_rewatching = _CANONICAL_TO_MAL[entry.status]
        try:
            await myanimelist_api.create_or_update_list_entry(
                mal_session,
                mal_token,
                entry.anime_id,
                status=mal_status,
                score=entry.score,
                num_watched_episodes=entry.episodes,
                is_rewatching=is_rewatching,
            )
            tally["created"] += 1
            print("  ✓ created on MAL")
            if autosync:
                await asyncio.sleep(_WRITE_DELAY_SEC)
        except aiohttp.ClientResponseError as e:
            tally["failed"] += 1
            print(f"  ✗ failed: {e.status} {e.message}")
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            tally["failed"] += 1
            print(f"  ✗ failed: {type(e).__name__}: {e}")
    return True


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


async def main() -> int:
    parser = argparse.ArgumentParser(
        description="Sync MyAnimeList ↔ Shikimori anime lists."
    )
    parser.add_argument(
        "--autosync",
        action="store_true",
        help="Skip per-entry confirmation prompts and create everything.",
    )
    args = parser.parse_args()

    async with (
        aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}) as mal_session,
        aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}) as shiki_session,
    ):
        print("Authorizing on MyAnimeList...")
        mal_token = await myanimelist_auth.get_access_token(mal_session)
        print("Authorizing on Shikimori...")
        shiki_token = await shikimori_auth.get_access_token(shiki_session)

        (mal_id, mal_name), (shiki_id, shiki_nick) = await asyncio.gather(
            myanimelist_api.get_user_id(mal_session, mal_token),
            shikimori_api.get_user_id(shiki_session, shiki_token),
        )
        print(f"MAL user:       #{mal_id} {mal_name}")
        print(f"Shikimori user: #{shiki_id} {shiki_nick}")

        print("Downloading lists...")
        mal_raw, shiki_raw = await asyncio.gather(
            myanimelist_api.get_anime_list(mal_session, mal_token),
            shikimori_api.get_anime_list(shiki_session, shiki_token, shiki_id),
        )

        mal_entries: dict[int, ListEntry] = {}
        for raw in mal_raw:
            e = _mal_entry_to_listentry(raw)
            mal_entries[e.anime_id] = e

        # Shikimori rates don't include titles — store raw, resolve titles lazily.
        shiki_raw_by_id: dict[int, dict] = {r["target_id"]: r for r in shiki_raw}

        print(
            f"MAL: {len(mal_entries)} entries, "
            f"Shikimori: {len(shiki_raw_by_id)} entries"
        )

        only_in_mal_ids = sorted(mal_entries.keys() - shiki_raw_by_id.keys())
        only_in_shiki_ids = sorted(shiki_raw_by_id.keys() - mal_entries.keys())
        print(
            f"{len(only_in_mal_ids)} to push MAL→Shikimori, "
            f"{len(only_in_shiki_ids)} to push Shikimori→MAL"
        )

        # Build the two work lists.
        to_push_shiki: list[ListEntry] = [mal_entries[i] for i in only_in_mal_ids]

        to_push_mal: list[ListEntry] = []
        if only_in_shiki_ids:
            title_cache = _load_title_cache()
            cached_hits = sum(1 for aid in only_in_shiki_ids if aid in title_cache)
            to_fetch = len(only_in_shiki_ids) - cached_hits
            print(f"Resolving titles for {len(only_in_shiki_ids)} Shikimori entries ({cached_hits} cached, {to_fetch} to fetch)...")
            fetch_idx = 0
            for anime_id in only_in_shiki_ids:
                title = title_cache.get(anime_id)
                if title is None:
                    fetch_idx += 1
                    print(f"  [{fetch_idx}/{to_fetch}] fetching title for anime #{anime_id}...")
                    fetched = await shikimori_api.get_anime_title(shiki_session, anime_id)
                    if fetched is not None:
                        title_cache[anime_id] = fetched
                        _save_title_cache(title_cache)
                        title = fetched
                    else:
                        title = f"(anime #{anime_id})"
                to_push_mal.append(_shiki_entry_to_listentry(shiki_raw_by_id[anime_id], title))

        tally = {"created": 0, "skipped": 0, "failed": 0}

        if to_push_shiki:
            print("\n--- MAL → Shikimori ---")
            if not await _push_to_shikimori(
                shiki_session,
                shiki_token,
                shiki_id,
                to_push_shiki,
                tally,
                autosync=args.autosync,
            ):
                print("Stopped by user.")
                _print_tally(tally)
                return 1 if tally["failed"] else 0

        if to_push_mal:
            print("\n--- Shikimori → MAL ---")
            if not await _push_to_mal(
                mal_session,
                mal_token,
                to_push_mal,
                tally,
                autosync=args.autosync,
            ):
                print("Stopped by user.")
                _print_tally(tally)
                return 1 if tally["failed"] else 0

        if not to_push_shiki and not to_push_mal:
            print("Nothing to sync.")
        _print_tally(tally)
        return 1 if tally["failed"] else 0


def _print_tally(tally: dict) -> None:
    print(
        f"Done. Created: {tally['created']}, "
        f"Skipped: {tally['skipped']}, "
        f"Failed: {tally['failed']}"
    )


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
