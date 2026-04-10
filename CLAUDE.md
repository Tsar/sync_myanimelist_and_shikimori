# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

Install deps (only `aiohttp`):

    pip install -r requirements.txt

Run the sync (the only user-facing entry point):

    python3 sync.py              # interactive per-entry [Y/n/q] confirmation
    python3 sync.py --autosync   # skip prompts, create everything

Each auth/api module is also runnable standalone as a smoke test that prints `whoami`:

    python3 myanimelist_auth.py
    python3 shikimori_auth.py

No tests, linter, or formatter are configured.

## Runtime prerequisites

Before running, two app-credential files must exist in the project root (both gitignored):

- `myanimelist_app_oauth2_creds.json` — `{"client_id": ..., "client_secret": ...}`
- `shikimori_app_oauth2_creds.json` — same shape

First run opens a browser for each service; resulting tokens are cached to `myanimelist_tokens.json` / `shikimori_tokens.json` (also gitignored, chmod 600) and auto-refreshed on subsequent runs. The callback listeners bind to `127.0.0.1:53683` (MAL) and `127.0.0.1:53682` (Shikimori); those ports must match the redirect URIs registered on each service's app page. MAL's redirect URI uses the hostname `localhost` (byte-exact match required) while Shikimori's uses `127.0.0.1`.

## Architecture

Five modules, one canonical model, two external services. Everything is `asyncio` + `aiohttp`.

**`sync.py` — orchestrator.** Downloads both lists, diffs by anime id, and for each missing-on-one-side entry prompts `[Y/n/q]` before creating it on the other side. Current scope is **create-only**: updates and deletes are explicitly out of scope.

**The join key is the MAL anime id.** Shikimori's `user_rate.target_id` *is* the MAL anime id, so set difference on ids is the whole diff — no title matching or fuzzy resolution anywhere.

**Canonical `ListEntry` + status mapping tables.** `sync.py` defines `_MAL_TO_CANONICAL`, `_CANONICAL_TO_MAL`, `_SHIKI_TO_CANONICAL`, `_CANONICAL_TO_SHIKI`. The asymmetry to watch: MAL has no `rewatching` status — it is represented as `status=watching` + `is_rewatching=true`. `_mal_to_canonical` collapses that into canonical `rewatching`; `_CANONICAL_TO_MAL` expands it back into the tuple `(mal_status, is_rewatching)`. Any new status handling should go through these tables, not ad-hoc strings.

**`myanimelist_api.py` / `shikimori_api.py` — thin HTTP wrappers.** Each takes an `aiohttp.ClientSession` from the caller (sync.py creates both with `User-Agent: sync_myanimelist_and_shikimori` — Shikimori rejects generic UAs). Only the endpoints sync.py currently needs exist; add more as features land.

Notable quirks encoded in these wrappers:
- MAL list fetch passes `nsfw=true` — without it, R+/Rx entries are silently dropped from user content endpoints.
- MAL uses an asymmetric field name: reads return `num_episodes_watched`, writes take `num_watched_episodes`. `_mal_entry_to_listentry` reads both defensively.
- MAL's create/update is a single `PUT /anime/{id}/my_list_status` (the docs show PATCH on the badge but PUT in the curl example; PUT works).
- Shikimori `user_rates?user_id=...` returns the entire list in one response — no pagination to follow.
- Shikimori `user_rates` entries do **not** include titles. `sync.py` resolves titles lazily via `shikimori_api.get_anime_title` only for the Shikimori→MAL push list, and persists them to `shikimori_titles_cache.json` so reruns don't re-fetch. The cache is saved atomically (tmp + `os.replace`) after each successful fetch so Ctrl+C mid-run never truncates it.

**`myanimelist_auth.py` / `shikimori_auth.py` — OAuth2 browser flows.** Both run a tiny `aiohttp.web` server on the callback port for exactly one request, launch `webbrowser.open`, and exchange the code. Both cache tokens and refresh when `expires_in` is within `EXPIRY_SAFETY_MARGIN_SEC` (60s) of expiry; if refresh fails they fall back to the browser flow. 

MAL-specific: uses PKCE with `code_challenge_method=plain` (MAL doesn't support S256, so `code_verifier == code_challenge`). MAL's token response omits `created_at`, so `_stamp()` injects `int(time.time())` before saving — `_is_expired` relies on that field.

## Rate limiting

Two separate delays, both tuned for Shikimori's 90 rpm global cap:

- `sync._WRITE_DELAY_SEC = 1.0` — between successful writes during a push loop.
- `shikimori_api._POLITE_DELAY_SEC = 0.8` — before every `get_anime_title` call (~75 rpm with margin).

If adding new Shikimori read/write endpoints, keep these limits in mind — bursting past ~1.5 rps gets throttled.
