# Codex operations and research contract

Codex interprets requests and researches sources. Python accepts structured JSON,
validates it and merges it into YAML. It never fetches the web or calls an LLM.
The same `apply_sync()` function handles targeted, full, and mixed operations.
The executable schema is `validate_state()` plus the strict field validators in
`scripts/sync_calendar.py`, reusing v1 `validate()` and `event_timing()`; no second
schema engine or dependency is needed. All examples are fictional.

## Command reference

Run from the repository root with Python 3.12+ on Linux/macOS (the CLI uses POSIX
file locks). Global `--data-dir PATH` selects a copied data directory for experiments;
`--now ISO_TIMESTAMP` fixes one run's timezone-aware timestamp for retries/tests.
Put these options **before** the subcommand.

```sh
# These are read-only, including file bytes and modification times:
python scripts/sync_calendar.py validate
python scripts/sync_calendar.py list
python scripts/sync_calendar.py upcoming --days 30

# Research only the requested tracked IDs, then write JSON to /tmp/proposal.json:
python scripts/sync_calendar.py apply --proposal /tmp/proposal.json --dry-run
python scripts/sync_calendar.py apply --proposal /tmp/proposal.json

# Research every authoritative tracked game, then merge and clean expired events:
python scripts/sync_calendar.py apply --full --proposal /tmp/full-research.json

# Explicit user-requested additions: JSON list with game configuration, plus
# immediate research results for EACH addition (a reported failure is acceptable):
python scripts/sync_calendar.py apply --add /tmp/add-games.json --proposal /tmp/new-games-research.json

# Pure removal needs no proposal or research:
python scripts/sync_calendar.py apply --remove diablo-iv
python scripts/sync_calendar.py apply --remove diablo-iv borderlands-4

# Mixed changes use one application and one commit. These IDs are examples;
# ARC Raiders and Marathon are already tracked in the current production data.
python scripts/sync_calendar.py apply --add /tmp/add-games.json --remove diablo-iv --proposal /tmp/new-games-research.json

# Full sync includes cleanup automatically; this maintenance primitive is also available:
python scripts/sync_calendar.py cleanup

# Only after an interrupted application left a journal:
python scripts/sync_calendar.py recover
```

For “Add Marathon and ARC Raiders”, read the authoritative list first and report
that both are already tracked; don't insert duplicates or refresh other games.
For genuinely new games, create configurations like `examples/add-games.json`,
research those games immediately, and supply all results in the same `apply`.
A failed new-game research attempt leaves the game tracked with failed status,
no invented events, and a clear summary. Retry targeted research when available.
Removing an unknown ID is an idempotent no-op. Syncing an untracked ID is an error.

Validate after every write, run tests, and generate a temporary preview:

```sh
python scripts/sync_calendar.py validate
python -m unittest discover -s tests -v
python scripts/generate_calendar.py --output /tmp/gaming-calendar.ics
```

Review and commit source state only (`data/games.yaml`, `data/events.yaml`,
`data/sync.yaml`), then push to `main` or use the repository's normal PR process.
Do not commit temporary research files or repeatedly commit generated ICS. The
Pages workflow validates and regenerates the feed on relevant pushes to `main`.
If branch rules prevent direct pushes, report the pending PR; deployment follows
its merge. The legacy generator `--remove-game` also clears adjacent sync metadata for
compatibility; prefer `apply --remove` for locking and journaled state writes.

## Proposal schema

The top level permits only `{"games": [...]}`. Research never includes tracking
configuration, additions, removals, or unrelated game metadata. Explicit CLI
`--add` / `--remove` options are the separate ownership boundary; Codex may use
them only on explicit user instruction. A schema cannot prove natural-language
intent, so this boundary is reinforced by `AGENTS.md` and the scheduled prompt.

Each result permits:

| Field | Contract |
| --- | --- |
| `game_id` | Required exact tracked stable ID; unique per envelope. |
| `status` | Required `OK`, `warning`, or `failed`. |
| `message` | Optional text explaining an incomplete/failed attempt. |
| `latest_news` | Required for `OK`/`warning`; at most three sentences; `""` explicitly clears news. |
| `latest_news_source_url` | Optional HTTP(S) source; omitted means no news source. Supply it for sourced news. |
| `events` | Optional list of valid event mappings to upsert; omission means none proposed, never deletion. |
| `cancelled_events` | Optional list of explicit evidenced cancellations/invalidations. |

A `failed` result accepts only `game_id`, `status`, and `message`; no replacement
news/events. `warning` accepts verified partial data, preserves previous events
against capacity eviction, and does not advance `last_success`. Every `OK` or
`warning` result must refresh the news text (it may remain identical); only those
games' news is touched. An empty event list is normal for a successful search
that finds no new dated events. Missing results in a full sync become failures.
A malformed game result is rejected as a unit, preserving that game's data and
allowing other valid results to apply. Structural envelope errors (duplicate or
unknown game IDs, invalid add/remove instructions) reject the transaction.

Events use the v1 fields: `title`, `type`, `status`, `source_url`, optional `notes`,
and either an exact `date` or a reliable `start_at` with optional `end_at` and
`source_timezone`. Event `game_id` may be omitted; if present it must match.
`updated_at` is assigned by the sync code only when content changes; a supplied
value is ignored. Keep exact source timestamps, never guessed UTC offsets.
All-day retention uses the next Prague midnight; timed retention uses actual end
(one elapsed hour if no end is known). An event is expired only when its end is
strictly more than the configured retention days before the run timestamp.

Use `confirmed` for verified exact dates; `tentative` retains uncertain dated
candidates outside the final ICS. `rumored`/`delayed` remain supported and excluded.
Legacy `expected` events keep their v1 export behavior, but new research must use
`confirmed` or `tentative`. Vague dates such as “early 2027” belong in news, never
an event. Validation proves date/time syntax, not the truth of a cited source;
Codex is responsible for assessing evidence and relevance.

### Identity and deduplication

For existing events, reuse their `id` on every date, time, title or status change.
Never create date-based IDs. For new events, supply a permanent slug `logical_key`
such as `patch-one-five` or `rogue-ops-supporter-preview`. The deterministic ID is
`<game-id>-<logical-key>`; future proposals with that key reuse it. Existing v1
IDs remain unchanged. To attach a key to a legacy event, supply both the stored
`id` and the new key the first time. A legacy ID can also be used directly as its
logical key for matching. IDs and keys are immutable, and duplicate keys, IDs or
case-insensitive title/type combinations are rejected rather than duplicated.
Different editions/platform milestones need descriptive distinct titles/keys.
Renames without a known ID/key cannot be identified reliably; inspect stored
records and reuse their identity. Source URLs and titles are not identity keys.

At most five stored events per game, including retained past/filtered events.
V1 had no executable capacity selection rule, so selection now deterministically
prefers upcoming events, nearest start, then higher optional `importance` (integer
0–3) for equal starts, then ID. Recent past events fill remaining capacity, newest
first. Incomplete research protects existing IDs; verified corrections and
explicit evidenced cancellations can still apply. Successful complete research
may evict lower-ranked candidates to meet the hard limit. Generator validation
also rejects hand-edited state with more than five entries per game.

A cancellation is `{"id": "stored-id", "source_url": "https://...", "reason":
"Publisher confirmed cancellation"}`. It must identify this game's existing
event, may not conflict with an upsert, and must include evidence. An omitted
previous event is retained until cancellation, capacity selection or retention
cleanup; transient source failure never implies cancellation.

### Status, summaries and persistence

`data/sync.yaml` stores schema version, configurable
`past_event_retention_days: 30`, and per-game `last_attempt`, optional
`last_success`, `status`, and `message`. Legacy games have unknown timestamps and
report `warning — Not yet synced` until an actual attempt; no fictitious history
is backfilled. Meaningful `OK` checkpoints advance success; failed/incomplete
checkpoints retain the prior success. List output counts future exported events and returns metadata without
research. Upcoming queries return eligible starts in `[now, now + days)`, ordered
by instant then ID. They do not include events already in progress.

Application returns JSON per-game added/updated/removed counts, news changes,
status/errors, the current run's `attempted_at`, `checkpoint_updated`, and overall
totals. `changed` includes meaningful persisted status/diagnostic changes;
`content_changed` reflects only tracking/news/events.

Routine re-verification does **not** advance persisted timestamps or rewrite files.
A checkpoint is recorded only when events/news change (including retention cleanup),
status or diagnostic message changes, or a game's sync status is first established.
A first failure/warning, a changed error, and recovery to OK are meaningful state
changes even without calendar edits. Repeated identical failures/warnings and
identical successful runs at later timestamps are no-ops and require no commit.

Persisted `last_attempt` / `last_success` mean the last recorded meaningful attempt
and successful checkpoint, not every unchanged scheduled check. The run summary
always reports the actual current attempt and outcome, including suppressed
checkpoints, so Scheduled task history retains useful per-run information without
Git churn. Event/news revisions also remain unchanged when their content does.
Commit/push only meaningful source changes; never timestamps alone, empty commits
or generated preview churn. No-op applications leave no uncommitted state changes.

The CLI locks the data directory (no lock file is created), so concurrent CLI
writers serialize and read-only commands take shared locks. Pure Python functions
are side-effect-free; callers using `save_store()` must serialize their writes.
Multiple YAML replacements use a validated recovery journal
`data/.sync-transaction.json`; after interruption reads fail safely until
`recover` finishes the transaction. Never commit the journal. Run the generator
and legacy commands after the sync completes, not concurrently with state writes.
Only changed files are serialized; changed YAML can lose comments/formatting.

Stale-game detection is deferred: missing upcoming events and last-success metadata
are available for an informational future detector. Never automatically untrack a
game. No database, NLP parser, API key, LLM runner or Actions research job is added.
