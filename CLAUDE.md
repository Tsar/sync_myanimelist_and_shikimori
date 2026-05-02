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

Unit tests for the conflict-resolution heuristic (`_pick_winner`):

    python3 test_pick_winner.py

No linter or formatter is configured.

## Runtime prerequisites

Before running, two app-credential files must exist in the project root (both gitignored):

- `myanimelist_app_oauth2_creds.json` — `{"client_id": ..., "client_secret": ...}`
- `shikimori_app_oauth2_creds.json` — same shape

First run opens a browser for each service; resulting tokens are cached to `myanimelist_tokens.json` / `shikimori_tokens.json` (also gitignored, chmod 600) and auto-refreshed on subsequent runs. The callback listeners bind to `127.0.0.1:53683` (MAL) and `127.0.0.1:53682` (Shikimori); those ports must match the redirect URIs registered on each service's app page. MAL's redirect URI uses the hostname `localhost` (byte-exact match required) while Shikimori's uses `127.0.0.1`.

## Architecture

Five modules, one canonical model, two external services. Everything is `asyncio` + `aiohttp`.

**`sync.py` — orchestrator.** Three phases: (1) create on Shikimori anything that exists only on MAL, (2) create on MAL anything that exists only on Shikimori, (3) for entries present on both sides, diff the tracked fields and push the "newer" side. Each write is gated by a `[Y/n/q]` prompt (or `[m/s/n/q]` for conflicts) unless `--autosync` is passed. Deletes are out of scope.

**The join key is the MAL anime id.** Shikimori's `user_rate.target_id` *is* the MAL anime id, so set difference on ids is the whole diff — no title matching or fuzzy resolution anywhere.

**Canonical `ListEntry` + status mapping tables.** `sync.py` defines `_MAL_TO_CANONICAL`, `_CANONICAL_TO_MAL`, `_SHIKI_TO_CANONICAL`, `_CANONICAL_TO_SHIKI`. MAL has no dedicated `rewatching` status — it is the orthogonal `is_rewatching` checkbox combined with any visible status (conventionally `completed`). `_mal_to_canonical` treats *any* `is_rewatching=true` as canonical `rewatching`; `_CANONICAL_TO_MAL` expands canonical `rewatching` to `(mal_status="completed", is_rewatching=true)`. Any new status handling should go through these tables, not ad-hoc strings.

**Conflict-resolution heuristic (`_pick_winner`).** For entries present on both sides, the per-field judgement is: status partial-order (`_STATUS_LT`); episodes higher-wins; rewatches higher-wins; non-zero score wins over zero (two disagreeing non-zero scores are a conflict). The `rewatching ↔ completed` pair has no static order but is resolved via combined progress `2 * rewatches + (1 if rewatching else 0)` — odd vs even, so it never ties. Rewatching ↔ {on_hold, dropped} and on_hold ↔ dropped remain incomparable and surface as conflicts. `test_pick_winner.py` covers all branches; run it after touching the heuristic.

**`myanimelist_api.py` / `shikimori_api.py` — thin HTTP wrappers.** Each takes an `aiohttp.ClientSession` from the caller (sync.py creates both with `User-Agent: sync_myanimelist_and_shikimori` — Shikimori rejects generic UAs). Only the endpoints sync.py currently needs exist; add more as features land.

Notable quirks encoded in these wrappers:
- MAL list fetch passes `nsfw=true` — without it, R+/Rx entries are silently dropped from user content endpoints.
- MAL list fetch passes `fields=list_status{num_times_rewatched}` — `num_times_rewatched` is *not* among the default `list_status` sub-fields and silently reads back as 0 unless explicitly requested. The other list_status fields (status, score, num_episodes_watched, is_rewatching) are defaults.
- MAL uses an asymmetric field name: reads return `num_episodes_watched`, writes take `num_watched_episodes`. `_mal_entry_to_listentry` reads both defensively.
- MAL's create/update is a single `PUT /anime/{id}/my_list_status` (the docs show PATCH on the badge but PUT in the curl example; PUT works).
- Shikimori create vs update are different verbs: `POST /v2/user_rates` for create, `PATCH /v2/user_rates/{user_rate_id}` for update. `user_rate_id` is the rate row's own id (returned in the list response), distinct from `target_id`.
- Shikimori `user_rates?user_id=...` returns the entire list in one response — no pagination to follow.
- Shikimori `user_rates` entries do **not** include titles. `sync.py` resolves titles lazily via `shikimori_api.get_anime_title` only for the Shikimori→MAL push list, and persists them to `shikimori_titles_cache.json` so reruns don't re-fetch. The cache is saved atomically (tmp + `os.replace`) after each successful fetch so Ctrl+C mid-run never truncates it.

**`myanimelist_auth.py` / `shikimori_auth.py` — OAuth2 browser flows.** Both run a tiny `aiohttp.web` server on the callback port for exactly one request, launch `webbrowser.open`, and exchange the code. Both cache tokens and refresh when `expires_in` is within `EXPIRY_SAFETY_MARGIN_SEC` (60s) of expiry; if refresh fails they fall back to the browser flow. 

MAL-specific: uses PKCE with `code_challenge_method=plain` (MAL doesn't support S256, so `code_verifier == code_challenge`). MAL's token response omits `created_at`, so `_stamp()` injects `int(time.time())` before saving — `_is_expired` relies on that field.

## Rate limiting

Two separate delays, both tuned for Shikimori's 90 rpm global cap:

- `sync._WRITE_DELAY_SEC = 1.0` — between successful writes during a push loop.
- `shikimori_api._POLITE_DELAY_SEC = 0.8` — before every `get_anime_title` call (~75 rpm with margin).

If adding new Shikimori read/write endpoints, keep these limits in mind — bursting past ~1.5 rps gets throttled.
