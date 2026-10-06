# Gaming Calendar

A personal iCalendar feed with all-day and exact-time events for Apple Calendar. Git-tracked YAML is the source of truth; Python generates the feed, and GitHub Actions publishes it to GitHub Pages. There is no database, server, frontend, or research automation.

The tracked games are Diablo IV, Path of Exile 2, Gray Zone Warfare, Borderlands 4, and Crimson Desert. Production news and upcoming events are curated from linked official sources in the YAML files. Events without reliable dates and minor updates are omitted. All test events are fictional.

## Architecture and files

```text
data/games.yaml                         tracked games and current game-level news
data/events.yaml                        actual calendar events
scripts/generate_calendar.py            validation and iCalendar generation
public/gaming-calendar.ics              generated local preview and Pages feed
tests/test_calendar.py                  standard-library unittest tests
.github/workflows/generate-calendar.yml  validate, generate, publish
requirements.txt                        single pinned dependency: PyYAML
```

PyYAML safely parses normal YAML, avoiding an incomplete handwritten parser. Everything else uses Python's standard library. Output is UTF-8 with CRLF line endings, iCalendar text escaping, and folding at 75 bytes without splitting Unicode characters.

Pages deploys the generated `public/` directory as an artifact. The workflow never commits output back into Git, avoiding bot commits and write permissions on repository contents. The included `.ics` is a local preview; each deployment regenerates it from source. You do not need to commit subsequent generated output changes.

## Run locally

Use Python 3.12 or newer from the repository root:

```sh
python -m venv .venv
source .venv/bin/activate
# Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python scripts/generate_calendar.py
```

The generator resolves default paths relative to its own location, so it also works from another directory. Optional `--games`, `--events`, and `--output` arguments select alternative files. Invalid input fails generation rather than publishing a partial feed. Tests validate YAML, required fields, stable identity, filtering, news injection, escaping, and byte-length folding.

## Manage tracked games

Add a mapping under `games:` or edit an existing one:

```yaml
# Fictional example only; do not copy as real game news.
- id: fictional-game
  name: Fictional Game
  tracking_status: interested
  tracked_event_types: [release, dlc, major_update]
  latest_news: ""
```

IDs must be unique lowercase letters/digits separated by hyphens. Keep IDs permanent even when a name changes. `tracking_status` is personal metadata: `playing`, `interested`, or `waiting`; it does not filter events. `tracked_event_types` controls export for that game. Use an empty list to pause its events. Supported types are `release`, `expansion`, `dlc`, `season`, `major_update`, `beta`, and `early_access`.

To remove a game and all its managed events, run:

```sh
python scripts/generate_calendar.py --remove-game stable-game-id
```

The command validates the remaining data and atomically replaces each file: first the dedicated feed, then event YAML, then game YAML. Interrupted operations can be retried with the same ID. It refuses an existing output file containing unrelated UIDs or lacking this project’s PRODID (legacy project UIDs remain accepted). It removes every event with that exact `game_id`, including filtered events, and preserves other games even when names match. It rewrites YAML formatting/comments through PyYAML; review the diff before committing. Commit the source changes and deploy the feed for subscribers to receive cleanup. Individual file replacements are atomic; the three files are not a single transaction, so run one writer at a time. Unknown game references still fail ordinary generation. Manual removal requires deleting both the game mapping and its event mappings. To change tracking, edit its event-type list; existing YAML events remain available for later re-enabling.

## Update latest news

Edit the game's `latest_news`, not individual events. Keep it to three short sentences or fewer. Optionally add `latest_news_source_url` (HTTP/HTTPS) and `latest_news_updated_at` (a quoted ISO 8601 timestamp with timezone, such as `"2026-10-05T12:00:00Z"`). Update that timestamp whenever news or its source changes; the generator uses it for exported event revision timestamps.

The sentence check treats `.`, `!`, or `?` followed by whitespace or end of text as a boundary; abbreviations may count as sentences. Prefer simple prose. Leave news as `""` or omit the field when nothing is verified. Omit optional fields rather than setting them to YAML `null`.

Every exported event for the game receives the current summary and optional source. When news is absent, both sections are omitted. Clear stale news and update affected event `updated_at` timestamps to record its removal. Future automation can edit these same fields without changing the generator.

## Add, edit, or remove events

Add mappings under `events:`. The following is **fictional test data**, not production information:

```yaml
events:
  - id: fictional-game-release
    game_id: fictional-game
    title: Release
    type: release
    date: '2030-01-01'
    status: confirmed
    source_url: https://example.com/announcement
    updated_at: '2026-10-05T12:00:00Z'
    notes: |
      Optional notes about this fictional event.
```

All shown fields except `notes` are required for all-day events. Dates are ISO `YYYY-MM-DD`, without a time. Never infer an hour from a date or a timezone mentioned alongside a date.

For a reliably sourced exact time, replace `date` with a quoted `start_at`. It must have an explicit UTC/numeric offset, or an accompanying IANA `source_timezone`:

```yaml
# Fictional timing examples:
start_at: '2026-12-11T17:00:00'
source_timezone: Europe/Prague
# Alternatively: start_at: '2026-12-11T08:00:00'
#                source_timezone: America/Los_Angeles
# Or: start_at: '2026-12-11T16:00:00Z'
```

Optional `end_at` records a meaningful known end, using the same rules and source timezone. Otherwise duration is one elapsed hour, including across DST changes. `date` and `start_at` are mutually exclusive. Date-only timestamps, missing timezone information, nonexistent wall times, ambiguous wall times without an explicit offset, offset/zone conflicts, and non-increasing ends fail validation. Retain the exact source timestamp strings and IANA zone in YAML; they are also included in the event description for debugging. A numeric offset is authoritative when no IANA zone is provided. `source_url` supplies provenance: the curator must verify the time against that source; the generator does not discover or assess sources. `updated_at` is a timezone-aware ISO timestamp: advance it whenever the event's date, title, status, source, or notes change. When changing a game name, tracking, or removing news, also advance affected event timestamps. Keep the event ID unchanged when its date changes. A truly distinct event needs a new ID; never reuse deleted IDs for unrelated events.

Statuses are `confirmed`, `expected`, `rumored`, and `delayed`. Only confirmed and expected events of a tracked type are exported. Expected events use iCalendar `TENTATIVE`; confirmed events use `CONFIRMED`. Do not mark a date confirmed without evidence. Delayed entries retain their prior date in YAML but are excluded until a new date and exportable status are supplied.

Remove an event by deleting its mapping. Removing or filtering events removes them from the next full subscription feed; subscribers reconcile on refresh. This MVP publishes full snapshots, not cancellation notifications. Apple refresh behavior and timing vary, so verify edits in your subscribed calendar after refresh.

## Calendar behavior

All-day events have inclusive `DTSTART` and an exclusive next-day `DTEND`, both date-only. Timed events are normalized using Python `zoneinfo` and `Europe/Prague`, with no fixed offset. Output uses `TZID=Europe/Prague` and embeds IANA-derived `VTIMEZONE` transitions for the event years plus neighboring years. The second occurrence of an autumn repeated hour uses UTC to preserve its unambiguous instant; the calendar timezone remains Prague. Python requires system IANA timezone data (as provided by the Linux CI runner); on platforms without it, install Python's `tzdata` package.

Every generated event includes `X-GAMING-CALENDAR-MANAGED:TRUE`, `X-GAMING-CALENDAR-GAME-ID`, and `X-GAMING-CALENDAR-EVENT-ID`. The full feed is exclusively project-managed; never point `--output` at a manual or mixed calendar file. There is no external calendar API deletion. Cleanup replaces this project's feed and subscription clients reconcile it on refresh. A future API integration must require the ownership marker plus stable game/event identifiers before updating or deleting external events. UIDs use `<event-id>@gaming-calendar`, independent of dates and titles; editing a date updates the same event identity. Keep this namespace unchanged once subscribed, and use globally distinctive IDs if running multiple copies of this project.

`DTSTAMP` and `LAST-MODIFIED` use the later of the event revision and available game-news revision. Explicit revisions keep generation deterministic and help clients recognize changes. Events are sorted by ID. Descriptions contain game, type, status, optional notes, optional game-level latest news and news source, and event source. There are no empty news sections. Events are transparent so they do not block your availability.

## Publish with GitHub Pages

To operate completely free, use a **public repository on GitHub Free**. Pages and standard hosted Actions for this public project require no paid services.

1. Push these files to your chosen GitHub repository's `main` branch. If your default branch has another name, change `main` in the workflow's push filter and both deployment conditions before pushing.
2. In **Settings → Actions → General**, allow GitHub Actions and the official `actions/*` actions if repository or organization policy currently blocks them. The workflow declares its own permissions; no repository-wide write permission or personal access token is needed.
3. In **Settings → Pages → Build and deployment → Source**, select **GitHub Actions**. Do not choose deployment from a branch.
4. In **Actions → Generate and publish calendar → Run workflow**, select `main` and run it. Later relevant pushes regenerate automatically. Pull requests validate and generate without deploying. Review any `github-pages` environment protection rules if deployment waits for approval.
5. Once both jobs succeed, use:
   `https://<username>.github.io/<repository>/gaming-calendar.ics`
   Replace the placeholders with your actual owner and repository. For an owner site repository named `<username>.github.io`, the URL is `https://<username>.github.io/gaming-calendar.ics`. A custom domain changes the host.
6. Open the direct `.ics` URL and verify the response starts with `BEGIN:VCALENDAR`. The root page may return 404 because this project intentionally has no website index.

The feed is public: never put secrets or private notes in the YAML. No scheduled workflow runs are needed because this MVP does not discover events. Manual dispatch is available for regeneration. GitHub outages and Apple refresh intervals can delay updates; this is a personal feed without service guarantees.

## Subscribe from Apple Calendar

On macOS, open **Calendar → File → New Calendar Subscription**, paste the HTTPS `.ics` URL, and click **Subscribe**. Choose a name and an auto-refresh interval (for example, daily). Choose iCloud as the location if offered and you want the subscription across devices. Use **View → Refresh Calendars** to request a refresh.

On iPhone/iPad, open **Settings → Apps → Calendar → Calendar Accounts → Add Account → Other → Add Subscribed Calendar** (older versions start at **Settings → Calendar → Accounts**), enter the URL, and save. Games without qualifying dated events have no entries; that is expected. Subscribe using the URL rather than downloading and importing the file, which would only create a one-time copy.

## Existing events and future sync

No database/schema migration is required. Existing date-only YAML remains valid. Regenerate and deploy once to add ownership metadata and Prague timing definitions to the feed. Existing UIDs are unchanged, including when an all-day event becomes timed, so subscribed events update rather than duplicate. The Crimson Desert event now uses its previously recorded verified `22:00 UTC` source time (23:00 Prague, ending at midnight). Other production entries remain all-day.

Subscriptions reconcile removed events after refresh. Previously downloaded/imported copies are independent; remove those copies manually or replace them with a subscription. No manual events are searched or deleted, and pre-existing feed events need no ID migration.

Future discovery/sync should continue editing these YAML records, retain stable event IDs across date/time changes, and advance `updated_at` when data changes. `event_timing()` provides normalized all-day/timed start/end values for comparison; compare timed instants in UTC (especially during repeated DST hours), and include relevant content fields. Remove obsolete source mappings before generating the next full snapshot. Repeated generation is deterministic and creates no additional UIDs. No periodic scheduler has been added.

## Next step

First verify publishing and Apple subscription, then add a genuinely verified event with its source. Later, a separate process can discover reliable announcements and propose YAML changes with sources and revision timestamps. It can preserve IDs for date changes and update game-level news; this existing pipeline then handles validation and publishing. Research automation is intentionally outside this MVP.
