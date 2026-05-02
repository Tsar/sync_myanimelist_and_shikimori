#!/usr/bin/env python3
"""Sync MyAnimeList ↔ Shikimori anime lists.

Three phases:
  1. Create on Shikimori anything that exists only on MAL.
  2. Create on MAL anything that exists only on Shikimori.
  3. For entries present on both sides, diff the tracked fields and push
     the "newer" side over the older one. Ambiguous diffs (each side
     ahead on a different field, disagreeing non-zero scores, or status
     pairs that aren't strictly ordered like rewatching↔completed) are
     surfaced as conflicts for the user to resolve.

By default every write is confirmed interactively; pass ``--autosync`` to
skip the prompts. In ``--autosync`` mode conflicts are skipped — the tool
never silently overwrites ambiguous user data. Deletions are still out
of scope.

Run:
    python3 sync.py            # interactive per-entry confirmation
    python3 sync.py --autosync # write everything non-conflicting without prompting
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
    rewatches: int


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
# Update-diff heuristics
# ---------------------------------------------------------------------------

# Strict "earlier < later" pairs on the canonical status. The relation is a
# *partial* order — pairs not listed (in either direction) are incomparable
# and surface as conflicts that the user has to resolve. Notably:
#   - on_hold ↔ dropped: both are reassessment states, neither strictly later.
#   - rewatching ↔ {on_hold, dropped, completed}: rewatching is the user's
#     deliberate re-pass; the tool must not auto-overwrite a completed entry
#     just because the other side flipped to rewatching.
_STATUS_LT: frozenset[tuple[str, str]] = frozenset({
    ("planned", "watching"),
    ("planned", "on_hold"),
    ("planned", "dropped"),
    ("planned", "completed"),
    ("planned", "rewatching"),
    ("watching", "on_hold"),
    ("watching", "dropped"),
    ("watching", "completed"),
    ("watching", "rewatching"),
    ("on_hold", "completed"),
    ("dropped", "completed"),
})


def _status_cmp(a: str, b: str) -> int | None:
    """Partial-order compare. -1 if a<b, 0 if equal, 1 if a>b, None if incomparable."""
    if a == b:
        return 0
    if (a, b) in _STATUS_LT:
        return -1
    if (b, a) in _STATUS_LT:
        return 1
    return None


def _pick_winner(mal: "ListEntry", shiki: "ListEntry") -> str:
    """Decide which side is "newer" for an entry present on both services.

    Returns one of:
        "agree"    — the two sides already match on every tracked field.
        "mal"      — MAL has the newer state; push MAL → Shikimori.
        "shiki"    — Shikimori has the newer state; push Shiki → MAL.
        "conflict" — ambiguous; the user (or autosync skip) must decide.
    """
    status_cmp = _status_cmp(mal.status, shiki.status)
    if status_cmp is None:
        return "conflict"

    # Score: zero == unset, so non-zero wins over zero. Two disagreeing
    # non-zero scores are a real conflict — never silently overwrite a
    # rating the user set by hand.
    if mal.score == shiki.score:
        score_winner: str | None = None
    elif mal.score == 0:
        score_winner = "shiki"
    elif shiki.score == 0:
        score_winner = "mal"
    else:
        return "conflict"

    if mal.episodes == shiki.episodes:
        episodes_winner: str | None = None
    elif mal.episodes > shiki.episodes:
        episodes_winner = "mal"
    else:
        episodes_winner = "shiki"

    if mal.rewatches == shiki.rewatches:
        rewatches_winner: str | None = None
    elif mal.rewatches > shiki.rewatches:
        rewatches_winner = "mal"
    else:
        rewatches_winner = "shiki"

    if status_cmp == 0:
        status_winner: str | None = None
    elif status_cmp > 0:
        status_winner = "mal"
    else:
        status_winner = "shiki"

    winners = {w for w in (status_winner, score_winner, episodes_winner, rewatches_winner) if w}
    if not winners:
        return "agree"
    if len(winners) == 1:
        return winners.pop()
    return "conflict"


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
        rewatches=ls.get("num_times_rewatched", 0),
    )


def _shiki_entry_to_listentry(raw: dict, title: str) -> ListEntry:
    return ListEntry(
        anime_id=raw["target_id"],
        title=title,
        status=_shiki_to_canonical(raw),
        score=raw.get("score", 0),
        episodes=raw.get("episodes", 0),
        rewatches=raw.get("rewatches", 0),
    )


# ---------------------------------------------------------------------------
# User interaction
# ---------------------------------------------------------------------------


async def _prompt(message: str) -> str:
    return (await asyncio.to_thread(input, message)).strip().lower()


def _print_entry(direction: str, entry: ListEntry) -> None:
    print(f"[{direction}] #{entry.anime_id} {entry.title!r}")
    print(f"  status:    {entry.status}")
    print(f"  score:     {entry.score}")
    print(f"  episodes:  {entry.episodes}")
    print(f"  rewatches: {entry.rewatches}")


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


def _print_update_diff(mal: ListEntry, shiki: ListEntry, verdict: str) -> None:
    """Show a side-by-side view of an entry that exists on both sides."""
    title = mal.title or shiki.title
    print(f"[update] #{mal.anime_id} {title!r}")
    print(f"  {'':<8} {'MAL':<14} {'Shiki':<14}")
    for field in ("status", "score", "episodes", "rewatches"):
        m = getattr(mal, field)
        s = getattr(shiki, field)
        marker = "  " if m == s else " *"
        print(f"  {field + ':':<8} {str(m):<14} {str(s):<14}{marker}")
    if verdict == "mal":
        print("  → winner: MAL  (push MAL → Shikimori)")
    elif verdict == "shiki":
        print("  → winner: Shikimori  (push Shikimori → MAL)")
    elif verdict == "conflict":
        print("  → conflict: each side is ahead on a different field")


async def _confirm_update(
    mal: ListEntry, shiki: ListEntry, verdict: str
) -> str:
    """Prompt the user about one planned update.

    Returns one of 'mal', 'shiki', 'skip', 'quit'. For non-conflict
    verdicts the prompt is [Y/n/q] (Enter accepts the auto-picked
    winner). For conflict verdicts it's [m/s/n/q] (no default — the
    user must pick a side explicitly).
    """
    _print_update_diff(mal, shiki, verdict)
    if verdict == "conflict":
        while True:
            answer = await _prompt("  Sync? [m=MAL→Shiki, s=Shiki→MAL, n=skip, q=quit] ")
            if answer == "m":
                return "mal"
            if answer == "s":
                return "shiki"
            if answer == "n":
                return "skip"
            if answer == "q":
                return "quit"
            print("  Please answer m, s, n, or q.")
    while True:
        answer = await _prompt("  Sync? [Y/n/q] ")
        if answer in ("", "y"):
            return verdict  # "mal" or "shiki"
        if answer == "n":
            return "skip"
        if answer == "q":
            return "quit"
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
                rewatches=entry.rewatches,
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
                num_times_rewatched=entry.rewatches,
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


async def _push_updates(
    mal_session: aiohttp.ClientSession,
    mal_token: str,
    shiki_session: aiohttp.ClientSession,
    shiki_token: str,
    items: list[tuple[ListEntry, ListEntry, str, int]],
    tally: dict,
    *,
    autosync: bool,
) -> bool:
    """Push divergent updates between the two services. Returns False if user quit.

    Each item is ``(mal_entry, shiki_entry, verdict, shiki_user_rate_id)``
    where ``verdict`` is "mal" / "shiki" / "conflict" — never "agree", as
    those are filtered out by the caller.
    """
    for mal_entry, shiki_entry, verdict, user_rate_id in items:
        if autosync:
            _print_update_diff(mal_entry, shiki_entry, verdict)
            if verdict == "conflict":
                tally["conflicts"] += 1
                print("  ! conflict, skipped")
                continue
            action = verdict
        else:
            action = await _confirm_update(mal_entry, shiki_entry, verdict)
            if action == "quit":
                return False
            if action == "skip":
                if verdict == "conflict":
                    tally["conflicts"] += 1
                    print("  conflict skipped")
                else:
                    tally["skipped"] += 1
                    print("  skipped")
                continue

        try:
            if action == "mal":
                # MAL is the winner: PATCH Shikimori to match MAL.
                await shikimori_api.update_list_entry(
                    shiki_session,
                    shiki_token,
                    user_rate_id,
                    status=_CANONICAL_TO_SHIKI[mal_entry.status],
                    score=mal_entry.score,
                    episodes=mal_entry.episodes,
                    rewatches=mal_entry.rewatches,
                )
                tally["updated"] += 1
                print("  ✓ updated on Shikimori")
            else:  # action == "shiki"
                mal_status, is_rewatching = _CANONICAL_TO_MAL[shiki_entry.status]
                await myanimelist_api.create_or_update_list_entry(
                    mal_session,
                    mal_token,
                    shiki_entry.anime_id,
                    status=mal_status,
                    score=shiki_entry.score,
                    num_watched_episodes=shiki_entry.episodes,
                    is_rewatching=is_rewatching,
                    num_times_rewatched=shiki_entry.rewatches,
                )
                tally["updated"] += 1
                print("  ✓ updated on MAL")
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
        in_both_ids = sorted(mal_entries.keys() & shiki_raw_by_id.keys())
        print(
            f"{len(only_in_mal_ids)} to push MAL→Shikimori, "
            f"{len(only_in_shiki_ids)} to push Shikimori→MAL, "
            f"{len(in_both_ids)} present on both"
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

        # Build the update work list. No extra title fetches needed —
        # entries present on both sides already have a MAL title we can
        # display, so we reuse it for the Shikimori-side ListEntry too.
        to_update: list[tuple[ListEntry, ListEntry, str, int]] = []
        for anime_id in in_both_ids:
            mal_entry = mal_entries[anime_id]
            shiki_raw = shiki_raw_by_id[anime_id]
            shiki_entry = _shiki_entry_to_listentry(shiki_raw, mal_entry.title)
            verdict = _pick_winner(mal_entry, shiki_entry)
            if verdict == "agree":
                continue
            to_update.append((mal_entry, shiki_entry, verdict, shiki_raw["id"]))

        if to_update:
            print(f"{len(to_update)} entries diverge and need update review")

        tally = {
            "created": 0,
            "updated": 0,
            "skipped": 0,
            "conflicts": 0,
            "failed": 0,
        }

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

        if to_update:
            print("\n--- Updates ---")
            if not await _push_updates(
                mal_session,
                mal_token,
                shiki_session,
                shiki_token,
                to_update,
                tally,
                autosync=args.autosync,
            ):
                print("Stopped by user.")
                _print_tally(tally)
                return 1 if tally["failed"] else 0

        if not to_push_shiki and not to_push_mal and not to_update:
            print("Nothing to sync.")
        _print_tally(tally)
        return 1 if tally["failed"] else 0


def _print_tally(tally: dict) -> None:
    print(
        f"Done. Created: {tally['created']}, "
        f"Updated: {tally['updated']}, "
        f"Skipped: {tally['skipped']}, "
        f"Conflicts: {tally['conflicts']}, "
        f"Failed: {tally['failed']}"
    )


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
