#!/usr/bin/env python3
"""Tests for ``sync._pick_winner`` — the conflict-resolution heuristic.

No test framework is configured for this project; just run:

    python3 test_pick_winner.py

Exits non-zero if any case fails. Each case is a 4-tuple
``(description, mal_entry, shiki_entry, expected_verdict)``. The
notation ``(status/score/episodes/rewatches)`` is used throughout the
descriptions.
"""

from __future__ import annotations

import sys

from sync import ListEntry, _pick_winner


def E(status: str, score: int, episodes: int, rewatches: int) -> ListEntry:
    return ListEntry(
        anime_id=1,
        title="t",
        status=status,
        score=score,
        episodes=episodes,
        rewatches=rewatches,
    )


CASES: list[tuple[str, ListEntry, ListEntry, str]] = [
    # ---------------- Group 1: Agreement ----------------
    ("identical entries",
     E("watching", 8, 5, 0), E("watching", 8, 5, 0), "agree"),
    ("both score=0, rest identical",
     E("watching", 0, 5, 0), E("watching", 0, 5, 0), "agree"),
    ("both rewatching, same progress",
     E("rewatching", 8, 5, 1), E("rewatching", 8, 5, 1), "agree"),
    ("both planned, default zeros",
     E("planned", 0, 0, 0), E("planned", 0, 0, 0), "agree"),

    # ---------------- Group 2: Score handling ----------------
    ("MAL score=0 unset, Shiki=8",
     E("watching", 0, 5, 0), E("watching", 8, 5, 0), "shiki"),
    ("MAL=8, Shiki score=0 unset",
     E("watching", 8, 5, 0), E("watching", 0, 5, 0), "mal"),
    ("both nonzero scores disagree",
     E("watching", 8, 5, 0), E("watching", 7, 5, 0), "conflict"),
    ("both equal nonzero score",
     E("watching", 9, 5, 0), E("watching", 9, 5, 0), "agree"),

    # ---------------- Group 3: Status total order (rest equal) ----------------
    ("planned < watching",
     E("planned", 0, 0, 0), E("watching", 0, 0, 0), "shiki"),
    ("watching < on_hold",
     E("watching", 0, 5, 0), E("on_hold", 0, 5, 0), "shiki"),
    ("watching < dropped",
     E("watching", 0, 5, 0), E("dropped", 0, 5, 0), "shiki"),
    ("watching < completed",
     E("watching", 0, 5, 0), E("completed", 0, 5, 0), "shiki"),
    ("on_hold < completed",
     E("on_hold", 0, 5, 0), E("completed", 0, 5, 0), "shiki"),
    ("dropped < completed",
     E("dropped", 0, 5, 0), E("completed", 0, 5, 0), "shiki"),
    ("planned < rewatching",
     E("planned", 0, 0, 0), E("rewatching", 0, 0, 0), "shiki"),
    ("watching < rewatching",
     E("watching", 0, 5, 0), E("rewatching", 0, 5, 0), "shiki"),
    ("completed > watching (reverse direction)",
     E("completed", 0, 5, 0), E("watching", 0, 5, 0), "mal"),

    # ---------------- Group 4: Status incomparable ----------------
    ("on_hold ↔ dropped",
     E("on_hold", 0, 5, 0), E("dropped", 0, 5, 0), "conflict"),
    ("rewatching ↔ on_hold",
     E("rewatching", 0, 5, 0), E("on_hold", 0, 5, 0), "conflict"),
    ("rewatching ↔ dropped",
     E("rewatching", 0, 5, 0), E("dropped", 0, 5, 0), "conflict"),

    # ---------------- Group 5: rewatching ↔ completed special case ----------------
    ("just started 1st rewatch on MAL",
     E("rewatching", 0, 5, 0), E("completed", 0, 12, 0), "mal"),
    ("just started 1st rewatch on Shiki",
     E("completed", 0, 12, 0), E("rewatching", 0, 5, 0), "shiki"),
    ("real example: MAL mid-2nd rewatch, Shiki finished 2nd",
     E("rewatching", 9, 11, 1), E("completed", 9, 12, 2), "shiki"),
    ("Shiki started 3rd rewatch, MAL completed 2",
     E("completed", 9, 12, 2), E("rewatching", 9, 5, 2), "shiki"),
    ("MAL mid-2nd-rewatch beats Shiki just-completed-1st",
     E("rewatching", 8, 10, 1), E("completed", 8, 12, 1), "mal"),

    # ---------------- Group 6: Single-field diffs (status agrees) ----------------
    ("same status, MAL more episodes",
     E("watching", 0, 8, 0), E("watching", 0, 5, 0), "mal"),
    ("same status, Shiki more episodes",
     E("watching", 0, 5, 0), E("watching", 0, 8, 0), "shiki"),
    ("both completed, MAL more rewatches",
     E("completed", 0, 12, 3), E("completed", 0, 12, 2), "mal"),
    ("both rewatching, Shiki more rewatches",
     E("rewatching", 0, 5, 1), E("rewatching", 0, 5, 2), "shiki"),

    # ---------------- Group 7: Multi-field, all winners agree ----------------
    ("MAL ahead on status AND episodes",
     E("watching", 0, 5, 0), E("planned", 0, 0, 0), "mal"),
    ("Shiki ahead on status, episodes, rewatches",
     E("watching", 0, 5, 0), E("completed", 0, 12, 1), "shiki"),
    ("MAL ahead on score (nonzero vs zero) AND episodes",
     E("watching", 8, 8, 0), E("watching", 0, 5, 0), "mal"),

    # ---------------- Group 8: Multi-field, winners disagree → conflict ----------------
    ("status MAL, episodes Shiki",
     E("completed", 0, 8, 0), E("watching", 0, 12, 0), "conflict"),
    ("status MAL, rewatches Shiki",
     E("completed", 0, 12, 1), E("watching", 0, 12, 2), "conflict"),
    ("episodes MAL, rewatches Shiki",
     E("watching", 0, 8, 1), E("watching", 0, 5, 2), "conflict"),
    ("score one way (zero vs nonzero), episodes the other",
     E("watching", 0, 8, 0), E("watching", 8, 5, 0), "conflict"),
    ("two nonzero scores disagreeing dominates everything",
     E("rewatching", 8, 11, 1), E("completed", 7, 12, 2), "conflict"),

    # ---------------- Group 9: rewatching↔completed combined with score ----------------
    ("progress→shiki, score→shiki (zero vs nonzero)",
     E("rewatching", 0, 11, 1), E("completed", 8, 12, 2), "shiki"),
    ("progress→mal, score→shiki (zero vs nonzero) — conflict",
     E("rewatching", 0, 5, 0), E("completed", 8, 12, 0), "conflict"),
    ("progress→shiki, both scores nonzero disagreeing — conflict",
     E("rewatching", 8, 11, 1), E("completed", 9, 12, 2), "conflict"),
]


def main() -> int:
    passed = 0
    for desc, mal, shiki, expected in CASES:
        got = _pick_winner(mal, shiki)
        if got == expected:
            passed += 1
            continue
        print(f"FAIL: {desc}")
        print(f"  MAL:      {mal}")
        print(f"  Shiki:    {shiki}")
        print(f"  expected: {expected}")
        print(f"  got:      {got}")
    print(f"{passed}/{len(CASES)} passed")
    return 0 if passed == len(CASES) else 1


if __name__ == "__main__":
    sys.exit(main())
